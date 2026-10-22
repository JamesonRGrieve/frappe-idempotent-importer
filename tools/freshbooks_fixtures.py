# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the FreshBooks fixtures (tests/fixtures/freshbooks/<state>/*.json): a small studio's business as the
API answers, shaped as tests/schemas/freshbooks.json transcribes FreshBooks' reference, for exactly the
requests the reader sends: who approved (users/me), the chart of accounts, taxes, items, services and their
rates, clients, vendors, projects, time entries, estimates and invoices with their lines, credit notes, bills
(and one by id), payments, bill payments, the manual journal entries and the general ledger. The two states
differ only by their records (v2: a new price, an invoice sent and paid, a new time entry, a client
deleted).

``python -m tools.freshbooks_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.sources.freshbooks import reader

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "freshbooks"
ACCOUNT, BUSINESS, UUID = "Zv8pR3", 240340, "046cc001-0002-e93e-1db1-1186b2983879"
Record = dict[str, Any]
CHECKING, SOFTWARE, SALES, TAXES = (
	"0f321fa9-78db-41ce-8c5f-11605718e5fb",
	"184ba7f5-ce2a-4435-ab67-824cbc082c47",
	"48c444f7-17cf-4977-a0b3-bc04c8bd0f53",
	"5b2e0c11-4a51-4c0e-9d2f-2b7c1f0e6a10",
)
CASH, OFFICE = "7a6c2b9e-1d2f-4c3a-8b5e-6f7a8b9c0d1e", "8b7d3cae-2e3f-4d4b-9c6f-7a8b9c0d1e2f"
LEDGER_TODAY = r"\d{4}-\d{2}-\d{2}"


def money(amount: str) -> Record:
	return {"amount": amount, "code": "CAD"}


def line(lineid: int, name: str, qty: str, unit: str, tax: str | None = "15") -> Record:
	return {
		"lineid": lineid,
		"name": name,
		"description": "",
		"qty": qty,
		"unit_cost": money(unit),
		"amount": money(f"{float(qty) * float(unit):.2f}"),
		"taxName1": "HST" if tax else None,
		"taxAmount1": tax,
		"taxName2": None,
		"taxAmount2": None,
		"type": 0,
		"updated": "2026-09-15 15:00:00",
	}


def account(
	uuid: str, name: str, number: str, kind: str, sub_type: str, subs: list[Record] | None = None
) -> Record:
	body: Record = {
		"account_uuid": uuid,
		"account_name": name,
		"account_number": number,
		"account_type": kind,
		"account_sub_type": sub_type,
		"currency_code": "CAD",
		"system_account_name": None,
		"auto_created": False,
		"state": "active",
		"balance": money("0.00"),
	}
	if subs is not None:
		body["sub_accounts"] = subs
	return body


def sub(uuid: str, name: str, number: str, kind: str, sub_type: str, parent: str) -> Record:
	return account(uuid, name, number, kind, sub_type) | {"parent_account_uuid": parent}


def transaction(kind: str, amount: str, debit: bool, **ids: int) -> Record:
	body: Record = {
		"entity_type": kind,
		"reference": "",
		"description": {"expense": "Design software", "income": "Workshop fee", "invoice": "Invoice 0000007"}[
			kind
		],
		"date": "2026-09-10",
		"client_name": None,
		"debit": money(amount) if debit else None,
		"credit": None if debit else money(amount),
	}
	for name in (
		"expenseid",
		"invoiceid",
		"paymentid",
		"incomeid",
		"creditid",
		"clientid",
		"billid",
		"bill_paymentid",
	):
		body[name] = ids.get(name)
	return body


def store_v1() -> dict[str, Any]:
	return {
		"accounts": [
			account(
				CASH,
				"Cash & Bank",
				"1000",
				"asset",
				"Cash & Bank",
				[sub(CHECKING, "Checking", "1010", "asset", "Cash & Bank", CASH)],
			),
			account(
				OFFICE,
				"Office Expenses",
				"6000",
				"expense",
				"Office",
				[sub(SOFTWARE, "Software", "6010", "expense", "Office", OFFICE)],
			),
			account(SALES, "Sales", "4000", "income", "Sales", []),
			account(TAXES, "Sales Taxes", "2200", "liability", "Sales Taxes", []),
		],
		"taxes": [
			{
				"taxid": 58728,
				"id": 58728,
				"name": "HST",
				"amount": "15",
				"number": "123456789RT0001",
				"accounting_systemid": ACCOUNT,
				"updated": "2026-08-03 15:00:00",
				"compound": False,
			},
			{
				"taxid": 58729,
				"id": 58729,
				"name": "QST",
				"amount": "9.975",
				"number": None,
				"accounting_systemid": ACCOUNT,
				"updated": "2026-08-03 15:00:00",
				"compound": True,
			},
		],
		"items": [
			{
				"itemid": 201225,
				"id": 201225,
				"accounting_systemid": ACCOUNT,
				"name": "Harbour print",
				"description": "A print of the harbour",
				"qty": "1",
				"inventory": "12",
				"unit_cost": money("45.00"),
				"tax1": 58728,
				"tax2": None,
				"vis_state": 0,
				"updated": "2026-08-03 15:00:00",
				"sku": "PRINT",
			},
		],
		"services": [
			{"business_id": BUSINESS, "id": 4054453, "name": "Design", "billable": True, "vis_state": 0}
		],
		"rates": {4054453: {"rate": "150.00", "business_id": BUSINESS, "service_id": 4054453}},
		"clients": [
			client(220399, "Riley", "River", "River Co", "riley@river-fb.example", 0),
			client(220400, "Sam", "Shore", None, "sam@shore-fb.example", 2),
		],
		"vendors": [
			{
				"vendorid": 1563,
				"vendor_name": "Truro Paper Mill",
				"primary_contact_first_name": "Pat",
				"primary_contact_last_name": "Mill",
				"primary_contact_email": "pat@mill.example",
				"phone": "+1 902 555 0190",
				"website": "mill.example",
				"street": "9 Mill Rd",
				"street2": None,
				"city": "Truro",
				"province": "Nova Scotia",
				"postal_code": "B2N 1A1",
				"country": "Canada",
				"currency_code": "CAD",
				"account_number": "45454545",
				"is_1099": False,
				"vis_state": 0,
				"tax_defaults": [],
				"created_at": "2026-08-03 15:00:00",
				"updated_at": "2026-08-03 15:00:00",
			}
		],
		"projects": [
			{
				"id": 779597,
				"title": "River rebrand",
				"description": "A new identity for the tours",
				"due_date": "2026-11-30",
				"client_id": 220399,
				"internal": False,
				"budget": 4800,
				"fixed_price": None,
				"rate": "150.00",
				"billing_method": "project_rate",
				"project_type": "hourly_rate",
				"active": True,
				"complete": False,
				"sample": False,
				"created_at": "2026-08-20T15:00:00",
				"updated_at": "2026-09-15T15:00:00",
				"logged_duration": None,
				"services": [{"business_id": BUSINESS, "name": "Design", "id": 4054453}],
			}
		],
		"time_entries": [time_entry(5095, "2026-09-02T15:00:00Z", 5400, "Concepts")],
		"estimates": [
			{
				"estimateid": 500,
				"id": 500,
				"estimate_number": "0000001",
				"customerid": 220399,
				"create_date": "2026-09-01",
				"currency_code": "CAD",
				"amount": money("310.50"),
				"discount_value": "10",
				"discount_total": money("30.00"),
				"status": 2,
				"ui_status": "sent",
				"vis_state": 0,
				"notes": "Identity work",
				"terms": None,
				"lines": [line(1, "Design", "2", "150.00")],
			}
		],
		"invoices": [
			invoice(600, "0000007", "paid", [line(1, "Harbour print", "2", "45.00")], "103.50"),
			invoice(601, "0000008", "draft", [line(1, "Design", "1", "150.00")], "172.50"),
		],
		"credit_notes": [
			{
				"creditid": 31131,
				"id": 31131,
				"credit_number": "00002134",
				"clientid": "220399",
				"create_date": "2026-09-22",
				"credit_type": "goodwill",
				"amount": money("25.00"),
				"paid": money("0.00"),
				"payment_status": "unpaid",
				"display_status": "created",
				"status": "created",
				"vis_state": 0,
			},
		],
		"bills": [
			{
				"id": 1141,
				"bill_number": "BN101",
				"vendorid": 1563,
				"issue_date": "2026-09-05",
				"due_date": "2026-10-05",
				"currency_code": "CAD",
				"amount": money("1500.00"),
				"total_amount": money("1725.00"),
				"tax_amount": money("225.00"),
				"outstanding": money("0.00"),
				"paid": money("1725.00"),
				"status": "paid",
				"vis_state": 0,
				"created_at": "2026-09-05 15:00:00",
				"updated_at": "2026-09-20 15:00:00",
				"lines": [
					{
						"id": 2621,
						"list_index": 1,
						"description": "Paper stock",
						"quantity": "15",
						"unit_cost": money("100.00"),
						"amount": money("1500.00"),
						"tax_name1": "HST",
						"tax_percent1": "15",
						"tax_amount1": money("225.00"),
						"tax_name2": None,
						"tax_percent2": None,
						"tax_amount2": None,
						"categoryid": 3696445,
						"category": {"category": "Supplies", "categoryid": 3696445},
					}
				],
			}
		],
		"payments": [payment(900, 600, "103.50", "2026-09-16")],
		"bill_payments": [
			{
				"id": 1490,
				"billid": 1141,
				"amount": money("1725.00"),
				"payment_type": "Cash",
				"paid_date": "2026-09-20",
				"note": "",
				"vis_state": 0,
				"matched_with_expense": False,
			},
		],
		"journal_entries": [
			{
				"id": "005f5c22-8166-47e9-926d-098fe523f929",
				"userEnteredDate": {"year": 2026, "month": 9, "day": 30},
				"name": "Licence true-up",
				"journalEntryNumber": "JE1",
				"description": "Annual licence",
				"details": [
					{
						"id": "a560a0b8-a9b5-4df1-a58e-98e33b88d682",
						"accountId": SOFTWARE,
						"amount": money("100.000"),
						"type": "TYPE_DEBIT",
						"manualJournalEntryId": "005f5c22-8166-47e9-926d-098fe523f929",
					},
					{
						"id": "406d5579-5a09-4f1a-b79b-ebf7c4529cc5",
						"accountId": CHECKING,
						"amount": money("100.000"),
						"type": "TYPE_CREDIT",
						"manualJournalEntryId": "005f5c22-8166-47e9-926d-098fe523f929",
					},
				],
			}
		],
		"ledger": [
			ledger_account(
				CASH,
				"Cash & Bank",
				"1000",
				"asset",
				[
					ledger_sub(
						CHECKING,
						"Checking",
						"1010",
						"asset",
						[
							transaction("expense", "56.50", False, expenseid=77),
							transaction("income", "200.00", True, incomeid=88),
							transaction("invoice", "103.50", True, invoiceid=600, clientid=220399),
						],
					)
				],
			),
			ledger_account(
				OFFICE,
				"Office Expenses",
				"6000",
				"expense",
				[
					ledger_sub(
						SOFTWARE,
						"Software",
						"6010",
						"expense",
						[transaction("expense", "50.00", True, expenseid=77)],
					)
				],
			),
			ledger_account(
				TAXES,
				"Sales Taxes",
				"2200",
				"liability",
				[],
				[transaction("expense", "6.50", True, expenseid=77)],
			),
			ledger_account(
				SALES, "Sales", "4000", "income", [], [transaction("income", "200.00", False, incomeid=88)]
			),
		],
	}


def client(id_: int, first: str, last: str, organisation: str | None, email: str, vis: int) -> Record:
	return {
		"id": id_,
		"userid": id_,
		"fname": first,
		"lname": last,
		"organization": organisation,
		"email": email,
		"home_phone": None,
		"bus_phone": "+1 902 555 0101",
		"mob_phone": None,
		"p_street": "1 Harbour Rd",
		"p_street2": None,
		"p_city": "Halifax",
		"p_province": "NS",
		"p_code": "B3H 1A1",
		"p_country": "Canada",
		"currency_code": "CAD",
		"vis_state": vis,
		"updated": "2026-09-15 15:00:00",
		"accounting_systemid": ACCOUNT,
		"vat_name": "HST",
		"vat_number": "987654321RT0001" if organisation else None,
	}


def time_entry(id_: int, started: str, duration: int, note: str) -> Record:
	return {
		"id": id_,
		"identity_id": 8804571,
		"is_logged": True,
		"started_at": started,
		"client_id": 220399,
		"project_id": 779597,
		"service_id": 4054453,
		"note": note,
		"active": True,
		"billable": True,
		"billed": False,
		"internal": False,
		"duration": duration,
		"timer": None,
	}


def invoice(id_: int, number: str, status: str, lines: list[Record], amount: str) -> Record:
	return {
		"invoiceid": id_,
		"id": id_,
		"accountid": ACCOUNT,
		"invoice_number": number,
		"customerid": 220399,
		"create_date": "2026-09-10",
		"due_date": "2026-10-10",
		"currency_code": "CAD",
		"amount": money(amount),
		"outstanding": money("0.00" if status == "paid" else amount),
		"paid": money(amount if status == "paid" else "0.00"),
		"discount_value": "0",
		"discount_total": money("0.00"),
		"status": 4 if status == "paid" else 1,
		"v3_status": status,
		"payment_status": "paid" if status == "paid" else "unpaid",
		"display_status": status,
		"vis_state": 0,
		"lines": lines,
	}


def payment(id_: int, invoice_id: int, amount: str, date: str) -> Record:
	return {
		"id": id_,
		"logid": id_,
		"invoiceid": invoice_id,
		"creditid": None,
		"amount": money(amount),
		"date": date,
		"type": "Credit Card",
		"note": "",
		"clientid": 220399,
		"vis_state": 0,
		"updated": f"{date} 15:00:00",
		"gateway": None,
		"from_credit": False,
		"overpaymentid": None,
		"orderid": None,
		"accounting_systemid": ACCOUNT,
	}


def ledger_account(
	uuid: str, name: str, number: str, kind: str, subs: list[Record], transactions: list[Record] | None = None
) -> Record:
	body: Record = {
		"accountid": uuid,
		"account_name": name,
		"account_number": number,
		"debit": money("0.00"),
		"credit": money("0.00"),
		"net_movement_debit": money("0.00"),
		"net_movement_credit": money("0.00"),
		"account_type": kind,
		"account_sub_type": name,
		"sub_accounts": subs,
	}
	if transactions is not None:
		body["transactions"] = transactions
	return body


def ledger_sub(uuid: str, name: str, number: str, kind: str, transactions: list[Record]) -> Record:
	return {
		"sub_accountid": uuid,
		"sub_account_name": name,
		"sub_account_number": number,
		"debit": money("0.00"),
		"credit": money("0.00"),
		"net_movement_debit": money("0.00"),
		"net_movement_credit": money("0.00"),
		"account_type": kind,
		"account_sub_type": name,
		"transactions": transactions,
	}


def store_v2() -> dict[str, Any]:
	store = copy.deepcopy(store_v1())
	store["items"][0]["unit_cost"] = money("50.00")
	sent = store["invoices"][1]
	sent |= {
		"v3_status": "paid",
		"status": 4,
		"payment_status": "paid",
		"outstanding": money("0.00"),
		"paid": money("172.50"),
	}
	store["payments"].append(payment(901, 601, "172.50", "2026-10-02"))
	store["time_entries"].append(time_entry(5096, "2026-10-01T15:00:00Z", 1800, "Revisions"))
	store["clients"][1]["vis_state"] = 1
	return store


def fixture(path: str, query: Record, response: Any, patterns: Record | None = None) -> Record:
	request: Record = {"path": path, "query": {k: str(v) for k, v in query.items()}}
	if patterns:
		request["query_patterns"] = patterns
	return {"request": request, "response": response}


def accounting_list(key: str, records: list[Record]) -> Record:
	return {
		"response": {
			"result": {
				key: records,
				"page": 1,
				"pages": 1,
				"per_page": reader.PER_PAGE,
				"total": len(records),
			}
		}
	}


def meta(count: int) -> Record:
	return {"page": 1, "pages": 1, "per_page": reader.PER_PAGE, "total": count}


def fixtures(store: dict[str, Any]) -> dict[str, Record]:
	page = {"page": 1, "per_page": reader.PER_PAGE}
	accounting = f"/accounting/account/{ACCOUNT}"
	business = f"/accounting/businesses/{UUID}"
	written: dict[str, Record] = {
		"me": fixture(
			"/auth/api/v1/users/me",
			{},
			{
				"response": {
					"id": 8804571,
					"business_memberships": [
						{
							"id": 111,
							"role": "owner",
							"business": {
								"id": BUSINESS,
								"business_uuid": UUID,
								"name": "Harbour Studio",
								"account_id": ACCOUNT,
							},
						}
					],
				}
			},
		),
		"accounts": fixture(
			f"{business}/reports/chart_of_accounts",
			{"use_ledger_entries": "true"},
			{"response": {"result": {"journal_entry_accounts": store["accounts"]}}},
		),
		"taxes": fixture(f"{accounting}/taxes/taxes", page, accounting_list("taxes", store["taxes"])),
		"items": fixture(f"{accounting}/items/items", page, accounting_list("items", store["items"])),
		"services": fixture(
			f"/comments/business/{BUSINESS}/services",
			page,
			{"services": store["services"], "meta": meta(len(store["services"]))},
		),
		"clients": fixture(f"{accounting}/users/clients", page, accounting_list("clients", store["clients"])),
		"vendors": fixture(
			f"{accounting}/bill_vendors/bill_vendors", page, accounting_list("bill_vendors", store["vendors"])
		),
		"projects": fixture(
			f"/projects/business/{BUSINESS}/projects",
			page,
			{"projects": store["projects"], "meta": meta(len(store["projects"]))},
		),
		"time-entries": fixture(
			f"/timetracking/business/{BUSINESS}/time_entries",
			page,
			{"time_entries": store["time_entries"], "meta": meta(len(store["time_entries"]))},
		),
		"estimates": fixture(
			f"{accounting}/estimates/estimates",
			page | {"include[]": "lines"},
			accounting_list("estimates", store["estimates"]),
		),
		"invoices": fixture(
			f"{accounting}/invoices/invoices",
			page | {"include[]": "lines"},
			accounting_list("invoices", store["invoices"]),
		),
		"credit-notes": fixture(
			f"{accounting}/credit_notes/credit_notes",
			page,
			accounting_list("credit_notes", store["credit_notes"]),
		),
		"bills": fixture(f"{accounting}/bills/bills", page, accounting_list("bills", store["bills"])),
		"payments": fixture(
			f"{accounting}/payments/payments", page, accounting_list("payments", store["payments"])
		),
		"bill-payments": fixture(
			f"{accounting}/bill_payments/bill_payments",
			page,
			accounting_list("bill_payments", store["bill_payments"]),
		),
		"journal-entries": fixture(
			f"{business}/journal_entries",
			{"page_number": 1, "page_size": reader.PER_PAGE},
			{
				"manualJournalEntries": store["journal_entries"],
				"page": {
					"number": 1,
					"size": reader.PER_PAGE,
					"pages": 1,
					"total": len(store["journal_entries"]),
				},
			},
		),
		"general-ledger": fixture(
			f"{business}/reports/general_ledger",
			{"start_date": reader.LEDGER_START},
			{
				"response": {
					"result": {
						"general_ledger": {
							"company_name": "Harbour Studio",
							"start_date": reader.LEDGER_START,
							"end_date": "2026-10-21",
							"currency_code": "CAD",
							"locale": "en",
							"summary_only": False,
							"summary": {"total_credit": money("0.00")},
							"data": store["ledger"],
						}
					}
				}
			},
			{"end_date": LEDGER_TODAY},
		),
	}
	for service, rate in store["rates"].items():
		written[f"rate-{service}"] = fixture(
			f"/comments/business/{BUSINESS}/service/{service}/rate", {}, {"service_rate": rate}
		)
	for bill in store["bills"]:
		written[f"bill-{bill['id']}"] = fixture(
			f"{accounting}/bills/bills/{bill['id']}", {}, {"response": {"result": {"bill": bill}}}
		)
	return written


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
