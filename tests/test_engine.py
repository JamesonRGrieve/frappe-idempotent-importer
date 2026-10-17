# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine over in-memory stores: one object's map → hash → write path, and stepped runs with their
cursors, retirement, pauses and credential hold."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import count

import pytest

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import (
	CredentialsRejected,
	KindUnavailable,
	RetryLater,
	SourceError,
	UnresolvedDependency,
	Unsupported,
)
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.run import CURSOR_OVERLAP, Mode, PausePolicy, RunCounts, RunDependencies, Runner
from idempotent_importer.core.source import Page
from idempotent_importer.core.sync import Handler, ObjectSync, Outcome
from tests.conftest import (
	MemoryCursors,
	MemoryHold,
	MemoryLinks,
	MemoryLock,
	MemoryRedirects,
	MemoryRunStore,
	MemoryScope,
	MemoryWriter,
)

PREFIX = "gid://test/a/"
START = datetime(2026, 9, 1, 12, tzinfo=UTC)


def gid(n: int) -> str:
	return f"{PREFIX}Page/{n}"


class PageMapper:
	"""Maps {"id", "title", "gone"?, "unsupported"?} to a Desired page."""

	kind = Kind.PAGE
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.get("unsupported"):
			raise Unsupported("no home")
		if node.get("gone"):
			return None
		return Desired(Kind.PAGE, node.id(), {"title": node.string("title")}, (f"/old/{node.id()[-1]}",))


def node(n: int, title: str = "t", **extra: JsonValue) -> Node:
	return Node({"id": gid(n), "title": title, **extra})


class Engine:
	def __init__(self) -> None:
		self.links = MemoryLinks(PREFIX)
		self.writer = MemoryWriter()
		self.scope = MemoryScope()
		self.redirects = MemoryRedirects()
		self.sync = ObjectSync(
			{Kind.PAGE: Handler(PageMapper(), self.writer)}, self.links, self.scope, self.redirects
		)


class TestObjectSync:
	def test_creates_then_skips_an_unchanged_object(self) -> None:
		engine = Engine()
		assert engine.sync.sync(Kind.PAGE, node(1), False) is Outcome.CREATED
		assert engine.sync.sync(Kind.PAGE, node(1), False) is Outcome.SKIPPED
		assert engine.scope.commits == 1
		assert engine.redirects.synced[gid(1)] == (("/old/1",), None)

	def test_updates_a_changed_object(self) -> None:
		engine = Engine()
		engine.sync.sync(Kind.PAGE, node(1), False)
		assert engine.sync.sync(Kind.PAGE, node(1, "new"), False) is Outcome.UPDATED
		assert engine.writer.written[gid(1)].fields["title"] == "new"

	def test_rewrites_a_record_deleted_by_hand(self) -> None:
		engine = Engine()
		engine.sync.sync(Kind.PAGE, node(1), False)
		engine.writer.gone.add("1")
		assert engine.sync.sync(Kind.PAGE, node(1), False) is Outcome.UPDATED

	def test_dry_runs_write_nothing(self) -> None:
		engine = Engine()
		assert engine.sync.sync(Kind.PAGE, node(1), True) is Outcome.CREATED
		assert engine.writer.written == {}
		assert engine.links.links == {}

	def test_unsupported_is_reported_not_written(self) -> None:
		engine = Engine()
		assert engine.sync.sync(Kind.PAGE, node(1, unsupported=True), False) is Outcome.UNSUPPORTED
		assert engine.writer.written == {}

	def test_a_missing_dependency_defers_and_rolls_back(self) -> None:
		engine = Engine()
		engine.writer.error = UnresolvedDependency("later")
		assert engine.sync.sync(Kind.PAGE, node(1), False) is Outcome.DEFERRED
		assert engine.scope.rollbacks == 1
		assert engine.links.links == {}

	def test_a_failure_rolls_back_and_the_run_carries_on(self) -> None:
		engine = Engine()
		engine.writer.error = RuntimeError("boom")
		assert engine.sync.sync(Kind.PAGE, node(1), False) is Outcome.FAILED
		assert engine.scope.rollbacks == 1
		engine.writer.error = None
		assert engine.sync.sync(Kind.PAGE, node(1), False) is Outcome.CREATED

	def test_retires_and_revives(self) -> None:
		engine = Engine()
		engine.sync.sync(Kind.PAGE, node(1), False)
		assert engine.sync.sync(Kind.PAGE, node(1, gone=True), False) is Outcome.RETIRED
		assert engine.writer.retired == [gid(1)]
		assert engine.sync.retire(Kind.PAGE, gid(1), False) is Outcome.SKIPPED
		assert engine.sync.sync(Kind.PAGE, node(1), False) is Outcome.UPDATED
		link = engine.links.find(Kind.PAGE, gid(1))
		assert link is not None
		assert not link.retired

	def test_retiring_something_never_mirrored_is_a_skip(self) -> None:
		assert Engine().sync.retire(Kind.PAGE, gid(9), False) is Outcome.SKIPPED

	def test_unknown_kinds_are_a_programming_error(self) -> None:
		with pytest.raises(LookupError):
			Engine().sync.sync(Kind.ORDER, node(1), False)


class FakeReader:
	"""Serves pages of nodes per kind; ``errors`` raise instead of the next page."""

	def __init__(self, pages: dict[Kind, list[list[Node]]]) -> None:
		self.pages = pages
		self.errors: list[Exception] = []
		self.calls: list[tuple[Kind, str | None, datetime | None, str]] = []

	def is_incremental(self, kind: Kind) -> bool:
		return True

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		self.calls.append((kind, cursor, since, filter_))
		if self.errors:
			raise self.errors.pop(0)
		pages = self.pages.get(kind, [[]])
		index = int(cursor or 0)
		more = index + 1 < len(pages)
		return Page(pages[index], more, str(index + 1) if more else None)

	def node(self, kind: Kind, gid_: str) -> Node | None:
		return None


class Clock:
	def __init__(self) -> None:
		self.now = START

	def __call__(self) -> datetime:
		return self.now


class Harness:
	def __init__(self, pages: dict[Kind, list[list[Node]]], filters: dict[Kind, str] | None = None) -> None:
		self.engine = Engine()
		self.reader = FakeReader(pages)
		self.store = MemoryRunStore()
		self.cursors = MemoryCursors()
		self.lock = MemoryLock()
		self.hold = MemoryHold()
		self.clock = Clock()
		ids = count(1)
		self.runner = Runner(
			[Kind.PAGE],
			RunDependencies(
				reader=self.reader,
				sync=self.engine.sync,
				store=self.store,
				cursors=self.cursors,
				lock=self.lock,
				credential_hold=self.hold,
				seen=self.engine.links,
				links=self.engine.links,
				filters=filters or {},
				new_run_id=lambda: f"run-{next(ids)}",
				clock=self.clock,
				monotonic=lambda: 0.0,
			),
		)

	def run(self, mode: Mode = Mode.FULL) -> RunCounts:
		state = self.runner.start(mode, dry_run=False)
		assert state is not None
		while not self.runner.step(60):
			pass
		return state.counts


class TestRunner:
	def test_a_full_run_reads_every_page(self) -> None:
		harness = Harness({Kind.PAGE: [[node(1), node(2)], [node(3)]]})
		counts = harness.run()
		assert counts.get(Kind.PAGE, Outcome.CREATED) == 3
		assert harness.store.finished == [("run-1", "Complete")]
		assert harness.lock.holder is None

	def test_only_one_run_at_a_time(self) -> None:
		harness = Harness({})
		assert harness.runner.start(Mode.FULL, False) is not None
		assert harness.runner.start(Mode.FULL, False) is None

	def test_a_full_run_retires_only_what_its_connection_no_longer_has(self) -> None:
		harness = Harness({Kind.PAGE: [[node(1), node(2)]]})
		harness.run()
		other = "gid://test/b/Page/1"
		harness.engine.links.save(
			Kind.PAGE, other, harness.engine.writer.write(Desired(Kind.PAGE, other, {}), None), "h", 1
		)
		harness.reader.pages[Kind.PAGE] = [[node(1)]]
		counts = harness.run()
		assert counts.get(Kind.PAGE, Outcome.RETIRED) == 1
		assert harness.engine.writer.retired == [gid(2)]

	def test_a_filtered_kind_never_retires(self) -> None:
		harness = Harness({Kind.PAGE: [[node(1), node(2)]]}, filters={Kind.PAGE: "status:active"})
		harness.run()
		harness.reader.pages[Kind.PAGE] = [[node(1)]]
		assert harness.run().get(Kind.PAGE, Outcome.RETIRED) == 0

	def test_incremental_runs_ask_since_the_last_clean_run(self) -> None:
		harness = Harness({Kind.PAGE: [[node(1)]]})
		harness.run(Mode.INCREMENTAL)
		assert harness.reader.calls[0][2] is None
		assert harness.cursors.cursors[Kind.PAGE] == START
		harness.clock.now = START + timedelta(hours=1)
		harness.run(Mode.INCREMENTAL)
		assert harness.reader.calls[-1][2] == START - CURSOR_OVERLAP

	def test_an_incomplete_kind_keeps_its_cursor(self) -> None:
		harness = Harness({Kind.PAGE: [[node(1)]]})
		harness.engine.writer.error = UnresolvedDependency("later")
		counts = harness.run()
		assert counts.get(Kind.PAGE, Outcome.DEFERRED) == 1
		assert Kind.PAGE not in harness.cursors.cursors
		assert harness.store.finished[-1][1] == "Incomplete"

	def test_a_source_that_pushes_back_pauses_the_run_in_place(self) -> None:
		harness = Harness({Kind.PAGE: [[node(1)], [node(2)]]})
		state = harness.runner.start(Mode.FULL, False)
		assert state is not None
		harness.reader.errors = [RetryLater(5, "slow down")]
		assert harness.runner.step(60) is False
		paused = harness.store.state
		assert paused is not None
		assert paused.resume_at == START + timedelta(seconds=PausePolicy.BASE_SECONDS)
		assert paused.pauses == 1
		assert harness.lock.held_until == paused.resume_at
		assert harness.runner.step(60) is False, "nothing happens during the pause"
		assert len(harness.reader.calls) == 1
		harness.clock.now = paused.resume_at
		assert harness.runner.step(60) is True
		assert harness.reader.calls[1][1] is None, "it reads the page it paused on"
		assert harness.store.finished[-1][1] == "Complete"

	def test_pauses_give_up_after_about_a_day(self) -> None:
		harness = Harness({Kind.PAGE: [[node(1)]]})
		harness.runner.start(Mode.FULL, False)
		harness.reader.errors = [RetryLater(1, "down")] * (PausePolicy.MAX_CONSECUTIVE_PAUSES + 1)
		for _ in range(PausePolicy.MAX_CONSECUTIVE_PAUSES):
			assert harness.runner.step(60) is False
			state = harness.store.state
			assert state is not None and state.resume_at is not None
			harness.clock.now = state.resume_at
		assert harness.runner.step(60) is True
		assert harness.store.finished[-1][1] == "Incomplete"

	def test_rejected_credentials_stop_the_run_and_hold(self) -> None:
		harness = Harness({Kind.PAGE: [[node(1)]]})
		harness.runner.start(Mode.FULL, False)
		harness.reader.errors = [CredentialsRejected("HTTP 401")]
		assert harness.runner.step(60) is True
		assert harness.hold.reasons == ["HTTP 401"]
		assert harness.store.finished[-1][1] == "Incomplete"
		assert harness.lock.holder is None

	def test_a_kind_the_credentials_cannot_read_is_skipped_and_retires_nothing(self) -> None:
		harness = Harness({Kind.PAGE: [[node(1), node(2)]]})
		harness.run()
		cursor = harness.cursors.cursors[Kind.PAGE]
		harness.clock.now = START + timedelta(hours=1)
		state = harness.runner.start(Mode.FULL, False)
		assert state is not None
		harness.reader.errors = [KindUnavailable("the app lacks a scope")]
		assert harness.runner.step(60) is True
		assert harness.store.finished[-1][1] == "Complete"
		assert state.counts.get(Kind.PAGE, Outcome.UNAVAILABLE) == 1
		assert harness.engine.writer.retired == [], "its copies are left as they are"
		assert harness.cursors.cursors[Kind.PAGE] == cursor

	def test_a_source_error_fails_the_kind_and_moves_on(self) -> None:
		harness = Harness({Kind.PAGE: [[node(1)]]})
		harness.runner.start(Mode.FULL, False)
		harness.reader.errors = [SourceError("bad shape")]
		assert harness.runner.step(60) is True
		assert harness.store.finished[-1][1] == "Incomplete"


class TestPausePolicy:
	def test_pauses_grow_to_an_hour(self) -> None:
		assert [PausePolicy.seconds(n, 0) for n in (1, 2, 3, 7, 8)] == [60, 120, 240, 3600, 3600]
		assert PausePolicy.seconds(1, 300.2) == 301

	def test_exhaustion(self) -> None:
		assert not PausePolicy.exhausted(PausePolicy.MAX_CONSECUTIVE_PAUSES)
		assert PausePolicy.exhausted(PausePolicy.MAX_CONSECUTIVE_PAUSES + 1)


class TestRunCounts:
	def test_counts_round_trip_and_summarise(self) -> None:
		counts = RunCounts()
		counts.add(Kind.PAGE, Outcome.CREATED)
		counts.add(Kind.PAGE, Outcome.SKIPPED)
		counts.add(Kind.ORDER, Outcome.RETIRED)
		assert counts.writes() == 2
		assert RunCounts.from_json(counts.to_json()) == counts
		assert counts.summary() == "page: created 1, skipped 1\norder: retired 1"
		assert RunCounts().summary() == "nothing to do"
		assert RunCounts.from_json({"page": {"created": "x", "skipped": True}}).to_json() == {"page": {}}
