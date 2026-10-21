# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the Sage Intacct fixtures (tests/fixtures/sage_intacct/<state>/*.json): a small company's gateway
answers to the reader's ``get_list`` requests, as XML shaped by the 2.1 response DTD (pinned in
tools/specs.py and proven by tools/prove.py). Each fixture names the request it answers by the parts of its
body the responder reads (tests/support/fixtures.py: the function, object and start, and a key filter for a
lookup) and holds the response as ``response_text``. The company's two states differ only by their records
(v2: a renamed and an inactive account, a customer's new phone, an employee who left, a changed sales order,
a reversed invoice and a new bill).

``python -m tools.sage_intacct_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from idempotent_importer.sources.sage_intacct import gateway, reader
from tests.support.sage_intacct import INTACCT_COMPANY, INTACCT_SENDER, INTACCT_USER
from tools.dtd import Dtd
from tools.specs import INTACCT_RESPONSE

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "sage_intacct"
MODIFIED = "09/01/2026 12:00:00"
LATER = "10/02/2026 12:00:00"
type Tree = dict[str, Any]


def date(text: str) -> Tree:
	year, month, day = text.split("-")
	return {"year": year, "month": str(int(month)), "day": str(int(day))}


def address(line1: str, city: str, state: str, zip_: str, country: str) -> Tree:
	return {
		"address1": line1,
		"city": city,
		"state": state,
		"zip": zip_,
		"country": country,
		"isocountrycode": country,
	}


def contact(name: str, **fields: Any) -> Tree:
	return {"contact": {"contactname": name, "printas": fields.pop("printas", name), **fields}}


def store_v1() -> dict[str, list[Tree]]:
	return {
		"glaccount": [
			{
				"glaccountno": "1000",
				"title": "Operating Cash",
				"normalbalance": "debit",
				"accounttype": "balancesheet",
				"whenmodified": MODIFIED,
				"status": "active",
			},
			{
				"glaccountno": "1100",
				"title": "Accounts Receivable",
				"normalbalance": "debit",
				"accounttype": "balancesheet",
				"whenmodified": MODIFIED,
				"status": "active",
			},
			{
				"glaccountno": "2000",
				"title": "Accounts Payable",
				"normalbalance": "credit",
				"accounttype": "balancesheet",
				"whenmodified": MODIFIED,
				"status": "active",
			},
			{
				"glaccountno": "3000",
				"title": "Retained Earnings",
				"normalbalance": "credit",
				"accounttype": "balancesheet",
				"whenmodified": MODIFIED,
				"status": "active",
			},
			{
				"glaccountno": "4000",
				"title": "Consulting Revenue",
				"normalbalance": "credit",
				"accounttype": "incomestatement",
				"whenmodified": MODIFIED,
				"status": "active",
			},
			{
				"glaccountno": "6000",
				"title": "Rent",
				"normalbalance": "debit",
				"accounttype": "incomestatement",
				"whenmodified": MODIFIED,
				"status": "active",
			},
			{
				"glaccountno": "6100",
				"title": "Software",
				"normalbalance": "debit",
				"accounttype": "incomestatement",
				"whenmodified": MODIFIED,
				"status": "active",
			},
		],
		"department": [
			{"departmentid": "ADV", "title": "Advisory", "whenmodified": MODIFIED, "status": "active"},
			{
				"departmentid": "ADV-TAX",
				"title": "Tax Advisory",
				"parentid": "ADV",
				"whenmodified": MODIFIED,
				"status": "active",
			},
		],
		"warehouse": [
			{
				"warehouseid": "WH1",
				"name": "Head Office Store",
				"shipto": contact(
					"Head Office", mailaddress=address("50 King St W", "Toronto", "ON", "M5H 1J8", "CA")
				),
				"status": "active",
			}
		],
		"icitem": [
			{
				"itemid": "AUDIT",
				"itemname": "Audit Engagement",
				"extdescription": "Annual audit, per engagement",
				"itemtype": "Non-Inventory",
				"whenmodified": MODIFIED,
				"status": "active",
			},
			{
				"itemid": "GUIDE",
				"itemname": "Tax Guide (print)",
				"itemtype": "Inventory",
				"upc": "0627843009991",
				"whenmodified": MODIFIED,
				"status": "active",
			},
		],
		"customer": [
			{
				"customerid": "C-NORTH",
				"name": "Northgate Dental",
				"taxid": "445566778RT0001",
				"whenmodified": MODIFIED,
				"status": "active",
				"currency": "CAD",
				"billto": contact(
					"Northgate Dental AP",
					email1="ap@northgatedental.example",
					mailaddress=address("10 Front St", "Kingston", "ON", "K7L 1A1", "CA"),
				),
				"contactinfo": contact(
					"Northgate Dental",
					firstname="Ana",
					lastname="Silva",
					phone1="+1 613 555 0141",
					email1="office@northgatedental.example",
				),
				"contactlist": {"contactitem": [{"category": "Accounting", "contactname": "Paul Okafor"}]},
			},
			{
				"customerid": "C-WEST",
				"name": "Westbrook Cycles",
				"whenmodified": MODIFIED,
				"status": "active",
				"currency": "CAD",
				"contactinfo": contact("Westbrook Cycles", email1="hello@westbrookcycles.example"),
			},
		],
		"vendor": [
			{
				"vendorid": "V-SOFT",
				"name": "Ledgerline Software",
				"taxid": "998877665RT0001",
				"status": "active",
				"currency": "CAD",
				"whenmodified": MODIFIED,
				"contactinfo": contact(
					"Ledgerline Software", email1="billing@ledgerline.example", phone1="+1 416 555 0177"
				),
			},
		],
		"employee": [
			{
				"employeeid": "E-01",
				"title": "Partner",
				"departmentid": "ADV",
				"birthdate": date("1979-04-02"),
				"startdate": date("2015-06-01"),
				"gender": "female",
				"whenmodified": MODIFIED,
				"status": "active",
				"personalinfo": contact(
					"Grace Lin", firstname="Grace", lastname="Lin", email1="grace@lincpa.example"
				),
			},
			{
				"employeeid": "E-02",
				"title": "Tax Analyst",
				"departmentid": "ADV-TAX",
				"supervisorid": "E-01",
				"startdate": date("2022-09-06"),
				"whenmodified": MODIFIED,
				"status": "active",
				"personalinfo": contact("Omar Haddad", firstname="Omar", lastname="Haddad"),
			},
		],
		"project": [
			{
				"key": "P-100",
				"name": "Northgate Year-end",
				"customerid": "C-NORTH",
				"begindate": date("2026-09-01"),
				"enddate": date("2026-12-31"),
				"projectstatus": "In Progress",
				"whenmodified": MODIFIED,
				"status": "active",
			},
		],
		"sotransaction": [
			{
				"key": "11",
				"transactiontype": "Sales Quote",
				"transactionid": "Sales Quote-Q001",
				"documentnumber": "Q001",
				"datecreated": date("2026-09-10"),
				"customerid": "C-NORTH",
				"transactionstate": "Pending",
				"whenmodified": MODIFIED,
				"basecurr": "CAD",
				"currency": "CAD",
				"sotransitems": {
					"sotransitem": [
						{
							"itemid": "AUDIT",
							"itemdesc": "Audit Engagement",
							"line_no": "1",
							"quantity": "1",
							"price": "4500",
							"totalamount": "4500",
						}
					]
				},
			},
			{
				"key": "12",
				"transactiontype": "Sales Order",
				"transactionid": "Sales Order-SO001",
				"documentnumber": "SO001",
				"datecreated": date("2026-09-12"),
				"customerid": "C-NORTH",
				"referenceno": "PO-88",
				"transactionstate": "Pending",
				"whenmodified": MODIFIED,
				"basecurr": "CAD",
				"currency": "CAD",
				"sotransitems": {
					"sotransitem": [
						{
							"itemid": "AUDIT",
							"itemdesc": "Audit Engagement",
							"line_no": "1",
							"quantity": "1",
							"price": "4500",
							"totalamount": "4500",
						},
						{
							"itemid": "GUIDE",
							"itemdesc": "Tax Guide (print)",
							"line_no": "2",
							"quantity": "3",
							"price": "40",
							"totalamount": "120",
						},
					]
				},
			},
			{
				"key": "13",
				"transactiontype": "Sales Invoice",
				"transactionid": "Sales Invoice-SI001",
				"datecreated": date("2026-09-14"),
				"customerid": "C-NORTH",
				"transactionstate": "Posted",
				"whenmodified": MODIFIED,
				"basecurr": "CAD",
				"currency": "CAD",
			},
		],
		"potransaction": [
			{
				"key": "21",
				"transactiontype": "Purchase Order",
				"transactionid": "Purchase Order-PO001",
				"datecreated": date("2026-09-08"),
				"vendorid": "V-SOFT",
				"vendordocno": "LL-5501",
				"transactionstate": "Pending",
				"whenmodified": MODIFIED,
				"basecurr": "CAD",
				"currency": "CAD",
				"potransitems": {
					"potransitem": [
						{
							"itemid": "GUIDE",
							"itemdesc": "Tax Guide (print)",
							"quantity": "10",
							"price": "25",
							"totalamount": "250",
						}
					]
				},
			},
		],
		"invoice": [
			{
				"key": "31",
				"customerid": "C-NORTH",
				"datecreated": date("2026-09-15"),
				"datedue": date("2026-10-15"),
				"invoiceno": "INV-1001",
				"ponumber": "PO-88",
				"totalamount": "4620",
				"description": "Audit and guides",
				"basecurr": "CAD",
				"currency": "CAD",
				"whenmodified": MODIFIED,
				"state": "Posted",
				"invoiceitems": {
					"lineitem": [
						{
							"line_num": "1",
							"glaccountno": "4000",
							"amount": "4500",
							"memo": "Audit engagement",
							"departmentid": "ADV",
						},
						{"line_num": "2", "glaccountno": "4000", "amount": "120", "memo": "Tax guides"},
					]
				},
				"retainagereleased": "false",
			},
			{
				"key": "32",
				"customerid": "C-WEST",
				"datecreated": date("2026-09-16"),
				"invoiceno": "INV-1002",
				"totalamount": "800",
				"basecurr": "CAD",
				"currency": "CAD",
				"whenmodified": MODIFIED,
				"state": "Posted",
				"invoiceitems": {
					"lineitem": [
						{"line_num": "1", "glaccountno": "4000", "amount": "800", "memo": "Bookkeeping"}
					]
				},
				"retainagereleased": "false",
			},
		],
		"bill": [
			{
				"key": "41",
				"vendorid": "V-SOFT",
				"datecreated": date("2026-09-14"),
				"datedue": date("2026-10-14"),
				"billno": "LL-9001",
				"totalamount": "350",
				"description": "Ledgerline licences",
				"basecurr": "CAD",
				"currency": "CAD",
				"whenmodified": MODIFIED,
				"state": "Posted",
				"billitems": {
					"lineitem": [
						{
							"line_num": "1",
							"glaccountno": "6100",
							"amount": "350",
							"memo": "Licences",
							"departmentid": "ADV",
						}
					]
				},
				"retainagereleased": "false",
			},
		],
		"arpayment": [
			{
				"customerid": "C-NORTH",
				"paymentamount": "4620",
				"refid": "EFT-2201",
				"state": "Posted",
				"datereceived": date("2026-09-20"),
				"key": "51",
				"undepglaccountno": "1000",
				"whenmodified": MODIFIED,
				"arpaymentitems": {"arpaymentitem": [{"invoicekey": "31", "amount": "4620"}]},
			},
		],
		"appayment": [
			{
				"key": "61",
				"vendorid": "V-SOFT",
				"paymentamount": "350",
				"refid": "CHK-301",
				"paymentdate": date("2026-09-21"),
				"paymentmethod": "Printed Check",
				"financialentity": "BANK-01",
				"transactionstate": "Posted",
				"currency": "CAD",
				"whenmodified": MODIFIED,
				"appaymentitems": {
					"appaymentitem": [
						{
							"billkey": "41",
							"billno": "LL-9001",
							"lineitemkey": "411",
							"glaccountno": "6100",
							"amount": "350",
						}
					]
				},
			},
		],
		"gltransaction": [
			{
				"journalid": "GJ",
				"batchno": "7",
				"datecreated": date("2026-09-30"),
				"description": "September rent",
				"gltransactionentries": {
					"glentry": [
						{"trtype": "debit", "amount": "2200", "glaccountno": "6000", "departmentid": "ADV"},
						{"trtype": "credit", "amount": "2200", "glaccountno": "1000"},
					]
				},
			},
			{
				"journalid": "ARJ",
				"batchno": "8",
				"datecreated": date("2026-09-15"),
				"description": "AR invoice INV-1001",
				"gltransactionentries": {
					"glentry": [
						{"trtype": "debit", "amount": "4620", "glaccountno": "1100", "customerid": "C-NORTH"},
						{"trtype": "credit", "amount": "4620", "glaccountno": "4000"},
					]
				},
			},
		],
	}


def find(store: dict[str, list[Tree]], name: str, key: str, value: str) -> Tree:
	return next(record for record in store[name] if record[key] == value)


def store_v2() -> dict[str, list[Tree]]:
	store = copy.deepcopy(store_v1())
	find(store, "glaccount", "glaccountno", "6100").update(title="Software Subscriptions", whenmodified=LATER)
	find(store, "glaccount", "glaccountno", "6000").update(status="inactive", whenmodified=LATER)
	find(store, "customer", "customerid", "C-NORTH")["contactinfo"]["contact"]["phone1"] = "+1 613 555 0199"
	find(store, "employee", "employeeid", "E-02").update(
		status="inactive", enddate=date("2026-10-01"), whenmodified=LATER
	)
	find(store, "sotransaction", "key", "12").update(
		message="Deliver guides to reception", whenmodified=LATER
	)
	find(store, "invoice", "key", "32").update(state="Reversed", whenmodified=LATER)
	store["bill"].append(
		{
			"key": "42",
			"vendorid": "V-SOFT",
			"datecreated": date("2026-10-02"),
			"billno": "LL-9002",
			"totalamount": "90",
			"basecurr": "CAD",
			"currency": "CAD",
			"whenmodified": LATER,
			"state": "Posted",
			"billitems": {
				"lineitem": [{"line_num": "1", "glaccountno": "6100", "amount": "90", "memo": "Extra seat"}]
			},
			"retainagereleased": "false",
		}
	)
	return store


def element(tag: str, value: Any, dtd: Dtd) -> list[ET.Element]:
	"""A value as elements, each object's fields in its DTD content model's order."""
	if isinstance(value, list):
		return [e for item in value for e in element(tag, item, dtd)]
	node = ET.Element(tag)
	if isinstance(value, dict):
		order = dtd.order(tag)
		for key in sorted(value, key=lambda k: order.index(k) if k in order else len(order)):
			node.extend(element(key, value[key], dtd))
	else:
		node.text = str(value)
	return [node]


def response(object_: str, records: list[Tree], start: int, total: int, dtd: Dtd) -> str:
	root = ET.Element("response")
	control = ET.SubElement(root, "control")
	for tag, text in (
		("status", "success"),
		("senderid", INTACCT_SENDER),
		("controlid", gateway.CONTROL_ID),
		("uniqueid", "false"),
		("dtdversion", gateway.DTD_VERSION),
	):
		ET.SubElement(control, tag).text = text
	operation = ET.SubElement(root, "operation")
	authentication = ET.SubElement(operation, "authentication")
	for tag, text in (("status", "success"), ("userid", INTACCT_USER), ("companyid", INTACCT_COMPANY)):
		ET.SubElement(authentication, tag).text = text
	result = ET.SubElement(operation, "result")
	for tag, text in (("status", "success"), ("function", "get_list"), ("controlid", gateway.FUNCTION_ID)):
		ET.SubElement(result, tag).text = text
	end = start + len(records) - 1
	ET.SubElement(result, "listtype", start=str(start), end=str(end), total=str(total)).text = object_
	data = ET.SubElement(result, "data")
	for record in records:
		data.extend(element(object_, record, dtd))
	return ET.tostring(root, encoding="unicode")


def answers(store: dict[str, list[Tree]], dtd: Dtd) -> list[tuple[str, dict[str, Any]]]:
	found: list[tuple[str, dict[str, Any]]] = []
	listings = {listing.object: listing for listing in [*reader.LISTINGS.values(), *reader.PAYMENTS]}
	for object_, listing in listings.items():
		records = sorted(store[object_], key=lambda r: r[listing.key])
		request = {
			"method": "POST",
			"path": gateway.GATEWAY_PATH,
			"body": {"function": "get_list", "object": object_, "start": "0"},
		}
		found.append(
			(
				f"{object_}-2-list",
				{"request": request, "response_text": response(object_, records, 0, len(records), dtd)},
			)
		)
	north = find(store, "customer", "customerid", "C-NORTH")
	lookup = {
		"method": "POST",
		"path": gateway.GATEWAY_PATH,
		"body": {
			"function": "get_list",
			"object": "customer",
			"start": "0",
			"filter": [["customerid", "=", "C-NORTH"]],
		},
	}
	found.append(
		(
			"customer-1-lookup-C-NORTH",
			{"request": lookup, "response_text": response("customer", [north], 0, 1, dtd)},
		)
	)
	return found


def write(state: str, store: dict[str, list[Tree]], dtd: Dtd) -> None:
	target = FIXTURES / state
	shutil.rmtree(target, ignore_errors=True)
	target.mkdir(parents=True)
	for name, fixture in answers(store, dtd):
		(target / f"{name}.json").write_text(json.dumps(fixture, indent="\t", ensure_ascii=False) + "\n")


def main() -> int:
	dtd = INTACCT_RESPONSE.dtd()
	write("v1", store_v1(), dtd)
	write("v2", store_v2(), dtd)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
