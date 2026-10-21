# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the PeopleSoft fixtures (tests/fixtures/peoplesoft/<state>/*.json): a small installation's
answers to the queries its field map names (tests/fixtures/peoplesoft/field-map.json), as the REST Query
Access Service answers a JSON/NONFILE request (tests/schemas/peoplesoft-qas.json, transcribed from
PeopleBooks): one row per record, or per line of a document with its header repeated, keyed by the columns
the map names. Some columns carry their record alias (``A.EMPLID``), as a query answers when it names it.
The installation's two states differ only by their rows (v2: a department inactive, an employee
terminated, a supplier renamed, a voucher posted and paid, a new journal).

``python -m tools.peoplesoft_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.sources.peoplesoft import reader
from tests.support.peoplesoft import FIELD_MAP, MAX_ROWS, NODE

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "peoplesoft"
Row = dict[str, Any]


def store_v1() -> dict[str, list[Row]]:
	return {
		"department": [
			{"DEPTID": "10000", "DESCR": "Operations", "PARENT_DEPTID": None, "EFF_STATUS": "A"},
			{"DEPTID": "10100", "DESCR": "Field Services", "PARENT_DEPTID": "10000", "EFF_STATUS": "A"},
		],
		"designation": [
			{"JOBCODE": "KC001", "DESCR": "Field Technician"},
			{"JOBCODE": "KC002", "DESCR": "Operations Manager"},
		],
		"employee": [
			employee("KU0001", "Morgan", "Reyes", "F", "1982-03-14", "2019-04-01", "KC002", "10000", None),
			employee("KU0002", "Avery", "Chen", "M", "1990-07-22", "2021-09-13", "KC001", "10100", "KU0001"),
		],
		"customer": [
			party(
				"CUST01",
				"Harbour Freight Ltd",
				"1 Harbour Rd",
				"Halifax",
				"NS",
				"B3H 1A1",
				"CAN",
				"ap@harbour.example",
			)
		],
		"supplier": [
			party(
				"VEND01",
				"Northern Tools Inc",
				"500 King St W",
				"Toronto",
				"ON",
				"M5V 1L9",
				"CAN",
				"sales@ntools.example",
			)
		],
		"account": [
			{"ACCOUNT": "101000", "DESCR": "Cash", "ACCOUNT_TYPE": "A"},
			{"ACCOUNT": "200000", "DESCR": "Accounts Payable", "ACCOUNT_TYPE": "L"},
			{"ACCOUNT": "400000", "DESCR": "Service Revenue", "ACCOUNT_TYPE": "R"},
			{"ACCOUNT": "640000", "DESCR": "Tools and Supplies", "ACCOUNT_TYPE": "E"},
		],
		"sales_invoice": [
			ar_line("INV0000101", 1, "Site visit", 2, "300.00", "40.00", "640.00", "P"),
			ar_line("INV0000101", 2, "Calibration", 1, "300.00", "40.00", "640.00", "P"),
		],
		"purchase_invoice": [
			voucher_line("00000201", 1, "Torque wrench", 3, "450.00", "450.00", "U"),
		],
		"ap_payment": [],
		"ar_payment": [
			{
				"DEPOSIT_BU": "US001",
				"DEPOSIT_ID": "DEP0001",
				"PAYMENT_SEQ_NUM": 1,
				"PAYMENT_ID": "CHK-5521",
				"CUST_ID": "CUST01",
				"ACCOUNTING_DT": "2026-09-20",
				"PAYMENT_AMT": "640.00",
				"PAYMENT_CURRENCY": "CAD",
				"BUSINESS_UNIT": "US001",
				"INVOICE": "INV0000101",
				"PAY_AMT": "640.00",
			}
		],
		"journal_entry": [
			journal_line("JRN0000301", "2026-09-30", 1, "640000", "125.00"),
			journal_line("JRN0000301", "2026-09-30", 2, "101000", "-125.00"),
		],
	}


def employee(
	emplid: str,
	first: str,
	last: str,
	sex: str,
	birth: str,
	hired: str,
	job: str,
	department: str,
	supervisor: str | None,
) -> Row:
	return {
		"A.EMPLID": emplid,
		"FIRST_NAME": first,
		"LAST_NAME": last,
		"SEX": sex,
		"BIRTHDATE": birth,
		"HIRE_DT": hired,
		"TERMINATION_DT": None,
		"EMPL_STATUS": "A",
		"DEPTID": department,
		"JOBCODE": job,
		"SUPERVISOR_ID": supervisor,
		"EMAIL_ADDR": f"{first.lower()}.{last.lower()}@kiln.example",
	}


def party(
	key: str, name: str, street: str, city: str, state: str, postal: str, country: str, email: str
) -> Row:
	column = "CUST_ID" if key.startswith("CUST") else "VENDOR_ID"
	return {
		column: key,
		"NAME1": name,
		"ADDRESS1": street,
		"CITY": city,
		"STATE": state,
		"POSTAL": postal,
		"COUNTRY": country,
		"EMAIL_ADDR": email,
		"EFF_STATUS": "A",
	}


def ar_line(
	invoice: str, line: int, descr: str, qty: int, amount: str, tax: str, total: str, status: str
) -> Row:
	return {
		"BUSINESS_UNIT": "US001",
		"INVOICE": invoice,
		"INVOICE_DT": "2026-09-15",
		"DUE_DT": "2026-10-15",
		"BILL_TO_CUST_ID": "CUST01",
		"BI_CURRENCY_CD": "CAD",
		"INVOICE_AMOUNT": total,
		"TAX_AMOUNT": tax,
		"INVOICE_STATUS": status,
		"LINE_SEQ_NUM": line,
		"DESCR": descr,
		"QTY": qty,
		"NET_EXTENDED_AMT": amount,
		"ACCOUNT": "400000",
	}


def voucher_line(voucher: str, line: int, descr: str, qty: int, amount: str, gross: str, status: str) -> Row:
	return {
		"BUSINESS_UNIT": "US001",
		"VOUCHER_ID": voucher,
		"INVOICE_ID": "NT-7781",
		"INVOICE_DT": "2026-09-18",
		"DUE_DT": "2026-10-18",
		"VENDOR_ID": "VEND01",
		"TXN_CURRENCY_CD": "CAD",
		"GROSS_AMT": gross,
		"POST_STATUS_AP": status,
		"VOUCHER_LINE_NUM": line,
		"DESCR": descr,
		"QTY_VCHR": qty,
		"MERCHANDISE_AMT": amount,
		"ACCOUNT": "640000",
	}


def journal_line(journal: str, date: str, line: int, account: str, amount: str) -> Row:
	return {
		"BUSINESS_UNIT": "US001",
		"JOURNAL_ID": journal,
		"JOURNAL_DATE": date,
		"DESCR254": "Month-end tool accrual",
		"JRNL_HDR_STATUS": "P",
		"JOURNAL_LINE": line,
		"ACCOUNT": account,
		"MONETARY_AMOUNT": amount,
	}


def store_v2() -> dict[str, list[Row]]:
	store = copy.deepcopy(store_v1())
	store["department"][1]["EFF_STATUS"] = "I"
	store["employee"][1] |= {"EMPL_STATUS": "T", "TERMINATION_DT": "2026-10-09"}
	store["supplier"][0]["NAME1"] = "Northern Tools & Supply Inc"
	store["purchase_invoice"][0]["POST_STATUS_AP"] = "P"
	store["ap_payment"].append(
		{
			"BANK_CD": "RBC01",
			"PYMNT_ID": "0000000451",
			"REMIT_VENDOR": "VEND01",
			"PYMNT_DT": "2026-10-10",
			"PYMNT_AMT": "450.00",
			"CURRENCY_PYMNT": "CAD",
			"BUSINESS_UNIT": "US001",
			"VOUCHER_ID": "00000201",
			"PAID_AMT": "450.00",
		}
	)
	store["journal_entry"] += [
		journal_line("JRN0000302", "2026-10-15", 1, "640000", "80.00"),
		journal_line("JRN0000302", "2026-10-15", 2, "200000", "-80.00"),
	]
	return store


def queries() -> dict[str, str]:
	"""Each store table's query name, from the field map."""
	kinds: dict[str, Any] = FIELD_MAP["kinds"]
	names = {kind: entry["query"] for kind, entry in kinds.items() if isinstance(entry, dict)}
	ap, ar = kinds["payment_entry"]
	return names | {"ap_payment": ap["query"], "ar_payment": ar["query"]}


def answer(query: str, rows: list[Row]) -> Row:
	return {
		"status": reader.SUCCESS,
		"data": {
			"query": {
				"numrows": len(rows),
				"queryname=": query,
				"rows": [{reader.ROW_NUMBER: number, **row} for number, row in enumerate(rows, start=1)],
			}
		},
	}


def fixtures(store: dict[str, list[Row]]) -> dict[str, Row]:
	written: dict[str, Row] = {}
	for table, query in queries().items():
		path = "/".join((reader.GATEWAY, NODE, reader.SERVICE, FIELD_MAP["owner"], query, reader.OUTPUT))
		request = {
			"path": path,
			"query": {"isconnectedquery": "N", "maxrows": str(MAX_ROWS), "json_resp": "true"},
		}
		written[query] = {"request": request, "response": answer(query, store[table])}
	return written


def write(state: str, store: dict[str, list[Row]]) -> None:
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
