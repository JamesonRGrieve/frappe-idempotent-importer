# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the Pipedrive fixtures (tests/fixtures/pipedrive/<state>/*.json): a small design studio's company as
the API answers, shaped as Pipedrive's v1 and v2 OpenAPI documents define each response (pinned in
tools/specs.py), for exactly the requests the reader sends: users, the field lists with the studio's custom
fields, products and their variations, persons, organizations and each one's persons, pipelines, stages,
deals (archived, and won for the customers), leads (archived too), activity types, activities, notes, call
logs, and each won deal's organization or person by id. The two states differ only by their records (v2: a
user deactivated, a new price, a won deal, a done task, a new note).

``python -m tools.pipedrive_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.sources.pipedrive import reader

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "pipedrive"
ADDED = "2026-08-03T15:00:00Z"
UPDATED = "2026-09-15T15:00:00Z"
V1_ADDED = "2026-08-03 15:00:00"
Record = dict[str, Any]
FINISH = "5f0e" + "a" * 36
CONTACT_TIME = "7c1d" + "b" * 36
TIER = "9e2b" + "c" * 36
SOURCE_DETAIL = "3a4f" + "d" * 36
NEXT_CURSOR = None


def field(code: str, name: str, kind: str, *options: tuple[int, str]) -> Record:
	return {
		"field_name": name,
		"field_code": code,
		"field_type": kind,
		"options": [{"id": number, "label": label} for number, label in options] if options else None,
		"subfields": None,
		"is_custom_field": True,
		"is_optional_response_field": False,
	}


def standard(code: str, name: str, kind: str) -> Record:
	return field(code, name, kind) | {"is_custom_field": False}


def emails(address: str) -> list[Record]:
	return [{"value": address, "primary": True, "label": "work"}]


def address(street_number: str, route: str, city: str, state: str, postcode: str, country: str) -> Record:
	return {
		"value": f"{street_number} {route}, {city}, {state} {postcode}, {country}",
		"country": country,
		"admin_area_level_1": state,
		"admin_area_level_2": None,
		"locality": city,
		"sublocality": None,
		"route": route,
		"street_number": street_number,
		"subpremise": None,
		"postal_code": postcode,
	}


def person(id_: int, first: str, last: str, org: int | None, **extra: Any) -> Record:
	body: Record = {
		"id": id_,
		"name": f"{first} {last}",
		"first_name": first,
		"last_name": last,
		"owner_id": 1,
		"add_time": ADDED,
		"update_time": UPDATED,
		"emails": emails(f"{first.lower()}@river-co.example"),
		"phones": [{"value": f"+1 902 555 0{id_}", "primary": True, "label": "work"}],
		"is_deleted": False,
		"visible_to": 3,
		"label_ids": [],
		"custom_fields": {},
	}
	if org is not None:
		body["org_id"] = org
	return body | extra


def deal(id_: int, title: str, stage: int, status: str, value: float, **extra: Any) -> Record:
	return {
		"id": id_,
		"title": title,
		"owner_id": 1,
		"person_id": None,
		"org_id": None,
		"pipeline_id": 1,
		"stage_id": stage,
		"value": value,
		"currency": "CAD",
		"add_time": "2026-08-20T15:00:00Z",
		"update_time": UPDATED,
		"stage_change_time": None,
		"is_deleted": False,
		"is_archived": False,
		"status": status,
		"probability": None,
		"lost_reason": None,
		"visible_to": 3,
		"close_time": None,
		"won_time": None,
		"lost_time": None,
		"expected_close_date": None,
		"label_ids": [],
		"origin": "ManuallyCreated",
		"origin_id": None,
		"channel": None,
		"channel_id": None,
		"custom_fields": {},
	} | extra


def activity(id_: int, kind: str, subject: str, done: bool, **extra: Any) -> Record:
	return {
		"id": id_,
		"subject": subject,
		"type": kind,
		"owner_id": 1,
		"creator_user_id": 1,
		"is_deleted": False,
		"add_time": ADDED,
		"update_time": UPDATED,
		"deal_id": None,
		"lead_id": None,
		"person_id": None,
		"org_id": None,
		"project_id": None,
		"due_date": "2026-09-10",
		"due_time": None,
		"duration": None,
		"busy": False,
		"done": done,
		"marked_as_done_time": None,
		"participants": [],
		"note": "",
	} | extra


def lead(id_: str, title: str, **extra: Any) -> Record:
	return {
		"id": id_,
		"title": title,
		"owner_id": 1,
		"creator_id": 1,
		"label_ids": [],
		"person_id": None,
		"organization_id": None,
		"source_name": "Manually created",
		"origin": "ManuallyCreated",
		"origin_id": None,
		"channel": None,
		"channel_id": None,
		"is_archived": False,
		"was_seen": True,
		"value": {"amount": 2000, "currency": "CAD"},
		"expected_close_date": "2026-12-15",
		"next_activity_id": None,
		"add_time": "2026-09-01T15:00:00.000Z",
		"update_time": "2026-09-01T15:00:00.000Z",
		"visible_to": "3",
		"cc_email": "harbour+lead@pipedrivemail.com",
	} | extra


def note(id_: int, content: str, **extra: Any) -> Record:
	return {
		"id": id_,
		"active_flag": True,
		"add_time": "2026-09-04 14:30:00",
		"content": content,
		"lead_id": None,
		"deal_id": None,
		"org_id": None,
		"person_id": None,
		"pinned_to_deal_flag": False,
		"pinned_to_organization_flag": False,
		"pinned_to_person_flag": False,
		"update_time": "2026-09-04 14:30:00",
		"user_id": 1,
	} | extra


def store_v1() -> dict[str, Any]:
	return {
		"users": [user(1, "Morgan Sales", True), user(2, "Jamie Front", True)],
		"fields": {
			"Deal": [
				standard("title", "Title", "varchar"),
				field(SOURCE_DETAIL, "Lead source detail", "varchar"),
			],
			"Person": [field(CONTACT_TIME, "Preferred contact time", "varchar")],
			"Organization": [field(TIER, "Account tier", "enum", (21, "Gold"), (22, "Silver"))],
			"Product": [field(FINISH, "Finish", "enum", (11, "Matte"), (12, "Gloss"))],
			"Activity": [],
		},
		"products": [
			product(401, "Design session", "SESSION", [price("150")], []),
			product(
				402,
				"Studio print",
				"PRINT",
				[],
				[variation(501, 402, "A4", "45"), variation(502, 402, "A3", "60")],
				{FINISH: 11},
			),
		],
		"persons": [
			person(
				101, "Riley", "River", 201, job_title="Director", custom_fields={CONTACT_TIME: "Mornings"}
			),
			person(102, "Sam", "Shore", 201),
			person(103, "Quinn", "Quay", None),
		],
		"organizations": [
			organization(
				201,
				"River Co",
				address=address("1", "Harbour Rd", "Halifax", "NS", "B3H 1A1", "Canada"),
				website="https://river.example",
				custom_fields={TIER: 21},
			),
			organization(202, "Delta Works", website="https://delta.example"),
		],
		"pipelines": [
			{
				"id": 1,
				"name": "Sales",
				"order_nr": 1,
				"is_deleted": False,
				"is_deal_probability_enabled": True,
				"add_time": ADDED,
				"update_time": ADDED,
			}
		],
		"stages": [stage(11, "Qualified", 20), stage(12, "Proposal made", 60), stage(13, "Negotiations", 80)],
		"deals": [
			deal(
				301,
				"River rebrand",
				12,
				"open",
				4800,
				org_id=201,
				person_id=101,
				custom_fields={SOURCE_DETAIL: "Expo 2026"},
			),
			deal(302, "Quay audit", 11, "lost", 900, person_id=103, lost_reason="Budget"),
			deal(304, "Prints for River", 13, "won", 135, org_id=201, person_id=102),
			deal(305, "Quinn workshop", 13, "won", 300, person_id=103),
		],
		"archived_deals": [deal(303, "Old pitch", 11, "open", 500, org_id=202, is_archived=True)],
		"leads": [lead("6f1b0a52-0000-4000-8000-000000000001", "Delta identity", organization_id=202)],
		"archived_leads": [
			lead("6f1b0a52-0000-4000-8000-000000000002", "Shore catalogue", person_id=102, is_archived=True)
		],
		"activity_types": [
			activity_type(1, "Call", "call"),
			activity_type(2, "Meeting", "meeting"),
			activity_type(3, "Task", "task"),
			activity_type(4, "Deadline", "deadline"),
			activity_type(5, "Email", "email"),
			activity_type(6, "Lunch", "lunch"),
		],
		"activities": [
			activity(
				601,
				"call",
				"Kick-off call",
				True,
				person_id=101,
				deal_id=301,
				marked_as_done_time="2026-09-05T15:20:00Z",
				note="<p>Walked through the brief.</p>",
			),
			activity(
				602,
				"meeting",
				"Concept review",
				True,
				person_id=101,
				deal_id=301,
				org_id=201,
				due_time="16:00",
				duration="01:00",
				participants=[{"person_id": 101, "primary": True}, {"person_id": 102, "primary": False}],
			),
			activity(603, "task", "Send samples", False, person_id=102, due_date="2026-09-30"),
			activity(
				604,
				"email",
				"Sent the proposal",
				True,
				org_id=202,
				marked_as_done_time="2026-09-12T15:00:00Z",
			),
			activity(605, "call", "Call Quinn back", False, person_id=103),
		],
		"notes": [
			note(701, "<p>Budget approved</p>", deal_id=301),
			note(702, "<div>Expanding in spring</div>", org_id=201),
		],
		"call_logs": [
			{
				"id": "cl-0001",
				"user_id": 1,
				"subject": "Samples follow-up",
				"duration": "125",
				"outcome": "connected",
				"from_phone_number": "+1 902 555 0100",
				"to_phone_number": "+1 902 555 0102",
				"start_time": "2026-09-08T15:00:00.000Z",
				"end_time": "2026-09-08T15:02:05.000Z",
				"person_id": 102,
				"note": "Samples arrive Friday.",
				"has_recording": False,
				"company_id": 7001,
			}
		],
	}


def user(id_: int, name: str, active: bool) -> Record:
	first = name.split(" ", maxsplit=1)[0].lower()
	return {
		"id": id_,
		"name": name,
		"default_currency": "CAD",
		"locale": "en_CA",
		"lang": 1,
		"email": f"{first}@studio.example",
		"phone": None,
		"activated": True,
		"last_login": "2026-10-01 15:00:00",
		"created": V1_ADDED,
		"modified": V1_ADDED,
		"has_created_company": id_ == 1,
		"access": [
			{"app": "sales", "admin": id_ == 1, "permission_set_id": "62cc4d7f-4038-4352-abf3-a8c1c822b631"}
		],
		"active_flag": active,
		"timezone_name": "America/Halifax",
		"timezone_offset": "-03:00",
		"role_id": 1,
		"icon_url": None,
		"is_you": id_ == 1,
		"is_deleted": False,
	}


def price(amount: str) -> Record:
	return {"price": float(amount), "cost": 0, "currency": "CAD", "direct_cost": 0, "notes": ""}


def product(
	id_: int,
	name: str,
	code: str,
	prices: list[Record],
	variations: list[Record],
	custom: Record | None = None,
) -> Record:
	return {
		"id": id_,
		"name": name,
		"code": code,
		"unit": "each",
		"tax": 0,
		"is_deleted": False,
		"is_linkable": True,
		"owner_id": 1,
		"add_time": ADDED,
		"update_time": UPDATED,
		"description": "",
		"category": None,
		"custom_fields": custom or {},
		"billing_frequency": "one-time",
		"billing_frequency_cycles": None,
		"prices": [p | {"product_id": id_} for p in prices],
		"variations": variations,
	}


def variation(id_: int, product_id: int, name: str, amount: str) -> Record:
	return {
		"id": id_,
		"name": name,
		"product_id": product_id,
		"prices": [price(amount) | {"product_variation_id": id_}],
	}


def organization(id_: int, name: str, **extra: Any) -> Record:
	return {
		"id": id_,
		"name": name,
		"owner_id": 1,
		"add_time": ADDED,
		"update_time": UPDATED,
		"is_deleted": False,
		"visible_to": 3,
		"label_ids": [],
		"custom_fields": {},
	} | extra


def stage(id_: int, name: str, probability: int) -> Record:
	return {
		"id": id_,
		"order_nr": id_ - 10,
		"name": name,
		"is_deleted": False,
		"deal_probability": probability,
		"pipeline_id": 1,
		"is_deal_rot_enabled": False,
		"days_to_rotten": None,
		"add_time": ADDED,
		"update_time": ADDED,
	}


def activity_type(id_: int, name: str, key: str) -> Record:
	return {
		"id": id_,
		"name": name,
		"icon_key": key,
		"color": "000000",
		"order_nr": id_,
		"key_string": key,
		"active_flag": True,
		"is_custom_flag": False,
		"add_time": V1_ADDED,
		"update_time": V1_ADDED,
	}


def store_v2() -> dict[str, Any]:
	store = copy.deepcopy(store_v1())
	store["users"][1]["active_flag"] = False
	store["products"][0]["prices"][0]["price"] = 175.0
	store["deals"][0] |= {"status": "won", "won_time": "2026-10-10T15:00:00Z", "stage_id": 13}
	store["activities"][2] |= {"done": True, "marked_as_done_time": "2026-10-01T15:00:00Z"}
	store["notes"].append(note(703, "<p>Signed and returned</p>", deal_id=301))
	return store


def v2(data: list[Record]) -> Record:
	return {"success": True, "data": data, "additional_data": {"next_cursor": NEXT_CURSOR}}


def v1(data: list[Record], nested: bool = False) -> Record:
	pagination = {"start": 0, "limit": reader.PAGE_SIZE, "more_items_in_collection": False}
	return {
		"success": True,
		"data": data,
		"additional_data": {"pagination": pagination} if nested else pagination,
	}


def fixture(path: str, query: Record, response: Record) -> Record:
	return {"request": {"path": path, "query": {k: str(v) for k, v in query.items()}}, "response": response}


def without_variations(product_: Record) -> Record:
	return {key: value for key, value in product_.items() if key != "variations"}


def fixtures(store: dict[str, Any]) -> dict[str, Record]:
	v1_page = {"limit": reader.PAGE_SIZE, "start": 0}
	page = {"limit": reader.PAGE_SIZE}
	written: dict[str, Record] = {
		"users": fixture("/api/v1/users", v1_page, {"success": True, "data": store["users"]}),
		"products": fixture("/api/v2/products", page, v2([without_variations(p) for p in store["products"]])),
		"persons": fixture("/api/v2/persons", page, v2(store["persons"])),
		"organizations": fixture("/api/v2/organizations", page, v2(store["organizations"])),
		"pipelines": fixture("/api/v2/pipelines", page, v2(store["pipelines"])),
		"stages": fixture("/api/v2/stages", page, v2(store["stages"])),
		"deals": fixture("/api/v2/deals", page, v2(store["deals"])),
		"deals-won": fixture(
			"/api/v2/deals",
			{**page, "status": "won"},
			v2([d for d in store["deals"] if d["status"] == "won"]),
		),
		"deals-archived": fixture("/api/v2/deals/archived", page, v2(store["archived_deals"])),
		"leads": fixture("/api/v1/leads", v1_page, v1(store["leads"])),
		"leads-archived": fixture("/api/v1/leads/archived", v1_page, v1(store["archived_leads"])),
		"activity-types": fixture(
			"/api/v1/activityTypes", {}, {"success": True, "data": store["activity_types"]}
		),
		"activities": fixture("/api/v2/activities", page, v2(store["activities"])),
		"notes": fixture("/api/v1/notes", v1_page, v1(store["notes"], nested=True)),
		"call-logs": fixture("/api/v1/callLogs", v1_page, v1(store["call_logs"], nested=True)),
	}
	for type_, path in reader.FIELD_LISTS.items():
		written[f"fields-{type_.lower()}"] = fixture(f"/api/v2{path}", page, v2(store["fields"][type_]))
	for product_ in store["products"]:
		written[f"variations-{product_['id']}"] = fixture(
			f"/api/v2/products/{product_['id']}/variations", page, v2(product_["variations"])
		)
	for organization_ in store["organizations"]:
		members = [p for p in store["persons"] if p.get("org_id") == organization_["id"]]
		written[f"persons-org-{organization_['id']}"] = fixture(
			"/api/v2/persons", {**page, "org_id": organization_["id"]}, v2(members)
		)
		written[f"organization-{organization_['id']}"] = fixture(
			f"/api/v2/organizations/{organization_['id']}", {}, {"success": True, "data": organization_}
		)
	for person_ in store["persons"]:
		written[f"person-{person_['id']}"] = fixture(
			f"/api/v2/persons/{person_['id']}", {}, {"success": True, "data": person_}
		)
	return written


def write(state: str, store: dict[str, Any]) -> None:
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
