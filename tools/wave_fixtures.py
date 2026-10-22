# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the Wave fixtures (tests/fixtures/wave/<state>/*.json): a small bakery's business as Wave's public
GraphQL API answers the importer's operations (idempotent_importer/sources/wave/operations.py), each answer
exactly the shape its operation selects from Wave's schema (tests/schemas/wave.json; tools/prove.py
executes every answer back through its operation). A fixture records a GraphQL request as a POST to the
endpoint whose body names the operation and its variables. The two states differ only by their records (v2: a
new price, the draft invoice sent and paid, a customer archived).

``python -m tools.wave_fixtures`` rewrites both states."""

from __future__ import annotations

import base64
import copy
import json
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.sources.wave import operations, reader

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "wave"
BUSINESS_UUID = "5d1c7a2e-3b4f-4e6a-9c8d-1a2b3c4d5e6f"
Record = dict[str, Any]
CAD = {"code": "CAD"}


def global_id(type_: str | None, id_: str | int | None = None) -> str:
	"""A Wave global id: base64 of ``Business:<uuid>`` and, for a record, ``;<Type>:<id>``."""
	text = f"Business:{BUSINESS_UUID}" + (f";{type_}:{id_}" if type_ else "")
	return base64.b64encode(text.encode()).decode()


BUSINESS = global_id(None)


def money(value: str) -> Record:
	return {"value": value, "currency": CAD}


def ref(type_: str, id_: int | str) -> Record:
	return {"id": global_id(type_, id_)}


def account(id_: int, name: str, number: str | None, kind: str, subtype: str, balance: str) -> Record:
	return {
		"id": global_id("Account", id_),
		"name": name,
		"description": None,
		"displayId": number,
		"currency": CAD,
		"type": {"value": kind},
		"subtype": {"value": subtype, "name": subtype.replace("_", " ").title()},
		"normalBalanceType": balance,
		"isArchived": False,
	}


def tax(id_: int, name: str, abbreviation: str, rate: str, compound: bool, archived: bool) -> Record:
	return {
		"id": global_id("SalesTax", id_),
		"name": name,
		"abbreviation": abbreviation,
		"taxNumber": "123456789RT0001" if abbreviation == "HST" else None,
		"rate": rate,
		"isCompound": compound,
		"isArchived": archived,
	}


def product(id_: int, name: str, price: str, archived: bool = False) -> Record:
	return {
		"id": global_id("Product", id_),
		"name": name,
		"description": f"{name}, baked daily" if not archived else None,
		"unitPrice": price,
		"isSold": True,
		"isBought": False,
		"isArchived": archived,
		"incomeAccount": ref("Account", 3),
		"expenseAccount": None,
	}


def party(type_: str, id_: int, name: str, first: str | None, last: str | None, email: str) -> Record:
	return {
		"id": global_id(type_, id_),
		"name": name,
		"firstName": first,
		"lastName": last,
		"displayId": None,
		"email": email,
		"mobile": None,
		"phone": "+1 902 555 0142",
		"tollFree": None,
		"website": None,
		"internalNotes": None,
		"currency": CAD,
		"address": {
			"addressLine1": "12 Water St",
			"addressLine2": None,
			"city": "Halifax",
			"postalCode": "B3J 1A1",
			"province": {"code": "CA-NS", "name": "Nova Scotia"},
			"country": {"code": "CA", "name": "Canada"},
		},
		"isArchived": False,
		"modifiedAt": "2026-09-01T15:00:00.000Z",
	}


def item(id_: int, product_id: int, name: str, quantity: str, price: str, subtotal: str, hst: str) -> Record:
	return {
		"id": global_id("InvoiceItem", id_),
		"description": None,
		"quantity": quantity,
		"unitPrice": price,
		"subtotal": money(subtotal),
		"product": {"id": global_id("Product", product_id), "name": name},
		"taxes": [
			{
				"salesTax": {
					"id": global_id("SalesTax", 1),
					"name": "Harmonized Sales Tax",
					"abbreviation": "HST",
				},
				"amount": money(hst),
			}
		],
	}


def document(
	id_: str, status: str, customer: int, items: list[Record], amounts: tuple[str, str, str, str]
) -> Record:
	subtotal, discount, taxes, total = amounts
	return {
		"id": id_,
		"status": status,
		"title": "Invoice" if "Invoice" in base64.b64decode(id_).decode() else "Estimate",
		"currency": CAD,
		"exchangeRate": "1",
		"customer": ref("Customer", customer),
		"memo": None,
		"footer": None,
		"poNumber": None,
		"subtotal": money(subtotal),
		"discountTotal": money(discount),
		"taxTotal": money(taxes),
		"total": money(total),
		"modifiedAt": "2026-09-15T15:00:00.000Z",
		"items": items,
	}


def invoice(
	id_: int, number: str, status: str, customer: int, items: list[Record], amounts: tuple[str, str, str, str]
) -> Record:
	return document(global_id("Invoice", id_), status, customer, items, amounts) | {
		"invoiceNumber": number,
		"invoiceDate": "2026-09-10",
		"dueDate": "2026-10-10",
	}


def payment(id_: int, customer: int, amount: str, date: str) -> Record:
	return {
		"id": global_id("InvoicePayment", id_),
		"paymentDate": date,
		"amount": amount,
		"memo": None,
		"paymentMethod": "BANK_TRANSFER",
		"exchangeRate": "1",
		"active": True,
		"account": ref("Account", 1),
		"customer": ref("Customer", customer),
		"paymentCurrency": CAD,
	}


def store_v1() -> dict[str, Any]:
	return {
		"accounts": [
			account(1, "Chequing", "1010", "ASSET", "CASH_AND_BANK", "DEBIT"),
			account(2, "Accounts Receivable", "1200", "ASSET", "RECEIVABLE_INVOICES", "DEBIT"),
			account(3, "Sales", "4000", "INCOME", "INCOME", "CREDIT"),
			account(4, "HST Payable", "2200", "LIABILITY", "SALES_TAX", "CREDIT"),
		],
		"taxes": [
			tax(1, "Harmonized Sales Tax", "HST", "0.15", False, False),
			tax(2, "Quebec Sales Tax", "QST", "0.09975", True, False),
			tax(3, "Provincial Sales Tax", "PST", "0.07", False, True),
		],
		"products": [
			product(1, "Sourdough loaf", "8.50"),
			product(2, "Catering", "120.00"),
			product(3, "Old muffin", "2.00", True),
		],
		"customers": [
			party("Customer", 1, "River Cafe", "Riley", "River", "riley@river-wave.example"),
			party("Customer", 2, "Sam Shore", "Sam", "Shore", "sam@shore-wave.example"),
		],
		"vendors": [party("Vendor", 1, "Mill Co", "Pat", "Mill", "pat@mill-wave.example")],
		"estimates": [
			document(
				global_id("Estimate", 1),
				"SENT",
				1,
				[item(11, 2, "Catering", "2", "120.00", "240.00", "32.40")],
				("240.00", "24.00", "32.40", "248.40"),
			)
			| {"estimateNumber": "1", "estimateDate": "2026-09-01", "dueDate": "2026-09-30"}
		],
		"invoices": [
			invoice(
				1,
				"1",
				"PAID",
				1,
				[item(21, 1, "Sourdough loaf", "4", "8.50", "34.00", "5.10")],
				("34.00", "0.00", "5.10", "39.10"),
			),
			invoice(
				2,
				"2",
				"DRAFT",
				2,
				[item(22, 2, "Catering", "1", "120.00", "120.00", "18.00")],
				("120.00", "0.00", "18.00", "138.00"),
			),
		],
		"payments": {1: [payment(1, 1, "39.10", "2026-09-16")], 2: []},
	}


def store_v2() -> dict[str, Any]:
	store = copy.deepcopy(store_v1())
	store["products"][0]["unitPrice"] = "9.00"
	store["invoices"][1]["status"] = "PAID"
	store["payments"][2] = [payment(2, 2, "138.00", "2026-10-02")]
	store["customers"][1]["isArchived"] = True
	return store


def answer(operation: operations.Operation, records: list[Record]) -> Record:
	connection = {
		"pageInfo": {"currentPage": 1, "totalPages": 1},
		"edges": [{"node": record} for record in records],
	}
	return {
		"request": {
			"method": "POST",
			"path": reader.API_PATH,
			"body": {
				"operationName": operation.name,
				"variables": {"business": BUSINESS, "page": 1, "pageSize": reader.PAGE_SIZE},
			},
		},
		"response": {"data": {"business": {operation.field: connection}}},
	}


def fixtures(store: dict[str, Any]) -> dict[str, Record]:
	payments = [
		{
			"id": invoice_["id"],
			"customer": invoice_["customer"],
			"currency": CAD,
			"payments": store["payments"][index],
		}
		for index, invoice_ in enumerate(store["invoices"], start=1)
	]
	return {
		"businesses": {
			"request": {
				"method": "POST",
				"path": reader.API_PATH,
				"body": {
					"operationName": operations.BUSINESSES.name,
					"variables": {"page": 1, "pageSize": reader.PAGE_SIZE},
				},
			},
			"response": {
				"data": {
					"businesses": {
						"pageInfo": {"currentPage": 1, "totalPages": 1},
						"edges": [{"node": {"id": BUSINESS, "name": "Harbour Bakery", "currency": CAD}}],
					}
				}
			},
		},
		"accounts": answer(operations.ACCOUNTS, store["accounts"]),
		"sales-taxes": answer(operations.SALES_TAXES, store["taxes"]),
		"products": answer(operations.PRODUCTS, store["products"]),
		"customers": answer(operations.CUSTOMERS, store["customers"]),
		"vendors": answer(operations.VENDORS, store["vendors"]),
		"estimates": answer(operations.ESTIMATES, store["estimates"]),
		"invoices": answer(operations.INVOICES, store["invoices"]),
		"invoice-payments": answer(operations.INVOICE_PAYMENTS, payments),
	}


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
