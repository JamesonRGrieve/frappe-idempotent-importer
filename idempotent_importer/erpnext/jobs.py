# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background work: every enabled connection gets an incremental run hourly and a full run daily, each
run is worked through by one step job per connection at a time (on the long queue), and a frequent tick
resumes runs whose pause is over. A connection whose credentials were rejected is skipped until the
operator saves it or starts a run by hand."""

from __future__ import annotations

import logging
from time import monotonic

import frappe
from frappe.utils.background_jobs import is_job_enqueued

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.run import Mode
from idempotent_importer.erpnext import engine
from idempotent_importer.erpnext.stores import CONNECTION, FrappeRunStore, now_utc

logger = logging.getLogger(__name__)
QUEUE = "long"
# A step job's queue timeout: longer than its working budget, so the page in flight always finishes.
JOB_TIMEOUT_SECONDS = 45 * 60


def job_id(connection: str) -> str:
	return f"idempotent_import::{connection}"


def enqueue_step(connection: str) -> None:
	"""Queues the connection's step job (at most one is queued or running per connection)."""
	if is_job_enqueued(job_id(connection)):
		return
	frappe.enqueue(
		"idempotent_importer.erpnext.jobs.step_job",
		queue=QUEUE,
		timeout=JOB_TIMEOUT_SECONDS,
		job_id=job_id(connection),
		deduplicate=True,
		enqueue_after_commit=True,
		connection=connection,
	)


def step_job(connection: str) -> None:
	"""Works on the connection's run until it ends, pauses for its source, or the job's budget is spent
	(the next tick queues another job)."""
	deadline = monotonic() + engine.JOB_BUDGET_SECONDS
	while monotonic() < deadline:
		if engine.step(connection, deadline - monotonic()):
			return
		state = FrappeRunStore(connection, []).load()
		if state is None or state.is_paused(now_utc()):
			return


def start_and_enqueue(connection: str, mode: Mode, dry_run: bool) -> None:
	if engine.start(connection, mode, dry_run, None) is not None:
		enqueue_step(connection)


def scheduled(mode: Mode) -> None:
	for connection in frappe.get_all(
		CONNECTION, filters={"enabled": 1, "credential_hold": ["is", "not set"]}, pluck="name"
	):
		try:
			start_and_enqueue(connection, mode, False)
		except SourceError as e:
			# A connection that can't start (not configured, or its adapter not built) never blocks the others.
			logger.warning("%s: %s", connection, e)
			frappe.db.rollback()


def scheduled_incremental() -> None:
	scheduled(Mode.INCREMENTAL)


def scheduled_full() -> None:
	scheduled(Mode.FULL)


def resume() -> None:
	"""Queues a step for every run in progress that isn't paused (one whose pause is over included)."""
	for connection in frappe.get_all(
		CONNECTION, filters={"credential_hold": ["is", "not set"]}, pluck="name"
	):
		state = FrappeRunStore(connection, []).load()
		if state is not None and not state.is_paused(now_utc()):
			enqueue_step(connection)
