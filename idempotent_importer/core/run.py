# SPDX-License-Identifier: AGPL-3.0-or-later
"""A run of one connection, one step at a time: each step reads and mirrors pages until its time budget is
spent, then persists where it got to. Kinds run in SYNC_ORDER. A full run retires the copies of this
connection's objects that the source no longer has, but only for a kind that completed cleanly and
wasn't filtered.

Lockout safety, which matters most in the long first catch-up: when the source keeps pushing back
(RetryLater) the run pauses exactly where it is (same kind, same page) and resumes after PausePolicy's
growing pause, instead of failing the kind and carrying on against a source that is refusing requests.
When the source rejects the credentials (CredentialsRejected) the run stops at once and the credential
hold keeps scheduled work from retrying them."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from idempotent_importer.core.errors import CredentialsRejected, KindUnavailable, RetryLater, SourceError
from idempotent_importer.core.kind import SYNC_ORDER, Kind
from idempotent_importer.core.node import JsonValue
from idempotent_importer.core.source import SourceReader
from idempotent_importer.core.sync import LinkStore, ObjectSync, Outcome

logger = logging.getLogger(__name__)

# The next incremental run asks for objects updated since the last clean run's start, minus this overlap
# for clock skew and objects updated while that run was reading.
CURSOR_OVERLAP = timedelta(minutes=10)


class Mode(StrEnum):
	"""Incremental runs read only what changed since the last clean run of each kind (where the source
	can say); full runs read everything and retire copies of objects the source no longer has."""

	INCREMENTAL = "incremental"
	FULL = "full"


class PausePolicy:
	"""How long a run pauses when its source keeps pushing back: at least what the source asked for,
	doubling with each consecutive pause (1, 2, 4 … minutes) up to an hour. The pauses hold on for about a
	day (MAX_CONSECUTIVE_PAUSES: two hours of growing pauses, then hourly ones), long enough to outlast a
	spent daily quota without losing the run's place; after that the kind is given up as failed, so a
	source that never recovers can't hold the run forever. A page read cleanly resets the count."""

	BASE_SECONDS = 60
	MAX_SECONDS = 3600
	MAX_CONSECUTIVE_PAUSES = 30

	@classmethod
	def seconds(cls, pause: int, hint_seconds: float) -> int:
		"""Seconds to pause for the ``pause``-th consecutive pause (1-based), given the source's hint."""
		backoff = cls.BASE_SECONDS * (1 << max(0, pause - 1))
		return min(cls.MAX_SECONDS, max(math.ceil(hint_seconds), backoff))

	@classmethod
	def exhausted(cls, pause: int) -> bool:
		return pause > cls.MAX_CONSECUTIVE_PAUSES


@dataclass
class RunCounts:
	"""Per-kind outcome counts of a run, persisted between steps and in the run log."""

	counts: dict[str, dict[str, int]] = field(default_factory=dict)

	@classmethod
	def from_json(cls, stored: JsonValue) -> RunCounts:
		counts: dict[str, dict[str, int]] = {}
		if isinstance(stored, dict):
			for kind, outcomes in stored.items():
				if isinstance(outcomes, dict):
					counts[kind] = {
						outcome: count
						for outcome, count in outcomes.items()
						if isinstance(count, int) and not isinstance(count, bool)
					}
		return cls(counts)

	def add(self, kind: Kind, outcome: Outcome) -> None:
		per_kind = self.counts.setdefault(kind.value, {})
		per_kind[outcome.value] = per_kind.get(outcome.value, 0) + 1

	def get(self, kind: Kind, outcome: Outcome) -> int:
		return self.counts.get(kind.value, {}).get(outcome.value, 0)

	def is_incomplete(self, kind: Kind) -> bool:
		return any(self.get(kind, outcome) > 0 for outcome in Outcome if outcome.is_incomplete())

	def writes(self) -> int:
		"""Objects written to ERPNext (created, updated or retired). Zero means the run changed nothing."""
		return sum(
			self.get(Kind(kind), outcome) for kind in self.counts for outcome in Outcome if outcome.is_write()
		)

	def to_json(self) -> dict[str, JsonValue]:
		return {kind: dict(outcomes) for kind, outcomes in self.counts.items()}

	def summary(self) -> str:
		"""One line per kind, e.g. "product: created 3, skipped 40"."""
		lines = [
			f"{kind}: " + ", ".join(f"{outcome} {count}" for outcome, count in outcomes.items())
			for kind, outcomes in self.counts.items()
		]
		return "\n".join(lines) if lines else "nothing to do"


@dataclass
class RunState:
	"""A run in progress, persisted between steps so a run survives across background jobs."""

	id: str
	mode: Mode
	dry_run: bool
	kinds: list[Kind]
	started_at: datetime
	index: int = 0
	cursor: str | None = None
	counts: RunCounts = field(default_factory=RunCounts)
	resume_at: datetime | None = None
	pauses: int = 0

	def is_paused(self, now: datetime) -> bool:
		return self.resume_at is not None and self.resume_at > now

	def current(self) -> Kind | None:
		return self.kinds[self.index] if self.index < len(self.kinds) else None

	def advance(self) -> None:
		self.index += 1
		self.cursor = None

	def has_failures(self) -> bool:
		return any(self.counts.is_incomplete(kind) for kind in self.kinds)


class RunStore(Protocol):
	"""Where the connection's run in progress lives between steps, and its log once finished."""

	def load(self) -> RunState | None: ...

	def start(self, state: RunState) -> None: ...

	def save(self, state: RunState) -> None: ...

	def finish(self, state: RunState, status: str) -> None: ...


class Cursors(Protocol):
	"""Per kind, the start time of the last run that completed it with nothing failed or deferred."""

	def last_clean_start(self, kind: Kind) -> datetime | None: ...

	def advance(self, kind: Kind, run_started_at: datetime) -> None: ...


class RunLock(Protocol):
	"""One run per connection at a time, as a lease that expires if its holder stops refreshing it."""

	def acquire(self, run_id: str) -> bool: ...

	def hold_until(self, run_id: str, until: datetime) -> None:
		"""Keeps the lease through a pause (it expires a TTL after ``until``, when the run resumes)."""
		...

	def refresh(self, run_id: str) -> None: ...

	def release(self, run_id: str) -> None: ...


class CredentialHold(Protocol):
	def hold(self, reason: str) -> None: ...


class SeenStore(Protocol):
	"""The identities a full pass has seen, so links it didn't see can be retired. Cleared per run."""

	def mark(self, run_id: str, kind: Kind, gids: Sequence[str]) -> None: ...

	def clear(self, run_id: str) -> None: ...


@dataclass(frozen=True)
class RunDependencies:
	reader: SourceReader
	sync: ObjectSync
	store: RunStore
	cursors: Cursors
	lock: RunLock
	credential_hold: CredentialHold
	seen: SeenStore
	links: LinkStore
	filters: Mapping[Kind, str]
	new_run_id: Callable[[], str]
	clock: Callable[[], datetime]
	monotonic: Callable[[], float]


class Runner:
	def __init__(self, kinds: Sequence[Kind], deps: RunDependencies) -> None:
		self._kinds = [kind for kind in SYNC_ORDER if kind in kinds and deps.sync.handles(kind)]
		self._deps = deps

	def start(self, mode: Mode, dry_run: bool, only_kinds: Sequence[Kind] | None = None) -> RunState | None:
		"""Starts a run unless one is in progress; returns it, or None when another run holds the lock."""
		run_id = self._deps.new_run_id()
		if not self._deps.lock.acquire(run_id):
			return None
		kinds = [kind for kind in self._kinds if only_kinds is None or kind in only_kinds]
		state = RunState(run_id, mode, dry_run, kinds, self._deps.clock())
		self._deps.store.start(state)
		logger.info("Run %s started (%s%s).", run_id, mode.value, ", dry run" if dry_run else "")
		return state

	def step(self, budget_seconds: float) -> bool:
		"""Works on the run in progress for up to ``budget_seconds``; True when it is finished."""
		state = self._deps.store.load()
		if state is None:
			return True
		if state.is_paused(self._deps.clock()):
			return False
		state.resume_at = None
		deadline = self._deps.monotonic() + budget_seconds
		while (kind := state.current()) is not None and self._deps.monotonic() < deadline:
			self._deps.lock.refresh(state.id)
			try:
				done = self._page(state, kind)
				state.pauses = 0
			except RetryLater as e:
				if self._pause(state, kind, e):
					return False
				done = True
			except CredentialsRejected as e:
				self._stop(state, kind, e)
				return True
			except KindUnavailable as e:
				logger.warning("%s not mirrored: %s", kind.value, e)
				state.counts.add(kind, Outcome.UNAVAILABLE)
				done = True
			except SourceError as e:
				logger.error("Reading %s failed: %s", kind.value, e)
				state.counts.add(kind, Outcome.FAILED)
				done = True
			if done:
				self._finish_kind(state, kind)
				state.advance()
			self._deps.store.save(state)
		if state.current() is not None:
			return False
		self._finish(state)
		return True

	def _page(self, state: RunState, kind: Kind) -> bool:
		"""Mirrors one page of a kind; True when the kind has no more pages."""
		since = None
		if state.mode is Mode.INCREMENTAL and self._deps.reader.is_incremental(kind):
			last = self._deps.cursors.last_clean_start(kind)
			since = None if last is None else last - CURSOR_OVERLAP
		page = self._deps.reader.page(kind, state.cursor, since, self._deps.filters.get(kind, ""))
		for node in page.nodes:
			state.counts.add(kind, self._deps.sync.sync(kind, node, state.dry_run))
		if state.mode is Mode.FULL:
			self._deps.seen.mark(state.id, kind, [node.id() for node in page.nodes])
		state.cursor = page.end_cursor
		return not page.has_next_page

	def _pause(self, state: RunState, kind: Kind, error: RetryLater) -> bool:
		"""Pauses where the run is (kind and cursor kept); False when the pauses are exhausted, so the kind
		is given up as failed instead."""
		state.pauses += 1
		if PausePolicy.exhausted(state.pauses):
			logger.error("Reading %s failed after %s pauses: %s", kind.value, state.pauses, error)
			state.counts.add(kind, Outcome.FAILED)
			state.pauses = 0
			return False
		seconds = PausePolicy.seconds(state.pauses, error.seconds)
		state.resume_at = self._deps.clock() + timedelta(seconds=seconds)
		self._deps.store.save(state)
		self._deps.lock.hold_until(state.id, state.resume_at)
		logger.warning(
			"Pausing %ss before reading %s again (pause %s): %s", seconds, kind.value, state.pauses, error
		)
		return True

	def _stop(self, state: RunState, kind: Kind, error: CredentialsRejected) -> None:
		logger.error(
			"Stopping the run: %s Scheduled runs are held until the connection is saved or a run is started by hand.",
			error,
		)
		state.counts.add(kind, Outcome.FAILED)
		self._deps.credential_hold.hold(str(error))
		self._finish(state)

	def _finish_kind(self, state: RunState, kind: Kind) -> None:
		if state.counts.is_incomplete(kind):
			logger.warning("%s did not complete cleanly; it is retried next run.", kind.value)
			return
		if state.counts.get(kind, Outcome.UNAVAILABLE):
			return
		if state.mode is Mode.FULL:
			if self._deps.filters.get(kind, ""):
				logger.info("%s is filtered, so deletions aren't inferred from this full run.", kind.value)
			else:
				for link in self._deps.links.unseen(kind, state.id):
					state.counts.add(kind, self._deps.sync.retire(kind, link.gid, state.dry_run))
		if not state.dry_run and not state.counts.is_incomplete(kind):
			self._deps.cursors.advance(kind, state.started_at)

	def _finish(self, state: RunState) -> None:
		status = "Incomplete" if state.has_failures() else "Complete"
		self._deps.store.finish(state, status)
		self._deps.seen.clear(state.id)
		self._deps.lock.release(state.id)
		logger.info("Run %s %s:\n%s", state.id, status.lower(), state.counts.summary())


def utc_now() -> datetime:
	return datetime.now(UTC)
