# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one QuickBooks Online company through the Accounting API's query endpoint
(developer.intuit.com/app/developer/qbo/docs/learn/explore-the-quickbooks-online-api/data-queries):
``SELECT * FROM <Entity>`` in pages of MAXRESULTS (1000, the most a query returns) from STARTPOSITION
(1-based), in creation order so a page boundary never moves while a pass reads. Name lists (accounts,
classes, departments, customers, vendors, employees, items) are read with ``Active IN (true, false)``,
since a query otherwise returns only active ones and an inactive record is still the company's. Incremental
runs ask for ``MetaData.LastUpdatedTime >= <since>``; deletions come only from a full pass (QuickBooks
deletes transactions outright and makes names inactive).

A kind QuickBooks keeps as several entities reads them one after another (sales invoices: invoices, sales
receipts, credit memos, refund receipts). Every node carries its identity as ``id``
(``gid://quickbooks_online/<connection>/<Entity>/<Id>``). The payment taken on a sales receipt (and paid
out on a refund receipt) is a payment entry of its own, ``SalesReceiptPayment`` / ``RefundReceiptPayment``.

Limits (developer.intuit.com/app/developer/qbo/docs/learn/limits-and-throttles): 500 requests a minute and
10 a second per company and app, 10 at once; a throttled request is a 429, after which Intuit asks for 60
seconds' wait, so a 429 pauses the run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.common import chained

SOURCE_ID = "quickbooks_online"
PRODUCTION_HOST = "quickbooks.api.intuit.com"
SANDBOX_HOST = "sandbox-quickbooks.api.intuit.com"
# Intuit's base minor version since 2025 (every lower one is retired).
MINOR_VERSION = 75
PAGE_SIZE = 1000
FIRST_POSITION = 1
QUERY_RESPONSE = "QueryResponse"
REQUESTS_PER_MINUTE = 500
REQUESTS_PER_SECOND = 10
THROTTLE_WAIT_SECONDS = 60.0
RATE_LIMIT = RateLimit(
	requests_per_second=min(RateLimit.per_period(REQUESTS_PER_MINUTE, 60.0), float(REQUESTS_PER_SECOND)),
	cooldown_seconds=THROTTLE_WAIT_SECONDS,
)
QUERY_TIME = "%Y-%m-%dT%H:%M:%S+00:00"
PAYMENT_SUFFIX = "Payment"
ITEM_CATEGORY = "Category"
UNDEPOSITED = "UndepositedFunds"


@dataclass(frozen=True)
class Entity:
	"""One QuickBooks entity a kind reads: its name, whether it is a name list (with inactive records), and
	which of its records the kind takes (None: all)."""

	name: str
	name_list: bool = False
	take: str | None = None


# Which records of an entity a kind takes, by a field's value.
CATEGORIES = "categories"
NOT_CATEGORIES = "not-categories"
PROJECTS = "projects"
NOT_PROJECTS = "not-projects"
PAID = "paid"


def is_category(record: Node) -> bool:
	return record.optional_string("Type") == ITEM_CATEGORY


def is_project(record: Node) -> bool:
	return record.optional_flag("IsProject") is True


TAKES: dict[str, Callable[[Node], bool]] = {
	CATEGORIES: is_category,
	NOT_CATEGORIES: lambda record: not is_category(record),
	PROJECTS: is_project,
	NOT_PROJECTS: lambda record: not is_project(record),
}

ENTITIES: dict[Kind, tuple[Entity, ...]] = {
	Kind.ACCOUNT: (Entity("Account", True),),
	Kind.COST_CENTER: (Entity("Class", True), Entity("Department", True)),
	Kind.COLLECTION: (Entity("Item", True, CATEGORIES),),
	Kind.PRODUCT: (Entity("Item", True, NOT_CATEGORIES),),
	Kind.CUSTOMER: (Entity("Customer", True, NOT_PROJECTS),),
	Kind.SUPPLIER: (Entity("Vendor", True),),
	Kind.EMPLOYEE: (Entity("Employee", True),),
	Kind.PROJECT: (Entity("Customer", True, PROJECTS),),
	Kind.DRAFT_ORDER: (Entity("Estimate"),),
	Kind.PURCHASE_ORDER: (Entity("PurchaseOrder"),),
	Kind.SALES_INVOICE: (
		Entity("Invoice"),
		Entity("SalesReceipt"),
		Entity("CreditMemo"),
		Entity("RefundReceipt"),
	),
	Kind.PURCHASE_INVOICE: (Entity("Bill"), Entity("VendorCredit")),
	Kind.PAYMENT_ENTRY: (
		Entity("Payment"),
		Entity("BillPayment"),
		Entity("SalesReceipt", take=PAID),
		Entity("RefundReceipt", take=PAID),
	),
	Kind.JOURNAL_ENTRY: (
		Entity("JournalEntry"),
		Entity("Deposit"),
		Entity("Transfer"),
		Entity("Purchase"),
	),
}


def api_base(config: ConnectionConfig) -> str:
	"""The company's API host: production, or Intuit's sandbox (the connection's Store URL)."""
	if config.endpoint_override:
		return config.endpoint_override
	host = urlsplit(config.store_url if "//" in config.store_url else f"//{config.store_url}").hostname
	if host not in (PRODUCTION_HOST, SANDBOX_HOST, None):
		raise SourceError(
			f"QuickBooks Online's Store URL is https://{PRODUCTION_HOST} (or https://{SANDBOX_HOST}), not {host}."
		)
	return f"https://{host or PRODUCTION_HOST}"


def query_text(entity: Entity, since: datetime | None, position: int) -> str:
	conditions = []
	if entity.name_list:
		conditions.append("Active IN (true, false)")
	if since is not None:
		conditions.append(f"MetaData.LastUpdatedTime >= '{since.astimezone(UTC).strftime(QUERY_TIME)}'")
	where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
	# QuickBooks' query language, not SQL, built only from the adapter's own entity names, a UTC timestamp
	# and integers: nothing a source or a user supplies reaches it.
	return (
		f"SELECT * FROM {entity.name}{where} ORDERBY MetaData.CreateTime "  # noqa: S608 - see above
		f"STARTPOSITION {position} MAXRESULTS {PAGE_SIZE}"
	)


class QuickBooksReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		if not config.account:
			raise SourceError("QuickBooks Online is not connected: use Connect to QuickBooks Online first.")
		self._config = config
		self._client = client
		self._company = f"/v3/company/{config.account}"
		self._undeposited: str | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in ENTITIES

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		entities = ENTITIES.get(kind)
		if entities is None:
			raise SourceError(f"QuickBooks Online has no {kind.value}.")
		return chained(
			len(entities), cursor, lambda feed, position: self._entity(entities[feed], position, since)
		)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		type_ = identity.type.removesuffix(PAYMENT_SUFFIX) if kind is Kind.PAYMENT_ENTRY else identity.type
		entity = next((e for e in ENTITIES.get(kind, ()) if e.name == type_), None)
		if entity is None:
			raise SourceError(f"QuickBooks Online can't read a {identity.type} as a {kind.value}.")
		result = self._client.get(
			f"{self._company}/{entity.name.lower()}/{identity.id}", {"minorversion": MINOR_VERSION}
		)
		if result is None:
			return None
		record = result.node.child(entity.name)
		if record is None:
			return None
		taken = self._taken(entity, record)
		return taken[0] if taken else None

	def _entity(self, entity: Entity, position: str | None, since: datetime | None) -> Page:
		start = int(position) if position else FIRST_POSITION
		result = self._client.get(
			f"{self._company}/query",
			{"query": query_text(entity, since, start), "minorversion": MINOR_VERSION},
		)
		if result is None:
			raise SourceError(f"QuickBooks Online has no query endpoint for company {self._config.account}.")
		response = result.node.required_child(QUERY_RESPONSE)
		records = response.children(entity.name)
		nodes = [node for record in records for node in self._taken(entity, record)]
		more = len(records) == PAGE_SIZE
		return Page(nodes, more, str(start + len(records)) if more else None)

	def _taken(self, entity: Entity, record: Node) -> list[Node]:
		"""The record as the kind's node(s): only those the kind takes, each with its identity."""
		if entity.take == PAID:
			return [self._identified(entity.name + PAYMENT_SUFFIX, record)]
		if entity.take is not None and not TAKES[entity.take](record):
			return []
		return [self._identified(entity.name, record)]

	def _identified(self, type_: str, record: Node) -> Node:
		gid = self._config.identity(type_, record.string("Id"))
		fields: dict[str, JsonValue] = {**record.raw(), "id": gid, "Entity": type_}
		if type_ == "Deposit" and any(line.children("LinkedTxn") for line in record.children("Line")):
			# A deposit of received payments moves them out of Undeposited Funds, which its lines don't name.
			fields["UndepositedFundsRef"] = {"value": self._undeposited_funds()}
		return Node(fields, gid)

	def _undeposited_funds(self) -> str:
		"""The company's Undeposited Funds account (the one of that sub-type), read once."""
		if self._undeposited is None:
			cursor: str | None = None
			while self._undeposited is None:
				page = self._entity(Entity("Account", True), cursor, None)
				self._undeposited = next(
					(
						node.string("Id")
						for node in page.nodes
						if node.optional_string("AccountSubType") == UNDEPOSITED
					),
					None,
				)
				if not page.has_next_page:
					break
				cursor = page.end_cursor
		if self._undeposited is None:
			raise SourceError("QuickBooks Online has no Undeposited Funds account for a deposit of payments.")
		return self._undeposited
