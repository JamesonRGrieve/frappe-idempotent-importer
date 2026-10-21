# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the Zoho CRM fixtures (tests/fixtures/zoho_crm/<state>/*.json): a small design studio's organisation
as the CRM API v8 answers, shaped as Zoho's v8 OpenAPI documents define each response (pinned in
tools/specs.py), for exactly the requests the reader sends: the modules, each module's fields (the studio's
custom ones among them), the users, the organisation, each module's records, an account's contacts, and each
record read by id. Field definitions carry every property Zoho's document requires, empty where the studio
has no value for it (``example``). The two states differ only by their records (v2: a user deactivated, a
new price, a won deal, a completed task, an approved invoice, a new note).

``python -m tools.zoho_crm_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.sources.zoho_crm import reader
from tools.specs import for_source

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "zoho_crm"
CREATED = "2026-08-03T09:00:00-06:00"
Record = dict[str, Any]
REF = "#/components/schemas/"
RIVER, DELTA = "5725767000000417001", "5725767000000417002"
RILEY, SAM = "5725767000000418001", "5725767000000418002"
REBRAND = "5725767000000419001"
SESSION, PRINT = "5725767000000420001", "5725767000000420002"
MODULES = (
	reader.PRODUCTS,
	reader.VENDORS,
	reader.LEADS,
	reader.ACCOUNTS,
	reader.CONTACT,
	reader.DEALS,
	reader.CALLS,
	reader.NOTES,
	reader.EVENTS,
	reader.TASKS,
	reader.QUOTES,
	reader.SALES_ORDERS,
	reader.INVOICES,
	reader.PURCHASE_ORDERS,
)
# The studio's custom fields, by module: api name, label, data type, picklist (actual, display).
CUSTOM: dict[str, list[tuple[str, str, str, list[tuple[str, str]]]]] = {
	"Accounts": [("Account_Tier", "Account Tier", "picklist", [("Gold", "Gold"), ("Silver", "Silver")])],
	"Leads": [("Preferred_Contact_Time", "Preferred Contact Time", "text", [])],
	"Deals": [("Lead_Source_Detail", "Lead Source Detail", "text", [])],
}
PICKLISTS: dict[str, dict[str, list[tuple[str, str]]]] = {
	"Deals": {
		"Stage": [
			("Qualification", "Qualification"),
			("Proposal/Price Quote", "Proposal/Price Quote"),
			("Closed Won", "Closed Won"),
			("Closed Lost", "Closed Lost"),
		],
		"Pipeline": [("Standard", "Standard")],
	},
	"Leads": {"Lead_Status": [("Contacted", "Contacted"), ("Junk Lead", "Junk Lead")]},
}


SCHEMAS: dict[str, Any] = {}


def schemas() -> dict[str, Any]:
	"""Every schema of Zoho's documents, to resolve their references."""
	if not SCHEMAS:
		for document in for_source("zoho_crm"):
			SCHEMAS.update(document.load().schemas)
	return SCHEMAS


def stable_id(*parts: str) -> str:
	"""A Zoho-like 19-digit id, the same on every run for the same parts."""
	digest = hashlib.sha256("/".join(parts).encode()).hexdigest()
	return str(5725767000000000000 + int(digest[:12], 16) % 10**12)


def resolved(schema: Any) -> Any:
	"""A schema with its references followed and its allOf members merged."""
	while isinstance(schema, dict) and "$ref" in schema:
		schema = schemas()[schema["$ref"].removeprefix(REF)]
	if isinstance(schema, dict) and "allOf" in schema:
		merged = {key: value for key, value in schema.items() if key != "allOf"}
		properties = dict(merged.get("properties") or {})
		required = list(merged.get("required") or [])
		for part in schema["allOf"]:
			member = resolved(part)
			properties |= member.get("properties") or {}
			required += [name for name in member.get("required") or [] if name not in required]
			merged.setdefault("type", member.get("type"))
		return {**merged, "properties": properties, "required": required}
	return schema


def example(schema: Any) -> Any:
	"""The emptiest value a schema allows: null where it may be, else an empty one of its type, an object
	with its required properties only."""
	schema = resolved(schema)
	for key in ("oneOf", "anyOf"):
		if key in schema:
			members = [resolved(member) for member in schema[key]]
			if any(member.get("type") == "null" for member in members):
				return None
			return example(members[0])
	kind = schema.get("type")
	if isinstance(kind, list):
		return None if "null" in kind else example({**schema, "type": kind[0]})
	if schema.get("nullable"):
		return None
	if "enum" in schema:
		return schema["enum"][0]
	match kind:
		case "string":
			return ""
		case "boolean":
			return False
		case "integer" | "number":
			return 0
		case "array":
			return []
	properties = schema.get("properties") or {}
	return {name: example(properties[name]) for name in schema.get("required") or [] if name in properties}


def field_definition(
	module: str,
	item_schema: Any,
	name: str,
	label: str,
	data_type: str,
	custom: bool,
	options: list[tuple[str, str]],
) -> Record:
	body: Record = dict(example(item_schema))
	choice_schema = resolved(resolved(item_schema)["properties"]["pick_list_values"])["oneOf"][0]["items"]
	choices = []
	for index, (actual, display) in enumerate(options, start=1):
		choice = example(choice_schema)
		choice.update(
			actual_value=actual,
			display_value=display,
			reference_value=display,
			sequence_number=index,
			type="used",
			id=stable_id(module, name, actual),
		)
		choices.append(choice)
	body.update(
		api_name=name,
		field_label=label,
		display_label=label,
		data_type=data_type,
		custom_field=custom,
		id=stable_id(module, name),
		pick_list_values=choices,
		type="used",
		visible=True,
	)
	return body


def lookup(name: str, id_: str) -> Record:
	return {"name": name, "id": id_}


def item(
	id_: str, product: str, name: str, qty: int, price: str, discount: str = "0", tax: str = "0"
) -> Record:
	amount = float(price) * qty
	return {
		"id": id_,
		"Product_Name": lookup(name, product),
		"Quantity": qty,
		"List_Price": float(price),
		"Discount": float(discount),
		"Tax": float(tax),
		"Total": amount,
		"Net_Total": amount - float(discount) + float(tax),
	}


def store_v1() -> dict[str, Any]:
	return {
		"users": [
			user("5725767000000401001", "Morgan", "Sales", "active"),
			user("5725767000000401002", "Jamie", "Front", "active", "5725767000000401001"),
		],
		"Products": [
			{
				"id": SESSION,
				"Product_Name": "Design session",
				"Product_Code": "SESSION",
				"Unit_Price": 150,
				"Product_Active": True,
			},
			{
				"id": PRINT,
				"Product_Name": "Studio print",
				"Product_Code": "PRINT",
				"Unit_Price": 45,
				"Product_Active": True,
				"Qty_in_Stock": 12,
			},
		],
		"Vendors": [
			{
				"id": "5725767000000421001",
				"Vendor_Name": "Paper Mill",
				"Email": "orders@mill.example",
				"Street": "9 Mill Rd",
				"City": "Truro",
				"State": "NS",
				"Zip_Code": "B2N 1A1",
				"Country": "Canada",
			}
		],
		"Leads": [
			{
				"id": "5725767000000422001",
				"First_Name": "Quinn",
				"Last_Name": "Quay",
				"Company": "Quay Ltd",
				"Email": "quinn@quay-zc.example",
				"Lead_Status": "Contacted",
				"Preferred_Contact_Time": "Mornings",
			},
			{
				"id": "5725767000000422002",
				"First_Name": "Casey",
				"Last_Name": "Cove",
				"Company": "Cove Cafe",
				"Lead_Status": "Junk Lead",
			},
		],
		"Accounts": [
			{
				"id": RIVER,
				"Account_Name": "River Co",
				"Account_Type": "Customer",
				"Phone": "+1 902 555 0100",
				"Website": "https://river.example",
				"Billing_Street": "1 Harbour Rd",
				"Billing_City": "Halifax",
				"Billing_State": "NS",
				"Billing_Code": "B3H 1A1",
				"Billing_Country": "Canada",
				"Account_Tier": "Gold",
			},
			{
				"id": DELTA,
				"Account_Name": "Delta Works",
				"Account_Type": "Prospect",
				"Website": "https://delta.example",
			},
		],
		"Contacts": [
			{
				"id": RILEY,
				"First_Name": "Riley",
				"Last_Name": "River",
				"Email": "riley@river.example",
				"Title": "Director",
				"Account_Name": lookup("River Co", RIVER),
			},
			{
				"id": SAM,
				"First_Name": "Sam",
				"Last_Name": "Shore",
				"Email": "sam@river.example",
				"Account_Name": lookup("River Co", RIVER),
			},
			{
				"id": "5725767000000418003",
				"First_Name": "Dana",
				"Last_Name": "Delta",
				"Email": "dana@delta.example",
				"Account_Name": lookup("Delta Works", DELTA),
			},
		],
		"Deals": [
			{
				"id": REBRAND,
				"Deal_Name": "River rebrand",
				"Account_Name": lookup("River Co", RIVER),
				"Contact_Name": lookup("Riley River", RILEY),
				"Amount": 4800,
				"Stage": "Proposal/Price Quote",
				"Probability": 75,
				"Closing_Date": "2026-11-30",
				"Pipeline": "Standard",
				"Created_Time": CREATED,
				"Lead_Source_Detail": "Expo 2026",
			},
			{
				"id": "5725767000000419002",
				"Deal_Name": "Delta audit",
				"Account_Name": lookup("Delta Works", DELTA),
				"Amount": 900,
				"Stage": "Closed Lost",
				"Probability": 0,
				"Pipeline": "Standard",
				"Created_Time": CREATED,
			},
		],
		"Calls": [
			{
				"id": "5725767000000423001",
				"Subject": "Kick-off call",
				"Call_Type": "Outbound",
				"Call_Start_Time": "2026-09-05T09:00:00-06:00",
				"Call_Duration_in_seconds": 754,
				"Call_Result": "Interested",
				"What_Id": lookup("River rebrand", REBRAND),
				"$se_module": "Deals",
				"Description": "Walked through the brief.",
				"Created_Time": CREATED,
			},
			{
				"id": "5725767000000423002",
				"Subject": "Samples",
				"Call_Type": "Inbound",
				"Call_Start_Time": "2026-09-06T09:00:00-06:00",
				"Who_Id": lookup("Sam Shore", SAM),
				"Created_Time": CREATED,
			},
		],
		"Notes": [
			{
				"id": "5725767000000424001",
				"Note_Title": "Budget",
				"Note_Content": "<div>Budget approved</div>",
				"Parent_Id": {
					"module": {"api_name": "Deals", "id": "5725767000000002175"},
					"name": "River rebrand",
					"id": REBRAND,
				},
				"Created_Time": "2026-09-04T08:30:00-06:00",
			},
		],
		"Events": [
			{
				"id": "5725767000000425001",
				"Event_Title": "Concept review",
				"Start_DateTime": "2026-09-10T10:00:00-06:00",
				"End_DateTime": "2026-09-10T11:00:00-06:00",
				"All_day": False,
				"Venue": "Studio",
				"What_Id": lookup("River rebrand", REBRAND),
				"$se_module": "Deals",
				"Participants": [
					{"type": "lead", "participant": "5725767000000422001", "name": "Quinn Quay"}
				],
				"Created_Time": CREATED,
			},
		],
		"Tasks": [
			{
				"id": "5725767000000426001",
				"Subject": "Send samples",
				"Due_Date": "2026-09-30",
				"Status": "Not Started",
				"Priority": "High",
				"What_Id": lookup("Delta Works", DELTA),
				"$se_module": "Accounts",
				"Created_Time": CREATED,
			},
		],
		"Quotes": [
			{
				"id": "5725767000000427001",
				"Subject": "Delta identity",
				"Quote_Stage": "Delivered",
				"Account_Name": lookup("Delta Works", DELTA),
				"Created_Time": CREATED,
				"Quoted_Items": [item("5725767000000427101", SESSION, "Design session", 2, "150", "30")],
				"Sub_Total": 270,
				"Discount": 0,
				"Tax": 40.5,
				"Adjustment": 0,
				"Grand_Total": 310.5,
			},
		],
		"Sales_Orders": [
			{
				"id": "5725767000000428001",
				"Subject": "Print run",
				"SO_Number": "SO-100",
				"Status": "Approved",
				"Account_Name": lookup("River Co", RIVER),
				"Created_Time": CREATED,
				"Ordered_Items": [item("5725767000000428101", PRINT, "Studio print", 3, "45")],
				"Sub_Total": 135,
				"Discount": 0,
				"Tax": 0,
				"Adjustment": 10,
				"Grand_Total": 145,
			},
		],
		"Invoices": [
			{
				"id": "5725767000000429001",
				"Subject": "Identity work",
				"Invoice_Number": "INV-1001",
				"Status": "Approved",
				"Account_Name": lookup("River Co", RIVER),
				"Invoice_Date": "2026-09-15",
				"Due_Date": "2026-10-15",
				"Created_Time": CREATED,
				"Invoiced_Items": [
					item("5725767000000429101", SESSION, "Design session", 2, "150", tax="45")
				],
				"Sub_Total": 345,
				"Discount": 0,
				"Tax": 0,
				"Adjustment": 0,
				"Grand_Total": 345,
			},
			{
				"id": "5725767000000429002",
				"Subject": "Prints",
				"Invoice_Number": "INV-1002",
				"Status": "Created",
				"Account_Name": lookup("River Co", RIVER),
				"Invoice_Date": "2026-09-20",
				"Created_Time": CREATED,
				"Invoiced_Items": [item("5725767000000429102", PRINT, "Studio print", 1, "45")],
				"Sub_Total": 45,
				"Discount": 5,
				"Tax": 0,
				"Adjustment": 0,
				"Grand_Total": 40,
			},
		],
		"Purchase_Orders": [
			{
				"id": "5725767000000430001",
				"Subject": "Paper",
				"PO_Number": "PO-77",
				"Status": "Approved",
				"Vendor_Name": lookup("Paper Mill", "5725767000000421001"),
				"PO_Date": "2026-09-01",
				"Created_Time": CREATED,
				"Purchased_Items": [item("5725767000000430101", PRINT, "Studio print", 10, "20")],
				"Sub_Total": 200,
				"Discount": 0,
				"Tax": 30,
				"Adjustment": 0,
				"Grand_Total": 230,
			},
		],
	}


def user(id_: str, first: str, last: str, status: str, manager: str | None = None) -> Record:
	return {
		"id": id_,
		"first_name": first,
		"last_name": last,
		"full_name": f"{first} {last}",
		"email": f"{first.lower()}@studio.example",
		"status": status,
		"Reporting_To": lookup("Morgan Sales", manager) if manager else None,
	}


def store_v2() -> dict[str, Any]:
	store = copy.deepcopy(store_v1())
	store["users"][1]["status"] = "disabled"
	store["Products"][0]["Unit_Price"] = 175
	store["Deals"][0] |= {"Stage": "Closed Won", "Probability": 100}
	store["Tasks"][0]["Status"] = "Completed"
	store["Invoices"][1]["Status"] = "Approved"
	store["Notes"].append(
		{
			"id": "5725767000000424002",
			"Note_Title": "Signed",
			"Note_Content": "Signed and returned",
			"Parent_Id": {
				"module": {"api_name": "Accounts", "id": "5725767000000002177"},
				"name": "Delta Works",
				"id": DELTA,
			},
			"Created_Time": "2026-10-02T08:30:00-06:00",
		}
	)
	return store


def fixture(path: str, query: Record, response: Record) -> Record:
	query_text = {key: str(value) for key, value in query.items()}
	return {"request": {"path": f"{reader.API_PATH}{path}", "query": query_text}, "response": response}


def records(listed: list[Record], sorted_: bool = True) -> Record:
	"""A page of records; a related list's page says nothing of its sort."""
	info: Record = {
		"per_page": reader.PER_PAGE,
		"count": len(listed),
		"page": 1,
		"more_records": False,
		"next_page_token": None,
		"previous_page_token": None,
		"page_token_expiry": None,
	}
	return {"data": listed, "info": info | ({"sort_by": "id", "sort_order": "desc"} if sorted_ else {})}


def fixtures(store: dict[str, Any]) -> dict[str, Record]:
	field_items = resolved(resolved(fields_schema())["properties"]["fields"])["items"]
	module_item = resolved(resolved(modules_schema())["properties"]["modules"])["items"]
	written: dict[str, Record] = {}
	listed_modules = []
	for module in MODULES:
		entry = example(module_item)
		entry.update(api_name=module.name, api_supported=True, id=stable_id(module.name))
		listed_modules.append(entry)
		picklists = PICKLISTS.get(module.name, {})
		definitions = [
			field_definition(
				module.name,
				field_items,
				name,
				name.replace("_", " "),
				"picklist" if name in picklists else "text",
				False,
				picklists.get(name, []),
			)
			for name in ("id", *module.fields)
		]
		definitions += [
			field_definition(module.name, field_items, name, label, kind, True, options)
			for name, label, kind, options in CUSTOM.get(module.name, [])
		]
		written[f"fields-{module.name}"] = fixture(
			"/settings/fields", {"module": module.name}, {"fields": definitions}
		)
		if module is reader.CONTACT:
			continue
		rows = store[module.name]
		written[f"records-{module.name}"] = fixture(
			f"/{module.name}", {"per_page": reader.PER_PAGE, "page": 1}, records(rows)
		)
		for row in rows:
			written[f"record-{module.name}-{row['id']}"] = fixture(
				f"/{module.name}", {"ids": row["id"]}, records([row])
			)
	for contact in store["Contacts"]:
		written[f"record-Contacts-{contact['id']}"] = fixture(
			"/Contacts", {"ids": contact["id"]}, records([contact])
		)
	for account in store["Accounts"]:
		people = [c for c in store["Contacts"] if c["Account_Name"]["id"] == account["id"]]
		written[f"contacts-{account['id']}"] = fixture(
			f"/Accounts/{account['id']}/Contacts",
			{"per_page": reader.PER_PAGE, "page": 1},
			records(people, False),
		)
	written["modules"] = fixture("/settings/modules", {}, {"modules": listed_modules})
	written["users"] = fixture(
		"/users",
		{"type": "AllUsers", "page": 1, "per_page": reader.PER_PAGE},
		{
			"users": store["users"],
			"info": {
				"per_page": reader.PER_PAGE,
				"count": len(store["users"]),
				"page": 1,
				"more_records": False,
			},
		},
	)
	organisation = example(resolved(resolved(org_schema())["properties"]["org"])["items"])
	organisation.update(
		company_name="Harbour Design",
		iso_code="CAD",
		currency="Canadian Dollar - CAD",
		id="5725767000000000001",
	)
	written["org"] = fixture("/org", {}, {"org": [organisation]})
	return written


def response_schema(document: str, path: str) -> Any:
	for spec in for_source("zoho_crm"):
		if spec.name == document:
			found = spec.load().response_schema("GET", f"{reader.API_PATH}{path}")
			if found is not None:
				return found
	raise LookupError(f"{document} has no GET {path}")


def fields_schema() -> Any:
	return response_schema("fields", "/settings/fields")


def modules_schema() -> Any:
	return response_schema("modules", "/settings/modules")


def org_schema() -> Any:
	return response_schema("org", "/org")


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
