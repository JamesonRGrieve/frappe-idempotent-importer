# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Frappe side of a run: builds one connection's engine (its adapter's reader and mappers, the ERPNext
writers, and the stores) and drives it. Runs are stepped: a background job (or the CLI, in the
foreground) works through pages until its budget is spent, the run pauses for its source, or it ends.
Every object's writes are one transaction, committed or rolled back on its own, with outgoing mail
muted."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from time import monotonic
from typing import Any

import frappe
from frappe.utils.password import set_encrypted_password

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import SystemSleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.run import Mode, RunDependencies, Runner, RunState
from idempotent_importer.core.source import Adapter, ConnectionConfig
from idempotent_importer.core.sync import Handler, ObjectSync, Outcome
from idempotent_importer.core.transport import RequestsTransport
from idempotent_importer.erpnext.context import ConnectionContext
from idempotent_importer.erpnext.media import Media
from idempotent_importer.erpnext.stores import (
	CONNECTION,
	FrappeCredentialHold,
	FrappeCursors,
	FrappeLinkStore,
	FrappeRunLock,
	FrappeRunStore,
	FrappeSeenStore,
	now_utc,
)
from idempotent_importer.erpnext.writers.base import Writer
from idempotent_importer.erpnext.writers.catalog import CollectionWriter, ProductWriter
from idempotent_importer.erpnext.writers.content import (
	ArticleWriter,
	AssetWriter,
	BlogCategoryWriter,
	CommentWriter,
	LegacyRedirects,
	PageWriter,
	RedirectWriter,
	ReviewWriter,
	flush_redirect_cache,
)
from idempotent_importer.erpnext.writers.discounts import DiscountWriter
from idempotent_importer.erpnext.writers.parties import CustomerWriter
from idempotent_importer.erpnext.writers.sales import DraftOrderWriter, OrderWriter
from idempotent_importer.erpnext.writers.shipping import DeliveryProfileWriter
from idempotent_importer.sources.registry import by_label

LOGGER = "idempotent_importer"
# The connection's field for the OAuth credential a source rotates (Etsy's refresh token).
ROTATING_CREDENTIAL_FIELD = "refresh_token"
TEST_ENDPOINTS = "idempotent_importer_test_endpoints"
# How long one background step job works before handing over to the next (a job's queue timeout is
# longer, so a page in flight always finishes).
JOB_BUDGET_SECONDS = 20 * 60
FOREGROUND_BUDGET_SECONDS = 60.0
WRITERS: dict[Kind, type[Writer]] = {
	Kind.ASSET: AssetWriter,
	Kind.COLLECTION: CollectionWriter,
	Kind.PRODUCT: ProductWriter,
	Kind.CUSTOMER: CustomerWriter,
	Kind.DISCOUNT: DiscountWriter,
	Kind.ORDER: OrderWriter,
	Kind.DRAFT_ORDER: DraftOrderWriter,
	Kind.PAGE: PageWriter,
	Kind.POLICY: PageWriter,
	Kind.BLOG: BlogCategoryWriter,
	Kind.ARTICLE: ArticleWriter,
	Kind.COMMENT: CommentWriter,
	Kind.REVIEW: ReviewWriter,
	Kind.URL_REDIRECT: RedirectWriter,
	Kind.DELIVERY_PROFILE: DeliveryProfileWriter,
}
# The connection's "What to Mirror" switch for each kind.
SWITCHES: dict[Kind, str] = {
	Kind.ASSET: "import_files",
	Kind.COLLECTION: "import_collections",
	Kind.PRODUCT: "import_products",
	Kind.CUSTOMER: "import_customers",
	Kind.DISCOUNT: "import_discounts",
	Kind.ORDER: "import_orders",
	Kind.DRAFT_ORDER: "import_draft_orders",
	Kind.PAGE: "import_pages",
	Kind.POLICY: "import_policies",
	Kind.BLOG: "import_blogs",
	Kind.ARTICLE: "import_articles",
	Kind.COMMENT: "import_comments",
	Kind.REVIEW: "import_reviews",
	Kind.URL_REDIRECT: "import_redirects",
	Kind.DELIVERY_PROFILE: "import_shipping",
}


class RunLogHandler(logging.Handler):
	"""Collects the engine's log lines for the run's Importer Run log."""

	def __init__(self, lines: list[str]) -> None:
		super().__init__(logging.INFO)
		self.lines = lines
		self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

	def emit(self, record: logging.LogRecord) -> None:
		self.lines.append(self.format(record))


@contextmanager
def write_scope() -> Iterator[None]:
	"""One object's writes: committed together, or rolled back together; no mail goes out."""
	muted = frappe.flags.mute_emails
	frappe.flags.mute_emails = True
	try:
		yield
	except BaseException:
		frappe.db.rollback()
		raise
	else:
		frappe.db.commit()
	finally:
		frappe.flags.mute_emails = muted


@dataclass
class Engine:
	connection: str
	runner: Runner
	sync: ObjectSync
	reader: Any
	lock: FrappeRunLock
	hold: FrappeCredentialHold
	cursors: FrappeCursors
	store: FrappeRunStore


def config_of(doc: Any, adapter: Adapter) -> ConnectionConfig:
	endpoints = frappe.conf.get(TEST_ENDPOINTS) or {}
	connection = str(doc.name)
	return ConnectionConfig(
		source=adapter.id,
		connection=connection,
		store_url=str(doc.store_url or ""),
		api_user=str(doc.api_user or ""),
		api_secret=str(doc.get_password("api_secret", raise_exception=False) or ""),
		auth_method=str(doc.auth_method or ""),
		account=str(doc.api_account or ""),
		refresh_token=str(doc.get_password("refresh_token", raise_exception=False) or ""),
		save_refresh_token=lambda token: save_refresh_token(connection, token),
		endpoint_override=endpoints.get(doc.name),
	)


def save_refresh_token(connection: str, token: str) -> None:
	"""Keeps the refresh token a source just issued, committed at once: the source has already retired
	the old one, so it must survive whatever the rest of the step rolls back."""
	set_encrypted_password(CONNECTION, connection, token, ROTATING_CREDENTIAL_FIELD)
	frappe.db.commit()


def build(connection: str, log_lines: list[str]) -> Engine:
	doc = frappe.get_doc(CONNECTION, connection)
	adapter = by_label(doc.source)
	config = config_of(doc, adapter)
	transport = RequestsTransport()
	reader = adapter.reader(config, transport, SystemSleeper())
	links = FrappeLinkStore(connection)
	context = ConnectionContext.of(doc, adapter.id)
	media = Media(context, links, transport)
	enabled = [kind for kind in adapter.kinds() if doc.get(SWITCHES[kind])]
	handlers = {
		mapper.kind: Handler(mapper, WRITERS[mapper.kind](context, links, media))
		for mapper in adapter.mappers(config)
		if mapper.kind in enabled
	}
	sync = ObjectSync(handlers, links, write_scope, LegacyRedirects(context, links))
	filters = json.loads(doc.filters) if doc.filters else {}
	store = FrappeRunStore(connection, log_lines)
	lock = FrappeRunLock(connection)
	hold = FrappeCredentialHold(connection)
	cursors = FrappeCursors(connection)
	deps = RunDependencies(
		reader=reader,
		sync=sync,
		store=store,
		cursors=cursors,
		lock=lock,
		credential_hold=hold,
		seen=FrappeSeenStore(),
		links=links,
		filters={Kind.parse(kind): text for kind, text in filters.items()},
		new_run_id=lambda: frappe.generate_hash(length=16),
		clock=now_utc,
		monotonic=monotonic,
	)
	return Engine(connection, Runner(enabled, deps), sync, reader, lock, hold, cursors, store)


@contextmanager
def logging_to(lines: list[str]) -> Iterator[None]:
	logger = logging.getLogger(LOGGER)
	handler = RunLogHandler(lines)
	logger.addHandler(handler)
	level = logger.level
	logger.setLevel(logging.INFO)
	try:
		yield
	finally:
		logger.removeHandler(handler)
		logger.setLevel(level)


def start(connection: str, mode: Mode, dry_run: bool, kinds: list[Kind] | None) -> RunState | None:
	"""Starts a run of ``connection`` unless one is in progress (then None)."""
	lines: list[str] = []
	with logging_to(lines):
		engine = build(connection, lines)
		state = engine.runner.start(mode, dry_run, kinds)
		if state is not None:
			engine.store.save(state)
		return state


def step(connection: str, budget_seconds: float) -> bool:
	"""Works on the connection's run for up to ``budget_seconds``; True when it has ended."""
	lines: list[str] = []
	with logging_to(lines):
		engine = build(connection, lines)
		try:
			finished = engine.runner.step(budget_seconds)
		finally:
			flush_redirect_cache()
		state = engine.store.load()
		if state is not None:
			engine.store.save(state)
		return finished


def run_by_hand(connection: str, full: bool, dry_run: bool, kinds: list[Kind] | None) -> dict[str, Any]:
	"""A run started by the operator: the credential hold is released (one deliberate attempt), and the
	run is stepped in the foreground until it ends or pauses for its source."""
	FrappeCredentialHold(connection).release()
	try:
		state = start(connection, Mode.FULL if full else Mode.INCREMENTAL, dry_run, kinds)
	except SourceError as e:
		return {"status": "not started", "reason": str(e)}
	if state is None:
		return {"status": "another run of this connection is in progress"}
	while not step(connection, FOREGROUND_BUDGET_SECONDS):
		paused = FrappeRunStore(connection, []).load()
		if paused is not None and paused.is_paused(now_utc()):
			return {"status": "paused", "run": paused.id, "resume_at": str(paused.resume_at)}
	return status(connection, state.id)


def status(connection: str, run: str | None = None) -> dict[str, Any]:
	name = run or frappe.db.get_value(CONNECTION, connection, "last_run")
	result: dict[str, Any] = {"connection": connection}
	if name:
		row = frappe.db.get_value(
			"Importer Run",
			name,
			["status", "mode", "dry_run", "counts", "resume_at", "summary"],
			as_dict=True,
		)
		result |= {
			"run": name,
			"status": row.status,
			"mode": row.mode,
			"dry_run": bool(row.dry_run),
			"counts": json.loads(row.counts or "{}"),
			"paused_until": str(row.resume_at) if row.resume_at else None,
		}
	hold = FrappeCredentialHold(connection).reason()
	if hold:
		result["credential_hold"] = hold
	return result


def sync_one(connection: str, kind: Kind, gid: str) -> Outcome:
	"""Re-reads one object and mirrors it now (or retires it, when the source no longer has it)."""
	lines: list[str] = []
	with logging_to(lines):
		engine = build(connection, lines)
		if not engine.sync.handles(kind):
			raise SourceError(f"{connection} doesn't mirror {kind.value}.")
		node = engine.reader.node(kind, gid)
		try:
			outcome = (
				engine.sync.retire(kind, gid, False) if node is None else engine.sync.sync(kind, node, False)
			)
		finally:
			flush_redirect_cache()
		for line in lines:
			frappe.logger(LOGGER).info(line)
		return outcome
