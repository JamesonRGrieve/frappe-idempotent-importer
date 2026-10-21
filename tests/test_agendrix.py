# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Agendrix adapter over the recorded café (tests/fixtures/agendrix, proven against Agendrix's OpenAPI
document): OAuth with JSON token requests and the redirect URI on refresh, paging, the updated and created
windows of incremental runs, a member's days gathered into attendance, and every mapper."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from idempotent_importer.core.errors import RetryLater, SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.oauth import TokenGrant
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.agendrix.adapter import AgendrixAdapter
from idempotent_importer.sources.agendrix.reader import AgendrixReader, api_base
from tests.support.transport import (
	OAUTH_ACCESS_TOKEN,
	OAUTH_ROTATED_REFRESH_TOKEN,
	FakeTransport,
	RecordingSleeper,
	oauth_source,
	scripted,
)

ADAPTER = AgendrixAdapter()
GRANT_PATH = "/oauth/token"
SITE, POSITION, MEMBER, LEAVE_TYPE, SHIFT, CLOCK, ENTRY, REQUEST = range(1, 9)
# The clock the reader's windows end at.
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()


def uuid(group: int, n: int) -> str:
	return f"00000000-0000-4000-{group:04d}-{n:012d}"


def gid(type_: str, group: int, n: int) -> str:
	return f"gid://agendrix/agendrix-a/{type_}/{uuid(group, n)}"


def day(member: int, date: str) -> str:
	return f"gid://agendrix/agendrix-a/Attendance/{uuid(MEMBER, member)}:{date}"


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="agendrix",
		connection="agendrix-a",
		store_url="https://app.agendrix.com",
		api_user="e2e-agendrix-client",
		api_secret="e2e-agendrix-secret",
		refresh_token="e2e-agendrix-refresh",
		save_refresh_token=lambda token: None,
		site_url="https://erp.example",
		endpoint_override="http://agendrix-fake:8080",
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[AgendrixReader, FakeTransport]:
	fake = oauth_source("agendrix", state, GRANT_PATH)
	built = ADAPTER.reader(config, fake, RecordingSleeper(NOW))
	assert isinstance(built, AgendrixReader)
	return built, fake


def nodes(
	config: ConnectionConfig, kind: Kind, state: str = "v1", since: datetime | None = None
) -> dict[str, Node]:
	source, _ = reader(config, state)
	found: dict[str, Node] = {}
	cursor: str | None = None
	while True:
		page = source.page(kind, cursor, since, "")
		found |= {node.id(): node for node in page.nodes}
		if not page.has_next_page:
			return found
		cursor = page.end_cursor


def desired(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> object:
	mapper = {m.kind: m for m in ADAPTER.mappers(config)}[kind]
	return mapper.map(nodes(config, kind, state)[key])


def values(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> Values:
	mapped = desired(config, kind, key, state)
	assert mapped is not None and hasattr(mapped, "values")
	result = mapped.values()
	assert isinstance(result, Values)
	return result


def query_of(url: str) -> dict[str, list[str]]:
	return parse_qs(urlsplit(url).query)


class TestOAuth:
	def test_the_approval_asks_to_read(self, config: ConnectionConfig) -> None:
		url = ADAPTER.oauth(config, scripted()).authorization_url("https://erp/cb", "s", "v")
		assert url.startswith("https://app.agendrix.com/oauth/authorize?")
		assert query_of(url)["scope"] == ["read"]
		sandbox = replace(config, store_url="https://sandbox.agendrix.net")
		assert (
			ADAPTER.oauth(sandbox, scripted())
			.authorization_url("cb", "s", "v")
			.startswith("https://sandbox.agendrix.net/oauth/authorize?")
		)

	def test_a_refresh_is_json_and_names_the_redirect_uri(self, config: ConnectionConfig) -> None:
		saved: list[str] = []
		source, fake = reader(replace(config, save_refresh_token=saved.append))
		source.page(Kind.BRANCH, None, None, "")
		token = fake.requests[0]
		assert token.headers["Content-Type"] == "application/json"
		assert json.loads(token.body) == {
			"grant_type": "refresh_token",
			"refresh_token": "e2e-agendrix-refresh",
			"redirect_uri": "https://erp.example/api/method/idempotent_importer.api.oauth_callback",
			"client_id": "e2e-agendrix-client",
			"client_secret": "e2e-agendrix-secret",
		}
		assert saved == [OAUTH_ROTATED_REFRESH_TOKEN], "the single-use refresh token is replaced"
		api = fake.requests[1]
		assert api.headers["Authorization"] == f"Bearer {OAUTH_ACCESS_TOKEN}"
		assert api.headers["Content-Type"] == "application/json"
		assert ADAPTER.connected(config, scripted(), TokenGrant("a"), {}) == {}

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="client ID and secret"):
			ADAPTER.oauth(replace(config, api_secret=""), scripted())
		with pytest.raises(SourceError, match="Store URL"):
			api_base(replace(config, store_url="https://agendrix.example", endpoint_override=None))
		assert api_base(replace(config, endpoint_override=None)) == "https://api.agendrix.com/v1"
		without_site = replace(config, site_url="")
		source, _ = reader(without_site)
		with pytest.raises(SourceError, match="redirect URI"):
			source.page(Kind.BRANCH, None, None, "")


class TestReader:
	def test_lists_read_archived_records_too(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.EMPLOYEE, None, None, "")
		assert query_of(fake.requests[-1].url)["search[with_deleted]"] == ["true"]
		assert set(nodes(config, Kind.EMPLOYEE)) == {gid("Member", MEMBER, n) for n in (1, 2, 3)}

	def test_shifts_are_read_by_their_last_update(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		assert source.is_incremental(Kind.SHIFT_ASSIGNMENT) and not source.is_incremental(Kind.EMPLOYEE)
		source.page(Kind.SHIFT_ASSIGNMENT, None, None, "")
		full = query_of(fake.requests[-1].url)
		assert full["search[updated_since]"] == ["2000-01-01T00:00:00Z"]
		assert full["search[updated_until]"] == ["2026-10-20T12:00:00Z"]
		assert "search[with_deleted]" not in full, "a full pass doesn't ask for removed shifts"
		since = datetime(2026, 10, 1, tzinfo=UTC)
		source.page(Kind.SHIFT_ASSIGNMENT, None, since, "")
		incremental = query_of(fake.requests[-1].url)
		assert incremental["search[updated_since]"] == ["2026-10-01T00:00:00Z"]
		assert incremental["search[with_deleted]"] == ["true"]
		assert gid("Shift", SHIFT, 3) not in nodes(config, Kind.SHIFT_ASSIGNMENT, "v2")
		assert gid("Shift", SHIFT, 3) in nodes(config, Kind.SHIFT_ASSIGNMENT, "v2", since)

	def test_clock_logs_since_they_were_created(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.EMPLOYEE_CHECKIN, None, datetime(2026, 10, 1, tzinfo=UTC), "")
		assert query_of(fake.requests[-1].url)["search[created_since]"] == ["2026-10-01T00:00:00Z"]

	def test_paging_follows_the_last_page_flag(self, config: ConnectionConfig) -> None:
		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if method == "POST":
				return HttpResponse(200, b'{"access_token": "t", "refresh_token": "r"}')
			page = {"data": [], "pagination": {"current_page": 1, "next_page": 2, "last_page": False}}
			return HttpResponse(200, json.dumps(page).encode())

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		page = source.page(Kind.SHIFT_ASSIGNMENT, None, None, "")
		assert page.has_next_page and page.end_cursor == "2|2026-10-20T12:00:00Z"
		source.page(Kind.SHIFT_ASSIGNMENT, page.end_cursor, None, "")

	def test_a_spent_window_pauses_the_run(self, config: ConnectionConfig) -> None:
		grant = HttpResponse(200, b'{"access_token": "t", "refresh_token": "r"}')
		limited = HttpResponse(429, b"{}", {"ratelimit-reset": "120", "ratelimit-remaining": "0"})
		source = ADAPTER.reader(config, scripted(grant, limited), RecordingSleeper(NOW))
		with pytest.raises(RetryLater):
			source.page(Kind.BRANCH, None, None, "")

	def test_a_full_pass_gathers_each_members_days(self, config: ConnectionConfig) -> None:
		days = nodes(config, Kind.ATTENDANCE)
		assert set(days) == {day(1, "2026-10-05"), day(1, "2026-10-09"), day(2, "2026-10-05")}
		assert len(days[day(1, "2026-10-05")].children("entries")) == 2

	def test_an_incremental_run_reads_the_touched_days_whole(self, config: ConnectionConfig) -> None:
		source, fake = reader(config, "v2")
		page = source.page(Kind.ATTENDANCE, None, datetime(2026, 10, 1, tzinfo=UTC), "")
		changed = query_of(fake.requests[1].url)
		assert changed["search[with_deleted]"] == ["true"] and "search[updated_since]" in changed
		days = {node.id(): node for node in page.nodes}
		assert set(days) == {day(1, "2026-10-05"), day(1, "2026-10-09"), day(2, "2026-10-05")}
		read = query_of(fake.requests[2].url)
		assert read["search[from]"] == read["search[to]"] == ["2026-10-05"]
		assert len(days[day(1, "2026-10-05")].children("entries")) == 2

	def test_one_object_by_its_identity(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		member = source.node(Kind.EMPLOYEE, gid("Member", MEMBER, 1))
		assert member is not None and member.id() == gid("Member", MEMBER, 1)
		assert source.node(Kind.EMPLOYEE, gid("Member", MEMBER, 9)) is None, "no longer there"
		assert source.node(Kind.ATTENDANCE, day(1, "2026-10-05")) is not None
		with pytest.raises(SourceError, match="can't read"):
			source.node(Kind.EMPLOYEE, gid("Site", SITE, 1))
		with pytest.raises(SourceError, match="no lead"):
			source.page(Kind.LEAD, None, None, "")

	def test_every_kind_has_a_mapper(self, config: ConnectionConfig) -> None:
		assert {mapper.kind for mapper in ADAPTER.mappers(config)} == set(ADAPTER.kinds())


class TestMappers:
	def test_sites_positions_and_members(self, config: ConnectionConfig) -> None:
		assert values(config, Kind.BRANCH, gid("Site", SITE, 1), "v2").string("name") == "Downtown Café"
		assert (
			values(config, Kind.DESIGNATION, gid("Position", POSITION, 3)).string("name") == "Kitchen - Prep"
		)
		avery = values(config, Kind.EMPLOYEE, gid("Member", MEMBER, 1))
		assert (
			avery.string("first_name"),
			avery.optional_string("date_of_birth"),
			avery.optional_string("date_of_joining"),
			avery.optional_string("designation"),
			avery.optional_string("branch"),
			avery.optional_string("employee_number"),
			avery.string("status"),
		) == (
			"Avery",
			"1990-04-05",
			"2025-03-01",
			gid("Position", POSITION, 1),
			gid("Site", SITE, 1),
			"E-001",
			"Active",
		)
		assert avery.optional_string("gender") is None, "Agendrix keeps no gender"
		sam = values(config, Kind.EMPLOYEE, gid("Member", MEMBER, 3), "v2")
		assert (sam.string("status"), sam.optional_string("relieving_date")) == ("Left", "2026-10-15")
		member = Node({"id": "x", "profile": {"last_name": "Only"}})
		with pytest.raises(Unsupported, match="first name"):
			{m.kind: m for m in ADAPTER.mappers(config)}[Kind.EMPLOYEE].map(member)

	def test_leave_types(self, config: ConnectionConfig) -> None:
		assert values(config, Kind.LEAVE_TYPE, gid("LeaveType", LEAVE_TYPE, 2)).all() == {
			"name": "Sick leave",
			"paid": False,
		}

	def test_shifts(self, config: ConnectionConfig) -> None:
		morning = values(config, Kind.SHIFT_ASSIGNMENT, gid("Shift", SHIFT, 1))
		assert morning.required_record("shift").all() == {
			"name": "08:00-16:00",
			"start_time": "08:00:00",
			"end_time": "16:00:00",
		}
		assert morning.flag("submitted") and morning.string("employee") == gid("Member", MEMBER, 1)
		night = values(config, Kind.SHIFT_ASSIGNMENT, gid("Shift", SHIFT, 2))
		assert (night.string("date"), night.required_record("shift").string("name")) == (
			"2026-10-05",
			"22:00-06:00",
		)
		assert not values(config, Kind.SHIFT_ASSIGNMENT, gid("Shift", SHIFT, 3)).flag("submitted")
		moved = values(config, Kind.SHIFT_ASSIGNMENT, gid("Shift", SHIFT, 1), "v2")
		assert moved.required_record("shift").string("name") == "09:00-17:00"
		with pytest.raises(Unsupported, match="open shift"):
			desired(config, Kind.SHIFT_ASSIGNMENT, gid("Shift", SHIFT, 4))
		assert desired(config, Kind.SHIFT_ASSIGNMENT, gid("Shift", SHIFT, 5)) is None, "its request's"
		removed = nodes(config, Kind.SHIFT_ASSIGNMENT, "v2", datetime(2026, 10, 1, tzinfo=UTC))
		shifts = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.SHIFT_ASSIGNMENT]
		assert shifts.map(removed[gid("Shift", SHIFT, 3)]) is None
		unrequested = Node({"id": "x", "time_off": True, "member_id": uuid(MEMBER, 1)})
		with pytest.raises(Unsupported, match="without a request"):
			shifts.map(unrequested)

	def test_punches(self, config: ConnectionConfig) -> None:
		punch = values(config, Kind.EMPLOYEE_CHECKIN, gid("ClockLog", CLOCK, 1))
		assert punch.all() == {
			"employee": gid("Member", MEMBER, 1),
			"time": "2026-10-05T11:58:12Z",
			"log_type": "IN",
			"device": "tablet",
		}
		assert desired(config, Kind.EMPLOYEE_CHECKIN, gid("ClockLog", CLOCK, 3)) is None, "a failed clock"
		checkins = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.EMPLOYEE_CHECKIN]
		with pytest.raises(Unsupported, match="neither"):
			checkins.map(Node({"id": "x", "success": True, "action": "break", "member_id": uuid(MEMBER, 1)}))

	def test_attendance(self, config: ConnectionConfig) -> None:
		avery = values(config, Kind.ATTENDANCE, day(1, "2026-10-05"))
		assert avery.all() == {
			"employee": gid("Member", MEMBER, 1),
			"date": "2026-10-05",
			"status": "Present",
			"working_minutes": 450,
			"in_time": "2026-10-05T12:00:00Z",
			"out_time": "2026-10-05T20:00:00Z",
			"leave_type": None,
			"submitted": True,
		}
		assert values(config, Kind.ATTENDANCE, day(1, "2026-10-05"), "v2").integer("working_minutes") == 465
		jordan = values(config, Kind.ATTENDANCE, day(2, "2026-10-05"))
		assert (jordan.integer("working_minutes"), jordan.flag("submitted")) == (480, False)
		assert desired(config, Kind.ATTENDANCE, day(1, "2026-10-09")) is None, "a day of leave is the leave's"
		attendance = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.ATTENDANCE]
		open_entry = Node(
			{
				"id": "x",
				"member_id": uuid(MEMBER, 1),
				"date": "2026-10-19",
				"entries": [{"start_at": "2026-10-19T08:00:00-04:00", "regular_minutes": 60}],
			}
		)
		still_in = attendance.map(open_entry)
		assert still_in is not None and still_in.values().optional_string("out_time") is None

	def test_leave_requests(self, config: ConnectionConfig) -> None:
		vacation = values(config, Kind.LEAVE_APPLICATION, gid("LeaveRequest", REQUEST, 1))
		assert vacation.all() == {
			"employee": gid("Member", MEMBER, 1),
			"leave_type": gid("LeaveType", LEAVE_TYPE, 1),
			"from_date": "2026-10-09",
			"to_date": "2026-10-09",
			"status": "Approved",
			"posting_date": "2026-09-01",
			"half_day": False,
			"description": "Family trip",
		}
		sick = gid("LeaveRequest", REQUEST, 2)
		assert values(config, Kind.LEAVE_APPLICATION, sick).string("status") == "Open"
		assert values(config, Kind.LEAVE_APPLICATION, sick, "v2").string("status") == "Rejected"
		with pytest.raises(Unsupported, match="part of a single day"):
			desired(config, Kind.LEAVE_APPLICATION, gid("LeaveRequest", REQUEST, 3))
		leaves = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.LEAVE_APPLICATION]
		cancelled = Node(
			{
				"id": "x",
				"member_id": uuid(MEMBER, 1),
				"all_day": True,
				"start_date": "2026-11-02",
				"end_date": "2026-11-02",
				"created_at": "2026-10-01T09:00:00-04:00",
				"canceled_at": "2026-10-02T09:00:00-04:00",
				"leave_type": {"id": uuid(LEAVE_TYPE, 1)},
			}
		)
		mapped = leaves.map(cancelled)
		assert mapped is not None and mapped.values().string("status") == "Cancelled"
		with pytest.raises(SourceError, match="no time off type"):
			leaves.map(Node({"id": "x", "all_day": True, "member_id": uuid(MEMBER, 1)}))
