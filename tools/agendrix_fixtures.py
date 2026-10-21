# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the Agendrix fixtures (tests/fixtures/agendrix/<state>/*.json): a small café's organisation as its
public API answers (``{data, pagination}``, shaped as Agendrix's OpenAPI document defines each record,
pinned in tools/specs.py), for exactly the requests the reader sends (built from the reader's own
collections, so they can't drift): each list, the time entries of each member, and each sub-position's
parent. A list with archived records has two answers: with ``search[with_deleted]`` (an incremental run)
and without (a full pass). The organisation's two states differ only by their records (v2: a renamed site,
a member archived, a shift moved and a draft removed, a new punch, a longer afternoon and a declined
request).

``python -m tools.agendrix_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.core.kind import Kind
from idempotent_importer.sources.agendrix import reader

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "agendrix"
PREFIX = reader.API_PATH
CREATED = "2026-09-01T09:00:00-04:00"
UPDATED = "2026-09-15T09:00:00-04:00"
LATER = "2026-10-16T09:00:00-04:00"
# Every time parameter an incremental run sends: any value.
ANY = ".+"
Record = dict[str, Any]


def uuid(group: int, n: int) -> str:
	return f"00000000-0000-4000-{group:04d}-{n:012d}"


SITE, POSITION, MEMBER, LEAVE_TYPE, SHIFT, CLOCK, ENTRY, REQUEST, PROFILE = range(1, 10)


def site(n: int, name: str, address: str) -> Record:
	return {
		"id": uuid(SITE, n),
		"name": name,
		"address": address,
		"created_at": CREATED,
		"updated_at": UPDATED,
	}


def position(n: int, name: str, rank: int, parent: int | None = None) -> Record:
	body: Record = {
		"id": uuid(POSITION, n),
		"name": name,
		"normalized_name": name.lower(),
		"rank": rank,
		"color": "#0f6973",
		"created_at": CREATED,
		"updated_at": UPDATED,
	}
	return body | ({"parent_id": uuid(POSITION, parent)} if parent else {})


def member(
	n: int, first: str, last: str, main_position: int, birthdate: str | None = None, **fields: Any
) -> Record:
	profile: Record = {
		"id": uuid(PROFILE, n),
		"first_name": first,
		"last_name": last,
		"full_name": f"{first} {last}",
		"email": f"{first.lower()}@cafe.example",
		"phone_1_number": f"+1514555{n:04d}",
		"phone_1_type": "mobile",
		"created_at": CREATED,
		"updated_at": UPDATED,
	}
	if birthdate:
		profile["birthdate"] = birthdate
	return {
		"id": uuid(MEMBER, n),
		"user_id": uuid(MEMBER + 100, n),
		"profile_id": profile["id"],
		"identification_number": f"E-{n:03d}",
		"highest_role": "member",
		"main_position_id": uuid(POSITION, main_position),
		"main_site_id": uuid(SITE, 1),
		"admin": False,
		"visible": True,
		"seniority_date": "2025-03-01",
		"created_at": CREATED,
		"updated_at": UPDATED,
		"profile": profile,
	} | fields


def leave_type(n: int, name: str, paid: bool) -> Record:
	return {
		"id": uuid(LEAVE_TYPE, n),
		"name": name,
		"paid": paid,
		"created_at": CREATED,
		"updated_at": UPDATED,
	}


def shift(n: int, worker: int | None, date: str, start: str, end: str, **fields: Any) -> Record:
	end_date = date if end > start else "2026-10-06"
	body: Record = {
		"id": uuid(SHIFT, n),
		"date": date,
		"start_date": date,
		"start_at": f"{date}T{start}:00-04:00",
		"end_date": end_date,
		"end_at": f"{end_date}T{end}:00-04:00",
		"status": "published",
		"site_id": uuid(SITE, 1),
		"position_id": uuid(POSITION, 1),
		"paid_break": 0,
		"unpaid_break": "30",
		"open": False,
		"time_off": False,
		"same_date": end_date == date,
		"created_at": CREATED,
		"updated_at": UPDATED,
	}
	if worker is not None:
		body["member_id"] = uuid(MEMBER, worker)
	return body | fields


def clock(n: int, worker: int, action: str, at: str, success: bool = True) -> Record:
	return {
		"id": uuid(CLOCK, n),
		"member_id": uuid(MEMBER, worker),
		"code": f"{worker:04d}",
		"action": action,
		"type": "tablet",
		"success": success,
		"has_questions": False,
		"created_at": at,
		"updated_at": at,
	}


def entry(n: int, worker: int, date: str, start: str, end: str | None, minutes: int, **fields: Any) -> Record:
	body: Record = {
		"id": uuid(ENTRY, n),
		"member_id": uuid(MEMBER, worker),
		"date": date,
		"start_at": f"{date}T{start}:00-04:00",
		"regular_minutes": minutes,
		"overtime_minutes": 0,
		"position_id": uuid(POSITION, 1),
		"site_id": uuid(SITE, 1),
		"time_off": False,
		"approved": True,
		"created_at": CREATED,
		"updated_at": UPDATED,
	}
	if end is not None:
		end_date = date if end > start else "2026-10-06"
		body["end_at"] = f"{end_date}T{end}:00-04:00"
	return body | fields


def request(n: int, worker: int, kind: int, start: str, end: str, **fields: Any) -> Record:
	types = {1: leave_type(1, "Vacation", True), 2: leave_type(2, "Sick leave", False)}
	return {
		"id": uuid(REQUEST, n),
		"member_id": uuid(MEMBER, worker),
		"created_by": uuid(MEMBER, worker),
		"justification": "Family trip" if kind == 1 else "Flu",
		"paid": types[kind]["paid"],
		"all_day": True,
		"multiple_days": start != end,
		"start_date": start,
		"start_at": f"{start}T00:00:00-04:00",
		"end_date": end,
		"end_at": f"{end}T23:59:59-04:00",
		"created_at": CREATED,
		"updated_at": UPDATED,
		"leave_type": types[kind],
	} | fields


def store_v1() -> dict[str, list[Record]]:
	return {
		"sites": [
			site(1, "Downtown", "100 Rue Sainte-Catherine, Montréal"),
			site(2, "Airport", "975 Boul. Roméo-Vachon"),
		],
		"positions": [position(1, "Barista", 1), position(2, "Kitchen", 2), position(3, "Prep", 3, parent=2)],
		"members": [
			member(1, "Avery", "Writer", 1, "1990-04-05"),
			member(2, "Jordan", "Lee", 3),
			member(3, "Sam", "Ortiz", 1, "1988-11-30"),
		],
		"leave_types": [leave_type(1, "Vacation", True), leave_type(2, "Sick leave", False)],
		"shifts": [
			shift(1, 1, "2026-10-05", "08:00", "16:00"),
			shift(2, 2, "2026-10-05", "22:00", "06:00", position_id=uuid(POSITION, 3)),
			shift(3, 1, "2026-10-12", "08:00", "16:00", status="draft"),
			shift(4, None, "2026-10-13", "07:00", "15:00", open=True, open_quantity=1),
			shift(
				5,
				1,
				"2026-10-09",
				"00:00",
				"23:59",
				time_off=True,
				all_day=True,
				leave_request_id=uuid(REQUEST, 1),
				leave_type_id=uuid(LEAVE_TYPE, 1),
			),
		],
		"clock_logs": [
			clock(1, 1, "clocked_in", "2026-10-05T07:58:12-04:00"),
			clock(2, 1, "clocked_out", "2026-10-05T16:02:40-04:00"),
			clock(3, 2, "clocked_in", "2026-10-05T21:40:00-04:00", success=False),
			clock(4, 2, "clocked_in", "2026-10-05T21:58:05-04:00"),
		],
		"time_entries": [
			entry(1, 1, "2026-10-05", "08:00", "12:00", 240),
			entry(2, 1, "2026-10-05", "12:30", "16:00", 210),
			entry(3, 2, "2026-10-05", "22:00", "06:00", 450, overtime_minutes=30, approved=False),
			entry(
				4,
				1,
				"2026-10-09",
				"00:00",
				"23:59",
				0,
				time_off=True,
				time_off_leave_type_id=uuid(LEAVE_TYPE, 1),
				time_off_paid=True,
			),
		],
		"leave_requests": [
			request(1, 1, 1, "2026-10-09", "2026-10-09", approved_at=UPDATED, approved_by=uuid(MEMBER, 3)),
			request(2, 2, 2, "2026-10-20", "2026-10-21"),
			request(3, 3, 1, "2026-10-14", "2026-10-14", all_day=False, multiple_days=False),
		],
	}


def store_v2() -> dict[str, list[Record]]:
	store = copy.deepcopy(store_v1())
	store["sites"][0] |= {"name": "Downtown Café", "updated_at": LATER}
	store["members"][2] |= {
		"deleted_at": "2026-10-15T17:00:00-04:00",
		"deletion_date": "2026-10-15",
		"updated_at": LATER,
	}
	store["shifts"][0] |= {
		"start_at": "2026-10-05T09:00:00-04:00",
		"end_at": "2026-10-05T17:00:00-04:00",
		"updated_at": LATER,
	}
	store["shifts"][2] |= {"deleted_at": LATER, "updated_at": LATER}
	store["clock_logs"].append(clock(5, 2, "clocked_out", "2026-10-06T06:03:30-04:00"))
	store["time_entries"][1] |= {
		"end_at": "2026-10-05T16:15:00-04:00",
		"regular_minutes": 225,
		"updated_at": LATER,
	}
	store["leave_requests"][1] |= {"declined_at": LATER, "declined_by": uuid(MEMBER, 3), "updated_at": LATER}
	return store


def listing(records: list[Record]) -> Record:
	return {
		"data": records,
		"pagination": {
			"page_size": reader.PAGE_SIZE,
			"limit": reader.PAGE_SIZE,
			"current_page": reader.FIRST_PAGE,
			"total_pages": 1,
			"first_page": True,
			"last_page": True,
			"out_of_range": False,
			"total_count": len(records),
		},
	}


def fixture(path: str, query: Record, response: Record, patterns: Record | None = None) -> Record:
	request_: Record = {"path": PREFIX + path, "query": query}
	if patterns:
		request_["query_patterns"] = patterns
	return {"request": request_, "response": response}


def present(records: list[Record]) -> list[Record]:
	return [record for record in records if "deleted_at" not in record]


def fixtures(store: dict[str, list[Record]]) -> dict[str, Record]:
	"""Each file's name and content: every request the reader sends to this organisation."""
	written: dict[str, Record] = {}
	page = {"page": str(reader.FIRST_PAGE)}
	for kind, collection in reader.COLLECTIONS.items():
		name = collection.path.strip("/")
		records = store[name]
		query = {**page, **{key: str(value) for key, value in collection.query}}
		patterns = None
		if collection.updated:
			patterns = {"search[updated_since]": ANY, "search[updated_until]": ANY}
			# An incremental run asks for archived records too; a full pass doesn't get them.
			written[f"{name}-0-with-deleted"] = fixture(
				collection.path, {**query, "search[with_deleted]": "true"}, listing(records), patterns
			)
			records = present(records)
		written[f"{name}-1"] = fixture(collection.path, query, listing(records), patterns)
		if kind is Kind.DESIGNATION:
			for record in records:
				if "parent_id" in record:
					parent = next(p for p in records if p["id"] == record["parent_id"])
					written[f"positions-parent-{parent['name'].lower()}"] = fixture(
						f"/positions/{parent['id']}", {}, {"data": parent}
					)
	first = store["members"][0]
	written["members-one"] = fixture(f"{reader.MEMBERS}/{first['id']}", {}, {"data": first})
	changed = {"search[updated_since]": ANY, "search[updated_until]": ANY}
	written["time_entries-0-changed"] = fixture(
		reader.TIME_ENTRIES, {**page, "search[with_deleted]": "true"}, listing(store["time_entries"]), changed
	)
	for record in store["members"]:
		entries = [e for e in store["time_entries"] if e["member_id"] == record["id"]]
		written[f"time_entries-member-{record['profile']['first_name'].lower()}"] = fixture(
			reader.TIME_ENTRIES, {**page, "search[member_id]": record["id"]}, listing(present(entries))
		)
	return written


def write(state: str, store: dict[str, list[Record]]) -> None:
	folder = FIXTURES / state
	shutil.rmtree(folder, ignore_errors=True)
	folder.mkdir(parents=True)
	for name, content in fixtures(store).items():
		(folder / f"{name}.json").write_text(json.dumps(content, indent="\t", ensure_ascii=False) + "\n")


def main() -> int:
	write("v1", store_v1())
	write("v2", store_v2())
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
