# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one Agendrix organisation through its public API (api.agendrix.com/v1), as Agendrix's OpenAPI
document describes it (github.com/agendrix/openapi, en/open_api.json, version 1.14.4, pinned in
tools/specs.py): sites, positions and members (archived ones too: ``search[with_deleted]``), time off types
and requests, shifts, clock logs and time entries. Every list pages by ``page`` and ``page_size`` (at most
100) and answers ``{data, pagination}``; ``pagination.last_page`` ends a kind. What the token sees is what
its owner's role sees, so the organisation's administrator connects it.

Shifts are read by when they were last updated (``search[updated_since]`` / ``search[updated_until]``, the
filter that needs no 31-day date window): a full pass from FULL_PASS_SINCE, an incremental run from the
cursor with archived shifts included, so a removed shift is retired. Clock logs never change once logged:
an incremental run asks only for those created since (``search[created_since]``). Members, positions,
sites, time off types and requests have no change filter: every run reads them whole, and the change hash
skips what didn't change.

A day's attendance is one node per member and day (``Attendance/<member>:<date>``), gathered from the
member's time entries: a full pass reads each member's entries in turn (``search[member_id]`` needs no date
window), an incremental run reads the entries updated since (archived ones included) and then each day they
touch whole, so a day keeps every entry, not only the changed one.

Limits (the API description's "Rate Limiting"): 360 requests a minute; a 429 carries RateLimit-Reset (seconds
until the window resets), which the client waits out or, when long, pauses the run on. Every node carries
its identity as ``id`` (``gid://agendrix/<connection>/<Type>/<id>``)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit, ResetFormat, Sleeper
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.core.times import format_utc

SOURCE_ID = "agendrix"
PRODUCTION_APP, SANDBOX_APP = "app.agendrix.com", "sandbox.agendrix.net"
PRODUCTION_API, SANDBOX_API = "https://api.agendrix.com", "https://api.sandbox.agendrix.net"
API_PATH = "/v1"
PAGE_SIZE = 100
FIRST_PAGE = 1
REQUESTS_PER_MINUTE = 360
RATE_LIMIT = RateLimit(
	requests_per_second=RateLimit.per_period(REQUESTS_PER_MINUTE, 60.0),
	remaining_header="ratelimit-remaining",
	reset_header="ratelimit-reset",
	reset_format=ResetFormat.SECONDS_FROM_NOW,
)
# Before any shift Agendrix holds was last updated (the organisation's whole history).
FULL_PASS_SINCE = datetime(2000, 1, 1, tzinfo=UTC)
WITH_DELETED: tuple[tuple[str, QueryValue], ...] = (("search[with_deleted]", "true"),)
CURSOR_SEPARATOR = "|"
DAY_SEPARATOR = ":"
ATTENDANCE_TYPE = "Attendance"
MEMBER_TYPE = "Member"
TIME_ENTRIES = "/time_entries"
MEMBERS = "/members"


@dataclass(frozen=True)
class Collection:
	"""One list a kind reads: its path, the identity type of its records, its fixed query, and how an
	incremental run narrows it: ``created_since`` (the search parameter of records that never change after
	they are made) or ``updated`` (the updated_since/until window, archived records included)."""

	path: str
	type: str
	query: tuple[tuple[str, QueryValue], ...] = ()
	created_since: str | None = None
	updated: bool = False


COLLECTIONS: dict[Kind, Collection] = {
	Kind.BRANCH: Collection("/sites", "Site", WITH_DELETED),
	Kind.DESIGNATION: Collection("/positions", "Position", WITH_DELETED),
	Kind.EMPLOYEE: Collection(MEMBERS, MEMBER_TYPE, WITH_DELETED),
	Kind.LEAVE_TYPE: Collection("/leave_types", "LeaveType"),
	Kind.SHIFT_ASSIGNMENT: Collection("/shifts", "Shift", (("sort[updated_at]", "asc"),), updated=True),
	Kind.EMPLOYEE_CHECKIN: Collection(
		"/clock_logs", "ClockLog", (("sort[created_at]", "asc"),), created_since="search[created_since]"
	),
	Kind.LEAVE_APPLICATION: Collection("/leave_requests", "LeaveRequest"),
}
KINDS: tuple[Kind, ...] = (*COLLECTIONS, Kind.ATTENDANCE)


def api_base(config: ConnectionConfig) -> str:
	"""The organisation's API: production, or Agendrix's sandbox (the connection's Store URL names its app)."""
	if config.endpoint_override:
		return config.endpoint_override + API_PATH
	return (SANDBOX_API if app_host(config) == SANDBOX_APP else PRODUCTION_API) + API_PATH


def app_host(config: ConnectionConfig) -> str:
	host = urlsplit(config.store_url if "//" in config.store_url else f"//{config.store_url}").hostname
	if host not in (PRODUCTION_APP, SANDBOX_APP, None):
		raise SourceError(
			f"Agendrix's Store URL is https://{PRODUCTION_APP} (or https://{SANDBOX_APP}), not {host}."
		)
	return host or PRODUCTION_APP


def pages_of(cursor: str | None) -> tuple[int, str | None]:
	"""A cursor's page and the end of its window (``<page>|<until>``)."""
	if cursor is None:
		return FIRST_PAGE, None
	page, _, until = cursor.partition(CURSOR_SEPARATOR)
	return int(page), until or None


class AgendrixReader:
	def __init__(self, config: ConnectionConfig, client: RestClient, sleeper: Sleeper) -> None:
		self._config = config
		self._client = client
		self._sleeper = sleeper
		self._positions: dict[str, str | None] = {}

	def is_incremental(self, kind: Kind) -> bool:
		if kind is Kind.ATTENDANCE:
			return True
		collection = COLLECTIONS.get(kind)
		return collection is not None and (collection.updated or collection.created_since is not None)

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		if kind is Kind.ATTENDANCE:
			return self._changed_days(cursor, since) if since else self._member_days(cursor)
		collection = COLLECTIONS.get(kind)
		if collection is None:
			raise SourceError(f"Agendrix has no {kind.value}.")
		number, until = pages_of(cursor)
		query: dict[str, QueryValue] = dict(collection.query)
		if collection.updated:
			until = until or self._now()
			query |= {
				"search[updated_since]": format_utc(since or FULL_PASS_SINCE),
				"search[updated_until]": until,
			}
			if since is not None:
				query |= dict(WITH_DELETED)
		elif collection.created_since and since is not None:
			query[collection.created_since] = format_utc(since)
		records, last = self._list(collection.path, number, query)
		nodes = [self._identified(collection.type, record) for record in records]
		if kind is Kind.DESIGNATION:
			nodes = [self._with_parent(node) for node in nodes]
		following = f"{number + 1}{CURSOR_SEPARATOR}{until or ''}"
		return Page(nodes, not last, None if last else following)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		if kind is Kind.ATTENDANCE:
			member, _, day = identity.id.partition(DAY_SEPARATOR)
			return self._day(member, day)
		collection = COLLECTIONS.get(kind)
		if collection is None or identity.type != collection.type:
			raise SourceError(f"Agendrix can't read a {identity.type} as a {kind.value}.")
		record = self._one(f"{collection.path}/{identity.id}")
		if record is None:
			return None
		node = self._identified(collection.type, record)
		return self._with_parent(node) if kind is Kind.DESIGNATION else node

	# -- attendance -----------------------------------------------------------------------------------------

	def _member_days(self, cursor: str | None) -> Page:
		"""A full pass: one member's days per page, every member in turn (cursor ``<page>:<index>``)."""
		number, index = (FIRST_PAGE, 0)
		if cursor is not None:
			page_text, _, index_text = cursor.partition(DAY_SEPARATOR)
			number, index = int(page_text), int(index_text)
		members, last = self._list(MEMBERS, number, dict(WITH_DELETED))
		if index >= len(members):
			return Page([], False, None)
		member = Node(members[index]).string("id")
		entries = list(self._all(TIME_ENTRIES, {"search[member_id]": member, "sort[date]": "asc"}))
		nodes = self._days(member, entries)
		if index + 1 < len(members):
			return Page(nodes, True, f"{number}{DAY_SEPARATOR}{index + 1}")
		return Page(nodes, not last, None if last else f"{number + 1}{DAY_SEPARATOR}0")

	def _changed_days(self, cursor: str | None, since: datetime) -> Page:
		"""An incremental run: the days the entries updated since touch, each read whole."""
		number, until = pages_of(cursor)
		until = until or self._now()
		query: dict[str, QueryValue] = {
			"search[updated_since]": format_utc(since),
			"search[updated_until]": until,
			**dict(WITH_DELETED),
		}
		entries, last = self._list(TIME_ENTRIES, number, query)
		touched = dict.fromkeys(
			(entry.string("member_id"), entry.string("date")) for entry in map(Node, entries)
		)
		nodes = [self._day(member, date) for member, date in touched]
		return Page(nodes, not last, None if last else f"{number + 1}{CURSOR_SEPARATOR}{until}")

	def _day(self, member: str, date: str) -> Node:
		query: dict[str, QueryValue] = {"search[member_id]": member, "search[from]": date, "search[to]": date}
		entries = [entry for entry in self._all(TIME_ENTRIES, query) if entry.get("date") == date]
		return self._day_node(member, date, entries)

	def _days(self, member: str, entries: list[dict[str, JsonValue]]) -> list[Node]:
		by_day: dict[str, list[dict[str, JsonValue]]] = {}
		for entry in entries:
			by_day.setdefault(Node(entry).string("date"), []).append(entry)
		return [self._day_node(member, date, day) for date, day in by_day.items()]

	def _day_node(self, member: str, date: str, entries: list[dict[str, JsonValue]]) -> Node:
		gid = self._config.identity(ATTENDANCE_TYPE, f"{member}{DAY_SEPARATOR}{date}")
		return Node({"id": gid, "member_id": member, "date": date, "entries": list[JsonValue](entries)}, gid)

	# -- requests -----------------------------------------------------------------------------------------

	def _list(
		self, path: str, number: int, query: dict[str, QueryValue]
	) -> tuple[list[dict[str, JsonValue]], bool]:
		"""One page of a list, and whether it is the last."""
		result = self._client.get(path, {**query, "page": number, "page_size": PAGE_SIZE})
		if result is None:
			raise SourceError(f"Agendrix has no {path}.")
		records = [child.raw() for child in result.node.children("data")]
		pagination = result.node.child("pagination")
		last = pagination is None or pagination.optional_flag("last_page") is not False
		return records, last

	def _all(self, path: str, query: dict[str, QueryValue]) -> Iterator[dict[str, JsonValue]]:
		number = FIRST_PAGE
		while True:
			records, last = self._list(path, number, query)
			yield from records
			if last:
				return
			number += 1

	def _one(self, path: str) -> dict[str, JsonValue] | None:
		result = self._client.get(path)
		record = result.node.child("data") if result else None
		return record.raw() if record else None

	def _identified(self, type_: str, record: dict[str, JsonValue]) -> Node:
		gid = self._config.identity(type_, Node(record).string("id"))
		return Node({**record, "id": gid, "source_id": record["id"]}, gid)

	def _with_parent(self, position: Node) -> Node:
		"""A sub-position with its parent's name (a Designation is named by both: sub-positions of different
		positions may share a name)."""
		parent = position.optional_string("parent_id")
		return position.with_fields(parent_name=self._position_name(parent)) if parent else position

	def _position_name(self, position: str) -> str | None:
		if position not in self._positions:
			record = self._one(f"/positions/{position}")
			self._positions[position] = Node(record).optional_string("name") if record else None
		return self._positions[position]

	def _now(self) -> str:
		return format_utc(datetime.fromtimestamp(self._sleeper.now(), UTC))
