# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the Dynamics 365 Sales fixtures (tests/fixtures/dynamics_sales/<state>/*.json): a small
environment's Dataverse Web API answers to the requests the adapter sends (its ``$select`` and ``$expand``
come from the reader's tables), shaped as tests/schemas/dynamics_sales.json (transcribed from Microsoft's
table reference) says Dataverse answers, in two states (v2: a price, a deactivated account, a won
opportunity, a cancelled order). The accounts list arrives in two pages (``@odata.nextLink``).

``python -m tools.dataverse_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.core.rest import query_string
from idempotent_importer.sources.dynamics_sales.reader import API_PATH, TABLES
from tests.support.dynamics import SALES_ORGANISATION

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "dynamics_sales"
CONTEXT = f"{SALES_ORGANISATION}{API_PATH}/$metadata"
LOOKUP = "@Microsoft.Dynamics.CRM.lookuplogicalname"
MODIFIED = "2026-03-01T09:00:00Z"
LATER = "2026-04-01T09:00:00Z"
NEXT_PAGE = "%3Ccookie%20pagenumber=%222%22%20/%3E"
CAD, USD = "c0000000-0000-4000-8000-0000000000ca", "c0000000-0000-4000-8000-0000000000a5"

type Row = dict[str, Any]


def guid(n: int) -> str:
	return f"d{n:07x}-0000-4000-8000-{n:012x}"


def row(**columns: Any) -> Row:
	return {"@odata.etag": 'W/"1001"', **columns}


def customer(table: str, n: int) -> dict[str, Any]:
	return {"_customerid_value": guid(n), f"_customerid_value{LOOKUP}": table}


def line(
	key: str,
	n: int,
	quantity: float,
	base: float,
	discount: float = 0,
	product: int | None = None,
	description: str | None = None,
	tax: float = 0,
) -> Row:
	return row(
		**{
			key: guid(n),
			"_productid_value": guid(product) if product else None,
			"productdescription": description,
			"isproductoverridden": product is None,
			"quantity": quantity,
			"priceperunit": base / quantity,
			"baseamount": base,
			"manualdiscountamount": discount,
			"volumediscountamount": 0,
			"tax": tax,
			"extendedamount": base - discount + tax,
			"sequencenumber": n % 10,
		}
	)


def document(
	key: str,
	number_field: str,
	n: int,
	number: str,
	state: int,
	party: dict[str, Any],
	lines: list[Row],
	**money: Any,
) -> Row:
	columns = {
		key: guid(n),
		number_field: number,
		"name": f"{number} for {party['_customerid_value'][-4:]}",
		**party,
		"_transactioncurrencyid_value": money.pop("currency", CAD),
		"exchangerate": money.pop("exchangerate", 1),
		"discountamount": money.pop("discountamount", 0),
		"discountpercentage": money.pop("discountpercentage", 0),
		"freightamount": money.pop("freightamount", 0),
		"description": None,
		"statecode": state,
		"createdon": money.pop("createdon"),
		"modifiedon": MODIFIED,
		**money,
	}
	return row(**columns) | {"lines": lines}


def store_v1() -> dict[str, list[Row]]:
	contoso_people = [
		row(
			contactid=guid(31),
			fullname="Yvonne McKay",
			jobtitle="Purchasing Manager",
			emailaddress1="yvonne@contoso.example",
			telephone1="+1 403 555 0131",
			statecode=0,
		)
	]
	return {
		"organizations": [
			row(organizationid=guid(1), name="Harbour Pine Trading", _basecurrencyid_value=CAD)
		],
		"transactioncurrencies": [
			row(transactioncurrencyid=CAD, isocurrencycode="CAD", currencyname="Canadian Dollar"),
			row(transactioncurrencyid=USD, isocurrencycode="USD", currencyname="US Dollar"),
		],
		"products": [
			row(
				productid=guid(11),
				name="Standing Desk",
				productnumber="SD-100",
				description="Electric standing desk",
				price=800,
				quantityonhand=6,
				producttypecode=1,
				productstructure=1,
				statecode=0,
				modifiedon=MODIFIED,
			),
			row(
				productid=guid(12),
				name="Installation",
				productnumber="SVC-INSTALL",
				description=None,
				price=150,
				quantityonhand=0,
				producttypecode=3,
				productstructure=1,
				statecode=0,
				modifiedon=MODIFIED,
			),
			row(
				productid=guid(13),
				name="Desk family",
				productnumber="FAM-DESK",
				description=None,
				price=None,
				quantityonhand=0,
				producttypecode=1,
				productstructure=2,
				statecode=0,
				modifiedon=MODIFIED,
			),
		],
		"accounts": [
			row(
				accountid=guid(21),
				name="Contoso Pharmaceuticals",
				accountnumber="CTSO-001",
				emailaddress1="ap@contoso.example",
				telephone1="+1 403 555 0121",
				websiteurl="https://contoso.example",
				address1_line1="1 Contoso Way",
				address1_line2=None,
				address1_city="Calgary",
				address1_stateorprovince="AB",
				address1_postalcode="T2P 1J9",
				address1_country="CA",
				statecode=0,
				_transactioncurrencyid_value=CAD,
				modifiedon=MODIFIED,
				contact_customer_accounts=contoso_people,
			),
			row(
				accountid=guid(22),
				name="Fourth Coffee",
				accountnumber="FC-002",
				emailaddress1="hello@fourthcoffee.example",
				telephone1=None,
				websiteurl=None,
				address1_line1="22 Bean Street",
				address1_line2=None,
				address1_city="Red Deer",
				address1_stateorprovince="AB",
				address1_postalcode="T4N 1A1",
				address1_country="Canada",
				statecode=0,
				_transactioncurrencyid_value=CAD,
				modifiedon=MODIFIED,
				contact_customer_accounts=[],
			),
		],
		"contacts": [
			row(
				contactid=guid(31),
				firstname="Yvonne",
				middlename=None,
				lastname="McKay",
				fullname="Yvonne McKay",
				jobtitle="Purchasing Manager",
				emailaddress1="yvonne@contoso.example",
				telephone1="+1 403 555 0131",
				mobilephone=None,
				address1_line1=None,
				address1_line2=None,
				address1_city=None,
				address1_stateorprovince=None,
				address1_postalcode=None,
				address1_country=None,
				_parentcustomerid_value=guid(21),
				statecode=0,
				modifiedon=MODIFIED,
			),
			row(
				contactid=guid(32),
				firstname="Susan",
				middlename=None,
				lastname="Burk",
				fullname="Susan Burk",
				jobtitle=None,
				emailaddress1="susan.burk@example.test",
				telephone1=None,
				mobilephone="+1 587 555 0132",
				address1_line1="9 Elm Street",
				address1_line2=None,
				address1_city="Edmonton",
				address1_stateorprovince="AB",
				address1_postalcode="T5J 0N3",
				address1_country="CA",
				_parentcustomerid_value=None,
				statecode=0,
				modifiedon=MODIFIED,
			),
		],
		"leads": [
			row(
				leadid=guid(41),
				subject="Interested in standing desks",
				firstname="Nancy",
				lastname="Anderson",
				companyname="Northwind Traders",
				jobtitle="Office Manager",
				emailaddress1="nancy@northwind.example",
				telephone1=None,
				mobilephone=None,
				websiteurl=None,
				description=None,
				statecode=0,
				modifiedon=MODIFIED,
			),
			row(
				leadid=guid(42),
				subject="Showroom visit",
				firstname="Rene",
				lastname="Valdes",
				companyname="Adventure Works",
				jobtitle=None,
				emailaddress1="rene@adventureworks.example",
				telephone1=None,
				mobilephone=None,
				websiteurl=None,
				description="Came to the showroom",
				statecode=1,
				modifiedon=MODIFIED,
			),
		],
		"opportunities": [
			row(
				opportunityid=guid(51),
				name="12 desks for Contoso",
				**customer("account", 21),
				_originatingleadid_value=None,
				estimatedvalue=9600,
				closeprobability=60,
				estimatedclosedate="2026-04-30",
				description=None,
				statecode=0,
				_transactioncurrencyid_value=CAD,
				createdon="2026-02-20T17:00:00Z",
				modifiedon=MODIFIED,
			),
			row(
				opportunityid=guid(52),
				name="Northwind fit-out",
				_customerid_value=None,
				_originatingleadid_value=guid(41),
				estimatedvalue=4000,
				closeprobability=20,
				estimatedclosedate=None,
				description=None,
				statecode=0,
				_transactioncurrencyid_value=CAD,
				createdon="2026-02-22T17:00:00Z",
				modifiedon=MODIFIED,
			),
		],
		"quotes": [
			document(
				"quoteid",
				"quotenumber",
				61,
				"QUO-01001",
				0,
				customer("account", 21),
				[line("quotedetailid", 62, 2, 1600, 100, product=11, description="Standing Desk", tax=75)],
				totallineitemamount=1500,
				totaldiscountamount=100,
				freightamount=50,
				totaltax=75,
				totalamount=1625,
				totalamount_base=1625,
				effectivefrom="2026-02-25",
				createdon="2026-02-25T17:00:00Z",
			)
		],
		"salesorders": [
			document(
				"salesorderid",
				"ordernumber",
				71,
				"ORD-01001",
				1,
				customer("account", 21),
				[
					line("salesorderdetailid", 72, 1, 800, product=11, description="Standing Desk"),
					line("salesorderdetailid", 73, 1, 100, description="Delivery setup"),
				],
				totallineitemamount=900,
				discountpercentage=10,
				totaldiscountamount=90,
				totaltax=40.5,
				totalamount=850.5,
				totalamount_base=850.5,
				datefulfilled=None,
				requestdeliveryby="2026-03-20",
				createdon="2026-03-02T17:00:00Z",
			)
		],
		"invoices": [
			document(
				"invoiceid",
				"invoicenumber",
				81,
				"INV-01001",
				0,
				customer("contact", 32),
				[line("invoicedetailid", 82, 1, 600, product=11, description="Standing Desk", tax=30)],
				currency=USD,
				exchangerate=0.7299270073,
				totallineitemamount=600,
				totaldiscountamount=0,
				totaltax=30,
				totalamount=630,
				totalamount_base=863.1,
				duedate="2026-04-05",
				createdon="2026-03-06T17:00:00Z",
			),
			document(
				"invoiceid",
				"invoicenumber",
				83,
				"INV-01002",
				2,
				customer("account", 21),
				[line("invoicedetailid", 84, 1, 150, product=12, description="Installation", tax=7.5)],
				totallineitemamount=150,
				totaldiscountamount=0,
				totaltax=7.5,
				totalamount=157.5,
				totalamount_base=157.5,
				duedate="2026-04-08",
				createdon="2026-03-09T17:00:00Z",
			),
		],
	}


def store_v2() -> dict[str, list[Row]]:
	db = copy.deepcopy(store_v1())

	def find(entity_set: str, n: int) -> Row:
		return next(item for item in db[entity_set] if guid(n) in item.values())

	find("products", 11).update(price=850, modifiedon=LATER)
	find("accounts", 22).update(statecode=1, modifiedon=LATER)
	find("opportunities", 51).update(statecode=1, closeprobability=100, modifiedon=LATER)
	find("salesorders", 71).update(statecode=2, modifiedon=LATER)
	return db


def query_of(entity_set: str) -> dict[str, str]:
	table = next(table for tables in TABLES.values() for table in tables if table.entity_set == entity_set)
	query = {"$select": ",".join(table.columns)}
	if table.expand:
		query["$expand"] = table.expand
	return query


SUPPORT_QUERIES = {
	"organizations": {"$select": "organizationid,name,_basecurrencyid_value"},
	"transactioncurrencies": {"$select": "transactioncurrencyid,isocurrencycode,currencyname"},
}
NAVIGATION = {"quotes": "quote_details", "salesorders": "order_details", "invoices": "invoice_details"}


def answers(db: dict[str, list[Row]]) -> list[tuple[str, dict[str, str], Any]]:
	found: list[tuple[str, dict[str, str], Any]] = []
	for entity_set, rows in db.items():
		listed = []
		for item in rows:
			shaped = {key: value for key, value in item.items() if key != "lines"}
			if entity_set in NAVIGATION:
				shaped[NAVIGATION[entity_set]] = item["lines"]
			listed.append(shaped)
		query = SUPPORT_QUERIES.get(entity_set) or query_of(entity_set)
		path = f"{API_PATH}/{entity_set}"
		context = f"{CONTEXT}#{entity_set}"
		if entity_set == "accounts":
			following = f"{SALES_ORGANISATION}{path}{query_string(query)}&$skiptoken={NEXT_PAGE}"
			found.append(
				(
					path,
					{"$skiptoken": '<cookie pagenumber="2" />'},
					{"@odata.context": context, "value": listed[1:]},
				)
			)
			found.append(
				(path, query, {"@odata.context": context, "value": listed[:1], "@odata.nextLink": following})
			)
			continue
		found.append((path, query, {"@odata.context": context, "value": listed}))
	return found


def write(state: str, db: dict[str, list[Row]]) -> None:
	folder = FIXTURES / state
	shutil.rmtree(folder, ignore_errors=True)
	folder.mkdir(parents=True)
	for index, (path, query, response) in enumerate(answers(db), 1):
		fixture = {"request": {"path": path, "query": query}, "response": response}
		(folder / f"{index:03d}-{path.rsplit('/', 1)[-1]}.json").write_text(
			json.dumps(fixture, indent="\t", ensure_ascii=False) + "\n"
		)


def main() -> int:
	write("v1", store_v1())
	write("v2", store_v2())
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
