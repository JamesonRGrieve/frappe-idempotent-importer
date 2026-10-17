# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end fixtures and checks, run on a real bench by tests/e2e/run.sh through ``bench execute``.

``setup_site`` completes ERPNext's setup and builds three businesses, each with its own company, Public
Site and Webshop Store, and one Importer Connection per business: two WooCommerce stores (woo-a, woo-b)
and a Shopify store (shop-s), all answered by the fake store container. The other functions are the
assertions run.sh makes between runs."""

from __future__ import annotations

import hashlib
import json
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import frappe
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
from frappe.installer import update_site_config

from idempotent_importer import api
from idempotent_importer.erpnext import engine, jobs
from idempotent_importer.erpnext.stores import (
	FrappeRunLock,
	FrappeRunStore,
	naive_utc,
	now_utc,
)

STATE_DIR = Path("/state")
FISCAL_YEAR_START = "2026-01-01"
FISCAL_YEAR_END = "2026-12-31"
CURRENCY = "CAD"
COUNTRY = "Canada"
FALLBACK_ITEM = "IMPORTED-LINE"
FAKE_PORT = 8080
# The Etsy connection as if the operator had saved the app and connected the shop.
ETSY_CONNECTED_REFRESH_TOKEN = "e2e-refresh-token"
# The refresh token the fake Etsy issues on every refresh (tests/e2e/fake_store.py).
ETSY_ROTATED_REFRESH_TOKEN = "fixture-refresh-token"


@dataclass(frozen=True)
class Business:
	"""One business of the suite: its company, Public Site and Webshop Store, and its connection to one
	source, answered by the fake store as ``fake`` with the test credentials it checks."""

	abbr: str
	company: str
	connection: str
	source: str
	source_id: str
	fake: str
	store_url: str
	credentials: dict[str, str] = field(default_factory=dict)
	endpoint_path: str = ""

	@property
	def site(self) -> str:
		return self.company.lower().replace(" ", "-")

	@property
	def endpoint(self) -> str:
		return f"http://{self.fake}:{FAKE_PORT}{self.endpoint_path}"


WOO_CREDENTIALS = {
	"auth_method": "Application Password",
	"api_user": "e2e",
	"api_secret": "e2e-application-password",
}
BUSINESSES = (
	Business(
		"WA",
		"Store A",
		"woo-a",
		"WooCommerce",
		"woocommerce",
		"woo-a-fake",
		"https://woo-a.example.test",
		WOO_CREDENTIALS,
	),
	Business(
		"WB",
		"Store B",
		"woo-b",
		"WooCommerce",
		"woocommerce",
		"woo-b-fake",
		"https://woo-b.example.test",
		WOO_CREDENTIALS,
	),
	Business(
		"SH",
		"Store S",
		"shop-s",
		"Shopify",
		"shopify",
		"shopify-fake",
		"e2e-fixture.myshopify.com",
		# A Dev Dashboard app's client credentials, exchanged at the fake store for its Admin API token.
		{"auth_method": "Client Credentials", "api_user": "e2e-client-id", "api_secret": "e2e-client-secret"},
		"/admin/api/2026-10/graphql.json",
	),
	Business(
		"ET",
		"Store E",
		"etsy-e",
		"Etsy",
		"etsy",
		"etsy-fake",
		"https://www.etsy.com/shop/ClayAndCloth",
		{
			"api_user": "e2e-keystring",
			"api_secret": "e2e-shared-secret",
			"refresh_token": ETSY_CONNECTED_REFRESH_TOKEN,
		},
	),
)


def business(abbr: str) -> Business:
	return next(spec for spec in BUSINESSES if spec.abbr == abbr)


def business_of(connection: str) -> Business:
	return next(spec for spec in BUSINESSES if spec.connection == connection)


CONTENT_DOCTYPES = (
	"Item Group",
	"Item",
	"Item Attribute Value",
	"Item Price",
	"Item Barcode",
	"Pricing Rule",
	"Coupon Code",
	"Stock Reconciliation",
	"Stock Ledger Entry",
	"Bin",
	"Website Item",
	"Website Item Group",
	"Item Website Specification",
	"Item Review",
	"File",
	"Brand",
	"Customer",
	"Contact",
	"Contact Email",
	"Contact Phone",
	"Address",
	"Dynamic Link",
	"Sales Invoice",
	"Sales Invoice Item",
	"Sales Taxes and Charges",
	"Payment Entry",
	"Payment Entry Reference",
	"GL Entry",
	"Payment Ledger Entry",
	"Comment",
	"Web Page",
	"Blog Category",
	"Blog Post",
	"Blogger",
	"Website Route Redirect",
	"Quotation",
	"Quotation Item",
	"Shipping Rule",
	"Shipping Rule Country",
	"Tag",
	"Tag Link",
)
# Columns that change without anything being mirrored (Frappe's bookkeeping of reads and caches).
VOLATILE_COLUMNS = frozenset({"_seen", "_liked_by", "_comments"})


def setup_site() -> None:
	frappe.set_user("Administrator")
	setup_complete(
		{
			"language": "English",
			"country": COUNTRY,
			"timezone": "America/Edmonton",
			"currency": CURRENCY,
			"full_name": "E2E Admin",
			"email": "e2e@example.test",
			"company_name": BUSINESSES[0].company,
			"company_abbr": BUSINESSES[0].abbr,
			"chart_of_accounts": "Standard",
			"fy_start_date": FISCAL_YEAR_START,
			"fy_end_date": FISCAL_YEAR_END,
		}
	)
	frappe.db.set_single_value("Webshop Settings", "enable_reviews", 1)
	make_fallback_item()
	for spec in BUSINESSES:
		if not frappe.db.exists("Company", spec.company):
			frappe.get_doc(
				{
					"doctype": "Company",
					"company_name": spec.company,
					"abbr": spec.abbr,
					"default_currency": CURRENCY,
					"country": COUNTRY,
					"chart_of_accounts": "Standard",
				}
			).insert()
		make_business(spec)
	endpoints = {spec.connection: spec.endpoint for spec in BUSINESSES}
	update_site_config("idempotent_importer_test_endpoints", endpoints)
	frappe.db.commit()


def make_fallback_item() -> None:
	if frappe.db.exists("Item", FALLBACK_ITEM):
		return
	frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": FALLBACK_ITEM,
			"item_name": "Imported line",
			"item_group": "Services",
			"stock_uom": "Nos",
			"is_stock_item": 0,
			"is_sales_item": 1,
		}
	).insert()


def account(abbr: str, name: str, parent: str, account_type: str) -> str:
	full = f"{name} - {abbr}"
	if not frappe.db.exists("Account", full):
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": name,
				"parent_account": f"{parent} - {abbr}",
				"company": business(abbr).company,
				"account_type": account_type,
			}
		).insert()
	return full


def make_business(spec: Business) -> None:
	company, abbr, site = spec.company, spec.abbr, spec.site
	price_list = f"{company} Prices"
	if not frappe.db.exists("Price List", price_list):
		frappe.get_doc(
			{"doctype": "Price List", "price_list_name": price_list, "currency": CURRENCY, "selling": 1}
		).insert()
	store = f"{company} Shop"
	if not frappe.db.exists("Webshop Store", store):
		frappe.get_doc(
			{
				"doctype": "Webshop Store",
				"store_name": store,
				"enabled": 1,
				"company": company,
				"price_list": price_list,
				"default_customer_group": "Individual",
				"quotation_series": "SAL-QTN-.YYYY.-",
			}
		).insert()
	if not frappe.db.exists("Public Site", site):
		frappe.get_doc(
			{
				"doctype": "Public Site",
				"site_name": site,
				"enabled": 1,
				"company": company,
				"route_prefix": site,
				"home_route": "home",
				"webshop_store": store,
				"domains": [{"domain": f"{site}.example.test"}],
			}
		).insert()
	root_group = f"{company} Catalogue"
	if not frappe.db.exists("Item Group", root_group):
		frappe.get_doc(
			{
				"doctype": "Item Group",
				"item_group_name": root_group,
				"parent_item_group": "All Item Groups",
				"is_group": 1,
			}
		).insert()
	guest = f"Guest {abbr}"
	if not frappe.db.exists("Customer", guest):
		frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": guest,
				"customer_group": "Individual",
				"territory": "Canada",
			}
		).insert()
	guest_name = frappe.db.get_value("Customer", {"customer_name": guest})
	if frappe.db.exists("Importer Connection", spec.connection):
		return
	frappe.get_doc(
		{
			"doctype": "Importer Connection",
			"connection_id": spec.connection,
			"enabled": 1,
			"source": spec.source,
			"store_url": spec.store_url,
			**spec.credentials,
			"public_site": site,
			"abbreviation": abbr,
			"item_group": root_group,
			"price_list": price_list,
			"warehouse": f"Stores - {abbr}",
			"customer_group": "Individual",
			"territory": "Canada",
			"guest_customer": guest_name,
			"fallback_item": FALLBACK_ITEM,
			"mode_of_payment": "Credit Card",
			"deposit_account": account(abbr, "Payment Processor", "Bank Accounts", "Bank"),
			"tax_account": account(abbr, "Sales Tax Collected", "Duties and Taxes", "Tax"),
			"shipping_account": account(abbr, "Shipping Revenue", "Direct Income", "Income Account"),
			"fee_account": account(abbr, "Fee Revenue", "Direct Income", "Income Account"),
			"cost_center": f"Main - {abbr}",
		}
	).insert()


def table_checksums() -> dict[str, str]:
	"""A digest of every content table, row by row (volatile bookkeeping columns left out)."""
	digests: dict[str, str] = {}
	for doctype in CONTENT_DOCTYPES:
		columns = [c for c in frappe.db.get_table_columns(doctype) if c not in VOLATILE_COLUMNS]
		rows = frappe.qb.from_(doctype).select(*columns).orderby("name").run(as_list=True)
		digest = hashlib.sha256()
		for row in rows:
			digest.update(json.dumps(row, default=str, sort_keys=True).encode())
		digests[doctype] = f"{len(rows)}:{digest.hexdigest()}"
	return digests


STEP_FILE = "e2e-step"
BACKGROUND_RUN_TIMEOUT_SECONDS = 300
POLL_SECONDS = 2


def main() -> None:
	"""Runs the check run.sh wrote to <state dir>/e2e-step as words: the function, then its arguments
	(so the shell never has to quote JSON for ``bench execute --kwargs``)."""
	name, *arguments = (STATE_DIR / STEP_FILE).read_text().split()
	steps: dict[str, Callable[..., Any]] = {
		"run": run,
		"checksum": checksum,
		"assert_unchanged": assert_unchanged,
		"assert_connection_untouched": assert_connection_untouched,
		"assert_woocommerce": assert_woocommerce,
		"assert_shopify": assert_shopify,
		"assert_etsy": assert_etsy,
		"assert_etsy_connect": assert_etsy_connect,
		"assert_shared_coupon_codes": assert_shared_coupon_codes,
		"assert_lockout_safety": assert_lockout_safety,
		"sync_now_in_background": sync_now_in_background,
		"assert_no_side_effects": assert_no_side_effects,
		"assert_listed_sources_refuse": assert_listed_sources_refuse,
	}
	steps[name](*arguments)
	print(f"ok: {name} {' '.join(arguments)}".strip())


def check[**P, R](function: Callable[P, R]) -> Callable[P, R]:
	"""``bench execute`` hides a failing function's exception behind its own fallback: a failed check
	prints why and exits non-zero instead."""

	@wraps(function)
	def checked(*args: P.args, **kwargs: P.kwargs) -> R:
		try:
			return function(*args, **kwargs)
		except Exception as e:
			traceback.print_exc()
			print(f"E2E CHECK FAILED: {function.__name__}: {e}")
			raise SystemExit(1) from e

	return checked


def connection_records(connection: str) -> dict[str, Any]:
	"""Every record a connection's links point at, as stored (with child rows)."""
	records: dict[str, Any] = {}
	for link in frappe.get_all(
		"Importer Link",
		filters={"connection": connection},
		fields=["kind", "gid", "target_doctype", "target_name"],
		order_by="kind, gid",
	):
		if frappe.db.exists(link.target_doctype, link.target_name):
			doc = frappe.get_doc(link.target_doctype, link.target_name).as_dict(no_default_fields=False)
			records[f"{link.kind} {link.gid}"] = {k: v for k, v in doc.items() if k not in VOLATILE_COLUMNS}
	return records


def digest_of(value: Any) -> str:
	return hashlib.sha256(json.dumps(value, default=str, sort_keys=True).encode()).hexdigest()


@check
def checksum(name: str) -> None:
	"""Records the content tables' digests, and each connection's records' digests, as ``name``."""
	tables = table_checksums()
	connections = {
		spec.connection: {
			key: digest_of(record) for key, record in connection_records(spec.connection).items()
		}
		for spec in BUSINESSES
	}
	state = {"tables": tables, "connections": connections}
	(STATE_DIR / f"checksum-{name}.json").write_text(json.dumps(state, indent=1, sort_keys=True))
	print(f"checksum {name}: {len(tables)} tables, {len(connections)} connections")


@check
def assert_unchanged(name: str) -> None:
	"""Nothing was written since checksum ``name``: every content table is byte-identical."""
	before = json.loads((STATE_DIR / f"checksum-{name}.json").read_text())["tables"]
	after = table_checksums()
	changed = sorted(doctype for doctype in before if before[doctype] != after[doctype])
	if changed:
		raise AssertionError(f"An unchanged re-run wrote to: {', '.join(changed)}")
	print(f"zero writes: {len(after)} content tables unchanged")


@check
def assert_connection_untouched(name: str, connection: str) -> None:
	"""Another connection's runs since checksum ``name`` left every record of ``connection`` as it was."""
	before: dict[str, str] = json.loads((STATE_DIR / f"checksum-{name}.json").read_text())["connections"][
		connection
	]
	after = {key: digest_of(record) for key, record in connection_records(connection).items()}
	changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
	if changed:
		raise AssertionError(f"{connection}'s records changed while only other connections ran: {changed}")
	print(f"{connection}: {len(after)} records untouched by the other connections' runs")


@check
def run(connection: str, mode: str) -> dict[str, Any]:
	"""A manual ``full`` or ``incremental`` run, stepped to the end (as `bench idempotent-import run` does);
	it must complete cleanly."""
	result = engine.run_by_hand(connection, full=mode == "full", dry_run=False, kinds=None)
	print(json.dumps(result, sort_keys=True))
	expect(result.get("status"), "Complete", f"{connection}'s {mode} run")
	return result


def target(connection: str, kind: str, type_: str, source_id: str) -> str:
	"""The record a source object was mirrored to."""
	gid = f"gid://{business_of(connection).source_id}/{connection}/{type_}/{source_id}"
	name = frappe.db.get_value(
		"Importer Link", {"connection": connection, "kind": kind, "gid": gid}, "target_name"
	)
	if not name:
		raise AssertionError(f"{gid} ({kind}) is not mirrored")
	return str(name)


def part(connection: str, kind: str, gid_suffix: str) -> str:
	name = frappe.db.get_value(
		"Importer Link",
		{"connection": connection, "kind": kind, "gid": ["like", f"%{gid_suffix}"], "retired": 0},
		"target_name",
	)
	if not name:
		raise AssertionError(f"no live {kind} part ending {gid_suffix} for {connection}")
	return str(name)


def tags_of(doctype: str, name: str) -> list[str]:
	return sorted(
		frappe.get_all("Tag Link", filters={"document_type": doctype, "document_name": name}, pluck="tag")
	)


def expect(actual: Any, wanted: Any, what: str) -> None:
	if actual != wanted:
		raise AssertionError(f"{what}: {actual!r}, expected {wanted!r}")


def amount(value: Any) -> Decimal:
	return Decimal(str(value)).quantize(Decimal("0.01"))


@check
def assert_woocommerce(connection: str, state: str) -> None:
	"""The WooCommerce fixtures' state ``state`` as it should land in ERPNext."""
	abbr = frappe.db.get_value("Importer Connection", connection, "abbreviation")
	toque = target(connection, "product", "Product", "101")
	expect(toque, f"{abbr}-TOQUE-1", "the toque's item code")
	price = frappe.db.get_value(
		"Item Price",
		{
			"item_code": toque,
			"price_list": frappe.db.get_value("Importer Connection", connection, "price_list"),
		},
		"price_list_rate",
	)
	warehouse = frappe.db.get_value("Importer Connection", connection, "warehouse")
	stock = frappe.db.get_value("Bin", {"item_code": toque, "warehouse": warehouse}, "actual_qty")
	hoodie = target(connection, "product", "Product", "102")
	expect(frappe.db.get_value("Item", hoodie, "has_variants"), 1, "the hoodie is a template")
	expect(frappe.db.count("Item", {"variant_of": hoodie}), 2, "the hoodie's variants")
	sale = part(connection, "price", "ProductVariation/1022#sale")
	expect(amount(frappe.db.get_value("Pricing Rule", sale, "rate")), amount("54"), "the hoodie M sale price")
	website_item = frappe.db.get_value(
		"Website Item", {"item_code": toque}, ["published", "route", "webshop_store"], as_dict=True
	)
	site = frappe.db.get_value(
		"Importer Connection", connection, ["route_prefix", "webshop_store"], as_dict=True
	)
	expect(
		(website_item.published, website_item.route, website_item.webshop_store),
		(1, f"{site.route_prefix}/wool-toque", site.webshop_store),
		"the toque's Website Item",
	)
	paid = target(connection, "order", "Order", "501")
	invoice = frappe.db.get_value(
		"Sales Invoice",
		paid,
		["docstatus", "grand_total", "outstanding_amount", "update_stock"],
		as_dict=True,
	)
	expect(
		(
			invoice.docstatus,
			amount(invoice.grand_total),
			amount(invoice.outstanding_amount),
			invoice.update_stock,
		),
		(1, amount("114.45"), amount("0"), 0),
		"order 501's invoice",
	)
	expect(
		frappe.db.count(
			"Comment",
			{"reference_doctype": "Sales Invoice", "reference_name": paid, "comment_type": "Comment"},
		),
		2,
		"order 501's notes",
	)
	unpaid = target(connection, "order", "Order", "503")
	expect(
		frappe.db.get_value(
			"Importer Link", {"connection": connection, "kind": "order", "gid": ["like", "%/Order/504"]}
		),
		None,
		"the cancelled order 504",
	)
	expect(
		frappe.db.get_value(
			"Item Group", target(connection, "collection", "ProductCategory", "12"), "parent_item_group"
		),
		target(connection, "collection", "ProductCategory", "11"),
		"Hoodies' parent group",
	)
	expect(
		frappe.db.get_value("Importer Link", {"connection": connection, "gid": ["like", "%/Product/104"]}),
		None,
		"the grouped product",
	)
	expect(tags_of("Item", toque), ["wool"], "the toque's tags")
	post = target(connection, "article", "Post", "901")
	expect(tags_of("Blog Post", post), ["Knitting"], "the post's tags, by name")
	about = str(
		frappe.db.get_value("Web Page", target(connection, "page", "Page", "701"), "main_section_html")
	)
	expect(
		("woo-fake" in about, '<img src="/files/workshop' in about),
		(False, True),
		"the page's localised image",
	)
	if state == "v1":
		expect(amount(price), amount("25"), "the toque's price")
		expect(amount(stock), amount("12"), "the toque's stock")
		expect(frappe.db.get_value("Sales Invoice", unpaid, "docstatus"), 0, "unpaid order 503 is a draft")
		expect(
			frappe.db.get_value("Item", target(connection, "product", "Product", "103"), "disabled"),
			0,
			"the gift wrap",
		)
		expect(
			frappe.db.get_value("Web Page", target(connection, "page", "Page", "702"), "published"),
			0,
			"the draft page",
		)
		return
	expect(amount(price), amount("27"), "the toque's new price")
	expect(amount(stock), amount("9"), "the toque's new stock")
	expect(
		frappe.db.get_value("Item", target(connection, "product", "Product", "103"), "disabled"),
		1,
		"the deleted gift wrap",
	)
	expect(
		frappe.db.get_value(
			"Website Item", {"item_code": target(connection, "product", "Product", "103")}, "published"
		),
		0,
		"the deleted gift wrap's Website Item",
	)
	expect(
		target(connection, "collection", "ProductCategory", "13"),
		f"Hats & Accessories - {abbr}",
		"the renamed category",
	)
	expect(
		frappe.db.get_value("Item", toque, "item_group"),
		f"Hats & Accessories - {abbr}",
		"the toque's renamed group",
	)
	unpaid_now = frappe.db.get_value(
		"Sales Invoice", unpaid, ["docstatus", "outstanding_amount"], as_dict=True
	)
	expect(
		(unpaid_now.docstatus, amount(unpaid_now.outstanding_amount)),
		(1, amount("0")),
		"order 503, paid since",
	)
	credit = part(connection, "refund", "OrderRefund/601")
	note = frappe.db.get_value(
		"Sales Invoice", credit, ["is_return", "docstatus", "grand_total"], as_dict=True
	)
	expect(
		(note.is_return, note.docstatus, amount(note.grand_total)),
		(1, 1, amount("-26.25")),
		"order 502's credit note",
	)
	refund_payment = part(connection, "refund_payment", "OrderRefund/601#payment")
	expect(
		frappe.db.get_value("Payment Entry", refund_payment, "payment_type"),
		"Pay",
		"order 502's refund payment",
	)
	expect(
		amount(
			frappe.db.get_value("Sales Invoice", target(connection, "order", "Order", "505"), "grand_total")
		),
		amount("67.20"),
		"new order 505",
	)
	expect(
		frappe.db.get_value("Pricing Rule", target(connection, "discount", "Coupon", "302"), "disable"),
		1,
		"the deleted coupon's rule",
	)
	expect(
		frappe.db.get_value("Web Page", target(connection, "page", "Page", "702"), "published"),
		0,
		"the deleted page",
	)
	expect(
		"since 2019"
		in frappe.db.get_value("Web Page", target(connection, "page", "Page", "701"), "main_section_html"),
		True,
		"the edited page",
	)
	target(connection, "article", "Post", "902")
	target(connection, "review", "ProductReview", "953")
	target(connection, "customer", "Customer", "203")
	expect(
		frappe.db.get_value(
			"Importer Link", {"connection": connection, "gid": ["like", "%/Redirect/962"]}, "retired"
		),
		1,
		"the deleted redirect",
	)


@check
def assert_shopify(state: str) -> None:
	connection = "shop-s"
	scarf = target(connection, "product", "Product", "301")
	expect(frappe.db.get_value("Item", scarf, "has_variants"), 0, "the scarf is one item")
	order = target(connection, "order", "Order", "801")
	invoice = frappe.db.get_value(
		"Sales Invoice", order, ["docstatus", "grand_total", "amended_from"], as_dict=True
	)
	expect(
		(invoice.docstatus, amount(invoice.grand_total), invoice.amended_from),
		(1, amount("137.76"), None),
		"order 801's invoice",
	)
	credit = part(connection, "refund", "Refund/8201")
	expect(
		amount(frappe.db.get_value("Sales Invoice", credit, "grand_total")),
		amount("-30"),
		"order 801's credit note",
	)
	expect(
		frappe.db.get_value("Importer Link", {"connection": connection, "gid": ["like", "%/Order/802"]}),
		None,
		"the voided order 802",
	)
	refunded = target(connection, "order", "Order", "804")
	expect(
		(
			amount(frappe.db.get_value("Sales Invoice", refunded, "grand_total")),
			amount(frappe.db.get_value("Sales Invoice", refunded, "outstanding_amount")),
		),
		(amount("118.59"), amount("0")),
		"fully refunded order 804's invoice",
	)
	credits = frappe.get_all(
		"Importer Link",
		filters={"connection": connection, "kind": "refund", "gid": ["like", "%/Order/804#%"], "retired": 0},
		pluck="target_name",
	)
	expect(
		[amount(frappe.db.get_value("Sales Invoice", name, "grand_total")) for name in credits],
		[amount("-118.59")],
		"order 804: Shopify's one refund, nothing derived from its REFUNDED status",
	)
	expect(
		frappe.db.count(
			"Importer Link",
			{
				"connection": connection,
				"kind": "refund_payment",
				"gid": ["like", "%/Order/804#%"],
				"retired": 0,
			},
		),
		1,
		"order 804's refund payments",
	)
	for policy, route in (
		("11", "store-s/policies/refund-policy"),
		("12", "store-s/policies/privacy-policy"),
	):
		page = frappe.db.get_value(
			"Web Page",
			target(connection, "policy", "ShopPolicy", policy),
			["route", "published"],
			as_dict=True,
		)
		expect((page.route, page.published), (route, 1), f"shop policy {policy}'s Web Page")
	expect(
		frappe.db.count(
			"Comment",
			{
				"reference_doctype": "Blog Post",
				"reference_name": target(connection, "article", "Article", "1201"),
			},
		),
		1,
		"the article's published comment",
	)
	for type_, file_id, name in (
		("MediaImage", "501", "storefront.png"),
		("GenericFile", "502", "size-guide.pdf"),
	):
		expect(
			frappe.db.get_value(
				"File", target(connection, "asset", type_, file_id), ["file_name", "is_private"]
			),
			(name, 0),
			f"Shopify file {file_id}",
		)
	about = str(
		frappe.db.get_value("Web Page", target(connection, "page", "Page", "1001"), "main_section_html")
	)
	expect(
		("/cdn/shop/" in about, 'src="/files/storefront' in about),
		(False, True),
		"the page's localised image",
	)
	expect(
		frappe.db.get_value(
			"Importer Link", {"connection": connection, "kind": "delivery_profile"}, "target_doctype"
		),
		"Company",
		"the delivery profile, mirrored onto the company",
	)
	expect(
		frappe.db.count("Importer Link", {"connection": connection, "kind": "shipping_rule"}),
		0,
		"no rate of a zone limited to provinces",
	)
	draft = target(connection, "draft_order", "DraftOrder", "901")
	quotation = frappe.db.get_value("Quotation", draft, ["docstatus", "grand_total", "title"], as_dict=True)
	if state == "v1":
		expect(
			frappe.db.get_value("Item", target(connection, "product", "Product", "302"), "disabled"),
			0,
			"the bra",
		)
		expect(
			(quotation.docstatus, amount(quotation.grand_total), quotation.title),
			(0, amount("75.6"), "Draft order #D1"),
			"open draft order #D1",
		)
		return
	expect(
		(quotation.docstatus, quotation.title),
		(0, "[Removed at source] Draft order #D1"),
		"the completed draft order, left to its order",
	)
	expect(
		frappe.db.get_value("Item", target(connection, "product", "Product", "302"), "disabled"),
		1,
		"the deleted bra",
	)
	target(connection, "order", "Order", "803")


def item_price(connection: str, item: str) -> Decimal:
	price_list = frappe.db.get_value("Importer Connection", connection, "price_list")
	return amount(
		frappe.db.get_value("Item Price", {"item_code": item, "price_list": price_list}, "price_list_rate")
	)


def stock_of(connection: str, item: str) -> Decimal:
	warehouse = frappe.db.get_value("Importer Connection", connection, "warehouse")
	return amount(frappe.db.get_value("Bin", {"item_code": item, "warehouse": warehouse}, "actual_qty") or 0)


def published(item: str) -> int:
	return int(frappe.db.get_value("Website Item", {"item_code": item}, "published") or 0)


@check
def assert_etsy(state: str) -> None:
	connection = "etsy-e"
	mug = target(connection, "product", "Listing", "601")
	expect(
		(
			mug,
			frappe.db.get_value("Item", mug, "item_name"),
			frappe.db.get_value("Item", mug, "has_variants"),
		),
		("ET-MUG-OCEAN", 'Hand-thrown mug "Ocean"', 0),
		"the mug, one item, entities decoded",
	)
	expect(frappe.db.get_value("Item", mug, "item_group"), "Mugs - ET", "section → Item Group")
	expect(stock_of(connection, mug), amount("4"), "the mug's stock, not reduced by its receipts")
	apron = target(connection, "product", "Listing", "602")
	expect(frappe.db.count("Item", {"variant_of": apron}), 2, "the apron's variants")
	charcoal = part(connection, "variant", "ListingVariant/602-1002")
	expect(stock_of(connection, charcoal), amount("0"), "the charcoal apron is out of stock")
	expect(published(target(connection, "product", "Listing", "604")), 0, "an inactive listing, unpublished")
	receipt = target(connection, "order", "Receipt", "801")
	invoice = frappe.db.get_value(
		"Sales Invoice", receipt, ["docstatus", "grand_total", "customer", "po_no"], as_dict=True
	)
	expect(
		(invoice.docstatus, amount(invoice.grand_total), invoice.customer, invoice.po_no),
		(1, amount("134.40"), target(connection, "customer", "User", "9001"), "#801"),
		"receipt 801's invoice, linked to its buyer",
	)
	expect(
		frappe.db.count(
			"Comment",
			{"reference_doctype": "Sales Invoice", "reference_name": receipt, "comment_type": "Comment"},
		),
		1,
		"receipt 801's shipment tracking",
	)
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "order", "Receipt", "802"), "docstatus"),
		0,
		"unpaid receipt 802",
	)
	expect(
		frappe.db.get_value("Item Review", target(connection, "review", "Review", "70011"), "item"),
		mug,
		"the mug's review",
	)
	for policy in ("Policy/5001-policy_privacy", "ReturnPolicy/41"):
		type_, policy_id = policy.split("/")
		expect(
			frappe.db.get_value("Web Page", target(connection, "policy", type_, policy_id), "published"),
			1,
			f"Etsy policy {policy}",
		)
	connected = frappe.get_doc("Importer Connection", connection)
	expect(
		connected.get_password("refresh_token"),
		ETSY_ROTATED_REFRESH_TOKEN,
		"the refresh token Etsy rotated, kept",
	)
	if state == "v1":
		expect(item_price(connection, mug), amount("32"), "the mug's price")
		expect(published(target(connection, "product", "Listing", "603")), 0, "a draft listing, unpublished")
		return
	expect(item_price(connection, mug), amount("34"), "the mug's new price")
	expect(
		frappe.db.get_value("Item", target(connection, "product", "Listing", "603"), "disabled"),
		1,
		"the listing removed at Etsy, disabled",
	)
	credit = part(connection, "refund", "Refund/801-1788432000")
	expect(
		amount(frappe.db.get_value("Sales Invoice", credit, "grand_total")),
		amount("-10"),
		"receipt 801's refund",
	)
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "order", "Receipt", "803"), "docstatus"),
		1,
		"new paid receipt 803",
	)
	expect(
		frappe.db.count("Importer Link", {"connection": connection, "kind": "review", "retired": 0}),
		2,
		"the new review on the apron",
	)


@check
def assert_etsy_connect(connection: str) -> None:
	"""Connect to Etsy: the approval URL asks for read scopes with PKCE, and Etsy's redirect back with a code
	is exchanged for the refresh token the connection keeps."""
	frappe.db.set_value("Importer Connection", connection, "credential_hold", "held for the check")
	frappe.db.commit()
	url = api.etsy_connect(connection)
	query = parse_qs(urlsplit(url).query)
	expect(
		(query["client_id"], query["code_challenge_method"], query["redirect_uri"]),
		(["e2e-keystring"], ["S256"], [api.etsy_redirect_uri()]),
		"the Etsy approval URL",
	)
	api.etsy_callback(code="e2e-authorization-code", state=query["state"][0])
	expect(
		frappe.get_doc("Importer Connection", connection).get_password("refresh_token"),
		ETSY_ROTATED_REFRESH_TOKEN,
		"the refresh token Etsy issued for the code",
	)
	expect(
		frappe.db.get_value("Importer Connection", connection, "credential_hold"), None, "the hold, released"
	)
	expect(
		frappe.local.response.get("location"),
		f"/app/importer-connection/{connection}?etsy=connected",
		"back on the connection",
	)
	try:
		api.etsy_callback(code="e2e-authorization-code", state=query["state"][0])
	except frappe.ValidationError:
		print("a used approval can't be replayed")
	else:
		raise AssertionError("an Etsy approval was accepted twice")


@check
def assert_shared_coupon_codes(code: str, *connections: str) -> None:
	"""Each connection holds ``code`` at once, as its own Coupon Code scoped to its Public Site's store."""
	held = {
		row.name: row.webshop_store
		for row in frappe.get_all(
			"Coupon Code", filters={"coupon_code": code}, fields=["name", "webshop_store"]
		)
	}
	for connection in connections:
		spec = frappe.db.get_value(
			"Importer Connection", connection, ["abbreviation", "webshop_store"], as_dict=True
		)
		expect(held.get(f"{code} - {spec.abbreviation}"), spec.webshop_store, f"{connection}'s {code}")
	print(f"{code}: held by {', '.join(connections)} at once, one per store")


def trouble(connection: str, kind: str | None) -> None:
	"""Makes the connection's fake store rate limit (``throttle``) or refuse (``reject``) every request."""
	path = STATE_DIR / f"{business_of(connection).fake}-trouble"
	if kind is None:
		path.unlink(missing_ok=True)
	else:
		path.write_text(kind)


def runs(connection: str) -> int:
	return int(frappe.db.count("Importer Run", {"connection": connection}))


@check
def assert_lockout_safety(connection: str) -> None:
	"""A source that keeps rate limiting pauses the run in place, and it carries on from there once the
	pause is over; rejected credentials stop the run and hold scheduled work until the connection is
	saved."""
	trouble(connection, "throttle")
	paused = engine.run_by_hand(connection, full=False, dry_run=False, kinds=None)
	expect(paused["status"], "paused", "a rate-limited run")
	state = FrappeRunStore(connection, []).load()
	if state is None or state.resume_at is None or state.resume_at <= now_utc() or state.pauses != 1:
		raise AssertionError(f"the run is not paused in place: {state}")
	expect(FrappeRunLock(connection).holder(), state.id, "the paused run keeps its lock")
	expect(engine.step(connection, 60), False, "a step during the pause")
	expect(FrappeRunStore(connection, []).load(), state, "the paused run, unmoved")
	trouble(connection, None)
	frappe.db.set_value("Importer Run", state.id, "resume_at", naive_utc(now_utc() - timedelta(seconds=1)))
	frappe.db.commit()
	while not engine.step(connection, 60):
		pass
	expect(frappe.db.get_value("Importer Run", state.id, "status"), "Complete", "the resumed run")
	print("rate limited: paused in place, then carried on")

	trouble(connection, "reject")
	stopped = engine.run_by_hand(connection, full=False, dry_run=False, kinds=None)
	expect(stopped["status"], "Incomplete", "a run whose credentials are rejected")
	if not frappe.db.get_value("Importer Connection", connection, "credential_hold"):
		raise AssertionError("rejected credentials didn't hold scheduled runs")
	before = runs(connection)
	jobs.scheduled_incremental()
	jobs.scheduled_full()
	expect(runs(connection), before, "scheduled runs of a held connection")
	print("credentials rejected: run stopped, scheduled runs held")

	trouble(connection, None)
	frappe.get_doc("Importer Connection", connection).save()
	frappe.db.commit()
	expect(
		frappe.db.get_value("Importer Connection", connection, "credential_hold"),
		None,
		"the hold after saving",
	)
	jobs.scheduled_incremental()
	started = FrappeRunStore(connection, []).load()
	if started is None:
		raise AssertionError("the saved connection's scheduled run didn't start")
	while not engine.step(connection, 60):
		pass
	expect(
		frappe.db.get_value("Importer Run", started.id, "status"),
		"Complete",
		"the scheduled run after saving",
	)
	print("saving the connection released the hold")


@check
def sync_now_in_background(connection: str) -> None:
	"""Sync Now queues the run on the background job queue, and a worker carries it to the end."""
	result = api.sync_now(connection, full=1)
	frappe.db.commit()
	run_name = result["run"]
	deadline = time.monotonic() + BACKGROUND_RUN_TIMEOUT_SECONDS
	while time.monotonic() < deadline:
		frappe.db.rollback()
		status = frappe.db.get_value("Importer Run", run_name, "status")
		if status != "Running":
			expect(status, "Complete", "the background run")
			print(f"background run {run_name}: {status}")
			return
		time.sleep(POLL_SECONDS)
	raise AssertionError(f"the background run {run_name} didn't finish in {BACKGROUND_RUN_TIMEOUT_SECONDS}s")


@check
def assert_listed_sources_refuse(template: str) -> None:
	"""A connection to a listed source (Odoo) can be saved, but its run refuses to start, says why, and
	touches nothing."""
	connection = "odoo-e2e"
	if not frappe.db.exists("Importer Connection", connection):
		doc = frappe.copy_doc(frappe.get_doc("Importer Connection", template))
		doc.update(
			{
				"connection_id": connection,
				"source": "Odoo",
				"store_url": "https://odoo.example.test",
				"abbreviation": "OD",
				"cursors": None,
				"last_run": None,
			}
		)
		doc.insert()
		frappe.db.commit()
	before = runs(connection)
	result = engine.run_by_hand(connection, full=True, dry_run=False, kinds=None)
	expect(result["status"], "not started", "a run of a listed source")
	if "not built yet" not in result["reason"]:
		raise AssertionError(f"the refusal doesn't say why: {result['reason']}")
	expect(runs(connection), before, "runs of a listed source")
	expect(frappe.db.count("Importer Link", {"connection": connection}), 0, "links of a listed source")
	print(f"{connection}: {result['reason']}")


@check
def assert_no_side_effects() -> None:
	"""Mirroring history sent nothing: no email queued, no notification raised."""
	expect(frappe.db.count("Email Queue"), 0, "queued emails")
	expect(frappe.db.count("Notification Log"), 0, "notifications")
	print("no emails or notifications")
