# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the Business Central fixtures (tests/fixtures/dynamics_business_central/<state>/*.json): a small
company's API v2.0 answers, shaped as tests/schemas/dynamics_business_central.json (transcribed from
Microsoft's API (v2.0) reference) says Business Central answers, in two states (v2: a price, a blocked
customer, a terminated employee, a posted draft invoice, a won opportunity and an order fully invoiced, which
Business Central deletes). The customers list arrives in two pages, as the server pages a list it is asked
for in smaller pages (``@odata.nextLink``).

``python -m tools.bc_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from tests.support.dynamics import BC_COMPANY, BC_ENVIRONMENT_PATH

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "dynamics_business_central"
API = f"{BC_ENVIRONMENT_PATH}/api/v2.0"
COMPANY = f"{API}/companies({BC_COMPANY})"
HOST = "https://api.businesscentral.dynamics.com"
CONTEXT = f"{HOST}{API}/$metadata#companies({BC_COMPANY})"
MODIFIED = "2026-03-01T09:00:00Z"
LATER = "2026-04-01T09:00:00Z"
BLANK_DATE = "0001-01-01"
NEXT_PAGE = "eyJza2lwIjoxfQ"

type Entity = dict[str, Any]


def guid(n: int) -> str:
	return f"{n:08x}-0000-4000-8000-{n:012x}"


def entity(n: int, **fields: Any) -> Entity:
	return {
		"@odata.etag": f'W/"JzQ0O{n}"',
		"id": guid(n),
		**fields,
		"lastModifiedDateTime": fields.get("lastModifiedDateTime", MODIFIED),
	}


def account(n: int, number: str, name: str, category: str, sub: str, kind: str = "Posting") -> Entity:
	return entity(
		n,
		number=number,
		displayName=name,
		category=category,
		subCategory=sub,
		blocked=False,
		accountType=kind,
		directPosting=kind == "Posting",
		netChange=0,
		consolidationTranslationMethod="Average_x0020_Rate_x0020__x0028_Manual_x0029_",
		consolidationDebitAccount="",
		consolidationCreditAccount="",
		excludeFromConsolidation=False,
	)


def line(n: int, document: int, kind: str, quantity: float, net: float, tax: float, **fields: Any) -> Entity:
	"""A document line (lines carry no modification time; a quote's line no invoice discount share)."""
	row = entity(
		n,
		documentId=guid(document),
		sequence=10000 * (n % 10 + 1),
		lineType=kind,
		lineObjectNumber=fields.pop("lineObjectNumber", ""),
		description=fields.pop("description", ""),
		quantity=quantity,
		discountAmount=0,
		discountPercent=0,
		amountExcludingTax=net,
		invoiceDiscountAllocation=0,
		netAmount=net,
		netTaxAmount=tax,
		netAmountIncludingTax=net + tax,
		**fields,
	)
	del row["lastModifiedDateTime"]
	return row


def quote_line(*args: Any, **fields: Any) -> Entity:
	row = line(*args, **fields)
	del row["invoiceDiscountAllocation"]
	return row


def document(
	n: int,
	number: str,
	status: str,
	party: dict[str, Any],
	lines: list[Entity],
	total: float,
	tax: float,
	**fields: Any,
) -> Entity:
	return entity(
		n,
		number=number,
		status=status,
		currencyCode="",
		totalAmountExcludingTax=total - tax,
		totalTaxAmount=tax,
		totalAmountIncludingTax=total,
		**party,
		**fields,
	) | {"lines": lines}


ADATUM = {"customerId": guid(301), "customerNumber": "10000", "customerName": "Adatum Corporation"}
FABRIKAM = {"vendorId": guid(311), "vendorNumber": "30000", "vendorName": "Fabrikam, Inc."}
DESK = {"itemId": guid(201), "lineObjectNumber": "1896-S", "description": "ATHENS Desk"}


def store_v1() -> dict[str, list[Entity]]:
	return {
		"companyInformation": [
			entity(1, displayName="Harbour Pine Trading", currencyCode="CAD", country="CA", city="Edmonton")
		],
		"accounts": [
			account(101, "10000", "BALANCE SHEET", "Assets", "", "Heading"),
			account(102, "10100", "Checking account", "Assets", "Cash"),
			account(103, "13100", "Customers Domestic", "Assets", "Accounts Receivable"),
			account(104, "22000", "Sales Tax Payable", "Liabilities", "Current Liabilities"),
			account(105, "40100", "Installation Income", "Income", "Income, Services"),
			account(106, "50100", "Cost of Materials", "CostOfGoodsSold", "Cost of Goods Sold"),
			account(107, "60100", "Rent", "Expense", "Rent Expense"),
		],
		"dimensions": [entity(111, code="DEPARTMENT", displayName="Department")],
		"dimensionValues": [
			entity(112, code="ADM", dimensionId=guid(111), displayName="Administration"),
			entity(113, code="SALES", dimensionId=guid(111), displayName="Sales"),
		],
		"locations": [
			entity(
				121,
				code="MAIN",
				displayName="Main Warehouse",
				contact="",
				addressLine1="100 Warehouse Road",
				addressLine2="",
				city="Edmonton",
				state="AB",
				country="CA",
				postalCode="T5J 0N3",
				phoneNumber="",
				email="",
				website="",
			)
		],
		"itemCategories": [entity(131, code="FURNITURE", displayName="Office Furniture")],
		"items": [
			entity(
				201,
				number="1896-S",
				displayName="ATHENS Desk",
				type="Inventory",
				itemCategoryId=guid(131),
				itemCategoryCode="FURNITURE",
				blocked=False,
				gtin="0601234567890",
				inventory=4,
				unitPrice=1000,
				priceIncludesTax=False,
				unitCost=600,
			)
			| {"itemVariants": []},
			entity(
				202,
				number="1900-S",
				displayName="PARIS Guest Chair",
				type="Inventory",
				itemCategoryId=guid(131),
				itemCategoryCode="FURNITURE",
				blocked=False,
				gtin="",
				inventory=10,
				unitPrice=190,
				priceIncludesTax=False,
				unitCost=95,
			)
			| {
				"itemVariants": [
					entity(203, itemId=guid(202), itemNumber="1900-S", code="BLACK", description="Black"),
					entity(204, itemId=guid(202), itemNumber="1900-S", code="RED", description="Red"),
				]
			},
			entity(
				205,
				number="SERVICE",
				displayName="Installation",
				type="Service",
				itemCategoryId="00000000-0000-0000-0000-000000000000",
				itemCategoryCode="",
				blocked=False,
				gtin="",
				inventory=0,
				unitPrice=100,
				priceIncludesTax=False,
				unitCost=0,
			)
			| {"itemVariants": []},
		],
		"customers": [
			entity(
				301,
				number="10000",
				displayName="Adatum Corporation",
				type="Company",
				addressLine1="192 Market Square",
				addressLine2="",
				city="Calgary",
				state="AB",
				country="CA",
				postalCode="T2P 1J9",
				phoneNumber="+1 403 555 0100",
				email="robert.townes@adatum.example",
				website="",
				taxRegistrationNumber="CA123456789",
				currencyCode="",
				blocked=" ",
				balanceDue=0,
				creditLimit=0,
				taxLiable=True,
			),
			entity(
				302,
				number="20000",
				displayName="Trey Research",
				type="Company",
				addressLine1="153 Thomas Drive",
				addressLine2="",
				city="Red Deer",
				state="AB",
				country="CA",
				postalCode="T4N 1A1",
				phoneNumber="",
				email="helen.ray@treyresearch.example",
				website="",
				taxRegistrationNumber="",
				currencyCode="",
				blocked=" ",
				balanceDue=0,
				creditLimit=0,
				taxLiable=True,
			),
		],
		"vendors": [
			entity(
				311,
				number="30000",
				displayName="Fabrikam, Inc.",
				addressLine1="51 Radcliff Street",
				addressLine2="",
				city="Edmonton",
				state="AB",
				country="CA",
				postalCode="T5K 2M3",
				phoneNumber="",
				email="sales@fabrikam.example",
				website="",
				taxRegistrationNumber="",
				currencyCode="",
				blocked=" ",
				balance=0,
				taxLiable=True,
			)
		],
		"employees": [
			entity(
				321,
				number="AH",
				displayName="Alicia Hansen",
				givenName="Alicia",
				middleName="",
				surname="Hansen",
				jobTitle="Sales Manager",
				addressLine1="",
				addressLine2="",
				city="",
				state="",
				country="",
				postalCode="",
				phoneNumber="",
				mobilePhone="+1 780 555 0102",
				email="alicia@harbourpine.example",
				personalEmail="",
				employmentDate="2021-04-12",
				terminationDate=BLANK_DATE,
				status="Active",
				birthDate=BLANK_DATE,
			)
		],
		"projects": [entity(331, number="JOB00010", displayName="Office fit-out")],
		"contacts": [
			entity(
				341,
				number="CT000001",
				type="Company",
				displayName="Adatum Corporation",
				companyNumber="CT000001",
				companyName="Adatum Corporation",
				jobTitle="",
				addressLine1="192 Market Square",
				addressLine2="",
				city="Calgary",
				state="AB",
				country="CA",
				postalCode="T2P 1J9",
				phoneNumber="+1 403 555 0100",
				mobilePhoneNumber="",
				email="robert.townes@adatum.example",
				website="",
				searchName="ADATUM CORPORATION",
				privacyBlocked=False,
				taxRegistrationNumber="",
				contactBusinessRelation="Customer",
			),
			entity(
				342,
				number="CT000002",
				type="Person",
				displayName="Robin Lead",
				companyNumber="",
				companyName="Northwind Interiors",
				jobTitle="Office Manager",
				addressLine1="",
				addressLine2="",
				city="",
				state="",
				country="",
				postalCode="",
				phoneNumber="",
				mobilePhoneNumber="+1 587 555 0144",
				email="robin@northwind.example",
				website="",
				searchName="ROBIN LEAD",
				privacyBlocked=False,
				taxRegistrationNumber="",
				contactBusinessRelation="None",
			),
		],
		"opportunities": [
			entity(
				351,
				number="OP000001",
				contactNumber="CT000002",
				contactName="Robin Lead",
				contactCompanyName="Northwind Interiors",
				salespersonCode="AH",
				description="Desks for the new office",
				status="In Progress",
				closed=False,
				creationDate="2026-02-20",
				dateClosed=BLANK_DATE,
				calculatedCurrentValue=2500,
				chancesOfSuccessPrc=50,
				completedPrc=40,
				estimatedValue=5000,
				estimatedClosingDate="2026-04-30",
			)
		],
		"salesQuotes": [
			document(
				401,
				"S-QUO1001",
				"Draft",
				ADATUM,
				[quote_line(402, 401, "Item", 2, 2000, 100, **DESK)],
				2100,
				100,
				documentDate="2026-02-25",
				postingDate="2026-02-25",
				validUntilDate="2026-03-25",
			)
		],
		"salesOrders": [
			document(
				411,
				"S-ORD1001",
				"Open",
				ADATUM,
				[
					line(412, 411, "Item", 1, 1000, 50, **DESK),
					line(413, 411, "Comment", 0, 0, 0, description="Deliver to the loading dock"),
				],
				1050,
				50,
				orderDate="2026-03-02",
				postingDate="2026-03-02",
				requestedDeliveryDate="2026-03-20",
				externalDocumentNumber="AD-PO-77",
			)
		],
		"salesInvoices": [
			document(
				421,
				"S-INV1001",
				"Paid",
				ADATUM,
				[
					line(422, 421, "Item", 1, 1000, 50, **DESK),
					line(
						423,
						421,
						"Account",
						1,
						100,
						5,
						accountId=guid(105),
						lineObjectNumber="40100",
						description="Installation",
					),
				],
				1155,
				55,
				invoiceDate="2026-03-05",
				postingDate="2026-03-05",
				dueDate="2026-04-04",
				externalDocumentNumber="AD-PO-77",
				remainingAmount=0,
			),
			document(
				431,
				"S-INV1002",
				"Draft",
				ADATUM,
				[
					line(
						432,
						431,
						"Item",
						2,
						380,
						19,
						itemId=guid(202),
						itemVariantId=guid(204),
						lineObjectNumber="1900-S",
						description="PARIS Guest Chair, red",
					)
				],
				399,
				19,
				invoiceDate="2026-03-28",
				postingDate="2026-03-28",
				dueDate="2026-04-27",
				externalDocumentNumber="",
				remainingAmount=399,
			),
		],
		"salesCreditMemos": [
			document(
				441,
				"S-CR1001",
				"Open",
				ADATUM,
				[
					line(
						442,
						441,
						"Account",
						1,
						100,
						5,
						accountId=guid(105),
						lineObjectNumber="40100",
						description="Installation refund",
					)
				],
				105,
				5,
				creditMemoDate="2026-03-08",
				postingDate="2026-03-08",
				dueDate="2026-03-08",
				invoiceId=guid(421),
				invoiceNumber="S-INV1001",
				externalDocumentNumber="",
			)
		],
		"purchaseOrders": [
			document(
				451,
				"P-ORD1001",
				"Open",
				FABRIKAM,
				[line(452, 451, "Item", 5, 3000, 150, **DESK)],
				3150,
				150,
				orderDate="2026-02-27",
				postingDate="2026-02-27",
				requestedReceiptDate="2026-03-06",
			)
		],
		"purchaseInvoices": [
			document(
				461,
				"P-INV1001",
				"Open",
				FABRIKAM,
				[line(462, 461, "Item", 5, 3000, 150, **DESK)],
				3150,
				150,
				invoiceDate="2026-03-06",
				postingDate="2026-03-06",
				dueDate="2026-04-05",
				vendorInvoiceNumber="FAB-9001",
			)
		],
		"purchaseCreditMemos": [],
	}


def store_v2() -> dict[str, list[Entity]]:
	db = copy.deepcopy(store_v1())

	def find(entity_set: str, n: int) -> Entity:
		return next(e for e in db[entity_set] if e["id"] == guid(n))

	find("items", 201).update(unitPrice=1050, lastModifiedDateTime=LATER)
	find("customers", 302).update(blocked="All", lastModifiedDateTime=LATER)
	find("employees", 321).update(
		status="Terminated", terminationDate="2026-03-31", lastModifiedDateTime=LATER
	)
	find("salesInvoices", 431).update(status="Open", number="S-INV1002", lastModifiedDateTime=LATER)
	find("opportunities", 351).update(
		status="Won", closed=True, dateClosed="2026-03-30", lastModifiedDateTime=LATER
	)
	db["salesOrders"] = []
	return db


LINE_SETS = {
	"salesQuotes": "salesQuoteLines",
	"salesOrders": "salesOrderLines",
	"salesInvoices": "salesInvoiceLines",
	"salesCreditMemos": "salesCreditMemoLines",
	"purchaseOrders": "purchaseOrderLines",
	"purchaseInvoices": "purchaseInvoiceLines",
	"purchaseCreditMemos": "purchaseCreditMemoLines",
}
EXPANDED = {**LINE_SETS, "items": "itemVariants"}


def answers(db: dict[str, list[Entity]]) -> list[tuple[str, dict[str, str], Any]]:
	"""Each list's answer (the customers in two pages), and the contacts' numbers an opportunity looks up."""
	found: list[tuple[str, dict[str, str], Any]] = []
	for entity_set, entities in db.items():
		rows = []
		for item in entities:
			row = {key: value for key, value in item.items() if key != "lines"}
			if entity_set in LINE_SETS:
				row[LINE_SETS[entity_set]] = item["lines"]
			rows.append(row)
		query = {"$expand": EXPANDED[entity_set]} if entity_set in EXPANDED else {}
		context = f"{CONTEXT}/{entity_set}"
		if entity_set == "customers":
			following = f"{HOST}{COMPANY}/customers?$skiptoken={NEXT_PAGE}"
			found.append(
				(
					f"{COMPANY}/customers",
					{"$skiptoken": NEXT_PAGE},
					{"@odata.context": context, "value": rows[1:]},
				)
			)
			found.append(
				(
					f"{COMPANY}/customers",
					query,
					{"@odata.context": context, "value": rows[:1], "@odata.nextLink": following},
				)
			)
			continue
		if entity_set == "contacts":
			found.append(
				(
					f"{COMPANY}/contacts",
					{"$select": "id,number"},
					{
						"@odata.context": context,
						"value": [
							{"@odata.etag": row["@odata.etag"], "id": row["id"], "number": row["number"]}
							for row in rows
						],
					},
				)
			)
		found.append((f"{COMPANY}/{entity_set}", query, {"@odata.context": context, "value": rows}))
	return found


def write(state: str, db: dict[str, list[Entity]]) -> None:
	folder = FIXTURES / state
	shutil.rmtree(folder, ignore_errors=True)
	folder.mkdir(parents=True)
	for index, (path, query, response) in enumerate(answers(db), 1):
		request: dict[str, Any] = {"path": path}
		if query:
			request["query"] = query
		name = path.rsplit("/", 1)[-1]
		fixture = {"request": request, "response": response}
		(folder / f"{index:03d}-{name}.json").write_text(
			json.dumps(fixture, indent="\t", ensure_ascii=False) + "\n"
		)


def main() -> int:
	write("v1", store_v1())
	write("v2", store_v2())
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
