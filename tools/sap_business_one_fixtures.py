# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the SAP Business One fixtures (tests/fixtures/sap_business_one/<state>/*.json): a small company's
Service Layer (``/b1s/v1``) answers, shaped as the OData v3 ``$metadata`` defines its entities (the
OpenAPI documents generated from it, pinned in tools/specs.py), for exactly the requests the reader sends
(built from the reader's own entity sets, so they can't drift): each set's listing in key order, the chart's
drawers, the business partner types an opportunity or a journal line needs, and the login. The company's
two states differ only by their entities (v2: a renamed account, an inactive warehouse, a frozen item, a
customer's new phone, a lead become a customer, an employee terminated, a changed sales order, a cancelled
invoice with its cancellation, and a new invoice).

``python -m tools.sap_business_one_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import re
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.sources.sap_business_one import reader
from tests.support.sap_business_one import B1_COMPANY_DB, B1_PASSWORD, B1_ROUTE, B1_SESSION, B1_USER

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "sap_business_one"
SERVICE = reader.SERVICE_PATH
METADATA = "https://b1.example.test:50000/b1s/v1/$metadata#"
UPDATED = "2026-09-01"
LATER = "2026-10-02"
Entity = dict[str, Any]


def account(code: str, name: str, father: str | None, kind: str = "at_Other", **fields: Any) -> Entity:
	body: Entity = {
		"Code": code,
		"Name": name,
		"FormatCode": code,
		"AccountType": kind,
		"ActiveAccount": "tNO" if father is None else "tYES",
		"AcctCurrency": "##",
		"CashAccount": "tNO",
		"FrozenFor": "tNO",
		"ValidFor": "tYES",
	}
	if father is not None:
		body["FatherAccountKey"] = father
	return body | fields


def address(
	card: str, name: str, kind: str, street: str, city: str, state: str, zip_: str, country: str
) -> Entity:
	return {
		"AddressName": name,
		"Street": street,
		"City": city,
		"State": state,
		"ZipCode": zip_,
		"Country": country,
		"AddressType": kind,
		"BPCode": card,
		"RowNum": 0 if kind == "bo_BillTo" else 1,
	}


def partner(card: str, name: str, card_type: str, **fields: Any) -> Entity:
	body: Entity = {
		"CardCode": card,
		"CardName": name,
		"CardType": card_type,
		"CompanyPrivate": "cCompany",
		"Currency": "CAD",
		"Valid": "tYES",
		"Frozen": "tNO",
		"UpdateDate": UPDATED,
		"UpdateTime": "09:30:00",
		"BPAddresses": [],
		"ContactEmployees": [],
	}
	return body | fields


def line(
	number: int,
	item: str | None,
	description: str,
	qty: float,
	price: float,
	total: float,
	tax: float,
	**fields: Any,
) -> Entity:
	body: Entity = {
		"LineNum": number,
		"ItemDescription": description,
		"Quantity": qty,
		"Price": price,
		"LineTotal": total,
		"RowTotalFC": 0.0,
		"TaxCode": "GST",
		"TaxTotal": tax,
		"NetTaxAmountFC": 0.0,
		"AccountCode": "4100",
	}
	if item is not None:
		body["ItemCode"] = item
	return body | fields


def document(
	entry: int,
	number: int,
	card: str,
	date: str,
	total: float,
	vat: float,
	lines: list[Entity],
	**fields: Any,
) -> Entity:
	body: Entity = {
		"DocEntry": entry,
		"DocNum": number,
		"DocType": "dDocument_Items",
		"DocDate": date,
		"DocDueDate": date,
		"CardCode": card,
		"DocCurrency": "CAD",
		"DocRate": 1.0,
		"DocTotal": total,
		"DocTotalFc": 0.0,
		"VatSum": vat,
		"VatSumFc": 0.0,
		"TotalDiscount": 0.0,
		"TotalDiscountFC": 0.0,
		"RoundingDiffAmount": 0.0,
		"RoundingDiffAmountFC": 0.0,
		"DownPaymentAmount": 0.0,
		"DownPaymentAmountFC": 0.0,
		"Cancelled": "tNO",
		"CancelStatus": "csNo",
		"DocumentStatus": "bost_Open",
		"UpdateDate": UPDATED,
		"UpdateTime": "10:00:00",
		"DocumentLines": lines,
		"DocumentAdditionalExpenses": [],
	}
	return body | fields


def store_v1() -> dict[str, list[Entity]]:
	freight = {
		"ExpenseCode": 1,
		"LineTotal": 10.0,
		"LineTotalFC": 0.0,
		"TaxCode": "GST",
		"TaxSum": 0.5,
		"TaxSumFC": 0.0,
		"Remarks": "Freight",
	}
	return {
		"ChartOfAccounts": [
			account("1", "Assets", None),
			account("1100", "Chequing", "1", CashAccount="tYES"),
			account("1200", "Accounts Receivable", "1"),
			account("2", "Liabilities", None),
			account("2100", "Accounts Payable", "2"),
			account("2200", "GST Payable", "2"),
			account("3", "Equity", None),
			account("3100", "Owner's Equity", "3"),
			account("4", "Revenues", None, "at_Revenues"),
			account("4100", "Sales", "4", "at_Revenues"),
			account("5", "Expenses", None, "at_Expenses"),
			account("5100", "Rent", "5", "at_Expenses"),
			account("5200", "Office Supplies", "5", "at_Expenses"),
		],
		"ProfitCenters": [
			{"CenterCode": "OPS", "CenterName": "Operations", "InWhichDimension": 1, "Active": "tYES"},
			{"CenterCode": "REGION-W", "CenterName": "West", "InWhichDimension": 2, "Active": "tYES"},
			{"CenterCode": "SALES", "CenterName": "Sales", "InWhichDimension": 1, "Active": "tYES"},
		],
		"Warehouses": [
			{
				"WarehouseCode": "01",
				"WarehouseName": "Main Warehouse",
				"Street": "200 Granville St",
				"City": "Vancouver",
				"State": "BC",
				"ZipCode": "V6C 1S4",
				"Country": "CA",
				"Inactive": "tNO",
			},
			{"WarehouseCode": "02", "WarehouseName": "Overflow Bay", "Inactive": "tNO"},
		],
		"ItemGroups": [{"Number": 100, "GroupName": "Mugs"}, {"Number": 101, "GroupName": "Services"}],
		"Items": [
			{
				"ItemCode": "A1000",
				"ItemName": "Trail Mug",
				"ItemsGroupCode": 100,
				"BarCode": "0627843001234",
				"User_Text": "Enamel camping mug, 350 ml",
				"InventoryItem": "tYES",
				"SalesItem": "tYES",
				"PurchaseItem": "tYES",
				"QuantityOnStock": 40.0,
				"Valid": "tYES",
				"Frozen": "tNO",
				"UpdateDate": UPDATED,
				"ItemPrices": [
					{"PriceList": 1, "Price": 18.0, "Currency": "CAD"},
					{"PriceList": 2, "Price": 15.0, "Currency": "CAD"},
				],
			},
			{
				"ItemCode": "K200",
				"ItemName": "Mug Set",
				"ItemsGroupCode": 100,
				"InventoryItem": "tNO",
				"SalesItem": "tYES",
				"PurchaseItem": "tNO",
				"QuantityOnStock": 0.0,
				"Valid": "tYES",
				"Frozen": "tNO",
				"UpdateDate": UPDATED,
				"ItemPrices": [{"PriceList": 1, "Price": 45.0, "Currency": "CAD"}],
			},
			{
				"ItemCode": "S100",
				"ItemName": "Installation",
				"ItemsGroupCode": 101,
				"InventoryItem": "tNO",
				"SalesItem": "tYES",
				"PurchaseItem": "tNO",
				"QuantityOnStock": 0.0,
				"Valid": "tYES",
				"Frozen": "tNO",
				"UpdateDate": UPDATED,
				"ItemPrices": [{"PriceList": 1, "Price": 95.0, "Currency": "CAD"}],
			},
		],
		"BusinessPartners": [
			partner(
				"C001",
				"Bayview Bistro",
				"cCustomer",
				EmailAddress="orders@bayview.example",
				Phone1="+1 604 555 0101",
				FederalTaxID="123456789RT0001",
				BPAddresses=[
					address(
						"C001",
						"Head office",
						"bo_BillTo",
						"12 Lakeshore Rd",
						"Kelowna",
						"BC",
						"V1Y 2A1",
						"CA",
					),
					address(
						"C001",
						"Kitchen door",
						"bo_ShipTo",
						"14 Lakeshore Rd",
						"Kelowna",
						"BC",
						"V1Y 2A1",
						"CA",
					),
				],
				ContactEmployees=[
					{
						"CardCode": "C001",
						"Name": "Lena Park",
						"FirstName": "Lena",
						"LastName": "Park",
						"Position": "Head Chef",
						"Phone1": "+1 250 555 0112",
						"E_Mail": "lena@bayview.example",
						"InternalCode": 11,
						"Active": "tYES",
					}
				],
			),
			partner(
				"C002",
				"Priya Nair",
				"cCustomer",
				CompanyPrivate="cPrivate",
				Currency="USD",
				EmailAddress="priya@example.com",
			),
			partner(
				"L001",
				"Summit Pilates",
				"cLid",
				EmailAddress="hello@summitpilates.example",
				Notes="Met at the spring trade show",
			),
			partner("L002", "Cedar Hollow Lodge", "cLid", EmailAddress="info@cedarhollow.example"),
			partner(
				"V001",
				"Coastal Paper Ltd",
				"cSupplier",
				EmailAddress="ap@coastalpaper.example",
				Phone1="+1 604 555 0201",
			),
		],
		"Departments": [{"Code": 1, "Name": "Sales"}, {"Code": 2, "Name": "Operations"}],
		"EmployeePosition": [
			{"PositionID": 1, "Name": "Sales Manager"},
			{"PositionID": 2, "Name": "Technician"},
		],
		"EmployeesInfo": [
			{
				"EmployeeID": 1,
				"FirstName": "Sam",
				"LastName": "Okoro",
				"Gender": "gt_Male",
				"DateOfBirth": "1985-06-15",
				"StartDate": "2021-03-01",
				"Department": 1,
				"Position": 1,
				"eMail": "sam@northwind.example",
				"MobilePhone": "+1 604 555 0100",
				"Active": "tYES",
				"ExternalEmployeeNumber": "E001",
				"UpdateDate": UPDATED,
			},
			{
				"EmployeeID": 2,
				"FirstName": "Robin",
				"LastName": "Vance",
				"Gender": "gt_Female",
				"DateOfBirth": "1992-11-02",
				"StartDate": "2023-01-09",
				"Department": 2,
				"Position": 2,
				"Manager": 1,
				"eMail": "robin@northwind.example",
				"Active": "tYES",
				"ExternalEmployeeNumber": "E002",
				"UpdateDate": UPDATED,
			},
		],
		"Projects": [
			{
				"Code": "P01",
				"Name": "Bayview Bistro Refit",
				"ValidFrom": "2026-08-01",
				"ValidTo": "2026-11-30",
				"Active": "tYES",
			}
		],
		"SalesOpportunities": [
			{
				"SequentialNo": 1,
				"CardCode": "L002",
				"OpportunityName": "Lodge mugs",
				"StartDate": "2026-09-11",
				"PredictedClosingDate": "2026-12-15",
				"MaxLocalTotal": 900.0,
				"ClosingPercentage": 40.0,
				"Status": "sos_Open",
				"Remarks": "Mugs for every cabin",
				"UpdateDate": UPDATED,
			}
		],
		"Quotations": [
			document(
				1,
				1001,
				"C001",
				"2026-09-10",
				37.8,
				1.8,
				[line(0, "A1000", "Trail Mug", 2.0, 18.0, 36.0, 1.8)],
			)
		],
		"Orders": [
			document(
				2,
				2001,
				"C001",
				"2026-09-12",
				148.05,
				7.05,
				[
					line(0, "A1000", "Trail Mug", 2.0, 18.0, 36.0, 1.8),
					line(1, "S100", "Installation", 1.0, 95.0, 95.0, 4.75),
				],
				DocumentAdditionalExpenses=[freight],
				DocDueDate="2026-09-30",
			)
		],
		"PurchaseOrders": [
			document(
				3,
				3001,
				"V001",
				"2026-09-08",
				90.0,
				0.0,
				[line(0, "A1000", "Trail Mug", 10.0, 9.0, 90.0, 0.0, AccountCode="5200")],
			)
		],
		"Invoices": [
			document(
				4,
				4001,
				"C001",
				"2026-09-15",
				126.0,
				6.0,
				[
					line(0, "A1000", "Trail Mug", 2.0, 18.0, 36.0, 1.8, CostingCode="SALES"),
					line(
						1, "S100", "Installation", 1.0, 95.0, 95.0, 4.2, CostingCode="OPS", ProjectCode="P01"
					),
				],
				TotalDiscount=11.0,
				NumAtCard="PO-7781",
				Comments="Thanks for your business",
				DocDueDate="2026-10-15",
			),
			document(
				5,
				4002,
				"C002",
				"2026-09-16",
				31.05,
				0.0,
				[line(0, "A1000", "Trail Mug", 1.0, 23.0, 31.05, 0.0, RowTotalFC=23.0, Currency="USD")],
				DocCurrency="USD",
				DocRate=1.35,
				DocTotalFc=23.0,
			),
		],
		"CreditNotes": [
			document(
				6,
				6001,
				"C001",
				"2026-09-18",
				18.9,
				0.9,
				[line(0, "A1000", "Trail Mug", 1.0, 18.0, 18.0, 0.9, BaseType=13, BaseEntry=4, BaseLine=0)],
				Comments="One mug arrived chipped",
			)
		],
		"PurchaseInvoices": [
			document(
				7,
				7001,
				"V001",
				"2026-09-14",
				120.75,
				5.75,
				[
					line(0, "A1000", "Trail Mug", 10.0, 9.0, 90.0, 4.5, AccountCode="5200"),
					line(
						1, None, "Packing paper", 1.0, 25.0, 25.0, 1.25, AccountCode="5200", CostingCode="OPS"
					),
				],
				NumAtCard="NP-3321",
			)
		],
		"PurchaseCreditNotes": [],
		"IncomingPayments": [
			{
				"DocEntry": 8,
				"DocNum": 8001,
				"DocType": "rCustomer",
				"DocDate": "2026-09-20",
				"CardCode": "C001",
				"DocCurrency": "CAD",
				"DocRate": 1.0,
				"CashSum": 0.0,
				"TransferSum": 107.1,
				"TransferAccount": "1100",
				"CounterReference": "EFT-7001",
				"Cancelled": "tNO",
				"CancelStatus": "csNo",
				"PaymentChecks": [],
				"PaymentCreditCards": [],
				"PaymentInvoices": [
					{"LineNum": 0, "DocEntry": 4, "SumApplied": 107.1, "InvoiceType": "it_Invoice"}
				],
			}
		],
		"VendorPayments": [
			{
				"DocEntry": 9,
				"DocNum": 9001,
				"DocType": "rSupplier",
				"DocDate": "2026-09-21",
				"CardCode": "V001",
				"DocCurrency": "CAD",
				"DocRate": 1.0,
				"CashSum": 0.0,
				"TransferSum": 0.0,
				"Cancelled": "tNO",
				"CancelStatus": "csNo",
				"PaymentChecks": [
					{
						"LineNum": 0,
						"CheckNumber": 1201,
						"CheckSum": 120.75,
						"CheckAccount": "1100",
						"Currency": "CAD",
					}
				],
				"PaymentCreditCards": [],
				"PaymentInvoices": [
					{"LineNum": 0, "DocEntry": 7, "SumApplied": 120.75, "InvoiceType": "it_PurchaseInvoice"}
				],
			}
		],
		"JournalEntries": [
			{
				"JdtNum": 10,
				"Number": 10,
				"ReferenceDate": "2026-09-30",
				"Memo": "September rent",
				"OriginalJournal": "ttJournalEntry",
				"JournalEntryLines": [
					{
						"Line_ID": 0,
						"AccountCode": "5100",
						"ShortName": "5100",
						"Debit": 1500.0,
						"Credit": 0.0,
						"CostingCode": "OPS",
					},
					{
						"Line_ID": 1,
						"AccountCode": "2100",
						"ShortName": "V001",
						"Debit": 0.0,
						"Credit": 1500.0,
					},
				],
			},
			{
				"JdtNum": 11,
				"Number": 11,
				"ReferenceDate": "2026-09-15",
				"Memo": "A/R Invoices - C001",
				"OriginalJournal": "ttARInvoice",
				"JournalEntryLines": [
					{"Line_ID": 0, "AccountCode": "1200", "ShortName": "C001", "Debit": 126.0, "Credit": 0.0},
					{"Line_ID": 1, "AccountCode": "4100", "ShortName": "4100", "Debit": 0.0, "Credit": 126.0},
				],
			},
		],
	}


def find(store: dict[str, list[Entity]], name: str, key: str, value: Any) -> Entity:
	return next(entity for entity in store[name] if entity[key] == value)


def store_v2() -> dict[str, list[Entity]]:
	store = copy.deepcopy(store_v1())
	find(store, "ChartOfAccounts", "Code", "5200")["Name"] = "Office Supplies & Postage"
	find(store, "Warehouses", "WarehouseCode", "02")["Inactive"] = "tYES"
	find(store, "Items", "ItemCode", "K200").update(Frozen="tYES", UpdateDate=LATER)
	find(store, "BusinessPartners", "CardCode", "C001").update(Phone1="+1 604 555 0199", UpdateDate=LATER)
	find(store, "BusinessPartners", "CardCode", "L001").update(CardType="cCustomer", UpdateDate=LATER)
	find(store, "EmployeesInfo", "EmployeeID", 2).update(
		Active="tNO", TerminationDate="2026-10-01", UpdateDate=LATER
	)
	find(store, "Orders", "DocEntry", 2).update(Comments="Deliver to the kitchen door", UpdateDate=LATER)
	find(store, "Invoices", "DocEntry", 5).update(Cancelled="tYES", CancelStatus="csYes", UpdateDate=LATER)
	cancellation = copy.deepcopy(find(store, "Invoices", "DocEntry", 5))
	cancellation.update(DocEntry=12, DocNum=4003, Cancelled="tNO", CancelStatus="csCancellation")
	store["Invoices"].append(cancellation)
	store["Invoices"].append(
		document(
			13,
			4004,
			"C001",
			"2026-10-02",
			94.5,
			4.5,
			[line(0, "S100", "Installation follow-up", 1.0, 90.0, 90.0, 4.5)],
			UpdateDate=LATER,
		)
	)
	return store


def answers(store: dict[str, list[Entity]]) -> list[tuple[str, dict[str, Any]]]:
	found: list[tuple[str, dict[str, Any]]] = []
	for kind, sets in reader.SETS.items():
		for entity_set in sets:
			entities = [
				entity
				for entity in store[entity_set.name]
				if not entity_set.condition or entity_set.condition == f"CardType eq '{entity['CardType']}'"
			]
			listing = {"odata.metadata": f"{METADATA}{entity_set.name}", "value": entities}
			query: dict[str, str] = {"$orderby": entity_set.key}
			if entity_set.condition:
				query["$filter"] = entity_set.condition
			name = f"{entity_set.name}-{kind.value}"
			found.append(
				(
					f"{name}-2-full",
					{
						"request": {"path": f"{SERVICE}/{entity_set.name}", "query": query},
						"response": listing,
					},
				)
			)
			if entity_set.condition and entity_set.updated:
				pattern = re.escape(entity_set.condition) + r" and UpdateDate ge '\d{4}-\d{2}-\d{2}'"
				found.append(
					(
						f"{name}-1-since",
						{
							"request": {
								"path": f"{SERVICE}/{entity_set.name}",
								"query": {"$orderby": entity_set.key},
								"query_patterns": {"$filter": pattern},
							},
							"response": listing,
						},
					)
				)
	drawers = [
		{"Code": a["Code"], "FatherAccountKey": a.get("FatherAccountKey")} for a in store["ChartOfAccounts"]
	]
	found.append(
		(
			"ChartOfAccounts-1-drawers",
			{
				"request": {
					"path": f"{SERVICE}/ChartOfAccounts",
					"query": {"$select": "Code,FatherAccountKey", "$orderby": "Code"},
				},
				"response": {
					"odata.metadata": f"{METADATA}ChartOfAccounts",
					"value": [{k: v for k, v in d.items() if v is not None} for d in drawers],
				},
			},
		)
	)
	for partner_ in store["BusinessPartners"]:
		found.append(
			(
				f"BusinessPartners-0-type-{partner_['CardCode']}",
				{
					"request": {
						"path": f"{SERVICE}/BusinessPartners",
						"query": {
							"$filter": f"CardCode eq '{partner_['CardCode']}'",
							"$select": "CardCode,CardType",
						},
					},
					"response": {
						"odata.metadata": f"{METADATA}BusinessPartners",
						"value": [{"CardCode": partner_["CardCode"], "CardType": partner_["CardType"]}],
					},
				},
			)
		)
	found.append(
		(
			"BusinessPartners-0-lookup-C001",
			{
				"request": {
					"path": f"{SERVICE}/BusinessPartners",
					"query": {"$filter": "CardCode eq 'C001'"},
				},
				"response": {
					"odata.metadata": f"{METADATA}BusinessPartners",
					"value": [find(store, "BusinessPartners", "CardCode", "C001")],
				},
			},
		)
	)
	found.append(
		(
			"Login",
			{
				"request": {
					"method": "POST",
					"path": f"{SERVICE}/Login",
					"body": {"CompanyDB": B1_COMPANY_DB, "UserName": B1_USER, "Password": B1_PASSWORD},
				},
				"headers": {
					"Set-Cookie": f"B1SESSION={B1_SESSION}; path=/b1s; HttpOnly, ROUTEID={B1_ROUTE}; path=/b1s"
				},
				"response": {
					"SessionId": B1_SESSION,
					"Version": "1000201",
					"SessionTimeout": 30,
				},
			},
		)
	)
	return found


def write(state: str, store: dict[str, list[Entity]]) -> None:
	target = FIXTURES / state
	shutil.rmtree(target, ignore_errors=True)
	target.mkdir(parents=True)
	for name, fixture in answers(store):
		(target / f"{name}.json").write_text(json.dumps(fixture, indent="\t", ensure_ascii=False) + "\n")


def main() -> int:
	write("v1", store_v1())
	write("v2", store_v2())
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
