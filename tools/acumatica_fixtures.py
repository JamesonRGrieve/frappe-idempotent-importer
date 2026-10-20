# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the Acumatica fixtures (tests/fixtures/acumatica/<state>/*.json): a small tenant's answers on the
Default endpoint 24.200.001, shaped as tests/schemas/acumatica.json (transcribed from the official REST
client's generated entities) says the API answers, for exactly the requests the reader sends (built from
the reader's own entities): each entity's first page with its expansions, the customers' currencies, the
ledger's currency, and the lookups by key. The tenant's two states differ only by their records (v2: a
renamed and an inactive account, an inactive warehouse, an item marked for deletion, a customer's new
phone, a lead converted, an employee gone inactive, a changed purchase order, a voided invoice, a new invoice).

``python -m tools.acumatica_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from idempotent_importer.sources.acumatica import reader

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "acumatica"
API = reader.ENDPOINT_PATH
MODIFIED = "2026-09-01T12:00:00+00:00"
LATER = "2026-10-02T12:00:00+00:00"
Record = dict[str, Any]


def guid(n: int) -> str:
	return str(uuid.UUID(int=n))


def wrap(fields: dict[str, Any]) -> Record:
	"""Fields as the API sends them: each ``{"value": …}``, linked entities and lists as they are."""
	return {
		name: field if isinstance(field, dict | list) else {"value": field} for name, field in fields.items()
	}


def entity(n: int, **fields: Any) -> Record:
	return {"id": guid(n), "rowNumber": 1, "note": {"value": ""}, **wrap(fields), "custom": {}, "_links": {}}


def detail(n: int, row: int, **fields: Any) -> Record:
	return {"id": guid(n), "rowNumber": row, "note": {"value": ""}, **wrap(fields), "custom": {}}


def address(line1: str, city: str, state: str, postal: str, country: str) -> Record:
	return detail(
		9000 + len(line1), 1, AddressLine1=line1, City=city, State=state, PostalCode=postal, Country=country
	)


def contact(n: int, **fields: Any) -> Record:
	return detail(n, 1, **fields)


def line(
	n: int,
	row: int,
	item: str | None,
	description: str,
	qty: float,
	price: float,
	amount: float,
	**fields: Any,
) -> Record:
	values: dict[str, Any] = {
		"LineNbr": row,
		"Qty": qty,
		"UnitPrice": price,
		"Amount": amount,
		"TransactionDescription": description,
	}
	if item is not None:
		values["InventoryID"] = item
	return detail(n, row, **values, **fields)


def tax(n: int, tax_id: str, amount: float) -> Record:
	return detail(n, 1, TaxID=tax_id, TaxAmount=amount)


def store_v1() -> dict[str, list[Record]]:
	return {
		"Account": [
			entity(
				1,
				AccountCD="10100",
				Description="Operating Bank",
				Type="Asset",
				Active=True,
				CashAccount=True,
				CurrencyID="CAD",
				LastModifiedDateTime=MODIFIED,
			),
			entity(
				2,
				AccountCD="11000",
				Description="Accounts Receivable",
				Type="Asset",
				Active=True,
				CashAccount=False,
				CurrencyID="CAD",
				LastModifiedDateTime=MODIFIED,
			),
			entity(
				3,
				AccountCD="20000",
				Description="Accounts Payable",
				Type="Liability",
				Active=True,
				CashAccount=False,
				LastModifiedDateTime=MODIFIED,
			),
			entity(
				4,
				AccountCD="40000",
				Description="Sales Revenue",
				Type="Income",
				Active=True,
				CashAccount=False,
				LastModifiedDateTime=MODIFIED,
			),
			entity(
				5,
				AccountCD="60000",
				Description="Rent Expense",
				Type="Expense",
				Active=True,
				CashAccount=False,
				LastModifiedDateTime=MODIFIED,
			),
			entity(
				6,
				AccountCD="61000",
				Description="Office Supplies",
				Type="Expense",
				Active=True,
				CashAccount=False,
				LastModifiedDateTime=MODIFIED,
			),
		],
		"Subaccount": [
			entity(11, SubaccountCD="000-000", Description="Default", Active=True),
			entity(12, SubaccountCD="OPS-000", Description="Operations", Active=True),
		],
		"Warehouse": [
			entity(
				21,
				WarehouseID="MAIN",
				Description="Main Warehouse",
				Active=True,
				LastModifiedDateTime=MODIFIED,
			),
			entity(
				22,
				WarehouseID="RETAIL",
				Description="Retail Store",
				Active=True,
				LastModifiedDateTime=MODIFIED,
			),
		],
		"ItemClass": [entity(31, ClassID="BOOTS", Description="Boots", LastModifiedDateTime=MODIFIED)],
		"StockItem": [
			entity(
				41,
				InventoryID="HIKER-BOOT",
				Description="Hiker Boot",
				ItemClass="BOOTS",
				ItemStatus="Active",
				DefaultPrice=120.0,
				LastModifiedDateTime=MODIFIED,
				WarehouseDetails=[
					detail(411, 1, WarehouseID="MAIN", QtyOnHand=12.0),
					detail(412, 2, WarehouseID="RETAIL", QtyOnHand=3.0),
				],
			),
			entity(
				42,
				InventoryID="TRAIL-SOCK",
				Description="Trail Sock",
				ItemClass="BOOTS",
				ItemStatus="Active",
				DefaultPrice=15.0,
				LastModifiedDateTime=MODIFIED,
				WarehouseDetails=[],
			),
		],
		"NonStockItem": [
			entity(
				43,
				InventoryID="FITTING",
				Description="Boot Fitting",
				ItemStatus="Active",
				DefaultPrice=40.0,
				LastModifiedDateTime=MODIFIED,
			)
		],
		"Customer": [
			entity(
				51,
				CustomerID="ALPINE",
				CustomerName="Alpine Outfitters",
				CustomerCategory="Organization",
				Status="Active",
				CurrencyID="CAD",
				EnableCurrencyOverride=False,
				TaxRegistrationID="987654321RT0001",
				LastModifiedDateTime=MODIFIED,
				MainContact=contact(
					511,
					Email="buying@alpine.example",
					Phone1="+1 403 555 0101",
					Address=address("400 Bow Valley Trail", "Canmore", "AB", "T1W 1N9", "CA"),
				),
				BillingContact=contact(
					512,
					Email="ap@alpine.example",
					Address=address("400 Bow Valley Trail", "Canmore", "AB", "T1W 1N9", "CA"),
				),
				ShippingContact=contact(
					513, Address=address("12 Railway Ave", "Canmore", "AB", "T1W 1P1", "CA")
				),
				Contacts=[
					detail(
						514,
						1,
						ContactID=101,
						Contact=contact(
							515,
							ContactID=101,
							DisplayName="Noor Haddad",
							Email="noor@alpine.example",
							Phone1="+1 403 555 0102",
							JobTitle="Buyer",
							Active=True,
						),
					),
				],
			),
			entity(
				52,
				CustomerID="RIVERS",
				CustomerName="Jun Rivers",
				CustomerCategory="Individual",
				Status="Active",
				CurrencyID="USD",
				EnableCurrencyOverride=False,
				LastModifiedDateTime=MODIFIED,
				MainContact=contact(521, Email="jun@example.com"),
				Contacts=[],
			),
		],
		"Vendor": [
			entity(
				61,
				VendorID="LEATHERCO",
				VendorName="Prairie Leather Co",
				Status="Active",
				CurrencyID="CAD",
				TaxRegistrationID="111222333RT0001",
				LastModifiedDateTime=MODIFIED,
				MainContact=contact(
					611,
					Email="sales@prairieleather.example",
					Phone1="+1 306 555 0199",
					Address=address("9 Tannery Rd", "Saskatoon", "SK", "S7K 1A1", "CA"),
				),
				Contacts=[],
			),
		],
		"Employee": [
			entity(
				71,
				EmployeeID="EP001",
				EmployeeName="Mara Quinn",
				Status="Active",
				LastModifiedDateTime=MODIFIED,
				ContactInfo=contact(
					711,
					FirstName="Mara",
					LastName="Quinn",
					Email="mara@summitgear.example",
					Phone1="+1 403 555 0110",
					JobTitle="Store Manager",
					Gender="Female",
					DateOfBirth="1984-02-11T00:00:00+00:00",
				),
				EmployeeSettings=detail(712, 1, DepartmentID="RETAIL"),
			),
			entity(
				72,
				EmployeeID="EP002",
				EmployeeName="Theo Grant",
				Status="Active",
				LastModifiedDateTime=MODIFIED,
				ContactInfo=contact(
					721,
					FirstName="Theo",
					LastName="Grant",
					Email="theo@summitgear.example",
					JobTitle="Fitter",
				),
				EmployeeSettings=detail(722, 1, DepartmentID="RETAIL", ReportsTo="EP001"),
			),
		],
		"Project": [
			entity(
				81,
				ProjectID="FITOUT",
				Description="Canmore Store Fit-out",
				Customer="ALPINE",
				Status="Active",
				LastModifiedDateTime=MODIFIED,
			)
		],
		"Lead": [
			entity(
				91,
				LeadID=201,
				FirstName="Ivy",
				LastName="Moss",
				CompanyName="Moss Guiding",
				Email="ivy@mossguiding.example",
				Phone1="+1 403 555 0120",
				Status="Open",
				Active=True,
				Description="Asked about group boot fittings",
				LastModifiedDateTime=MODIFIED,
			),
			entity(
				92,
				LeadID=202,
				CompanyName="Ridge Runners Club",
				Email="hello@ridgerunners.example",
				Status="New",
				Active=True,
				LastModifiedDateTime=MODIFIED,
			),
		],
		"Opportunity": [
			entity(
				101,
				OpportunityID="OP000001",
				Subject="Club boots",
				ContactID=202,
				Status="Open",
				Stage="Prospect",
				Estimation="2026-12-01T00:00:00+00:00",
				CurrencyID="CAD",
				Total=2400.0,
				LastModifiedDateTime=MODIFIED,
			),
		],
		"SalesOrder": [
			entity(
				111,
				OrderType="QT",
				OrderNbr="QT000001",
				CustomerID="ALPINE",
				Date="2026-09-10T00:00:00+00:00",
				Status="Open",
				CurrencyID="CAD",
				CurrencyRate=1.0,
				OrderTotal=252.0,
				Description="Spring boots",
				LastModified=MODIFIED,
				Details=[
					detail(
						1111,
						1,
						LineNbr=1,
						InventoryID="HIKER-BOOT",
						LineDescription="Hiker Boot",
						OrderQty=2.0,
						UnitPrice=120.0,
						Amount=240.0,
					)
				],
				TaxDetails=[tax(1112, "GST", 12.0)],
				Totals=detail(1113, 1, DiscountTotal=0.0, Freight=0.0, PremiumFreight=0.0, TaxTotal=12.0),
			),
			entity(
				112,
				OrderType="SO",
				OrderNbr="SO000001",
				CustomerID="ALPINE",
				Date="2026-09-12T00:00:00+00:00",
				RequestedOn="2026-09-30T00:00:00+00:00",
				Status="Open",
				CurrencyID="CAD",
				CurrencyRate=1.0,
				OrderTotal=283.5,
				CustomerOrder="PO-5521",
				LastModified=MODIFIED,
				Details=[
					detail(
						1121,
						1,
						LineNbr=1,
						InventoryID="HIKER-BOOT",
						LineDescription="Hiker Boot",
						OrderQty=2.0,
						UnitPrice=120.0,
						Amount=240.0,
					),
					detail(
						1122,
						2,
						LineNbr=2,
						InventoryID="TRAIL-SOCK",
						LineDescription="Trail Sock",
						OrderQty=2.0,
						UnitPrice=15.0,
						Amount=30.0,
					),
				],
				TaxDetails=[tax(1123, "GST", 13.5)],
				Totals=detail(1124, 1, DiscountTotal=10.0, Freight=10.0, PremiumFreight=0.0, TaxTotal=13.5),
			),
		],
		"PurchaseOrder": [
			entity(
				121,
				Type="Normal",
				OrderNbr="PO000001",
				VendorID="LEATHERCO",
				Date="2026-09-08T00:00:00+00:00",
				Status="Open",
				CurrencyID="CAD",
				CurrencyRate=1.0,
				OrderTotal=600.0,
				VendorRef="PL-881",
				LastModifiedDateTime=MODIFIED,
				Details=[
					detail(
						1211,
						1,
						LineNbr=1,
						InventoryID="HIKER-BOOT",
						LineDescription="Hiker Boot",
						OrderQty=10.0,
						UnitCost=60.0,
						ExtendedCost=600.0,
					)
				],
				TaxDetails=[],
			),
		],
		"Invoice": [
			entity(
				131,
				Type="Invoice",
				ReferenceNbr="AR000001",
				Customer="ALPINE",
				Date="2026-09-15T00:00:00+00:00",
				DueDate="2026-10-15T00:00:00+00:00",
				Status="Open",
				Amount=262.5,
				TaxTotal=12.5,
				CustomerOrder="PO-5521",
				Description="Boots and fitting",
				LastModifiedDateTime=MODIFIED,
				Details=[
					line(
						1311,
						1,
						"HIKER-BOOT",
						"Hiker Boot",
						2.0,
						120.0,
						240.0,
						Account="40000",
						Subaccount="OPS-000",
					),
					line(1312, 2, "FITTING", "Boot Fitting", 1.0, 40.0, 40.0, Account="40000"),
				],
				TaxDetails=[tax(1313, "GST", 12.5)],
				DiscountDetails=[detail(1314, 1, DiscountCode="SPRING", DiscountAmount=30.0)],
				ApplicationsCreditMemo=[],
			),
			entity(
				132,
				Type="Invoice",
				ReferenceNbr="AR000002",
				Customer="RIVERS",
				Date="2026-09-16T00:00:00+00:00",
				Status="Open",
				Amount=30.0,
				TaxTotal=0.0,
				LastModifiedDateTime=MODIFIED,
				Details=[line(1321, 1, "TRAIL-SOCK", "Trail Sock", 2.0, 15.0, 30.0)],
				TaxDetails=[],
				DiscountDetails=[],
				ApplicationsCreditMemo=[],
			),
			entity(
				133,
				Type="Credit Memo",
				ReferenceNbr="AR000003",
				Customer="ALPINE",
				Date="2026-09-18T00:00:00+00:00",
				Status="Closed",
				Amount=42.0,
				TaxTotal=2.0,
				Description="Fitting refunded",
				LastModifiedDateTime=MODIFIED,
				Details=[line(1331, 1, "FITTING", "Boot Fitting", 1.0, 40.0, 40.0)],
				TaxDetails=[tax(1332, "GST", 2.0)],
				DiscountDetails=[],
				ApplicationsCreditMemo=[
					detail(1333, 1, DocType="Invoice", ReferenceNbr="AR000001", AmountPaid=42.0)
				],
			),
		],
		"CashSale": [
			entity(
				141,
				Type="Cash Sale",
				ReferenceNbr="CS000001",
				CustomerID="ALPINE",
				Date="2026-09-17T00:00:00+00:00",
				Status="Closed",
				Amount=31.5,
				TaxTotal=1.5,
				PaymentRef="POS-1",
				LastModifiedDateTime=MODIFIED,
				Details=[
					detail(
						1411,
						1,
						InventoryID="TRAIL-SOCK",
						TransactionDescription="Trail Sock",
						Qty=2.0,
						UnitPrice=15.0,
						Amount=30.0,
					)
				],
			),
		],
		"Bill": [
			entity(
				151,
				Type="Bill",
				ReferenceNbr="AP000001",
				Vendor="LEATHERCO",
				Date="2026-09-14T00:00:00+00:00",
				DueDate="2026-10-14T00:00:00+00:00",
				Status="Open",
				CurrencyID="CAD",
				Amount=651.0,
				TaxTotal=31.0,
				VendorRef="PL-881-INV",
				LastModifiedDateTime=MODIFIED,
				Details=[
					detail(
						1511,
						1,
						InventoryID="HIKER-BOOT",
						TransactionDescription="Hiker Boot",
						Qty=10.0,
						UnitCost=60.0,
						Amount=600.0,
						Account="61000",
					),
					detail(
						1512,
						2,
						TransactionDescription="Packing",
						Qty=1.0,
						UnitCost=20.0,
						Amount=20.0,
						Account="61000",
						Subaccount="OPS-000",
					),
				],
				TaxDetails=[tax(1513, "GST", 31.0)],
				Applications=[],
			),
		],
		"Payment": [
			entity(
				161,
				Type="Payment",
				ReferenceNbr="PM000001",
				CustomerID="ALPINE",
				ApplicationDate="2026-09-20T00:00:00+00:00",
				Status="Closed",
				CurrencyID="CAD",
				PaymentAmount=220.5,
				PaymentRef="EFT-3301",
				CashAccount="10100",
				LastModifiedDateTime=MODIFIED,
				ApplicationHistory=[
					detail(1611, 1, AdjustedDocType="Invoice", AdjustedRefNbr="AR000001", AmountPaid=220.5)
				],
			),
		],
		"Check": [
			entity(
				171,
				Type="Payment",
				ReferenceNbr="CHK00001",
				Vendor="LEATHERCO",
				ApplicationDate="2026-09-21T00:00:00+00:00",
				Status="Closed",
				CurrencyID="CAD",
				PaymentAmount=651.0,
				PaymentRef="1201",
				CashAccount="10100",
				LastModifiedDateTime=MODIFIED,
				Details=[detail(1711, 1, DocType="Bill", ReferenceNbr="AP000001", AmountPaid=651.0)],
			),
		],
		"JournalTransaction": [
			entity(
				181,
				Module="GL",
				BatchNbr="GL000001",
				Status="Posted",
				TransactionDate="2026-09-30T00:00:00+00:00",
				LedgerID="ACTUAL",
				CurrencyID="CAD",
				Description="September rent",
				LastModifiedDateTime=MODIFIED,
				Details=[
					detail(
						1811,
						1,
						LineNbr=1,
						Account="60000",
						Subaccount="OPS-000",
						DebitAmount=1800.0,
						CreditAmount=0.0,
					),
					detail(
						1812,
						2,
						LineNbr=2,
						Account="10100",
						Subaccount="000-000",
						DebitAmount=0.0,
						CreditAmount=1800.0,
					),
				],
			),
		],
		"Ledger": [
			entity(191, LedgerID="ACTUAL", Description="Actual ledger", Currency="CAD", Type="Actual")
		],
	}


def find(store: dict[str, list[Record]], name: str, field: str, key: Any) -> Record:
	return next(record for record in store[name] if record[field]["value"] == key)


def store_v2() -> dict[str, list[Record]]:
	store = copy.deepcopy(store_v1())
	find(store, "Account", "AccountCD", "61000")["Description"] = {"value": "Office & Packing Supplies"}
	find(store, "Account", "AccountCD", "60000")["Active"] = {"value": False}
	find(store, "Warehouse", "WarehouseID", "RETAIL")["Active"] = {"value": False}
	find(store, "StockItem", "InventoryID", "TRAIL-SOCK")["ItemStatus"] = {"value": "Marked for Deletion"}
	find(store, "Customer", "CustomerID", "ALPINE")["MainContact"]["Phone1"] = {"value": "+1 403 555 0199"}
	find(store, "Lead", "LeadID", 201)["Status"] = {"value": "Converted"}
	find(store, "Employee", "EmployeeID", "EP002")["Status"] = {"value": "Inactive"}
	find(store, "PurchaseOrder", "OrderNbr", "PO000001")["Description"] = {"value": "Deliver to the store"}
	find(store, "Invoice", "ReferenceNbr", "AR000002")["Status"] = {"value": "Voided"}
	store["Invoice"].append(
		entity(
			134,
			Type="Invoice",
			ReferenceNbr="AR000004",
			Customer="ALPINE",
			Date="2026-10-02T00:00:00+00:00",
			Status="Balanced",
			Amount=42.0,
			TaxTotal=2.0,
			LastModifiedDateTime=LATER,
			Details=[line(1341, 1, "FITTING", "Boot Fitting", 1.0, 40.0, 40.0)],
			TaxDetails=[tax(1342, "GST", 2.0)],
			DiscountDetails=[],
			ApplicationsCreditMemo=[],
		)
	)
	return store


def listing(
	name: str, query: dict[str, Any], records: list[Record], patterns: dict[str, str] | None = None
) -> dict[str, Any]:
	request: dict[str, Any] = {"path": f"{API}/{name}", "query": query}
	if patterns:
		request["query_patterns"] = patterns
	return {"request": request, "response": records}


def answers(store: dict[str, list[Record]]) -> list[tuple[str, dict[str, Any]]]:
	found: list[tuple[str, dict[str, Any]]] = []
	for kind, entities in reader.ENTITIES.items():
		for entity_ in entities:
			records = store[entity_.name]
			if entity_.name == "SalesOrder":
				quotes = entity_.condition.endswith(f"eq '{reader.QUOTE_TYPE}'")
				records = [r for r in records if (r["OrderType"]["value"] == reader.QUOTE_TYPE) == quotes]
			query: dict[str, Any] = {"$top": reader.PAGE_SIZE, "$skip": 0}
			if entity_.expand:
				query["$expand"] = entity_.expand
			name = f"{entity_.name}-{kind.value}"
			if entity_.condition:
				found.append(
					(f"{name}-full", listing(entity_.name, {**query, "$filter": entity_.condition}, records))
				)
				pattern = (
					re.escape(entity_.condition) + r" and LastModifiedDateTime gt datetimeoffset'[0-9T:-]+'"
				)
				found.append((f"{name}-since", listing(entity_.name, query, records, {"$filter": pattern})))
			else:
				found.append((name, listing(entity_.name, query, records)))
	for customer in store["Customer"]:
		code = customer["CustomerID"]["value"]
		selected = {k: customer[k] for k in ("id", "CustomerID", "CurrencyID", "EnableCurrencyOverride")}
		found.append(
			(
				f"Customer-currency-{code}",
				listing(
					"Customer",
					{
						"$filter": f"CustomerID eq '{code}'",
						"$select": "CustomerID,CurrencyID,EnableCurrencyOverride",
					},
					[selected],
				),
			)
		)
	ledger = store["Ledger"][0]
	found.append(
		(
			"Ledger-ACTUAL",
			listing(
				"Ledger",
				{"$filter": "LedgerID eq 'ACTUAL'", "$select": "LedgerID,Currency"},
				[{k: ledger[k] for k in ("id", "LedgerID", "Currency")}],
			),
		)
	)
	alpine = find(store, "Customer", "CustomerID", "ALPINE")
	found.append(
		(
			"Customer-lookup-ALPINE",
			listing(
				"Customer", {"$filter": "CustomerID eq 'ALPINE'", "$expand": reader.PARTY_EXPAND}, [alpine]
			),
		)
	)
	return found


def write(state: str, store: dict[str, list[Record]]) -> None:
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
