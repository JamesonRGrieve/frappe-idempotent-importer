# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes tests/schemas/freshbooks.json: the FreshBooks API answers the adapter reads, as an OpenAPI document
transcribed from FreshBooks' API reference (FreshBooks publishes no OpenAPI document; each path below cites its
page under https://www.freshbooks.com/api/). Each record lists the documented fields the importer reads or
the reference's examples show; a field without a value is null. Money is FreshBooks' ``{amount, code}``: an
exact decimal string and a three-letter currency code.

``python -m tools.freshbooks_schema`` rewrites it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests" / "schemas" / "freshbooks.json"
DOCS = "https://www.freshbooks.com/api/"
Schema = dict[str, Any]
STRING, INTEGER, NUMBER, BOOLEAN = "string", "integer", "number", "boolean"


def nullable(kind: str) -> Schema:
	return {"type": [kind, "null"]}


def ref(name: str) -> Schema:
	return {"$ref": f"#/components/schemas/{name}"}


def obj(properties: dict[str, Schema], required: tuple[str, ...] = (), page: str | None = None) -> Schema:
	schema: Schema = {"type": "object", "properties": properties, "additionalProperties": False}
	if required:
		schema["required"] = list(required)
	if page:
		schema["description"] = DOCS + page
	return schema


MONEY = obj({"amount": nullable(STRING), "code": nullable(STRING)}, ("amount", "code"))
MAYBE_MONEY = {"oneOf": [ref("Money"), {"type": "null"}]}


def fields(**kinds: str) -> dict[str, Schema]:
	"""Fields by their kind: a JSON type (nullable), or ``money``."""
	return {name: MAYBE_MONEY if kind == "money" else nullable(kind) for name, kind in kinds.items()}


RECORDS: dict[str, tuple[str, Schema]] = {
	"Tax": (
		"taxes",
		obj(
			fields(
				taxid="integer",
				id="integer",
				name="string",
				amount="string",
				number="string",
				accounting_systemid="string",
				updated="string",
				compound="boolean",
			),
			("id",),
		),
	),
	"Item": (
		"items",
		obj(
			fields(
				itemid="integer",
				id="integer",
				accounting_systemid="string",
				name="string",
				description="string",
				qty="string",
				inventory="string",
				unit_cost="money",
				tax1="integer",
				tax2="integer",
				vis_state="integer",
				updated="string",
				sku="string",
			),
			("id",),
		),
	),
	"Client": (
		"clients",
		obj(
			fields(
				id="integer",
				userid="integer",
				fname="string",
				lname="string",
				organization="string",
				email="string",
				home_phone="string",
				bus_phone="string",
				mob_phone="string",
				p_street="string",
				p_street2="string",
				p_city="string",
				p_province="string",
				p_code="string",
				p_country="string",
				currency_code="string",
				vis_state="integer",
				updated="string",
				accounting_systemid="string",
				vat_name="string",
				vat_number="string",
			),
			("id",),
		),
	),
	"Vendor": (
		"vendors",
		obj(
			fields(
				vendorid="integer",
				vendor_name="string",
				primary_contact_first_name="string",
				primary_contact_last_name="string",
				primary_contact_email="string",
				phone="string",
				website="string",
				street="string",
				street2="string",
				city="string",
				province="string",
				postal_code="string",
				country="string",
				currency_code="string",
				account_number="string",
				is_1099="boolean",
				vis_state="integer",
				created_at="string",
				updated_at="string",
			)
			| {"tax_defaults": {"type": "array"}},
			("vendorid",),
		),
	),
	"Line": (
		"invoices",
		obj(
			fields(
				lineid="integer",
				name="string",
				description="string",
				qty="string",
				unit_cost="money",
				amount="money",
				taxName1="string",
				taxAmount1="string",
				taxName2="string",
				taxAmount2="string",
				type="integer",
				updated="string",
			),
		),
	),
	"Estimate": (
		"estimates",
		obj(
			fields(
				estimateid="integer",
				id="integer",
				estimate_number="string",
				customerid="integer",
				create_date="string",
				currency_code="string",
				amount="money",
				discount_value="string",
				discount_total="money",
				status="integer",
				ui_status="string",
				vis_state="integer",
				notes="string",
				terms="string",
			)
			| {"lines": {"type": "array", "items": ref("Line")}},
			("id",),
		),
	),
	"Invoice": (
		"invoices",
		obj(
			fields(
				invoiceid="integer",
				id="integer",
				accountid="string",
				invoice_number="string",
				customerid="integer",
				create_date="string",
				due_date="string",
				currency_code="string",
				amount="money",
				outstanding="money",
				paid="money",
				discount_value="string",
				discount_total="money",
				status="integer",
				v3_status="string",
				payment_status="string",
				display_status="string",
				vis_state="integer",
			)
			| {"lines": {"type": "array", "items": ref("Line")}},
			("id",),
		),
	),
	"CreditNote": (
		"credits",
		obj(
			fields(
				creditid="integer",
				id="integer",
				credit_number="string",
				clientid="string",
				create_date="string",
				credit_type="string",
				amount="money",
				paid="money",
				payment_status="string",
				display_status="string",
				status="string",
				vis_state="integer",
			),
			("creditid",),
		),
	),
	"BillLine": (
		"bills",
		obj(
			fields(
				id="integer",
				list_index="integer",
				description="string",
				quantity="string",
				unit_cost="money",
				amount="money",
				tax_name1="string",
				tax_percent1="string",
				tax_amount1="money",
				tax_name2="string",
				tax_percent2="string",
				tax_amount2="money",
				categoryid="integer",
			)
			| {
				"category": {
					"oneOf": [obj(fields(category="string", categoryid="integer")), {"type": "null"}]
				}
			},
		),
	),
	"Bill": (
		"bills",
		obj(
			fields(
				id="integer",
				bill_number="string",
				vendorid="integer",
				issue_date="string",
				due_date="string",
				currency_code="string",
				amount="money",
				total_amount="money",
				tax_amount="money",
				outstanding="money",
				paid="money",
				status="string",
				vis_state="integer",
				created_at="string",
				updated_at="string",
			)
			| {"lines": {"type": "array", "items": ref("BillLine")}},
			("id",),
		),
	),
	"Payment": (
		"payments",
		obj(
			fields(
				id="integer",
				logid="integer",
				invoiceid="integer",
				creditid="integer",
				amount="money",
				date="string",
				type="string",
				note="string",
				clientid="integer",
				vis_state="integer",
				updated="string",
				gateway="string",
				from_credit="boolean",
				overpaymentid="integer",
				orderid="integer",
				accounting_systemid="string",
			),
			("id",),
		),
	),
	"BillPayment": (
		"bill_payments",
		obj(
			fields(
				id="integer",
				billid="integer",
				amount="money",
				payment_type="string",
				paid_date="string",
				note="string",
				vis_state="integer",
				matched_with_expense="boolean",
			),
			("id", "billid"),
		),
	),
}
LISTS = {
	"taxes": ("Tax", "taxes"),
	"items": ("Item", "items"),
	"users/clients": ("Client", "clients"),
	"bill_vendors/bill_vendors": ("Vendor", "bill_vendors"),
	"estimates/estimates": ("Estimate", "estimates"),
	"invoices/invoices": ("Invoice", "invoices"),
	"credit_notes/credit_notes": ("CreditNote", "credit_notes"),
	"bills/bills": ("Bill", "bills"),
	"payments/payments": ("Payment", "payments"),
	"bill_payments/bill_payments": ("BillPayment", "bill_payments"),
}
PATHS = {"taxes": "taxes/taxes", "items": "items/items"}


def accounting_list(record: str, key: str) -> Schema:
	result = obj(
		{
			key: {"type": "array", "items": ref(record)},
			"page": {"type": INTEGER},
			"pages": {"type": INTEGER},
			"per_page": {"type": INTEGER},
			"total": {"type": INTEGER},
		},
		(key, "page", "pages", "per_page", "total"),
	)
	return obj({"response": obj({"result": result}, ("result",))}, ("response",))


def meta() -> Schema:
	return obj(
		{
			"page": {"type": INTEGER},
			"pages": {"type": INTEGER},
			"per_page": {"type": INTEGER},
			"total": {"type": INTEGER},
			"total_logged": {"type": INTEGER},
			"total_unbilled": {"type": INTEGER},
		},
		("page", "pages", "per_page", "total"),
	)


def ok(schema: Schema, page: str) -> Schema:
	return {
		"get": {
			"externalDocs": {"url": DOCS + page},
			"responses": {"200": {"description": "OK", "content": {"application/json": {"schema": schema}}}},
		}
	}


def document() -> Schema:
	schemas: dict[str, Schema] = {name: schema for name, (_, schema) in RECORDS.items()} | {"Money": MONEY}
	schemas["Service"] = obj(
		fields(business_id="integer", id="integer", name="string", billable="boolean", vis_state="integer"),
		("id",),
	)
	schemas["ServiceRate"] = obj(fields(rate="string", business_id="integer", service_id="integer"))
	schemas["Project"] = obj(
		fields(
			id="integer",
			title="string",
			description="string",
			due_date="string",
			client_id="integer",
			internal="boolean",
			budget="integer",
			fixed_price="string",
			rate="string",
			billing_method="string",
			project_type="string",
			active="boolean",
			complete="boolean",
			sample="boolean",
			created_at="string",
			updated_at="string",
			logged_duration="integer",
		)
		| {"services": {"type": "array"}},
		("id",),
	)
	schemas["TimeEntry"] = obj(
		fields(
			id="integer",
			identity_id="integer",
			is_logged="boolean",
			started_at="string",
			client_id="integer",
			project_id="integer",
			service_id="integer",
			note="string",
			active="boolean",
			billable="boolean",
			billed="boolean",
			internal="boolean",
			duration="integer",
		)
		| {"timer": {"oneOf": [obj(fields(id="integer", is_running="boolean")), {"type": "null"}]}},
		("id",),
	)
	sub_account = obj(
		fields(
			account_uuid="string",
			account_name="string",
			account_number="string",
			account_type="string",
			account_sub_type="string",
			currency_code="string",
			system_account_name="string",
			auto_created="boolean",
			state="string",
			parent_account_uuid="string",
		)
		| {"balance": MAYBE_MONEY},
		("account_uuid",),
	)
	schemas["SubAccount"] = sub_account
	schemas["Account"] = obj(
		{**sub_account["properties"], "sub_accounts": {"type": "array", "items": ref("SubAccount")}},
		("account_uuid",),
	)
	transaction = obj(
		fields(
			entity_type="string",
			reference="string",
			description="string",
			date="string",
			client_name="string",
			expenseid="integer",
			invoiceid="integer",
			paymentid="integer",
			incomeid="integer",
			creditid="integer",
			clientid="integer",
			billid="integer",
			bill_paymentid="integer",
		)
		| {"debit": MAYBE_MONEY, "credit": MAYBE_MONEY},
	)
	schemas["LedgerTransaction"] = transaction
	movement = {
		"debit": MAYBE_MONEY,
		"credit": MAYBE_MONEY,
		"net_movement_debit": MAYBE_MONEY,
		"net_movement_credit": MAYBE_MONEY,
	}
	schemas["LedgerSubAccount"] = obj(
		fields(
			sub_accountid="string",
			sub_account_name="string",
			sub_account_number="string",
			account_type="string",
			account_sub_type="string",
		)
		| movement
		| {"transactions": {"type": "array", "items": ref("LedgerTransaction")}},
		("sub_accountid",),
	)
	schemas["LedgerAccount"] = obj(
		fields(
			accountid="string",
			account_name="string",
			account_number="string",
			account_type="string",
			account_sub_type="string",
		)
		| movement
		| {
			"sub_accounts": {"type": "array", "items": ref("LedgerSubAccount")},
			"transactions": {"type": "array", "items": ref("LedgerTransaction")},
		},
		("accountid",),
	)
	schemas["JournalDetail"] = obj(
		fields(id="string", accountId="string", type="string", manualJournalEntryId="string")
		| {"amount": MAYBE_MONEY},
		("accountId", "type"),
	)
	schemas["JournalEntry"] = obj(
		fields(id="string", name="string", journalEntryNumber="string", description="string")
		| {
			"userEnteredDate": obj(
				{"year": {"type": INTEGER}, "month": {"type": INTEGER}, "day": {"type": INTEGER}},
				("year", "month", "day"),
			),
			"details": {"type": "array", "items": ref("JournalDetail")},
		},
		("id",),
	)
	schemas["Membership"] = obj(
		{
			"id": {"type": INTEGER},
			"role": {"type": STRING},
			"business": obj(
				{
					"id": {"type": INTEGER},
					"business_uuid": {"type": STRING},
					"name": {"type": STRING},
					"account_id": {"type": STRING},
				},
				("id", "business_uuid", "account_id"),
			),
		},
		("business",),
	)
	accounting = "/accounting/account/{accountId}/"
	paths: dict[str, Schema] = {
		"/auth/api/v1/users/me": ok(
			obj(
				{
					"response": obj(
						{
							"id": {"type": INTEGER},
							"business_memberships": {"type": "array", "items": ref("Membership")},
						},
						("business_memberships",),
					)
				},
				("response",),
			),
			"identity_model",
		),
	}
	for path, (record, key) in LISTS.items():
		full = accounting + PATHS.get(path, path)
		paths[full] = ok(accounting_list(record, key), RECORDS[record][0])
	paths[accounting + "bills/bills/{billId}"] = ok(
		obj({"response": obj({"result": obj({"bill": ref("Bill")}, ("bill",))}, ("result",))}, ("response",)),
		"bills",
	)
	paths["/comments/business/{businessId}/services"] = ok(
		obj({"services": {"type": "array", "items": ref("Service")}, "meta": meta()}, ("services", "meta")),
		"services",
	)
	paths["/comments/business/{businessId}/service/{serviceId}/rate"] = ok(
		obj({"service_rate": ref("ServiceRate")}, ("service_rate",)), "services"
	)
	paths["/projects/business/{businessId}/projects"] = ok(
		obj({"projects": {"type": "array", "items": ref("Project")}, "meta": meta()}, ("projects", "meta")),
		"project",
	)
	paths["/timetracking/business/{businessId}/time_entries"] = ok(
		obj(
			{"time_entries": {"type": "array", "items": ref("TimeEntry")}, "meta": meta()},
			("time_entries", "meta"),
		),
		"time_entries",
	)
	business = "/accounting/businesses/{businessUuid}/"
	paths[business + "reports/chart_of_accounts"] = ok(
		obj(
			{
				"response": obj(
					{
						"result": obj(
							{"journal_entry_accounts": {"type": "array", "items": ref("Account")}},
							("journal_entry_accounts",),
						)
					},
					("result",),
				)
			},
			("response",),
		),
		"chart-of-accounts",
	)
	ledger = obj(
		fields(
			company_name="string",
			start_date="string",
			end_date="string",
			currency_code="string",
			locale="string",
			summary_only="boolean",
		)
		| {
			"download_token": nullable(STRING),
			"summary": {"type": "object"},
			"data": {"type": "array", "items": ref("LedgerAccount")},
		},
		("data",),
	)
	paths[business + "reports/general_ledger"] = ok(
		obj(
			{"response": obj({"result": obj({"general_ledger": ledger}, ("general_ledger",))}, ("result",))},
			("response",),
		),
		"general-ledger-report",
	)
	paths[business + "journal_entries"] = ok(
		obj(
			{
				"manualJournalEntries": {"type": "array", "items": ref("JournalEntry")},
				"page": obj(
					{
						"number": {"type": INTEGER},
						"size": {"type": INTEGER},
						"pages": {"type": INTEGER},
						"total": {"type": INTEGER},
					}
				),
			},
			("manualJournalEntries",),
		),
		"journal-entries",
	)
	return {
		"openapi": "3.0.3",
		"info": {
			"title": "FreshBooks API: the answers read by frappe-idempotent-importer",
			"version": "2026-10-21",
			"description": (__doc__ or "").strip(),
		},
		"paths": paths,
		"components": {"schemas": schemas},
	}


def main() -> int:
	TARGET.write_text(json.dumps(document(), indent=1, ensure_ascii=False) + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
