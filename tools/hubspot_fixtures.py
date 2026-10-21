# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the HubSpot fixtures (tests/fixtures/hubspot/<state>/*.json): a small design studio's account as
the CRM API v3 answers, shaped as HubSpot's v3 OpenAPI documents define each response (pinned in
tools/specs.py), for exactly the requests the reader sends: the owners, the properties of each object type
(with the studio's own), the deal pipeline, every object list with its associations, the customer
searches, the incremental searches (nothing changed since), the batch reads of notes, line items, discounts,
fees, taxes and a company's people, and each contact and company by id. The two states differ only by their
records (v2: an owner archived, a new price, a won deal, a completed task, an opened invoice, a new email).

``python -m tools.hubspot_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.sources.hubspot import reader

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "hubspot"
ADDED = "2026-08-03T15:00:00Z"
UPDATED = "2026-09-15T15:00:00Z"
LATER = "2026-10-16T15:00:00Z"
DONE_AT = "2026-10-21T15:00:00Z"
Record = dict[str, Any]
OBJECT_TYPES = (*reader.BY_PATH.values(), reader.PAYMENT)


def obj(id_: str, properties: Record, **associations: list[str]) -> Record:
	"""One CRM object: its properties (strings) and associated ids by object type."""
	return {"id": id_, "properties": properties, "links": associations}


def store_v1() -> dict[str, Any]:
	return {
		"owners": [
			owner("1", "Morgan", "Sales"),
			owner("2", "Jamie", "Front"),
		],
		"archived_owners": [],
		"contacts": [
			obj(
				"101",
				{
					"firstname": "Riley",
					"lastname": "River",
					"email": "riley@river.example",
					"phone": "+1 902 555 0101",
					"company": "River Co",
					"jobtitle": "Director",
					"address": "1 Harbour Rd",
					"city": "Halifax",
					"state": "NS",
					"zip": "B3H 1A1",
					"country": "Canada",
					"lifecyclestage": "customer",
					"hs_lead_status": "CONNECTED",
					"preferred_contact_time": "mornings",
				},
				notes=["901"],
				companies=["201"],
			),
			obj(
				"102",
				{
					"firstname": "Sam",
					"lastname": "Shore",
					"email": "sam@river.example",
					"mobilephone": "+1 902 555 0102",
					"company": "River Co",
					"lifecyclestage": "lead",
					"hs_lead_status": "OPEN",
				},
				companies=["201"],
			),
			obj(
				"103",
				{
					"firstname": "Quinn",
					"lastname": "Quay",
					"email": "quinn@quay.example",
					"lifecyclestage": "lead",
					"hs_lead_status": "UNQUALIFIED",
				},
			),
		],
		"companies": [
			obj(
				"201",
				{
					"name": "River Co",
					"domain": "river.example",
					"phone": "+1 902 555 0100",
					"address": "1 Harbour Rd",
					"city": "Halifax",
					"state": "NS",
					"zip": "B3H 1A1",
					"country": "Canada",
					"description": "Harbour tours",
					"lifecyclestage": "customer",
					"account_tier": "gold",
				},
				contacts=["101", "102"],
				notes=["902"],
			),
			obj(
				"202",
				{"name": "Delta Works", "domain": "delta.example", "lifecyclestage": "lead"},
			),
		],
		"deals": [
			obj(
				"301",
				{
					"dealname": "River rebrand",
					"amount": "4800",
					"deal_currency_code": "CAD",
					"closedate": "2026-11-30T00:00:00Z",
					"createdate": "2026-08-20T15:00:00Z",
					"pipeline": "default",
					"dealstage": "qualifiedtobuy",
					"hs_deal_stage_probability": "0.4",
					"description": "A new identity for the tours",
					"lead_source_detail": "Expo 2026",
				},
				contacts=["101"],
				companies=["201"],
				notes=["903"],
			),
			obj(
				"302",
				{
					"dealname": "Quay audit",
					"amount": "900",
					"deal_currency_code": "CAD",
					"createdate": "2026-08-22T15:00:00Z",
					"pipeline": "default",
					"dealstage": "closedlost",
					"hs_deal_stage_probability": "0",
				},
				contacts=["103"],
			),
		],
		"notes": [
			obj(
				"901", {"hs_timestamp": "2026-09-02T14:30:00Z", "hs_note_body": "Wants the spring catalogue"}
			),
			obj("902", {"hs_timestamp": "2026-09-03T14:30:00Z", "hs_note_body": "Expanding in spring"}),
			obj("903", {"hs_timestamp": "2026-09-04T14:30:00Z", "hs_note_body": "Budget approved"}),
		],
		"calls": [
			obj(
				"501",
				{
					"hs_timestamp": "2026-09-05T15:00:00Z",
					"hs_call_title": "Kick-off call",
					"hs_call_body": "Walked through the brief.",
					"hs_call_direction": "OUTBOUND",
					"hs_call_duration": "754000",
					"hs_call_status": "COMPLETED",
					"hs_call_to_number": "+1 902 555 0101",
				},
				contacts=["101"],
				deals=["301"],
			)
		],
		"emails": [
			obj(
				"502",
				{
					"hs_timestamp": "2026-09-06T15:00:00Z",
					"hs_email_subject": "Samples",
					"hs_email_text": "Could you send the paper samples?",
					"hs_email_direction": "INCOMING_EMAIL",
					"hs_email_from_email": "sam@river.example",
				},
				contacts=["102"],
			)
		],
		"meetings": [
			obj(
				"601",
				{
					"hs_timestamp": "2026-09-10T16:00:00Z",
					"hs_meeting_title": "Concept review",
					"hs_meeting_body": "Three directions",
					"hs_meeting_location": "Studio",
					"hs_meeting_start_time": "2026-09-10T16:00:00Z",
					"hs_meeting_end_time": "2026-09-10T17:00:00Z",
					"hs_meeting_outcome": "COMPLETED",
				},
				contacts=["101"],
				deals=["301"],
			)
		],
		"tasks": [
			obj(
				"701",
				{
					"hs_timestamp": "2026-09-30T15:00:00Z",
					"hs_task_subject": "Send samples",
					"hs_task_body": "Paper and foil",
					"hs_task_status": "NOT_STARTED",
					"hs_task_priority": "HIGH",
					"hs_task_type": "TODO",
				},
				contacts=["102"],
			)
		],
		"products": [
			obj(
				"401",
				{
					"name": "Design session",
					"description": "One hour",
					"price": "150",
					"hs_product_type": "service",
				},
			),
			obj(
				"402",
				{
					"name": "Studio print",
					"price": "45",
					"hs_sku": "PRINT-1",
					"hs_product_type": "inventory",
					"finish": "matte",
				},
			),
		],
		"line_items": [
			obj(
				"811",
				{
					"name": "Design session",
					"quantity": "2",
					"price": "150",
					"amount": "300",
					"hs_product_id": "401",
				},
			),
			obj(
				"812",
				{
					"name": "Studio print",
					"quantity": "1",
					"price": "45",
					"amount": "45",
					"hs_product_id": "402",
				},
			),
			obj(
				"813",
				{
					"name": "Design session",
					"quantity": "2",
					"price": "150",
					"amount": "300",
					"hs_product_id": "401",
				},
			),
			obj(
				"814",
				{
					"name": "Studio print",
					"quantity": "3",
					"price": "45",
					"amount": "135",
					"hs_product_id": "402",
				},
			),
		],
		"discounts": [obj("821", adjustment("Returning client", "PERCENT", "10"))],
		"fees": [obj("841", adjustment("Shipping", "FIXED", "10"))],
		"taxes": [
			obj("831", adjustment("HST", "PERCENT", "15")),
			obj("832", adjustment("HST", "FIXED", "45")),
		],
		"quotes": [
			obj(
				"801",
				{
					"hs_title": "Delta identity",
					"hs_quote_number": "Q-2026-801",
					"hs_expiration_date": "2026-12-31T00:00:00Z",
					"hs_status": "APPROVED",
					"hs_quote_amount": "310.50",
					"hs_currency": "CAD",
					"hs_createdate": "2026-09-12T15:00:00Z",
				},
				line_items=["811"],
				discounts=["821"],
				taxes=["831"],
				companies=["202"],
			)
		],
		"orders": [
			obj(
				"851",
				{
					"hs_order_name": "Print run",
					"hs_currency_code": "CAD",
					"hs_pipeline_stage": "shipped",
					"hs_createdate": "2026-09-14T15:00:00Z",
				},
				line_items=["812"],
				fees=["841"],
				companies=["201"],
			)
		],
		"invoices": [
			obj(
				"861",
				{
					"hs_number": "INV-1001",
					"hs_invoice_date": "2026-09-15T00:00:00Z",
					"hs_due_date": "2026-10-15T00:00:00Z",
					"hs_currency": "CAD",
					"hs_invoice_status": "paid",
					"hs_purchase_order_number": "PO-77",
				},
				line_items=["813"],
				taxes=["832"],
				companies=["201"],
				contacts=["101"],
			),
			obj(
				"862",
				{
					"hs_number": "INV-1002",
					"hs_invoice_date": "2026-09-20T00:00:00Z",
					"hs_currency": "CAD",
					"hs_invoice_status": "draft",
				},
				line_items=["814"],
				contacts=["102"],
			),
			obj(
				"863",
				{
					"hs_number": "INV-1003",
					"hs_invoice_date": "2026-09-21T00:00:00Z",
					"hs_currency": "CAD",
					"hs_invoice_status": "voided",
				},
				companies=["201"],
			),
		],
		"commerce_payments": [
			obj(
				"871",
				{
					"hs_initial_amount": "345",
					"hs_currency_code": "CAD",
					"hs_latest_status": "succeeded",
					"hs_initiated_date": "2026-09-16T15:00:00Z",
					"hs_payment_method_type": "card",
					"hs_reference_number": "ch_871",
				},
				invoices=["861"],
				companies=["201"],
			),
			obj(
				"872",
				{
					"hs_initial_amount": "100",
					"hs_currency_code": "CAD",
					"hs_latest_status": "partially_refunded",
					"hs_initiated_date": "2026-09-17T15:00:00Z",
					"hs_refunds_amount": "20",
					"hs_payment_method_type": "ach",
				},
				contacts=["102"],
			),
			obj(
				"873",
				{
					"hs_initial_amount": "50",
					"hs_currency_code": "CAD",
					"hs_latest_status": "failed",
					"hs_initiated_date": "2026-09-18T15:00:00Z",
				},
				contacts=["102"],
			),
		],
		"properties": {
			"contacts": [
				custom_property(
					"preferred_contact_time",
					"Preferred contact time",
					("mornings", "Mornings"),
					("evenings", "Evenings"),
				)
			],
			"companies": [
				custom_property("account_tier", "Account tier", ("gold", "Gold"), ("silver", "Silver"))
			],
			"deals": [custom_property("lead_source_detail", "Lead source detail")],
			"products": [custom_property("finish", "Finish", ("matte", "Matte"), ("gloss", "Gloss"))],
		},
		"pipelines": [
			{
				"id": "default",
				"label": "Sales Pipeline",
				"displayOrder": 0,
				"archived": False,
				"createdAt": ADDED,
				"updatedAt": ADDED,
				"stages": [
					stage("appointmentscheduled", "Appointment scheduled", 0, "0.2", "false"),
					stage("qualifiedtobuy", "Qualified to buy", 1, "0.4", "false"),
					stage("closedwon", "Closed won", 2, "1.0", "true"),
					stage("closedlost", "Closed lost", 3, "0.0", "true"),
				],
			}
		],
	}


def owner(id_: str, first: str, last: str) -> Record:
	return {
		"id": id_,
		"email": f"{first.lower()}@studio.example",
		"firstName": first,
		"lastName": last,
		"type": "PERSON",
		"userId": int(id_) + 9000,
		"archived": False,
		"createdAt": ADDED,
		"updatedAt": UPDATED,
	}


def adjustment(label: str, kind: str, value: str) -> Record:
	return {"hs_label": label, "hs_type": kind, "hs_value": value, "hs_sort_order": "0"}


def custom_property(name: str, label: str, *options: tuple[str, str]) -> Record:
	return {
		"name": name,
		"label": label,
		"type": "enumeration" if options else "string",
		"fieldType": "select" if options else "text",
		"groupName": "studio",
		"description": "",
		"hubspotDefined": False,
		"options": [
			{"label": text, "value": value, "hidden": False, "displayOrder": index}
			for index, (value, text) in enumerate(options)
		],
	}


def stage(id_: str, label: str, order: int, probability: str, closed: str) -> Record:
	return {
		"id": id_,
		"label": label,
		"displayOrder": order,
		"metadata": {"probability": probability, "isClosed": closed},
		"archived": False,
		"createdAt": ADDED,
		"updatedAt": ADDED,
	}


def store_v2() -> dict[str, Any]:
	store = copy.deepcopy(store_v1())
	store["archived_owners"] = [store["owners"].pop(1) | {"archived": True, "updatedAt": LATER}]
	find(store, "products", "401")["properties"]["price"] = "175"
	find(store, "deals", "301")["properties"] |= {"dealstage": "closedwon", "hs_deal_stage_probability": "1"}
	find(store, "tasks", "701")["properties"]["hs_task_status"] = "COMPLETED"
	find(store, "invoices", "862")["properties"]["hs_invoice_status"] = "open"
	store["emails"].append(
		obj(
			"503",
			{
				"hs_timestamp": "2026-10-02T09:00:00Z",
				"hs_email_subject": "Signed",
				"hs_email_text": "Signed and returned.",
				"hs_email_direction": "EMAIL",
				"hs_email_to_email": "riley@river.example",
			},
			deals=["301"],
		)
	)
	return store


def find(store: dict[str, Any], kind: str, id_: str) -> Record:
	return next(record for record in store[kind] if record["id"] == id_)


def served(record: Record, names: tuple[str, ...]) -> Record:
	"""An object as the API answers it, with the associations asked for that it has."""
	body: Record = {
		"id": record["id"],
		"properties": {"hs_object_id": record["id"], **record["properties"]},
		"createdAt": ADDED,
		"updatedAt": UPDATED,
		"archived": False,
	}
	links = {
		name: {"results": [{"id": other, "type": f"{name}_association"} for other in record["links"][name]]}
		for name in names
		if record["links"].get(name)
	}
	return body | ({"associations": links} if links else {})


def plain(record: Record) -> Record:
	"""An object as search and batch reads answer it (no associations)."""
	return served(record, ())


def fixture(method: str, path: str, query: Record, response: Record, body: Record | None = None) -> Record:
	request: Record = {
		"method": method,
		"path": path,
		"query": {key: str(value) for key, value in query.items()},
	}
	if body is not None:
		request["body"] = body
	return {"request": request, "response": response}


def listed(results: list[Record]) -> Record:
	return {"results": results}


def batch(results: list[Record]) -> Record:
	return {
		"status": "COMPLETE",
		"results": results,
		"startedAt": DONE_AT,
		"completedAt": DONE_AT,
	}


def searched(results: list[Record]) -> Record:
	return {"total": len(results), "results": results}


def fixtures(store: dict[str, Any]) -> dict[str, Record]:
	page = {"limit": reader.PAGE_SIZE}
	written: dict[str, Record] = {
		"owners": fixture("GET", "/crm/v3/owners", {**page, "archived": "false"}, listed(store["owners"])),
		"owners-archived": fixture(
			"GET", "/crm/v3/owners", {**page, "archived": "true"}, listed(store["archived_owners"])
		),
		"pipelines-deals": fixture("GET", "/crm/v3/pipelines/deals", {}, listed(store["pipelines"])),
	}
	for object_type in OBJECT_TYPES:
		records = store[object_type.path if object_type.path != "0-3" else "deals"]
		name = object_type.name
		written[f"properties-{name}"] = fixture(
			"GET",
			f"/crm/v3/properties/{name}",
			{"archived": "false"},
			listed(store["properties"].get(name, [])),
		)
		query: Record = {**page, "archived": "false"}
		if object_type.associations:
			query["associations"] = ",".join(object_type.associations)
		written[f"list-{name}"] = fixture(
			"GET",
			f"/crm/v3/objects/{object_type.path}",
			query,
			listed([served(record, object_type.associations) for record in records]),
		)
		written[f"changed-{name}"] = fixture(
			"POST",
			f"/crm/v3/objects/{object_type.path}/search",
			{},
			searched([]),
			{"filterGroups": [{"filters": [{"propertyName": object_type.modified, "operator": "GTE"}]}]},
		)
		for record in records:
			written[f"{name}-{record['id']}"] = fixture(
				"GET",
				f"/crm/v3/objects/{object_type.path}/{record['id']}",
				{"associations": ",".join(object_type.associations)} if object_type.associations else {},
				served(record, object_type.associations),
			)
		for part in object_type.parts:
			batches = [[other for other in record["links"].get(part, [])] for record in records]
			batches.append([other for record in records for other in record["links"].get(part, [])])
			for wanted in batches:
				if wanted:
					written[f"batch-{part}-{'-'.join(dict.fromkeys(wanted))}"] = batch_read(
						store, part, wanted
					)
	for company in store["companies"]:
		people = company["links"].get("contacts", [])
		if people:
			written[f"batch-contacts-{'-'.join(people)}"] = batch_read(store, "contacts", people)
	for object_type in (reader.CONTACT, reader.COMPANY):
		customers = [
			r for r in store[object_type.path] if r["properties"].get("lifecyclestage") == "customer"
		]
		written[f"customers-{object_type.name}"] = fixture(
			"POST",
			f"/crm/v3/objects/{object_type.path}/search",
			{},
			searched([plain(record) for record in customers]),
			{
				"filterGroups": [
					{"filters": [{"propertyName": "lifecyclestage", "operator": "EQ", "value": "customer"}]}
				]
			},
		)
	for document in reader.BILLED:
		written[f"billed-{document.name}"] = fixture(
			"GET",
			f"/crm/v3/objects/{document.path}",
			{**page, "archived": "false", "associations": "contacts,companies"},
			listed([served(record, reader.PARTIES) for record in store[document.path]]),
		)
	return written


def batch_read(store: dict[str, Any], part: str, wanted: list[str]) -> Record:
	ids = list(dict.fromkeys(wanted))
	records = [find(store, part, id_) for id_ in ids]
	return fixture(
		"POST",
		f"/crm/v3/objects/{part}/batch/read",
		{},
		batch([plain(record) for record in records]),
		{"inputs": [{"id": id_} for id_ in ids]},
	)


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
