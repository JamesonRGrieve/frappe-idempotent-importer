# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the Odoo fixtures (tests/fixtures/odoo/<state>/*.json) by running the real OdooReader against a
small Odoo 19 database held in memory, and recording each JSON-2 request it sends with the answer Odoo
gives it: ``search_read`` and ``read`` filter (Odoo's domain operators), order by id and page by offset and
limit, and answer with ``id`` and the requested fields only; ``fields_get`` answers the model's fields. So
every fixture is what the reader asks, shaped as tests/schemas/odoo.json (transcribed from Odoo 19's model
definitions) says Odoo answers, and the store's two states differ only by their records.

``python -m tools.odoo_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from idempotent_importer.core.http import HttpResponse
from idempotent_importer.sources.odoo.adapter import OdooAdapter
from tests.support.odoo import ODOO_CONFIG
from tests.support.transport import FakeTransport, RecordingSleeper

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "odoo"
JSON2 = "/json/2/"
CONFIG = ODOO_CONFIG
F = False
TIME = "2026-03-01 09:00:00"
LATER = "2026-04-01 09:00:00"

type Record = dict[str, Any]
type Table = dict[int, Record]

# The fields of each model the reader reads, as Odoo 19 defines them (a database answers fields_get with
# every field it has; these are the ones the fixtures hold).
MODELS: dict[str, tuple[str, ...]] = {
	"res.company": ("name", "currency_id", "partner_id"),
	"res.users": ("partner_id",),
	"res.partner": (
		"name",
		"is_company",
		"parent_id",
		"type",
		"street",
		"street2",
		"city",
		"state_id",
		"zip",
		"country_code",
		"email",
		"phone",
		"vat",
		"ref",
		"website",
		"function",
		"active",
		"write_date",
		"commercial_partner_id",
		"customer_rank",
		"supplier_rank",
	),
	"account.account": ("code", "name", "account_type", "currency_id", "reconcile", "active", "write_date"),
	"account.tax.repartition.line": ("account_id",),
	"account.payment.method.line": ("payment_account_id",),
	"account.analytic.account": ("name", "code", "plan_id", "active", "company_id", "write_date"),
	"stock.warehouse": ("name", "code", "partner_id", "active", "company_id", "write_date"),
	"product.category": ("name", "parent_id", "write_date"),
	"product.template": (
		"name",
		"default_code",
		"barcode",
		"list_price",
		"type",
		"is_storable",
		"categ_id",
		"description_sale",
		"sale_ok",
		"purchase_ok",
		"active",
		"weight",
		"qty_available",
		"product_variant_count",
		"is_published",
		"write_date",
	),
	"product.product": (
		"product_tmpl_id",
		"default_code",
		"barcode",
		"lst_price",
		"active",
		"qty_available",
		"weight",
		"product_template_attribute_value_ids",
		"product_variant_count",
	),
	"product.template.attribute.value": ("name", "attribute_id"),
	"hr.department": ("name", "parent_id", "active", "company_id", "write_date"),
	"hr.job": ("name", "active", "company_id", "write_date"),
	"hr.employee": (
		"name",
		"sex",
		"birthday",
		"department_id",
		"job_id",
		"parent_id",
		"work_email",
		"private_email",
		"mobile_phone",
		"work_phone",
		"active",
		"departure_date",
		"contract_date_start",
		"company_id",
		"write_date",
	),
	"project.project": (
		"name",
		"partner_id",
		"date_start",
		"date",
		"active",
		"description",
		"last_update_status",
		"account_id",
		"company_id",
		"write_date",
	),
	"crm.lead": (
		"name",
		"type",
		"partner_id",
		"partner_name",
		"contact_name",
		"email_from",
		"phone",
		"function",
		"website",
		"probability",
		"expected_revenue",
		"date_deadline",
		"active",
		"description",
		"company_id",
		"create_date",
		"write_date",
	),
	"sale.order": (
		"name",
		"state",
		"partner_id",
		"date_order",
		"commitment_date",
		"validity_date",
		"currency_id",
		"amount_untaxed",
		"amount_tax",
		"amount_total",
		"client_order_ref",
		"note",
		"company_id",
		"write_date",
	),
	"sale.order.line": (
		"order_id",
		"sequence",
		"display_type",
		"product_id",
		"name",
		"product_uom_qty",
		"price_unit",
		"discount",
		"price_subtotal",
		"price_tax",
		"price_total",
	),
	"purchase.order": (
		"name",
		"state",
		"partner_id",
		"date_order",
		"date_planned",
		"currency_id",
		"amount_untaxed",
		"amount_tax",
		"amount_total",
		"partner_ref",
		"note",
		"company_id",
		"write_date",
	),
	"purchase.order.line": (
		"order_id",
		"sequence",
		"display_type",
		"product_id",
		"name",
		"product_qty",
		"price_unit",
		"discount",
		"price_subtotal",
		"price_tax",
		"price_total",
	),
	"account.move": (
		"name",
		"move_type",
		"state",
		"partner_id",
		"commercial_partner_id",
		"invoice_date",
		"invoice_date_due",
		"date",
		"currency_id",
		"amount_total",
		"amount_total_signed",
		"ref",
		"narration",
		"reversed_entry_id",
		"journal_id",
		"origin_payment_id",
		"company_id",
		"write_date",
	),
	"account.move.line": (
		"move_id",
		"sequence",
		"display_type",
		"product_id",
		"name",
		"quantity",
		"price_unit",
		"discount",
		"price_subtotal",
		"account_id",
		"partner_id",
		"analytic_distribution",
		"debit",
		"credit",
		"amount_currency",
		"currency_id",
		"matched_debit_ids",
		"matched_credit_ids",
	),
	"account.partial.reconcile": ("debit_move_id", "credit_move_id", "amount"),
	"account.payment": (
		"name",
		"payment_type",
		"partner_type",
		"partner_id",
		"amount",
		"currency_id",
		"date",
		"state",
		"memo",
		"move_id",
		"outstanding_account_id",
		"destination_account_id",
		"company_id",
		"write_date",
	),
}
# The many2one fields, and the model each names (the display name Odoo sends with the id).
MANY2ONE: dict[str, str] = {
	"currency_id": "res.currency",
	"partner_id": "res.partner",
	"parent_id": "",
	"state_id": "res.country.state",
	"commercial_partner_id": "res.partner",
	"account_id": "account.account",
	"plan_id": "account.analytic.plan",
	"company_id": "res.company",
	"categ_id": "product.category",
	"product_tmpl_id": "product.template",
	"attribute_id": "product.attribute",
	"department_id": "hr.department",
	"job_id": "hr.job",
	"order_id": "",
	"product_id": "product.product",
	"move_id": "account.move",
	"reversed_entry_id": "account.move",
	"journal_id": "account.journal",
	"origin_payment_id": "account.payment",
	"debit_move_id": "account.move.line",
	"credit_move_id": "account.move.line",
	"outstanding_account_id": "account.account",
	"destination_account_id": "account.account",
	"payment_account_id": "account.account",
}
ONE2MANY = frozenset({"product_template_attribute_value_ids", "matched_debit_ids", "matched_credit_ids"})
# Names of records the fixtures name but the reader never lists.
NAMES: dict[str, dict[int, str]] = {
	"res.currency": {1: "CAD"},
	"res.country.state": {66: "Alberta (CA)"},
	"account.analytic.plan": {1: "Projects"},
	"product.attribute": {1: "Wood"},
	"account.journal": {1: "Customer Invoices", 2: "Vendor Bills", 3: "Miscellaneous Operations", 4: "Bank"},
}
COMPANY = [1, "Harbour Pine Trading"]
CAD = [1, "CAD"]


def partner(id_: int, name: str, **fields: Any) -> Record:
	record: Record = {
		"name": name,
		"is_company": False,
		"parent_id": F,
		"type": "contact",
		"street": F,
		"street2": F,
		"city": F,
		"state_id": F,
		"zip": F,
		"country_code": F,
		"email": F,
		"phone": F,
		"vat": F,
		"ref": F,
		"website": F,
		"function": F,
		"active": True,
		"write_date": TIME,
		"commercial_partner_id": [id_, name],
		"customer_rank": 0,
		"supplier_rank": 0,
	}
	return record | fields


def move_line(id_: int, move: int, account: int, debit: str, credit: str, **fields: Any) -> Record:
	record: Record = {
		"move_id": move,
		"sequence": 10,
		"display_type": "product",
		"product_id": F,
		"name": F,
		"quantity": 1.0,
		"price_unit": 0.0,
		"discount": 0.0,
		"price_subtotal": 0.0,
		"account_id": account,
		"partner_id": F,
		"analytic_distribution": F,
		"debit": float(debit),
		"credit": float(credit),
		"amount_currency": float(debit) - float(credit),
		"currency_id": CAD,
		"matched_debit_ids": [],
		"matched_credit_ids": [],
	}
	return record | fields


def store_v1() -> dict[str, Table]:
	"""Harbour Pine Trading's database, as first read."""
	address = {
		"street": "12 Harbour Road",
		"city": "Edmonton",
		"state_id": [66, "Alberta (CA)"],
		"zip": "T5J 0N3",
		"country_code": "CA",
	}
	db: dict[str, Table] = {
		"res.company": {
			1: {"name": "Harbour Pine Trading", "currency_id": CAD, "partner_id": [1, "Harbour Pine Trading"]}
		},
		"res.users": {2: {"partner_id": [3, "Mitchell Admin"]}},
		"res.partner": {
			1: partner(1, "Harbour Pine Trading", is_company=True, **address),
			3: partner(3, "Mitchell Admin"),
			7: partner(
				7,
				"Deco Addict",
				is_company=True,
				email="info@decoaddict.example",
				phone="+1 780 555 0107",
				vat="CA123456789",
				customer_rank=3,
				website="https://decoaddict.example",
				street="77 Santa Barbara Rd",
				city="Calgary",
				state_id=[66, "Alberta (CA)"],
				zip="T2P 1J9",
				country_code="CA",
			),
			8: partner(
				8,
				"Douglas Fletcher",
				parent_id=[7, "Deco Addict"],
				email="douglas.fletcher@decoaddict.example",
				function="Purchasing Manager",
				commercial_partner_id=[7, "Deco Addict"],
			),
			9: partner(
				9,
				"Deco Addict, Warehouse",
				parent_id=[7, "Deco Addict"],
				type="delivery",
				street="5 Dock Lane",
				city="Calgary",
				zip="T2G 0A1",
				country_code="CA",
				commercial_partner_id=[7, "Deco Addict"],
			),
			14: partner(
				14,
				"Wood Corner",
				is_company=True,
				email="sales@woodcorner.example",
				supplier_rank=2,
				street="1 Mill Street",
				city="Red Deer",
				country_code="CA",
			),
			16: partner(16, "Lumber Lane Interiors", email="hello@lumberlane.example"),
		},
		"account.account": {
			101: {
				"code": "121000",
				"name": "Account Receivable",
				"account_type": "asset_receivable",
				"reconcile": True,
			},
			102: {
				"code": "211000",
				"name": "Account Payable",
				"account_type": "liability_payable",
				"reconcile": True,
			},
			103: {"code": "101401", "name": "Bank", "account_type": "asset_cash", "reconcile": False},
			104: {
				"code": "101402",
				"name": "Outstanding Receipts",
				"account_type": "asset_current",
				"reconcile": True,
			},
			105: {
				"code": "101403",
				"name": "Outstanding Payments",
				"account_type": "asset_current",
				"reconcile": True,
			},
			106: {"code": "400000", "name": "Product Sales", "account_type": "income", "reconcile": False},
			107: {"code": "600000", "name": "Expenses", "account_type": "expense", "reconcile": False},
			108: {
				"code": "251000",
				"name": "Tax Received",
				"account_type": "liability_current",
				"reconcile": False,
			},
			109: {"code": "131000", "name": "Tax Paid", "account_type": "asset_current", "reconcile": False},
			110: {"code": "990000", "name": "Commitments", "account_type": "off_balance", "reconcile": False},
		},
		"account.tax.repartition.line": {1: {"account_id": 108}, 2: {"account_id": 109}},
		"account.payment.method.line": {1: {"payment_account_id": 104}, 2: {"payment_account_id": 105}},
		"account.analytic.account": {
			201: {
				"name": "Office Design",
				"code": "PRJ",
				"plan_id": [1, "Projects"],
				"active": True,
				"company_id": COMPANY,
			}
		},
		"stock.warehouse": {
			1: {
				"name": "Harbour Warehouse",
				"code": "WH",
				"partner_id": [1, "Harbour Pine Trading"],
				"active": True,
				"company_id": COMPANY,
			}
		},
		"product.category": {
			1: {"name": "All", "parent_id": F},
			2: {"name": "Saleable", "parent_id": [1, "All"]},
		},
		"product.template": {
			10: {
				"name": "Office Chair",
				"default_code": "CHAIR",
				"barcode": "0601234567890",
				"list_price": 150.0,
				"type": "consu",
				"is_storable": True,
				"categ_id": [2, "All / Saleable"],
				"description_sale": "Ergonomic chair",
				"sale_ok": True,
				"purchase_ok": True,
				"active": True,
				"weight": 9.5,
				"qty_available": 12.0,
				"product_variant_count": 1,
				"is_published": True,
			},
			11: {
				"name": "Desk",
				"default_code": F,
				"barcode": F,
				"list_price": 400.0,
				"type": "consu",
				"is_storable": True,
				"categ_id": [2, "All / Saleable"],
				"description_sale": F,
				"sale_ok": True,
				"purchase_ok": True,
				"active": True,
				"weight": 0.0,
				"qty_available": 5.0,
				"product_variant_count": 2,
				"is_published": False,
			},
			12: {
				"name": "Consulting",
				"default_code": "CONSULT",
				"barcode": F,
				"list_price": 90.0,
				"type": "service",
				"is_storable": False,
				"categ_id": [1, "All"],
				"description_sale": F,
				"sale_ok": True,
				"purchase_ok": False,
				"active": True,
				"weight": 0.0,
				"qty_available": 0.0,
				"product_variant_count": 1,
				"is_published": False,
			},
		},
		"product.product": {
			20: {
				"product_tmpl_id": [10, "Office Chair"],
				"default_code": "CHAIR",
				"barcode": "0601234567890",
				"lst_price": 150.0,
				"active": True,
				"qty_available": 12.0,
				"weight": 9.5,
				"product_template_attribute_value_ids": [],
				"product_variant_count": 1,
			},
			21: {
				"product_tmpl_id": [11, "Desk"],
				"default_code": "DESK-OAK",
				"barcode": F,
				"lst_price": 400.0,
				"active": True,
				"qty_available": 3.0,
				"weight": 30.0,
				"product_template_attribute_value_ids": [1],
				"product_variant_count": 2,
			},
			22: {
				"product_tmpl_id": [11, "Desk"],
				"default_code": "DESK-WAL",
				"barcode": F,
				"lst_price": 450.0,
				"active": True,
				"qty_available": 2.0,
				"weight": 30.0,
				"product_template_attribute_value_ids": [2],
				"product_variant_count": 2,
			},
			23: {
				"product_tmpl_id": [12, "Consulting"],
				"default_code": "CONSULT",
				"barcode": F,
				"lst_price": 90.0,
				"active": True,
				"qty_available": 0.0,
				"weight": 0.0,
				"product_template_attribute_value_ids": [],
				"product_variant_count": 1,
			},
		},
		"product.template.attribute.value": {
			1: {"name": "Oak", "attribute_id": [1, "Wood"]},
			2: {"name": "Walnut", "attribute_id": [1, "Wood"]},
		},
		"hr.department": {
			1: {"name": "Administration", "parent_id": F, "active": True, "company_id": COMPANY},
			2: {"name": "Sales", "parent_id": [1, "Administration"], "active": True, "company_id": COMPANY},
		},
		"hr.job": {1: {"name": "Sales Manager", "active": True, "company_id": COMPANY}},
		"hr.employee": {
			1: {
				"name": "Mitchell Admin",
				"sex": "male",
				"birthday": "1980-02-14",
				"department_id": [1, "Administration"],
				"job_id": F,
				"parent_id": F,
				"work_email": "admin@harbourpine.example",
				"private_email": F,
				"mobile_phone": "+1 780 555 0100",
				"work_phone": F,
				"active": True,
				"departure_date": F,
				"contract_date_start": "2020-01-06",
				"company_id": COMPANY,
			},
			2: {
				"name": "Marc Demo",
				"sex": F,
				"birthday": F,
				"department_id": [2, "Administration / Sales"],
				"job_id": [1, "Sales Manager"],
				"parent_id": [1, "Mitchell Admin"],
				"work_email": "marc@harbourpine.example",
				"private_email": F,
				"mobile_phone": F,
				"work_phone": "+1 780 555 0101",
				"active": True,
				"departure_date": F,
				"contract_date_start": F,
				"company_id": COMPANY,
			},
		},
		"project.project": {
			1: {
				"name": "Office Design",
				"partner_id": [8, "Deco Addict, Douglas Fletcher"],
				"date_start": "2026-03-01",
				"date": "2026-05-31",
				"active": True,
				"description": "<p>New HQ fit-out</p>",
				"last_update_status": "on_track",
				"account_id": [201, "Office Design"],
				"company_id": COMPANY,
			}
		},
		"crm.lead": {
			1: {
				"name": "Website enquiry",
				"type": "lead",
				"partner_id": F,
				"partner_name": F,
				"contact_name": "Rae Lead",
				"email_from": "rae.website@example.test",
				"phone": F,
				"function": F,
				"website": F,
				"probability": 10.0,
				"expected_revenue": 0.0,
				"date_deadline": F,
				"active": True,
				"description": F,
				"company_id": COMPANY,
				"create_date": "2026-02-20 10:00:00",
			},
			2: {
				"name": "Desks for HQ",
				"type": "opportunity",
				"partner_id": [7, "Deco Addict"],
				"partner_name": "Deco Addict",
				"contact_name": "Douglas Fletcher",
				"email_from": "douglas.fletcher@decoaddict.example",
				"phone": F,
				"function": "Purchasing Manager",
				"website": F,
				"probability": 40.0,
				"expected_revenue": 4500.0,
				"date_deadline": "2026-04-30",
				"active": True,
				"description": "<p>Twelve desks</p>",
				"company_id": COMPANY,
				"create_date": "2026-02-25 10:00:00",
			},
			3: {
				"name": "Showroom chairs",
				"type": "opportunity",
				"partner_id": F,
				"partner_name": "Prospect Co",
				"contact_name": "Pat Prospect",
				"email_from": "pat@prospect.example",
				"phone": F,
				"function": F,
				"website": F,
				"probability": 20.0,
				"expected_revenue": 1200.0,
				"date_deadline": F,
				"active": True,
				"description": F,
				"company_id": COMPANY,
				"create_date": "2026-02-26 10:00:00",
			},
		},
		"sale.order": {
			1: {
				"name": "S00001",
				"state": "sale",
				"partner_id": [8, "Deco Addict, Douglas Fletcher"],
				"date_order": "2026-03-02 15:00:00",
				"commitment_date": "2026-03-20 15:00:00",
				"validity_date": F,
				"currency_id": CAD,
				"amount_untaxed": 300.0,
				"amount_tax": 15.0,
				"amount_total": 315.0,
				"client_order_ref": "DA-PO-77",
				"note": F,
				"company_id": COMPANY,
			},
			2: {
				"name": "S00002",
				"state": "sent",
				"partner_id": [7, "Deco Addict"],
				"date_order": "2026-03-04 15:00:00",
				"commitment_date": F,
				"validity_date": "2026-04-04",
				"currency_id": CAD,
				"amount_untaxed": 850.0,
				"amount_tax": 42.5,
				"amount_total": 892.5,
				"client_order_ref": F,
				"note": "<p>Valid one month</p>",
				"company_id": COMPANY,
			},
		},
		"sale.order.line": {
			1: {
				"order_id": [1, "S00001"],
				"sequence": 10,
				"display_type": F,
				"product_id": [20, "[CHAIR] Office Chair"],
				"name": "[CHAIR] Office Chair",
				"product_uom_qty": 2.0,
				"price_unit": 150.0,
				"discount": 0.0,
				"price_subtotal": 300.0,
				"price_tax": 15.0,
				"price_total": 315.0,
			},
			2: {
				"order_id": [1, "S00001"],
				"sequence": 20,
				"display_type": "line_note",
				"product_id": F,
				"name": "Deliver to the dock",
				"product_uom_qty": 0.0,
				"price_unit": 0.0,
				"discount": 0.0,
				"price_subtotal": 0.0,
				"price_tax": 0.0,
				"price_total": 0.0,
			},
			3: {
				"order_id": [2, "S00002"],
				"sequence": 10,
				"display_type": F,
				"product_id": [21, "[DESK-OAK] Desk (Oak)"],
				"name": "[DESK-OAK] Desk (Oak)",
				"product_uom_qty": 1.0,
				"price_unit": 400.0,
				"discount": 0.0,
				"price_subtotal": 400.0,
				"price_tax": 20.0,
				"price_total": 420.0,
			},
			4: {
				"order_id": [2, "S00002"],
				"sequence": 20,
				"display_type": F,
				"product_id": [22, "[DESK-WAL] Desk (Walnut)"],
				"name": "[DESK-WAL] Desk (Walnut)",
				"product_uom_qty": 1.0,
				"price_unit": 450.0,
				"discount": 0.0,
				"price_subtotal": 450.0,
				"price_tax": 22.5,
				"price_total": 472.5,
			},
		},
		"purchase.order": {
			1: {
				"name": "P00001",
				"state": "purchase",
				"partner_id": [14, "Wood Corner"],
				"date_order": "2026-02-27 12:00:00",
				"date_planned": "2026-03-06 12:00:00",
				"currency_id": CAD,
				"amount_untaxed": 800.0,
				"amount_tax": 40.0,
				"amount_total": 840.0,
				"partner_ref": "WC-SO-301",
				"note": F,
				"company_id": COMPANY,
			}
		},
		"purchase.order.line": {
			1: {
				"order_id": [1, "P00001"],
				"sequence": 10,
				"display_type": F,
				"product_id": [20, "[CHAIR] Office Chair"],
				"name": "[CHAIR] Office Chair",
				"product_qty": 10.0,
				"price_unit": 80.0,
				"discount": 0.0,
				"price_subtotal": 800.0,
				"price_tax": 40.0,
				"price_total": 840.0,
			}
		},
		"account.move": {},
		"account.move.line": {},
		"account.partial.reconcile": {},
		"account.payment": {},
	}
	moves = db["account.move"]
	lines = db["account.move.line"]

	def move(
		id_: int,
		name: str,
		move_type: str,
		partner_id: int | None,
		date: str,
		total: str,
		signed: str,
		**fields: Any,
	) -> None:
		partner_name = db["res.partner"][partner_id]["name"] if partner_id else None
		moves[id_] = {
			"name": name,
			"move_type": move_type,
			"state": "posted",
			"partner_id": [partner_id, partner_name] if partner_id else F,
			"commercial_partner_id": [partner_id, partner_name] if partner_id else F,
			"invoice_date": date if move_type != "entry" else F,
			"invoice_date_due": F,
			"date": date,
			"currency_id": CAD,
			"amount_total": float(total),
			"amount_total_signed": float(signed),
			"ref": F,
			"narration": F,
			"reversed_entry_id": F,
			"journal_id": [1, "Customer Invoices"],
			"origin_payment_id": F,
			"company_id": COMPANY,
		} | fields

	# INV/2026/00001: 2 chairs at 150, GST 5%; settled by the credit note (1 chair) and payment 41.
	move(
		31,
		"INV/2026/00001",
		"out_invoice",
		7,
		"2026-03-05",
		"315",
		"315",
		invoice_date_due="2026-04-04",
		ref="DA-PO-77",
	)
	lines[311] = move_line(
		311,
		31,
		106,
		"0",
		"300",
		product_id=[20, "[CHAIR] Office Chair"],
		name="[CHAIR] Office Chair",
		quantity=2.0,
		price_unit=150.0,
		price_subtotal=300.0,
		partner_id=[7, "Deco Addict"],
		analytic_distribution={"201": 100.0},
	)
	lines[312] = move_line(
		312, 31, 108, "0", "15", display_type="tax", name="GST 5%", partner_id=[7, "Deco Addict"]
	)
	lines[313] = move_line(
		313,
		31,
		101,
		"315",
		"0",
		display_type="payment_term",
		name="INV/2026/00001",
		partner_id=[7, "Deco Addict"],
		matched_credit_ids=[61, 62],
	)
	# RINV/2026/00001: one chair back.
	move(
		32,
		"RINV/2026/00001",
		"out_refund",
		7,
		"2026-03-08",
		"157.5",
		"-157.5",
		reversed_entry_id=[31, "INV/2026/00001"],
		ref="Reversal of: INV/2026/00001",
	)
	lines[321] = move_line(
		321,
		32,
		106,
		"150",
		"0",
		product_id=[20, "[CHAIR] Office Chair"],
		name="[CHAIR] Office Chair",
		quantity=1.0,
		price_unit=150.0,
		price_subtotal=150.0,
		partner_id=[7, "Deco Addict"],
	)
	lines[322] = move_line(
		322, 32, 108, "7.5", "0", display_type="tax", name="GST 5%", partner_id=[7, "Deco Addict"]
	)
	lines[323] = move_line(
		323,
		32,
		101,
		"0",
		"157.5",
		display_type="payment_term",
		name="RINV/2026/00001",
		partner_id=[7, "Deco Addict"],
		matched_debit_ids=[62],
	)
	# BILL/2026/03/0001: the purchase order's chairs; paid by payment 42.
	move(
		33,
		"BILL/2026/03/0001",
		"in_invoice",
		14,
		"2026-03-06",
		"840",
		"-840",
		journal_id=[2, "Vendor Bills"],
		ref="WC-INV-9001",
	)
	lines[331] = move_line(
		331,
		33,
		107,
		"800",
		"0",
		product_id=[20, "[CHAIR] Office Chair"],
		name="[CHAIR] Office Chair",
		quantity=10.0,
		price_unit=80.0,
		price_subtotal=800.0,
		partner_id=[14, "Wood Corner"],
	)
	lines[332] = move_line(
		332, 33, 109, "40", "0", display_type="tax", name="GST 5%", partner_id=[14, "Wood Corner"]
	)
	lines[333] = move_line(
		333,
		33,
		102,
		"0",
		"840",
		display_type="payment_term",
		name="WC-INV-9001",
		partner_id=[14, "Wood Corner"],
		matched_debit_ids=[63],
	)
	# MISC/2026/03/0001: March's rent.
	move(
		34,
		"MISC/2026/03/0001",
		"entry",
		None,
		"2026-03-01",
		"1000",
		"1000",
		journal_id=[3, "Miscellaneous Operations"],
		ref="March rent",
	)
	lines[341] = move_line(341, 34, 107, "1000", "0", display_type="product", name="Rent")
	lines[342] = move_line(342, 34, 103, "0", "1000", display_type="product", name="Rent")
	# BNK1/2026/00001: the bank statement line that matched payment 41 (outstanding receipts to the bank).
	move(35, "BNK1/2026/00001", "entry", None, "2026-03-11", "157.5", "157.5", journal_id=[4, "Bank"])
	lines[351] = move_line(351, 35, 103, "157.5", "0", display_type="product", name="Deco Addict payment")
	lines[352] = move_line(352, 35, 104, "0", "157.5", display_type="product", name="Deco Addict payment")
	# PBNK1/2026/00001 (payment 41) and PBNK1/2026/00002 (payment 42): their journal entries, not journals.
	move(
		51,
		"PBNK1/2026/00001",
		"entry",
		7,
		"2026-03-10",
		"157.5",
		"157.5",
		journal_id=[4, "Bank"],
		origin_payment_id=[41, "PBNK1/2026/00001"],
	)
	lines[511] = move_line(511, 51, 104, "157.5", "0", display_type="product", partner_id=[7, "Deco Addict"])
	lines[512] = move_line(
		512,
		51,
		101,
		"0",
		"157.5",
		display_type="product",
		partner_id=[7, "Deco Addict"],
		matched_debit_ids=[61],
	)
	move(
		52,
		"PBNK1/2026/00002",
		"entry",
		14,
		"2026-03-12",
		"840",
		"840",
		journal_id=[4, "Bank"],
		origin_payment_id=[42, "PBNK1/2026/00002"],
	)
	lines[521] = move_line(
		521,
		52,
		102,
		"840",
		"0",
		display_type="product",
		partner_id=[14, "Wood Corner"],
		matched_credit_ids=[63],
	)
	lines[522] = move_line(522, 52, 105, "0", "840", display_type="product", partner_id=[14, "Wood Corner"])
	db["account.partial.reconcile"] = {
		61: {
			"debit_move_id": [313, "INV/2026/00001"],
			"credit_move_id": [512, "PBNK1/2026/00001"],
			"amount": 157.5,
		},
		62: {
			"debit_move_id": [313, "INV/2026/00001"],
			"credit_move_id": [323, "RINV/2026/00001"],
			"amount": 157.5,
		},
		63: {
			"debit_move_id": [521, "PBNK1/2026/00002"],
			"credit_move_id": [333, "WC-INV-9001"],
			"amount": 840.0,
		},
	}
	db["account.payment"] = {
		41: {
			"name": "PBNK1/2026/00001",
			"payment_type": "inbound",
			"partner_type": "customer",
			"partner_id": [7, "Deco Addict"],
			"amount": 157.5,
			"currency_id": CAD,
			"date": "2026-03-10",
			"state": "paid",
			"memo": "INV/2026/00001",
			"move_id": [51, "PBNK1/2026/00001"],
			"outstanding_account_id": [104, "101402 Outstanding Receipts"],
			"destination_account_id": [101, "121000 Account Receivable"],
			"company_id": COMPANY,
		},
		42: {
			"name": "PBNK1/2026/00002",
			"payment_type": "outbound",
			"partner_type": "supplier",
			"partner_id": [14, "Wood Corner"],
			"amount": 840.0,
			"currency_id": CAD,
			"date": "2026-03-12",
			"state": "paid",
			"memo": "WC-INV-9001",
			"move_id": [52, "PBNK1/2026/00002"],
			"outstanding_account_id": [105, "101403 Outstanding Payments"],
			"destination_account_id": [102, "211000 Account Payable"],
			"company_id": COMPANY,
		},
	}
	return db


def store_v2() -> dict[str, Table]:
	"""The same database a month later: a price and a journal entry changed, a quotation confirmed, a
	purchase order cancelled, a customer archived, an employee gone, an opportunity lost, a lead deleted,
	and a new draft invoice."""
	db = copy.deepcopy(store_v1())
	db["product.template"][10] |= {"list_price": 155.0, "write_date": LATER}
	db["product.product"][20] |= {"lst_price": 155.0}
	db["sale.order"][2] |= {"state": "sale", "write_date": LATER}
	db["purchase.order"][1] |= {"state": "cancel", "write_date": LATER}
	db["res.partner"][16] |= {"active": False, "write_date": LATER}
	db["hr.employee"][2] |= {"active": False, "departure_date": "2026-03-31", "write_date": LATER}
	db["crm.lead"][3] |= {"active": False, "probability": 0.0, "write_date": LATER}
	del db["crm.lead"][1]
	moves, lines = db["account.move"], db["account.move.line"]
	moves[34] |= {"amount_total": 1100.0, "amount_total_signed": 1100.0, "write_date": LATER}
	lines[341] |= {"debit": 1100.0, "amount_currency": 1100.0}
	lines[342] |= {"credit": 1100.0, "amount_currency": -1100.0}
	moves[36] = moves[31] | {
		"name": F,
		"state": "draft",
		"invoice_date": "2026-04-01",
		"date": "2026-04-01",
		"amount_total": 945.0,
		"amount_total_signed": 945.0,
		"ref": F,
		"write_date": LATER,
	}
	lines[361] = move_line(
		361,
		36,
		106,
		"0",
		"900",
		product_id=[23, "[CONSULT] Consulting"],
		name="Consulting",
		quantity=10.0,
		price_unit=90.0,
		price_subtotal=900.0,
		partner_id=[7, "Deco Addict"],
	)
	lines[362] = move_line(
		362, 36, 108, "0", "45", display_type="tax", name="GST 5%", partner_id=[7, "Deco Addict"]
	)
	lines[363] = move_line(
		363, 36, 101, "945", "0", display_type="payment_term", partner_id=[7, "Deco Addict"]
	)
	return db


class OdooDatabase:
	"""Answers JSON-2 requests from one state's records."""

	def __init__(self, db: dict[str, Table]) -> None:
		self._db = db

	def answer(self, model: str, method: str, body: dict[str, Any]) -> Any:
		if method == "fields_get":
			return {
				name: {"type": "many2one" if name in MANY2ONE else "char"} for name in ("id", *MODELS[model])
			}
		table = self._db[model]
		fields = list(body.get("fields") or MODELS[model])
		if method == "read":
			ids = [i for i in body["ids"] if i in table]
		elif method == "search_read":
			ids = sorted(i for i in table if self._matches(model, i, list(body.get("domain") or [])))
			ids = ids[int(body.get("offset") or 0) :]
			if body.get("limit") is not None:
				ids = ids[: int(body["limit"])]
		else:
			raise ValueError(f"{model}.{method} isn't modelled")
		return [{"id": i, **{name: self._value(model, i, name) for name in fields}} for i in ids]

	def _value(self, model: str, id_: int, name: str) -> Any:
		record = self._db[model][id_]
		value = record.get(name, F)
		target = MANY2ONE.get(name)
		if isinstance(value, int) and not isinstance(value, bool) and target is not None:
			target = target or model
			label = (
				NAMES.get(target, {}).get(value)
				or self._db.get(target, {}).get(value, {}).get("name")
				or str(value)
			)
			return [value, label]
		return value

	def _matches(self, model: str, id_: int, domain: list[Any]) -> bool:
		stack: list[bool] = []
		for term in reversed(domain):
			if term == "|":
				stack.append(stack.pop() | stack.pop())
			elif term == "&":
				stack.append(stack.pop() & stack.pop())
			else:
				stack.append(self._term(model, id_, term))
		return all(stack)

	def _term(self, model: str, id_: int, term: list[Any]) -> bool:
		path, operator, wanted = term
		value = self._path(model, id_, path)
		match operator:
			case "=":
				return bool(value == wanted)
			case "!=":
				return bool(value != wanted)
			case "in":
				return value in wanted
			case "not in":
				return value not in wanted
			case ">":
				return bool(value > wanted)
			case ">=":
				return bool(value >= wanted)
		raise ValueError(f"operator {operator} isn't modelled")

	def _path(self, model: str, id_: int, path: str) -> Any:
		if path == "id":
			return id_
		name, _, rest = path.partition(".")
		value = self._value(model, id_, name)
		if isinstance(value, list) and value and name in MANY2ONE:
			value = value[0]
		if rest:
			target = MANY2ONE[name] or model
			return False if value is False else self._path(target, value, rest)
		return value


def record(state: str) -> list[tuple[str, dict[str, Any], Any]]:
	"""Every request the reader sends to read each kind of the state (and the answers)."""
	database = OdooDatabase(store_v1() if state == "v1" else store_v2())
	seen: list[tuple[str, dict[str, Any], Any]] = []

	def route(method: str, url: str, body: bytes, _headers: dict[str, str]) -> HttpResponse:
		path = urlsplit(url).path
		model, _, odoo_method = path.removeprefix(JSON2).partition("/")
		sent = json.loads(body)
		answer = database.answer(model, odoo_method, sent)
		if not any(p == path and b == sent for p, b, _ in seen):
			seen.append((path, sent, answer))
		return HttpResponse(200, json.dumps(answer).encode())

	adapter = OdooAdapter()
	reader = adapter.reader(CONFIG, FakeTransport(route), RecordingSleeper())
	for kind in adapter.kinds():
		cursor: str | None = None
		while True:
			page = reader.page(kind, cursor, None, "")
			if not page.has_next_page:
				break
			cursor = page.end_cursor
	return seen


def name_of(index: int, path: str) -> str:
	return f"{index:03d}-{path.removeprefix(JSON2).replace('/', '-').replace('.', '_')}.json"


def write(state: str, requests: list[tuple[str, dict[str, Any], Any]]) -> None:
	folder = FIXTURES / state
	shutil.rmtree(folder, ignore_errors=True)
	folder.mkdir(parents=True)
	for index, (path, body, answer) in enumerate(requests, 1):
		fixture = {"request": {"method": "POST", "path": path, "body": body}, "response": answer}
		(folder / name_of(index, path)).write_text(
			json.dumps(fixture, indent="\t", ensure_ascii=False) + "\n"
		)


def main() -> int:
	for state in ("v1", "v2"):
		write(state, record(state))
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
