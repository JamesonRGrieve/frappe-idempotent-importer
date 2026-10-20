# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the NetSuite fixtures (tests/fixtures/netsuite/<state>/*.json): a small account's records, shaped as
the REST API Browser 2026.1 defines them (pinned in tools/specs.py), read whole with their sublists
(``expandSubResources``), and SuiteQL's answers to exactly the queries the reader sends (built from the
reader's own listings, so they can't drift). The account's two states differ only by their records (v2: a
renamed and an inactive account, a new department, a changed and an inactive customer, a lead become a
customer, a supplier's new email, an employee who left, a closed project, a new price and stock level, an
inactive item, a changed sales order, a voided journal and a new invoice pending approval).

``python -m tools.netsuite_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.core.kind import Kind
from idempotent_importer.sources.netsuite import reader

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "netsuite"

HOST = "https://1234567.suitetalk.api.netsuite.com"
RECORD = HOST + "/services/rest/record/v1"
SUITEQL = "/services/rest/query/v1/suiteql"


def link(record: Any, id_: Any) -> Any:
	return [{"rel": "self", "href": f"{RECORD}/{record}/{id_}"}]


def ref(record: Any, id_: Any, name: Any) -> Any:
	return {"id": str(id_), "refName": name, "links": link(record, id_)}


def enum(id_: Any, name: Any) -> Any:
	return {"id": id_, "refName": name}


def sublist(owner: Any, id_: Any, name: Any, items: Any) -> Any:
	return {
		"links": [{"rel": "self", "href": f"{RECORD}/{owner}/{id_}/{name}"}],
		"count": len(items),
		"hasMore": False,
		"items": items,
		"offset": 0,
		"totalResults": len(items),
	}


CAD = ref("currency", 1, "Canadian Dollar")
USD = ref("currency", 2, "US Dollar")
CURRENCIES = {
	"1": {
		"links": link("currency", 1),
		"id": "1",
		"name": "Canadian Dollar",
		"symbol": "CAD",
		"isBaseCurrency": True,
		"isInactive": False,
		"exchangeRate": 1.0,
	},
	"2": {
		"links": link("currency", 2),
		"id": "2",
		"name": "US Dollar",
		"symbol": "USD",
		"isBaseCurrency": False,
		"isInactive": False,
		"exchangeRate": 1.35,
	},
}


def account(
	id_: Any,
	number: Any,
	name: Any,
	type_: Any,
	type_name: Any,
	parent: Any = None,
	summary: Any = False,
	special: Any = None,
	inactive: Any = False,
	currency: Any = CAD,
) -> Any:
	body = {
		"links": link("account", id_),
		"id": str(id_),
		"acctNumber": number,
		"acctName": name,
		"fullName": f"{number} {name}",
		"acctType": enum(type_, type_name),
		"isSummary": summary,
		"isInactive": inactive,
		"currency": currency,
		"lastModifiedDate": "2026-09-01T12:00:00Z",
	}
	if parent:
		body["parent"] = ref("account", parent[0], parent[1])
	if special:
		body["sSpecAcct"] = enum(special, "Sales Tax Payable")
	return body


def tree(
	record: Any, id_: Any, name: Any, parent: Any = None, inactive: Any = False, address: Any = None
) -> Any:
	body = {
		"links": link(record, id_),
		"id": str(id_),
		"name": name,
		"fullName": name if not parent else f"{parent[1]} : {name}",
		"isInactive": inactive,
		"lastModifiedDate": "2026-09-01T12:00:00Z",
	}
	if parent:
		body["parent"] = ref(record, parent[0], parent[1])
	if address:
		body["mainAddress"] = address
	return body


def address_entry(
	owner: Any,
	owner_id: Any,
	entry_id: Any,
	label: Any,
	billing: Any,
	shipping: Any,
	addressee: Any,
	addr1: Any,
	city: Any,
	state: Any,
	zip_: Any,
	country: Any,
	country_name: Any,
	phone: Any = None,
) -> Any:
	address = {
		"links": [
			{"rel": "self", "href": f"{RECORD}/{owner}/{owner_id}/addressBook/{entry_id}/addressBookAddress"}
		],
		"addressee": addressee,
		"addr1": addr1,
		"city": city,
		"state": state,
		"zip": zip_,
		"country": enum(country, country_name),
	}
	if phone:
		address["addrPhone"] = phone
	return {
		"links": [{"rel": "self", "href": f"{RECORD}/{owner}/{owner_id}/addressBook/{entry_id}"}],
		"id": int(entry_id),
		"label": label,
		"defaultBilling": billing,
		"defaultShipping": shipping,
		"addressBookAddress": address,
	}


def contact(
	owner: Any, owner_id: Any, id_: Any, first: Any, last: Any, email: Any, phone: Any, title: Any
) -> Any:
	return {
		"links": link("contact", id_),
		"id": str(id_),
		"entityId": f"{first} {last}",
		"firstName": first,
		"lastName": last,
		"email": email,
		"phone": phone,
		"title": title,
		"company": ref(owner, owner_id, "owner"),
	}


def customer(
	id_: Any,
	entity_id: Any,
	stage: Any,
	*,
	company: Any = None,
	first: Any = None,
	last: Any = None,
	person: Any = False,
	email: Any = None,
	phone: Any = None,
	currency: Any = CAD,
	addresses: Any = (),
	inactive: Any = False,
	comments: Any = None,
	contacts: Any = (),
) -> Any:
	body = {
		"links": link("customer", id_),
		"id": str(id_),
		"entityId": entity_id,
		"isPerson": person,
		"isInactive": inactive,
		"searchStage": stage,
		"currency": currency,
		"lastModifiedDate": "2026-09-02T09:30:00Z",
		"addressBook": sublist("customer", id_, "addressBook", list(addresses)),
	}
	if contacts:
		body["contactList"] = sublist("customer", id_, "contactList", list(contacts))
	for key, value in (
		("companyName", company),
		("firstName", first),
		("lastName", last),
		("email", email),
		("phone", phone),
		("comments", comments),
	):
		if value is not None:
			body[key] = value
	return body


def vendor(
	id_: Any,
	entity_id: Any,
	company: Any,
	email: Any,
	phone: Any,
	addresses: Any = (),
	inactive: Any = False,
	contacts: Any = (),
) -> Any:
	body = {
		"links": link("vendor", id_),
		"id": str(id_),
		"entityId": entity_id,
		"companyName": company,
		"isPerson": False,
		"isInactive": inactive,
		"email": email,
		"phone": phone,
		"currency": CAD,
		"vatRegNumber": "123456789RT0001",
		"lastModifiedDate": "2026-09-02T09:30:00Z",
		"addressBook": sublist("vendor", id_, "addressBook", list(addresses)),
	}
	if contacts:
		body["contactList"] = sublist("vendor", id_, "contactList", list(contacts))
	return body


def employee(
	id_: Any,
	entity_id: Any,
	first: Any,
	last: Any,
	title: Any,
	department: Any,
	gender: Any,
	hired: Any,
	born: Any,
	supervisor: Any = None,
	inactive: Any = False,
	released: Any = None,
	email: Any = None,
) -> Any:
	body = {
		"links": link("employee", id_),
		"id": str(id_),
		"entityId": entity_id,
		"firstName": first,
		"lastName": last,
		"title": title,
		"department": ref("department", department[0], department[1]),
		"gender": enum(gender[0], gender[1]),
		"hireDate": hired,
		"birthdate": born,
		"isInactive": inactive,
		"email": email or f"{first.lower()}@northwind.example",
		"mobilePhone": "+1 604 555 0100",
		"lastModifiedDate": "2026-09-03T08:00:00Z",
	}
	if supervisor:
		body["supervisor"] = ref("employee", supervisor[0], supervisor[1])
	if released:
		body["releasedate"] = released
	return body


def price_list(record: Any, id_: Any, amount: Any) -> Any:
	element = {
		"links": [{"rel": "self", "href": f"{RECORD}/{record}/{id_}/price/1"}],
		"priceLevel": ref("priceLevel", 1, "Base Price"),
		"priceLevelName": "Base Price",
		"currencyPage": CAD,
		"price": amount,
		"priceQty": 0,
	}
	return {
		"links": [{"rel": "self", "href": f"{RECORD}/{record}/{id_}/price"}],
		"count": 1,
		"hasMore": False,
		"items": [element],
		"offset": 0,
		"totalResults": 1,
	}


def item(
	record: Any,
	id_: Any,
	item_id: Any,
	name: Any,
	price: Any,
	*,
	item_type: Any,
	description: Any = None,
	upc: Any = None,
	on_hand: Any = None,
	weight: Any = None,
	inactive: Any = False,
	online: Any = False,
) -> Any:
	body = {
		"links": link(record, id_),
		"id": str(id_),
		"itemId": item_id,
		"displayName": name,
		"itemType": enum(item_type[0], item_type[1]),
		"isInactive": inactive,
		"isOnline": online,
		"lastModifiedDate": "2026-09-04T10:00:00Z",
		"price": price_list(record, id_, price),
	}
	if description:
		body["salesDescription"] = description
	if upc:
		body["upcCode"] = upc
	if on_hand is not None:
		body["quantityOnHand"] = on_hand
	if weight:
		body["weight"] = weight[0]
		body["weightUnit"] = enum(weight[1], weight[2])
	return body


ITEM_NAMES = {
	"501": "TRAIL-MUG",
	"502": "INSTALL",
	"503": "GIFT-WRAP",
	"504": "SPRING10",
	"505": "Subtotal",
	"506": "MUG-SET",
}
ITEM_RECORD_OF = {
	"501": "inventoryItem",
	"502": "serviceSaleItem",
	"503": "nonInventorySaleItem",
	"504": "discountItem",
	"505": "subtotalItem",
	"506": "kitItem",
}


def line(
	owner: Any,
	id_: Any,
	number: Any,
	item_id: Any,
	qty: Any,
	rate: Any,
	amount: Any,
	*,
	description: Any = None,
	department: Any = None,
	job: Any = None,
	account_: Any = None,
) -> Any:
	body = {
		"links": [{"rel": "self", "href": f"{RECORD}/{owner}/{id_}/item/{number}"}],
		"line": number,
		"item": ref(ITEM_RECORD_OF[item_id], item_id, ITEM_NAMES[item_id]),
		"amount": amount,
	}
	if qty is not None:
		body["quantity"] = qty
	if rate is not None:
		body["rate"] = rate
	if description:
		body["description"] = description
	if department:
		body["department"] = ref("department", department[0], department[1])
	if job:
		body["job"] = ref("job", job[0], job[1])
	if account_:
		body["account"] = ref("account", account_[0], account_[1])
	return body


def expense(
	owner: Any, id_: Any, number: Any, account_: Any, amount: Any, memo: Any, department: Any = None
) -> Any:
	body = {
		"links": [{"rel": "self", "href": f"{RECORD}/{owner}/{id_}/expense/{number}"}],
		"line": number,
		"account": ref("account", account_[0], account_[1]),
		"amount": amount,
		"memo": memo,
	}
	if department:
		body["department"] = ref("department", department[0], department[1])
	return body


def transaction(
	record: Any,
	id_: Any,
	tran_id: Any,
	date: Any,
	entity: Any,
	*,
	currency: Any = CAD,
	rate: Any = 1.0,
	total: Any,
	lines: Any = (),
	expenses: Any = (),
	tax: Any = None,
	status: Any = None,
	memo: Any = None,
	extra: Any = None,
) -> Any:
	body = {
		"links": link(record, id_),
		"id": str(id_),
		"tranId": tran_id,
		"tranDate": date,
		"currency": currency,
		"exchangeRate": rate,
		"total": total,
		"lastModifiedDate": "2026-09-05T15:00:00Z",
	}
	if entity:
		body["entity"] = entity
	if lines:
		body["item"] = sublist(record, id_, "item", list(lines))
	if expenses:
		body["expense"] = sublist(record, id_, "expense", list(expenses))
	if tax is not None:
		body["taxTotal"] = tax
	if status:
		body["status"] = enum(status[0], status[1])
	if memo:
		body["memo"] = memo
	body.update(extra or {})
	return body


def apply(owner: Any, id_: Any, applied: Any) -> Any:
	items = [
		{
			"links": [{"rel": "self", "href": f"{RECORD}/{owner}/{id_}/apply/{doc}"}],
			"doc": ref(
				"invoice" if kind == "Invoice" else "vendorBill" if kind == "Bill" else "creditMemo",
				doc,
				f"{kind} #{doc}",
			),
			"apply": on,
			"amount": amount,
			"type": kind,
			"applyDate": "2026-09-20",
		}
		for doc, kind, on, amount in applied
	]
	return sublist(owner, id_, "apply", items)


def journal_line(
	id_: Any,
	number: Any,
	account_: Any,
	debit: Any,
	credit: Any,
	memo: Any,
	entity: Any = None,
	department: Any = None,
) -> Any:
	body = {
		"links": [{"rel": "self", "href": f"{RECORD}/journalEntry/{id_}/line/{number}"}],
		"line": number,
		"account": ref("account", account_[0], account_[1]),
		"memo": memo,
	}
	if debit:
		body["debit"] = debit
	if credit:
		body["credit"] = credit
	if entity:
		body["entity"] = entity
	if department:
		body["department"] = ref("department", department[0], department[1])
	return body


SALES = ("1", "Sales")
OPS = ("2", "Operations")
FIELD = ("3", "Operations : Field Services")
LAKESIDE = ref("customer", 101, "C101 Lakeside Cafe")
MAYA = ref("customer", 102, "C102 Maya Chen")
PINE = ref("customer", 104, "C104 Pine Ridge Lodge")
PAPER = ref("vendor", 201, "V201 Northern Paper Co")
BOLT = ref("vendor", 202, "V202 Bolt Logistics")
CHEQUING = ("100", "1000 Chequing")
RENT = ("600", "6000 Rent")
SUPPLIES = ("610", "6100 Office Supplies")


def state_v1() -> Any:
	accounts = {
		"100": account(100, "1000", "Chequing", "Bank", "Bank"),
		"110": account(110, "1100", "Accounts Receivable", "AcctRec", "Accounts Receivable"),
		"130": account(130, "1500", "Fixed Assets", "FixedAsset", "Fixed Asset", summary=True),
		"131": account(
			131, "1510", "Equipment", "FixedAsset", "Fixed Asset", parent=("130", "1500 Fixed Assets")
		),
		"200": account(200, "2000", "Accounts Payable", "AcctPay", "Accounts Payable"),
		"210": account(
			210, "2100", "GST Payable", "OthCurrLiab", "Other Current Liability", special="SalesTaxPay"
		),
		"300": account(300, "3000", "Owner's Equity", "Equity", "Equity"),
		"400": account(400, "4000", "Sales", "Income", "Income"),
		"410": account(410, "4010", "Service Revenue", "Income", "Income", parent=("400", "4000 Sales")),
		"500": account(500, "5000", "Cost of Goods Sold", "COGS", "Cost of Goods Sold"),
		"600": account(600, "6000", "Rent", "Expense", "Expense"),
		"610": account(610, "6100", "Office Supplies", "Expense", "Expense"),
		"900": account(900, "9000", "Headcount", "NonPosting", "Non Posting"),
	}
	departments = {
		"1": tree("department", 1, "Sales"),
		"2": tree("department", 2, "Operations"),
		"3": tree("department", 3, "Field Services", parent=("2", "Operations")),
	}
	main_address = {
		"links": [{"rel": "self", "href": f"{RECORD}/location/1/mainAddress"}],
		"addressee": "Northwind Outfitters",
		"addr1": "200 Granville St",
		"city": "Vancouver",
		"state": "BC",
		"zip": "V6C 1S4",
		"country": enum("CA", "Canada"),
	}
	locations = {
		"1": tree("location", 1, "Main Warehouse", address=main_address),
		"2": tree("location", 2, "Overflow Bay", parent=("1", "Main Warehouse")),
	}
	customers = {
		"101": customer(
			101,
			"C101",
			"Customer",
			company="Lakeside Cafe",
			email="orders@lakeside.example",
			phone="+1 604 555 0101",
			addresses=[
				address_entry(
					"customer",
					101,
					"11",
					"Head office",
					True,
					False,
					"Lakeside Cafe",
					"12 Lakeshore Rd",
					"Kelowna",
					"BC",
					"V1Y 2A1",
					"CA",
					"Canada",
					"+1 250 555 0111",
				),
				address_entry(
					"customer",
					101,
					"12",
					"Kitchen door",
					False,
					True,
					"Lakeside Cafe Kitchen",
					"14 Lakeshore Rd",
					"Kelowna",
					"BC",
					"V1Y 2A1",
					"CA",
					"Canada",
				),
			],
			contacts=[
				contact(
					"customer",
					101,
					701,
					"Rosa",
					"Diaz",
					"rosa@lakeside.example",
					"+1 250 555 0112",
					"Head Chef",
				)
			],
		),
		"102": customer(
			102,
			"C102",
			"Customer",
			first="Maya",
			last="Chen",
			person=True,
			email="maya@example.com",
			currency=USD,
			addresses=[
				address_entry(
					"customer",
					102,
					"21",
					"Home",
					True,
					True,
					"Maya Chen",
					"88 Pike St",
					"Seattle",
					"WA",
					"98101",
					"US",
					"United States",
				)
			],
		),
		"103": customer(
			103,
			"C103",
			"Lead",
			company="Harbor Yoga",
			email="hello@harbor.example",
			phone="+1 604 555 0103",
			comments="Met at the spring trade show",
		),
		"104": customer(104, "C104", "Prospect", company="Pine Ridge Lodge", email="info@pineridge.example"),
	}
	vendors = {
		"201": vendor(
			201,
			"V201",
			"Northern Paper Co",
			"ap@northernpaper.example",
			"+1 604 555 0201",
			[
				address_entry(
					"vendor",
					201,
					"31",
					"Mill",
					True,
					True,
					"Northern Paper Co",
					"5 Mill Rd",
					"Prince George",
					"BC",
					"V2L 1A1",
					"CA",
					"Canada",
				),
			],
			contacts=[
				contact(
					"vendor",
					201,
					702,
					"Sam",
					"Okafor",
					"sam@northernpaper.example",
					"+1 250 555 0202",
					"Accounts Receivable Clerk",
				)
			],
		),
		"202": vendor(202, "V202", "Bolt Logistics", "billing@bolt.example", "+1 604 555 0202"),
	}
	employees = {
		"301": employee(
			301, "E301", "Alex", "Martin", "Sales Manager", SALES, ("m", "Male"), "2021-03-01", "1985-06-15"
		),
		"302": employee(
			302,
			"E302",
			"Jordan",
			"Lee",
			"Technician",
			FIELD,
			("f", "Female"),
			"2023-01-09",
			"1992-11-02",
			supervisor=("301", "E301 Alex Martin"),
		),
	}
	jobs = {
		"401": {
			"links": link("job", 401),
			"id": "401",
			"entityId": "P401",
			"companyName": "Lakeside Cafe Renovation",
			"parent": ref("customer", 101, "C101 Lakeside Cafe"),
			"entityStatus": enum("2", "In Progress"),
			"startDate": "2026-08-01",
			"scheduledEndDate": "2026-11-30",
			"isInactive": False,
			"comments": "Counter and lighting refit",
			"lastModifiedDate": "2026-09-06T10:00:00Z",
		}
	}
	items = {
		"501": item(
			"inventoryItem",
			501,
			"TRAIL-MUG",
			"Trail Mug",
			18.0,
			item_type=("InvtPart", "Inventory Item"),
			description="Enamel camping mug, 350 ml",
			upc="0627843001234",
			on_hand=40.0,
			weight=(0.5, "1", "lb"),
		),
		"502": item(
			"serviceSaleItem",
			502,
			"INSTALL",
			"Installation",
			95.0,
			item_type=("Service", "Service"),
			description="On-site installation, per visit",
		),
		"503": item(
			"nonInventorySaleItem",
			503,
			"GIFT-WRAP",
			"Gift Wrap",
			5.0,
			item_type=("NonInvtPart", "Non-inventory Item"),
			online=True,
		),
		"506": item("kitItem", 506, "MUG-SET", "Mug Set", 45.0, item_type=("Kit", "Kit/Package")),
	}
	item_rows = [
		{"id": "501", "itemtype": "InvtPart", "islotitem": "F", "isserialitem": "F"},
		{"id": "502", "itemtype": "Service", "subtype": "Sale", "islotitem": "F", "isserialitem": "F"},
		{"id": "503", "itemtype": "NonInvtPart", "subtype": "Sale", "islotitem": "F", "isserialitem": "F"},
		{"id": "506", "itemtype": "Kit", "islotitem": "F", "isserialitem": "F"},
	]
	item_types = [
		("501", "InvtPart"),
		("502", "Service"),
		("503", "NonInvtPart"),
		("504", "Discount"),
		("505", "Subtotal"),
		("506", "Kit"),
	]
	invoice_lines = [
		line("invoice", 604, 1, "501", 2.0, 18.0, 36.0, description="Trail Mug", department=SALES),
		line(
			"invoice",
			604,
			2,
			"502",
			1.0,
			95.0,
			95.0,
			description="Installation",
			department=FIELD,
			job=("401", "P401 Lakeside Cafe Renovation"),
		),
		line("invoice", 604, 3, "505", None, None, 131.0, description="Subtotal"),
		line("invoice", 604, 4, "504", None, -10.0, -11.0, description="Spring promotion"),
	]
	transactions = {
		"601": (
			"Estimate",
			"estimate",
			transaction(
				"estimate",
				601,
				"EST-1001",
				"2026-09-10",
				LAKESIDE,
				total=37.8,
				tax=1.8,
				status=("A", "Open"),
				memo="Mugs for the patio",
				lines=[
					line("estimate", 601, 1, "501", 2.0, 18.0, 36.0, description="Trail Mug"),
				],
				extra={"title": "Patio mugs"},
			),
		),
		"602": (
			"Opprtnty",
			"opportunity",
			transaction(
				"opportunity",
				602,
				"OPP-2001",
				"2026-09-11",
				PINE,
				total=900.0,
				status=("A", "In Progress"),
				memo="Mugs for every cabin",
				extra={
					"title": "Lodge mugs",
					"projectedTotal": 900.0,
					"probability": 40.0,
					"expectedCloseDate": "2026-12-15",
				},
			),
		),
		"603": (
			"SalesOrd",
			"salesOrder",
			transaction(
				"salesOrder",
				603,
				"SO-3001",
				"2026-09-12",
				LAKESIDE,
				total=147.55,
				tax=6.55,
				status=("B", "Pending Fulfillment"),
				lines=[
					line("salesOrder", 603, 1, "501", 2.0, 18.0, 36.0, description="Trail Mug"),
					line("salesOrder", 603, 2, "502", 1.0, 95.0, 95.0, description="Installation"),
				],
				extra={"dueDate": "2026-09-30", "shippingCost": 10.0},
			),
		),
		"604": (
			"CustInvc",
			"invoice",
			transaction(
				"invoice",
				604,
				"INV-4001",
				"2026-09-15",
				LAKESIDE,
				total=126.0,
				tax=6.0,
				status=("A", "Open"),
				memo="Thanks for your business",
				lines=invoice_lines,
				extra={
					"dueDate": "2026-10-15",
					"otherRefNum": "PO-7781",
					"account": ref("account", 110, "1100 Accounts Receivable"),
				},
			),
		),
		"605": (
			"CashSale",
			"cashSale",
			transaction(
				"cashSale",
				605,
				"CS-5001",
				"2026-09-16",
				MAYA,
				currency=USD,
				rate=1.35,
				total=23.0,
				tax=0.0,
				status=("C", "Deposited"),
				lines=[
					line("cashSale", 605, 1, "503", 1.0, 5.0, 5.0, description="Gift Wrap"),
					line("cashSale", 605, 2, "501", 1.0, 18.0, 18.0, description="Trail Mug"),
				],
				extra={"account": ref("account", 100, "1000 Chequing")},
			),
		),
		"606": (
			"CustCred",
			"creditMemo",
			transaction(
				"creditMemo",
				606,
				"CM-6001",
				"2026-09-18",
				LAKESIDE,
				total=18.9,
				tax=0.9,
				status=("B", "Fully Applied"),
				memo="One mug arrived chipped",
				lines=[
					line("creditMemo", 606, 1, "501", 1.0, 18.0, 18.0, description="Trail Mug"),
				],
				extra={
					"apply": apply("creditMemo", 606, [("604", "Invoice", True, 18.9)]),
					"account": ref("account", 110, "1100 Accounts Receivable"),
				},
			),
		),
		"607": (
			"CustPymt",
			"customerPayment",
			transaction(
				"customerPayment",
				607,
				"PMT-7001",
				"2026-09-20",
				None,
				total=107.1,
				status=("C", "Deposited"),
				extra={
					"customer": LAKESIDE,
					"payment": 107.1,
					"account": ref("account", 100, "1000 Chequing"),
					"arAcct": ref("account", 110, "1100 Accounts Receivable"),
					"apply": apply("customerPayment", 607, [("604", "Invoice", True, 107.1)]),
				},
			),
		),
		"608": (
			"PurchOrd",
			"purchaseOrder",
			transaction(
				"purchaseOrder",
				608,
				"PO-8001",
				"2026-09-08",
				PAPER,
				total=90.0,
				status=("B", "Pending Receipt"),
				lines=[
					line("purchaseOrder", 608, 1, "501", 10.0, 9.0, 90.0, description="Trail Mug"),
				],
			),
		),
		"609": (
			"VendBill",
			"vendorBill",
			transaction(
				"vendorBill",
				609,
				"NP-3321",
				"2026-09-14",
				PAPER,
				total=120.75,
				tax=5.75,
				status=("B", "Paid In Full"),
				lines=[
					line("vendorBill", 609, 1, "501", 10.0, 9.0, 90.0, description="Trail Mug"),
				],
				expenses=[
					expense("vendorBill", 609, 2, SUPPLIES, 25.0, "Packing paper", department=OPS),
				],
				extra={"dueDate": "2026-10-14", "account": ref("account", 200, "2000 Accounts Payable")},
			),
		),
		"610": (
			"VendPymt",
			"vendorPayment",
			transaction(
				"vendorPayment",
				610,
				"VP-9001",
				"2026-09-21",
				PAPER,
				total=120.75,
				status=("B", "Cleared"),
				extra={
					"account": ref("account", 100, "1000 Chequing"),
					"apAcct": ref("account", 200, "2000 Accounts Payable"),
					"apply": apply("vendorPayment", 610, [("609", "Bill", True, 120.75)]),
				},
			),
		),
		"611": (
			"Journal",
			"journalEntry",
			transaction(
				"journalEntry",
				611,
				"JE-1101",
				"2026-09-30",
				None,
				total=None,
				memo="September rent",
				extra={
					"line": sublist(
						"journalEntry",
						611,
						"line",
						[
							journal_line(611, 1, RENT, 1500.0, None, "Rent", department=OPS),
							journal_line(
								611,
								2,
								CHEQUING,
								None,
								1500.0,
								"Rent paid",
								entity=ref("vendor", 202, "V202 Bolt Logistics"),
							),
						],
					),
				},
			),
		),
		"612": (
			"Check",
			"check",
			transaction(
				"check",
				612,
				"CHK-1201",
				"2026-09-25",
				BOLT,
				total=42.0,
				status=("V", "Voided") if False else None,
				expenses=[
					expense("check", 612, 1, SUPPLIES, 42.0, "Courier supplies"),
				],
				extra={"account": ref("account", 100, "1000 Chequing")},
			),
		),
		"613": (
			"CustDep",
			"customerDeposit",
			transaction(
				"customerDeposit",
				613,
				"DEP-1301",
				"2026-09-22",
				None,
				currency=USD,
				rate=1.35,
				total=50.0,
				status=("B", "Not Deposited"),
				memo="Deposit for custom mugs",
				extra={
					"customer": MAYA,
					"payment": 50.0,
					"account": ref("account", 100, "1000 Chequing"),
				},
			),
		),
	}
	for body in transactions.values():
		if body[2].get("total") is None:
			del body[2]["total"]
	postings = {
		id_: ("F" if record in ("estimate", "opportunity", "salesOrder", "purchaseOrder") else "T", "F")
		for id_, (_, record, _) in transactions.items()
	}
	return {
		"accounts": accounts,
		"departments": departments,
		"locations": locations,
		"customers": customers,
		"vendors": vendors,
		"employees": employees,
		"jobs": jobs,
		"items": items,
		"item_rows": item_rows,
		"item_types": item_types,
		"transactions": transactions,
		"postings": postings,
		"stages": {"104": "Prospect"},
	}


def state_v2() -> Any:
	state = copy.deepcopy(state_v1())
	# A renamed account, an inactive one, a new department.
	state["accounts"]["610"]["acctName"] = "Office Supplies & Postage"
	state["accounts"]["610"]["fullName"] = "6100 Office Supplies & Postage"
	state["accounts"]["600"]["isInactive"] = True
	state["departments"]["4"] = tree("department", 4, "Marketing")
	# A changed customer, an inactive one, a lead converted to a customer.
	state["customers"]["101"]["phone"] = "+1 604 555 0199"
	state["customers"]["102"]["isInactive"] = True
	state["customers"]["103"]["searchStage"] = "Customer"
	# A changed supplier email; an employee who left; the project closed.
	state["vendors"]["202"]["email"] = "accounts@bolt.example"
	state["employees"]["302"]["isInactive"] = True
	state["employees"]["302"]["releasedate"] = "2026-10-01"
	state["jobs"]["401"]["entityStatus"] = enum("1", "Closed")
	# A new price and stock level, an inactive item.
	state["items"]["501"]["price"]["items"][0]["price"] = 19.5
	state["items"]["501"]["quantityOnHand"] = 35.0
	state["items"]["503"]["isInactive"] = True
	# An invoice's memo changed, the journal voided, a new invoice pending approval.
	state["transactions"]["603"][2]["memo"] = "Deliver to the kitchen door"
	state["postings"]["611"] = ("F", "T")
	state["transactions"]["614"] = (
		"CustInvc",
		"invoice",
		transaction(
			"invoice",
			614,
			"INV-4002",
			"2026-10-02",
			LAKESIDE,
			total=94.5,
			tax=4.5,
			status=("D", "Pending Approval"),
			lines=[
				line("invoice", 614, 1, "502", 1.0, 90.0, 90.0, description="Installation follow-up"),
			],
		),
	)
	state["postings"]["614"] = ("F", "F")
	return state


def suiteql(
	name: Any, query: Any, params: Any, rows: Any, *, limit: Any = reader.PAGE_SIZE, has_more: Any = False
) -> Any:
	body = {"q": query}
	if params is not None:
		body["params"] = params
	items = [{"links": [], **row} for row in rows]
	return name, {
		"request": {"method": "POST", "path": SUITEQL, "query": {"limit": limit, "offset": 0}, "body": body},
		"response": {
			"links": [{"rel": "self", "href": f"{HOST}{SUITEQL}?limit={limit}&offset=0"}],
			"count": len(items),
			"hasMore": has_more,
			"items": items,
			"offset": 0,
			"totalResults": len(items),
		},
	}


def get(name: Any, record: Any, id_: Any, body: Any) -> Any:
	return name, {
		"request": {"path": f"{reader.RECORD_PATH}/{record}/{id_}", "query": {"expandSubResources": "true"}},
		"response": body,
	}


def listing_fixtures(kind: Any, rows: Any) -> Any:
	listing = reader.LISTINGS[kind]
	full = reader.NetSuiteReader._select(listing, "id > ?")
	incremental = reader.NetSuiteReader._select(
		listing, "id > ? AND lastmodifieddate >= TO_DATE(?, 'YYYY-MM-DD')"
	)
	return [
		suiteql(f"suiteql-{kind.value}", full, ["0"], rows),
		suiteql(f"suiteql-{kind.value}-since", incremental, None, rows),
	]


def fixtures(state: Any) -> Any:
	out = []

	def plain(table: Any) -> Any:
		return [{"id": id_} for id_ in sorted(table, key=int)]

	def active_customers(stages: Any) -> Any:
		return [
			{"id": id_}
			for id_, body in sorted(state["customers"].items(), key=lambda kv: int(kv[0]))
			if body["searchStage"] in stages
		]

	out += listing_fixtures(Kind.ACCOUNT, plain(state["accounts"]))
	out += listing_fixtures(Kind.COST_CENTER, plain(state["departments"]))
	out += listing_fixtures(Kind.WAREHOUSE, plain(state["locations"]))
	out += listing_fixtures(Kind.PRODUCT, state["item_rows"])
	out += listing_fixtures(Kind.CUSTOMER, active_customers({"Customer"}))
	out += listing_fixtures(Kind.LEAD, active_customers({"Lead", "Prospect"}))
	out += listing_fixtures(Kind.SUPPLIER, plain(state["vendors"]))
	out += listing_fixtures(Kind.EMPLOYEE, plain(state["employees"]))
	out += listing_fixtures(Kind.PROJECT, plain(state["jobs"]))
	by_kind: dict[Kind, list[dict[str, str]]] = {}
	for id_, (type_, _record, _body) in state["transactions"].items():
		for kind, listing in reader.LISTINGS.items():
			if listing.table == "transaction" and f"'{type_}'" in listing.condition:
				posting, voided = state["postings"][id_]
				by_kind.setdefault(kind, []).append(
					{"id": id_, "type": type_, "posting": posting, "voided": voided}
				)
	for kind, listing in reader.LISTINGS.items():
		if listing.table == "transaction":
			out += listing_fixtures(kind, sorted(by_kind.get(kind, []), key=lambda row: int(row["id"])))
	titles = sorted({body["title"] for body in state["employees"].values() if body.get("title")})
	out.append(
		suiteql(
			"suiteql-designations",
			reader.DESIGNATIONS_QUERY,
			None,
			[{"title": t} for t in titles],
			limit=reader.FULL_PAGE_SIZE,
		)
	)
	for table, records in (("department", state["departments"]), ("location", state["locations"])):
		parents = sorted({body["parent"]["id"] for body in records.values() if "parent" in body}, key=int)
		out.append(
			suiteql(
				f"suiteql-parents-{table}",
				reader.PARENTS_QUERY.format(table=table),
				None,
				[{"parent": p} for p in parents],
				limit=reader.FULL_PAGE_SIZE,
			)
		)
	out.append(
		suiteql(
			"suiteql-item-types",
			reader.ITEM_TYPES_QUERY,
			["0"],
			[{"id": i, "itemtype": t} for i, t in state["item_types"]],
			limit=reader.FULL_PAGE_SIZE,
		)
	)
	for customer_id, stage in state["stages"].items():
		out.append(
			suiteql(
				f"suiteql-stage-{customer_id}",
				reader.STAGE_QUERY,
				[customer_id],
				[{"id": customer_id, "searchstage": stage}],
				limit=1,
			)
		)
	for id_, body in state["accounts"].items():
		out.append(get(f"account-{id_}", "account", id_, body))
	for id_, body in state["departments"].items():
		out.append(get(f"department-{id_}", "department", id_, body))
	for id_, body in state["locations"].items():
		out.append(get(f"location-{id_}", "location", id_, body))
	for id_, body in state["customers"].items():
		out.append(get(f"customer-{id_}", "customer", id_, body))
	for id_, body in state["vendors"].items():
		out.append(get(f"vendor-{id_}", "vendor", id_, body))
	for id_, body in state["employees"].items():
		out.append(get(f"employee-{id_}", "employee", id_, body))
	for id_, body in state["jobs"].items():
		out.append(get(f"job-{id_}", "job", id_, body))
	for id_, body in state["items"].items():
		record = body["links"][0]["href"].split("/record/v1/")[1].split("/")[0]
		out.append(get(f"item-{id_}", record, id_, body))
	for id_, (_type, record, body) in state["transactions"].items():
		out.append(get(f"{record}-{id_}", record, id_, body))
	for id_, body in CURRENCIES.items():
		out.append(get(f"currency-{id_}", "currency", id_, body))
	# Looking one record up by id (a single-object sync).
	listing = reader.LISTINGS[Kind.CUSTOMER]
	out.append(
		suiteql(
			"suiteql-customer-101",
			reader.NetSuiteReader._select(listing, "id = ?"),
			["101"],
			[{"id": "101"}],
			limit=1,
		)
	)
	return out


def write(state_name: str, state: Any) -> None:
	target = FIXTURES / state_name
	shutil.rmtree(target, ignore_errors=True)
	target.mkdir(parents=True)
	for name, fixture in fixtures(state):
		(target / f"{name}.json").write_text(json.dumps(fixture, indent="\t", ensure_ascii=False) + "\n")


def main() -> int:
	write("v1", state_v1())
	write("v2", state_v2())
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
