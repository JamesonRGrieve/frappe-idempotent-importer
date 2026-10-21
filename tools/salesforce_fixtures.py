# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the Salesforce fixtures (tests/fixtures/salesforce/<state>/*.json): a small design studio's org (one
currency, no person accounts) as the REST API answers, shaped as tests/schemas/salesforce.json transcribes
Salesforce's references, for exactly the requests the reader sends: each object's describe (with the
studio's custom fields), each kind's query (the reader's own SOQL), the incremental queries (nothing
changed since), and the org's currency. The two states differ only by their records (v2: a user
deactivated, a new price, a lead closed, a won opportunity, a closed task, an activated order, a new email).

``python -m tools.salesforce_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from idempotent_importer.sources.salesforce import reader

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "salesforce"
CREATED = "2026-08-03T15:00:00.000+0000"
Record = dict[str, Any]
RIVER, DELTA = "0015e000002AaAAAA1", "0015e000002AbBBAA2"
RILEY, SAM = "0035e000003AaAAAA1", "0035e000003AbBBAA2"
QUINN, CASEY, ALEX = "00Q5e000003AaAAAA1", "00Q5e000003AbBBAA2", "00Q5e000003AcCCAA3"
REBRAND, AUDIT = "0065e000004AaAAAA1", "0065e000004AbBBAA2"
SESSION, PRINT = "01t5e000000AaAAAA1", "01t5e000000AbBBAA2"
MORGAN, JAMIE = "0055e000001AaAAAA1", "0055e000001AbBBAA2"
# The custom fields the studio added, with their picklist values (value, label).
CUSTOM: dict[str, list[tuple[str, str, str, list[tuple[str, str]]]]] = {
	"Account": [("Account_Tier__c", "Account Tier", "picklist", [("gold", "Gold"), ("silver", "Silver")])],
	"Opportunity": [("Lead_Source_Detail__c", "Lead Source Detail", "string", [])],
	"Lead": [
		(
			"Preferred_Contact_Time__c",
			"Preferred Contact Time",
			"picklist",
			[("am", "Mornings"), ("pm", "Evenings")],
		)
	],
}
PICKLISTS: dict[str, dict[str, list[tuple[str, str]]]] = {
	"Opportunity": {
		"Type": [
			("New Customer", "New Customer"),
			("Existing Customer - Upgrade", "Existing Customer - Upgrade"),
		],
		"StageName": [
			("Prospecting", "Prospecting"),
			("Proposal/Price Quote", "Proposal/Price Quote"),
			("Closed Won", "Closed Won"),
			("Closed Lost", "Closed Lost"),
		],
	},
	"Lead": {
		"Status": [
			("Open - Not Contacted", "Open - Not Contacted"),
			("Working - Contacted", "Working - Contacted"),
			("Closed - Converted", "Closed - Converted"),
			("Closed - Not Converted", "Closed - Not Converted"),
		]
	},
}
# Fields an org with one currency and no person accounts doesn't have.
ABSENT = frozenset({"CurrencyIsoCode", "IsPersonAccount", "PersonEmail"})


def attributes(type_: str, id_: str) -> Record:
	return {"type": type_, "url": f"{reader.API_ROOT}/sobjects/{type_}/{id_}"}


def sobject(type_: str, id_: str, **fields: Any) -> Record:
	return {"attributes": attributes(type_, id_), "Id": id_, **fields}


def children(records: list[Record]) -> Record | None:
	return {"totalSize": len(records), "done": True, "records": records} if records else None


def note(id_: str, title: str, body: str) -> Record:
	return sobject("Note", id_, Title=title, Body=body, CreatedDate="2026-09-02T14:30:00.000+0000")


def contact(id_: str, first: str, last: str, title: str | None = None) -> Record:
	return sobject(
		"Contact",
		id_,
		FirstName=first,
		LastName=last,
		Email=f"{first.lower()}@river.example",
		Phone="+1 902 555 0101",
		MobilePhone=None,
		Title=title,
	)


def line(type_: str, id_: str, product: str, name: str, qty: int, price: str) -> Record:
	return sobject(
		type_,
		id_,
		Product2Id=product,
		Product2={"attributes": attributes("Product2", product), "Name": name},
		Description=None,
		Quantity=qty,
		UnitPrice=float(price),
		TotalPrice=float(price) * qty,
	)


def store_v1() -> dict[str, list[Record]]:
	return {
		"User": [
			sobject(
				"User",
				MORGAN,
				FirstName="Morgan",
				LastName="Sales",
				Email="morgan@studio.example",
				IsActive=True,
			),
			sobject(
				"User",
				JAMIE,
				FirstName="Jamie",
				LastName="Front",
				Email="jamie@studio.example",
				ManagerId=MORGAN,
				EmployeeNumber="E-2",
				IsActive=True,
			),
		],
		"Product2": [
			sobject(
				"Product2",
				SESSION,
				Name="Design session",
				ProductCode="SESSION",
				Description="One hour",
				IsActive=True,
				PricebookEntries=children([sobject("PricebookEntry", "01u5e000000AaAAAA1", UnitPrice=150.0)]),
			),
			sobject(
				"Product2",
				PRINT,
				Name="Studio print",
				StockKeepingUnit="PRINT-1",
				IsActive=True,
				PricebookEntries=children([sobject("PricebookEntry", "01u5e000000AbBBAA2", UnitPrice=45.0)]),
			),
		],
		"Account": [
			sobject(
				"Account",
				RIVER,
				Name="River Co",
				Type="Customer - Direct",
				Phone="+1 902 555 0100",
				Website="https://river.example",
				Description="Harbour tours",
				BillingStreet="1 Harbour Rd",
				BillingCity="Halifax",
				BillingState="NS",
				BillingPostalCode="B3H 1A1",
				BillingCountry="Canada",
				Account_Tier__c="gold",
				Contacts=children(
					[contact(RILEY, "Riley", "River", "Director"), contact(SAM, "Sam", "Shore")]
				),
				Notes=children([note("0025e000000AaAAAA1", "Spring", "Expanding in spring")]),
			),
			sobject("Account", DELTA, Name="Delta Works", Type="Prospect", Website="https://delta.example"),
		],
		"Lead": [
			sobject(
				"Lead",
				QUINN,
				FirstName="Quinn",
				LastName="Quay",
				Company="Quay Ltd",
				Email="quinn@quay-ltd.example",
				Status="Open - Not Contacted",
				IsConverted=False,
				Preferred_Contact_Time__c="am",
			),
			sobject(
				"Lead",
				CASEY,
				FirstName="Casey",
				LastName="Cove",
				Company="Cove Cafe",
				Email="casey@cove-cafe.example",
				MobilePhone="+1 902 555 0110",
				Status="Working - Contacted",
				IsConverted=False,
				Notes=children([note("0025e000000AbBBAA2", "Call back", "Wants the spring catalogue")]),
			),
			sobject(
				"Lead",
				ALEX,
				FirstName="Alex",
				LastName="Arm",
				Company="River Co",
				Status="Closed - Converted",
				IsConverted=True,
			),
		],
		"Opportunity": [
			sobject(
				"Opportunity",
				REBRAND,
				Name="River rebrand",
				AccountId=RIVER,
				Amount=4800.0,
				CloseDate="2026-11-30",
				StageName="Proposal/Price Quote",
				Probability=75.0,
				IsClosed=False,
				IsWon=False,
				Type="New Customer",
				CreatedDate="2026-08-20T15:00:00.000+0000",
				Lead_Source_Detail__c="Expo 2026",
				Notes=children([note("0025e000000AcCCAA3", "Budget", "Budget approved")]),
			),
			sobject(
				"Opportunity",
				AUDIT,
				Name="Delta audit",
				AccountId=DELTA,
				Amount=900.0,
				CloseDate="2026-09-30",
				StageName="Closed Lost",
				Probability=0.0,
				IsClosed=True,
				IsWon=False,
				Type="Existing Customer - Upgrade",
				CreatedDate="2026-08-22T15:00:00.000+0000",
			),
		],
		"Task": [
			sobject(
				"Task",
				"00T5e000005AaAAAA1",
				WhatId=REBRAND,
				AccountId=RIVER,
				Subject="Kick-off call",
				Description="Walked through the brief.",
				CreatedDate="2026-09-05T15:00:00.000+0000",
				Status="Completed",
				IsClosed=True,
				TaskSubtype="Call",
				CallType="Outbound",
				CallDurationInSeconds=754,
				CallDisposition="Connected",
			),
			sobject(
				"Task",
				"00T5e000005AbBBAA2",
				WhoId=CASEY,
				Subject="Send samples",
				Description="Paper and foil",
				CreatedDate="2026-09-06T15:00:00.000+0000",
				Status="Not Started",
				IsClosed=False,
				ActivityDate="2026-09-30",
				TaskSubtype="Task",
			),
			sobject(
				"Task",
				"00T5e000005AcCCAA3",
				WhoId=RILEY,
				AccountId=RIVER,
				Subject="Email: Proposal",
				Description="Attached the proposal.",
				CreatedDate="2026-09-07T15:00:00.000+0000",
				Status="Completed",
				IsClosed=True,
				TaskSubtype="Email",
			),
		],
		"Event": [
			sobject(
				"Event",
				"00U5e000006AaAAAA1",
				WhatId=REBRAND,
				AccountId=RIVER,
				Subject="Concept review",
				Description="Three directions",
				Location="Studio",
				StartDateTime="2026-09-10T16:00:00.000+0000",
				EndDateTime="2026-09-10T17:00:00.000+0000",
				IsAllDayEvent=False,
				ActivityDate="2026-09-10",
				CreatedDate="2026-09-01T15:00:00.000+0000",
			)
		],
		"EmailMessage": [
			sobject(
				"EmailMessage",
				"02s5e000007AaAAAA1",
				Subject="Samples",
				TextBody="Could you send the paper samples?",
				FromAddress="sam@river.example",
				ToAddress="morgan@studio.example",
				Incoming=True,
				MessageDate="2026-09-06T15:00:00.000+0000",
				RelatedToId=REBRAND,
				Status="1",
			)
		],
		"Quote": [
			sobject(
				"Quote",
				"0Q05e000008AaAAAA1",
				Name="Delta identity",
				QuoteNumber="00000001",
				Status="Presented",
				AccountId=DELTA,
				ExpirationDate="2026-12-31",
				CreatedDate="2026-09-12T15:00:00.000+0000",
				Subtotal=300.0,
				TotalPrice=270.0,
				Tax=40.5,
				ShippingHandling=0.0,
				GrandTotal=310.5,
				QuoteLineItems=children(
					[line("QuoteLineItem", "0QL5e000000AaAAAA1", SESSION, "Design session", 2, "150")]
				),
			)
		],
		"Order": [
			sobject(
				"Order",
				"8015e000009AaAAAA1",
				OrderNumber="00000100",
				AccountId=RIVER,
				EffectiveDate="2026-09-14",
				Status="Activated",
				StatusCode="Activated",
				TotalAmount=135.0,
				PoNumber="PO-77",
				OrderItems=children(
					[line("OrderItem", "8025e000000AaAAAA1", PRINT, "Studio print", 3, "45")]
				),
			),
			sobject(
				"Order",
				"8015e000009AbBBAA2",
				OrderNumber="00000101",
				AccountId=RIVER,
				EffectiveDate="2026-09-20",
				Status="Draft",
				StatusCode="Draft",
				TotalAmount=150.0,
				OrderItems=children(
					[line("OrderItem", "8025e000000AbBBAA2", SESSION, "Design session", 1, "150")]
				),
			),
		],
	}


def store_v2() -> dict[str, list[Record]]:
	store = copy.deepcopy(store_v1())
	find(store, "User", JAMIE)["IsActive"] = False
	session = find(store, "Product2", SESSION)
	session["PricebookEntries"]["records"][0]["UnitPrice"] = 175.0
	find(store, "Lead", QUINN)["Status"] = "Closed - Not Converted"
	find(store, "Opportunity", REBRAND).update(
		StageName="Closed Won", Probability=100.0, IsClosed=True, IsWon=True
	)
	find(store, "Task", "00T5e000005AbBBAA2").update(Status="Completed", IsClosed=True)
	find(store, "Order", "8015e000009AbBBAA2").update(Status="Activated", StatusCode="Activated")
	store["EmailMessage"].append(
		sobject(
			"EmailMessage",
			"02s5e000007AbBBAA2",
			Subject="Signed",
			TextBody="Signed and returned.",
			FromAddress="morgan@studio.example",
			ToAddress="dana@delta.example",
			Incoming=False,
			MessageDate="2026-10-02T09:00:00.000+0000",
			RelatedToId=DELTA,
			Status="3",
		)
	)
	return store


def find(store: dict[str, list[Record]], type_: str, id_: str) -> Record:
	return next(record for record in store[type_] if record["Id"] == id_)


def fields_of(sobject_: reader.SObject) -> list[str]:
	"""The fields the reader selects for an object: its standard ones the org has, then its custom ones."""
	return [f for f in sobject_.fields if f not in ABSENT] + [
		name for name, *_ in CUSTOM.get(sobject_.name, [])
	]


def served(sobject_: reader.SObject, record: Record) -> Record:
	"""A record as the query answers it: every selected field (null without a value) and its children."""
	body: Record = {"attributes": record["attributes"], "Id": record["Id"]}
	for name in fields_of(sobject_):
		body[name] = record.get(name)
	for child in sobject_.children:
		relationship = re.search(r" FROM (\w+)", child)
		if relationship is None:
			raise ValueError(f"No relationship in {child}")
		body[relationship.group(1)] = record.get(relationship.group(1))
	return body


def describe(name: str, fields: tuple[str, ...]) -> Record:
	picklists = PICKLISTS.get(name, {})
	described = [
		{
			"name": field,
			"label": re.sub(r"(?<!^)(?=[A-Z])", " ", field),
			"type": "picklist" if field in picklists else "string",
			"custom": False,
			"picklistValues": [
				{"value": value, "label": label, "active": True, "defaultValue": False}
				for value, label in picklists.get(field, [])
			],
		}
		for field in ("Id", *fields)
		if field not in ABSENT
	]
	described += [
		{
			"name": field,
			"label": label,
			"type": kind,
			"custom": True,
			"picklistValues": [
				{"value": value, "label": text, "active": True, "defaultValue": False}
				for value, text in options
			],
		}
		for field, label, kind, options in CUSTOM.get(name, [])
	]
	return {"name": name, "label": name, "custom": False, "fields": described}


def fixture(path: str, query: Record, response: Record, patterns: Record | None = None) -> Record:
	request: Record = {"path": path, "query": query}
	if patterns:
		request["query_patterns"] = patterns
	return {"request": request, "response": response}


def result(records: list[Record]) -> Record:
	return {"totalSize": len(records), "done": True, "records": records}


QUERIES = {
	"users": reader.USER,
	"products": reader.PRODUCT,
	"customers": reader.CUSTOMER,
	"accounts": reader.ACCOUNT,
	"leads": reader.LEAD,
	"opportunities": reader.OPPORTUNITY,
	"tasks": reader.TASK,
	"events": reader.EVENT,
	"emails": reader.EMAIL,
	"quotes": reader.QUOTE,
	"orders": reader.ORDER,
}
CHANGED_SINCE = "2000-01-01T00:00:00Z"


def fixtures(store: dict[str, list[Record]]) -> dict[str, Record]:
	written: dict[str, Record] = {
		"organization": fixture(
			f"{reader.API_ROOT}/query",
			{"q": reader.ORGANISATION_QUERY},
			result([{"attributes": {"type": "Organization"}, "DefaultCurrencyIsoCode": "CAD"}]),
		)
	}
	described: set[str] = set()
	for name, sobject_ in QUERIES.items():
		if sobject_.name not in described:
			described.add(sobject_.name)
			written[f"describe-{sobject_.name}"] = fixture(
				f"{reader.API_ROOT}/sobjects/{sobject_.name}/describe",
				{},
				describe(sobject_.name, sobject_.fields),
			)
		records = store[sobject_.name]
		if sobject_ is reader.CUSTOMER:
			records = [r for r in records if (r.get("Type") or "").startswith("Customer")]
		written[f"query-{name}"] = fixture(
			f"{reader.API_ROOT}/query",
			{"q": reader.soql(sobject_, fields_of(sobject_), None, None)},
			result([served(sobject_, record) for record in records]),
		)
		changed = re.escape(
			reader.soql(sobject_, fields_of(sobject_), None, datetime.fromisoformat(CHANGED_SINCE))
		)
		written[f"changed-{name}"] = fixture(
			f"{reader.API_ROOT}/query",
			{},
			result([]),
			{"q": changed.replace(re.escape(CHANGED_SINCE), r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")},
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
