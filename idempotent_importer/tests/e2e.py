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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from erpnext.accounts.doctype.pricing_rule.utils import validate_coupon_code
from frappe.desk.page.setup_wizard.setup_wizard import setup_complete
from frappe.installer import update_site_config
from webshop.webshop.discounts.coupon_limits import (
	CouponLimitReachedError,
	CouponNotEligibleError,
	customer_uses,
)

from idempotent_importer import api
from idempotent_importer.core.oauth import CALLBACK_PATH
from idempotent_importer.erpnext import engine, jobs
from idempotent_importer.erpnext.stores import (
	FrappeRunLock,
	FrappeRunStore,
	naive_utc,
	now_utc,
)
from idempotent_importer.erpnext.writers.sales import MULTI_CURRENCY_SETTING
from idempotent_importer.sources.registry import LISTED

STATE_DIR = Path("/state")
FISCAL_YEAR_START = "2026-01-01"
FISCAL_YEAR_END = "2026-12-31"
CURRENCY = "CAD"
FOREIGN_CURRENCIES = ("USD",)
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
	# Its secret is a certificate's private key, generated when the connection is made (never committed).
	signs_with_key: bool = False

	@property
	def site(self) -> str:
		return self.company.lower().replace(" ", "-")

	@property
	def endpoint(self) -> str:
		return f"http://{self.fake}:{FAKE_PORT}{self.endpoint_path}"


# An OAuth source's app, as if the operator had saved it and connected the account.
OAUTH_CREDENTIALS = {
	"api_user": "e2e-oauth-client",
	"api_secret": "e2e-oauth-secret",
	"refresh_token": "e2e-oauth-refresh-token",
}
# The test PeopleSoft installation's field map, shared with the unit tests (tests/support/peoplesoft.py).
PEOPLESOFT_FIELD_MAP = (
	Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "peoplesoft" / "field-map.json"
).read_text()
# The HighLevel sub-account the fixtures (and the fake's token) name.
HIGHLEVEL_LOCATION = "loc0harbour0studio01"
# The refresh token the fake OAuth sources issue for a code (tests/e2e/fake_store.py).
OAUTH_ROTATED_REFRESH_TOKEN = "e2e-oauth-rotated-refresh-token"
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
	Business(
		"SQ",
		"Store Q",
		"square-q",
		"Square Online",
		"square_online",
		"square_online-fake",
		"https://prairiepottery.square.site",
		{"api_secret": "e2e-square-token"},
	),
	Business(
		"BC",
		"Store C",
		"bigcommerce-c",
		"BigCommerce",
		"bigcommerce",
		"bigcommerce-fake",
		"http://bigcommerce-fake:8080",
		{"api_account": "bcfix123", "api_secret": "e2e-bigcommerce-token"},
	),
	Business(
		"SS",
		"Store P",
		"squarespace-p",
		"Squarespace",
		"squarespace",
		"squarespace-fake",
		"https://harbourpine.example",
		{"api_secret": "e2e-squarespace-key"},
	),
	Business(
		"WX",
		"Store X",
		"wix-x",
		"Wix",
		"wix",
		"wix-fake",
		"http://wix-fake:8080",
		{"api_account": "e2e-wix-site", "api_secret": "e2e-wix-key"},
	),
	Business(
		"MG",
		"Store M",
		"magento-m",
		"Magento / Adobe Commerce",
		"magento",
		"magento-fake",
		"http://magento-fake:8080",
		{"api_secret": "e2e-magento-token"},
	),
	Business(
		"LS",
		"Store L",
		"lightspeed-l",
		"Lightspeed eCom (C-Series)",
		"lightspeed",
		"lightspeed-fake",
		"https://harbourlane.example",
		{"api_account": "us1/en", "api_user": "e2e-lightspeed-key", "api_secret": "e2e-lightspeed-secret"},
	),
	Business(
		"OD",
		"Store O",
		"odoo-o",
		"Odoo",
		"odoo",
		"odoo-fake",
		"http://odoo-fake:8080",
		{"auth_method": "JSON-2 API Key", "api_account": "harbour", "api_secret": "e2e-odoo-key"},
	),
	Business(
		"DB",
		"Store N",
		"business-central-b",
		"Dynamics 365 Business Central",
		"dynamics_business_central",
		"dynamics_business_central-fake",
		"https://api.businesscentral.dynamics.com/v2.0/e2e-tenant/production",
		{
			"api_user": "e2e-entra-client",
			"api_secret": "e2e-entra-secret",
			"api_account": "c0ffee00-0000-4000-8000-000000000001",
		},
	),
	Business(
		"DS",
		"Store Y",
		"dynamics-sales-d",
		"Dynamics 365 Sales",
		"dynamics_sales",
		"dynamics_sales-fake",
		"https://harbourpine.crm3.dynamics.com",
		{"api_user": "e2e-entra-client", "api_secret": "e2e-entra-secret", "api_account": "e2e-tenant"},
	),
	Business(
		"DF",
		"Store T",
		"finance-operations-f",
		"Dynamics 365 Finance & Operations",
		"dynamics_finance_operations",
		"dynamics_finance_operations-fake",
		"https://harbourpine.operations.dynamics.com",
		{
			"api_user": "e2e-entra-client",
			"api_secret": "e2e-entra-secret",
			"api_account": "e2e-tenant/hpt",
			# The test environment's order-date field (the entity reference documents none).
			"field_map": '{"sales_order": {"fields": {"date": "ORDERDATE"}}}',
		},
	),
	Business(
		"EC",
		"Store W",
		"ecwid-c",
		"Ecwid",
		"ecwid",
		"ecwid-fake",
		"https://cedarwick.example",
		{"api_account": "7400100", "api_secret": "e2e-ecwid-token"},
	),
	Business(
		"QB",
		"Books Q",
		"quickbooks-q",
		"QuickBooks Online",
		"quickbooks_online",
		"quickbooks_online-fake",
		"https://quickbooks.api.intuit.com",
		{**OAUTH_CREDENTIALS, "api_account": "9341452345"},
	),
	Business(
		"XR",
		"Books X",
		"xero-x",
		"Xero",
		"xero",
		"xero-fake",
		"https://go.xero.com",
		{**OAUTH_CREDENTIALS, "api_account": "70784a63-d24b-46a9-a4db-0e70a274b056"},
	),
	Business(
		"ZB",
		"Books Z",
		"zoho-books-b",
		"Zoho Books",
		"zoho_books",
		"zoho_books-fake",
		"https://books.zoho.com",
		{**OAUTH_CREDENTIALS, "api_account": "10234695"},
	),
	Business(
		"ZI",
		"Books I",
		"zoho-inventory-i",
		"Zoho Inventory",
		"zoho_inventory",
		"zoho_inventory-fake",
		"https://inventory.zoho.com",
		{**OAUTH_CREDENTIALS, "api_account": "20000001"},
	),
	Business(
		"NS",
		"Books N",
		"netsuite-n",
		"NetSuite",
		"netsuite",
		"netsuite-fake",
		"https://1234567.app.netsuite.com",
		{"api_user": "e2e-netsuite-client", "api_key_id": "e2e-certificate", "api_account": "1234567"},
		signs_with_key=True,
	),
	Business(
		"SB",
		"Books B",
		"sap-b",
		"SAP Business One",
		"sap_business_one",
		"sap_business_one-fake",
		"https://b1.example.test:50000",
		# The test company's login (tests/support/sap_business_one.py) and its chart's drawers.
		{
			"api_user": "e2e-b1-user",
			"api_secret": "e2e-b1-password",
			"api_account": "SBO_NORTHWIND",
			"field_map": '{"root_types": {"1": "Asset", "2": "Liability", "3": "Equity"}}',
		},
	),
	Business(
		"AC",
		"Books A",
		"acumatica-a",
		"Acumatica",
		"acumatica",
		"acumatica-fake",
		"https://summitgear.acumatica.com",
		OAUTH_CREDENTIALS,
	),
	Business(
		"SI",
		"Books S",
		"intacct-s",
		"Sage Intacct",
		"sage_intacct",
		"sage_intacct-fake",
		"https://www.intacct.com",
		# The test company's sender and user (tests/support/sage_intacct.py), and its equity account.
		{
			"api_user": "e2e-reader",
			"api_secret": "e2e-reader-password",
			"api_account": "LINCPA",
			"api_key_id": "e2e-sender",
			"api_sender_password": "e2e-sender-password",
			"field_map": '{"root_types": {"3000": "Equity"}}',
		},
	),
	Business(
		"AG",
		"People A",
		"agendrix-a",
		"Agendrix",
		"agendrix",
		"agendrix-fake",
		"https://app.agendrix.com",
		OAUTH_CREDENTIALS,
	),
	Business(
		"HL",
		"Studio H",
		"highlevel-h",
		"HighLevel",
		"highlevel",
		"highlevel-fake",
		"https://app.gohighlevel.com",
		{**OAUTH_CREDENTIALS, "api_account": HIGHLEVEL_LOCATION},
	),
	Business(
		"PS",
		"People P",
		"peoplesoft-p",
		"PeopleSoft",
		"peoplesoft",
		"peoplesoft-fake",
		"https://hcm.example.test:8443",
		# The test installation's user (tests/support/peoplesoft.py) and the field map naming its queries.
		{"api_user": "e2e-ps-reader", "api_secret": "e2e-ps-password", "field_map": PEOPLESOFT_FIELD_MAP},
	),
	Business(
		"HS",
		"Pipeline H",
		"hubspot-s",
		"HubSpot",
		"hubspot",
		"hubspot-fake",
		"https://app.hubspot.com",
		OAUTH_CREDENTIALS,
	),
	Business(
		"SF",
		"Pipeline F",
		"salesforce-f",
		"Salesforce",
		"salesforce",
		"salesforce-fake",
		"https://northwind.my.salesforce.com",
		OAUTH_CREDENTIALS,
	),
)


# The connections that mirror gift cards or loyalty into the fake Square (tests/fixtures/square_stored_value).
SQUARE_FAKE = f"http://square_stored_value-fake:{FAKE_PORT}"
SQUARE_ACCOUNT = {
	"square_environment": "Sandbox",
	"square_location_id": "LOCMAIN1",
	"square_access_token": "e2e-square-token",
}
SQUARE_STORED_VALUE = {
	"woo-a": {"import_gift_cards": 1, **SQUARE_ACCOUNT},
	"bigcommerce-c": {"import_gift_cards": 1, **SQUARE_ACCOUNT},
	"wix-x": {"import_loyalty": 1, **SQUARE_ACCOUNT},
}
GIFT_CARD_NUMBER_KEY = "idempotent_importer_gift_card_number_key"


def gift_card(gid: str) -> Any:
	name = frappe.db.get_value("Importer Gift Card", {"connection": gid.split("/")[3], "gid": gid})
	if not name:
		raise AssertionError(f"{gid} is not mirrored into Square")
	return frappe.get_doc("Importer Gift Card", name)


def assert_gift_cards(
	gid: str, gan: str | None, square_id: str | None, source: str, loaded: str, kinds: list[str], what: str
) -> Any:
	card = gift_card(gid)
	expect((card.gan, card.square_id), (gan, square_id), f"{what}: its Square card")
	expect((card.source_balance, card.loaded_balance), (source, loaded), f"{what}: its balance in Square")
	expect(ledger_kinds(card), kinds, f"{what}: its ledger")
	return card


def ledger_kinds(card: Any) -> list[str]:
	return [row.entry_kind for row in card.entries]


def business(abbr: str) -> Business:
	return next(spec for spec in BUSINESSES if spec.abbr == abbr)


def business_of(connection: str) -> Business:
	return next(spec for spec in BUSINESSES if spec.connection == connection)


CONTENT_DOCTYPES = (
	"Importer Gift Card",
	"Importer Gift Card Entry",
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
	"Account",
	"Item Tax Template",
	"Item Tax Template Detail",
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
	"Shipment",
	"Tag",
	"Tag Link",
	"Supplier",
	"Cost Center",
	"Warehouse",
	"Project",
	"Lead",
	"Opportunity",
	"CRM Note",
	"Prospect",
	"Prospect Lead",
	"Opportunity Type",
	"Communication",
	"Communication Link",
	"Event",
	"Event Participants",
	"ToDo",
	"Department",
	"Designation",
	"Branch",
	"Employee",
	"Shift Type",
	"Shift Assignment",
	"Employee Checkin",
	"Attendance",
	"Leave Type",
	"Leave Application",
	"Leave Ledger Entry",
	"Sales Order",
	"Sales Order Item",
	"Purchase Order",
	"Purchase Order Item",
	"Purchase Invoice",
	"Purchase Invoice Item",
	"Purchase Taxes and Charges",
	"Journal Entry",
	"Journal Entry Account",
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
	# Orders paid in another currency (BigCommerce's USD order) book to the customer's own receivable.
	frappe.db.set_single_value("Accounts Settings", MULTI_CURRENCY_SETTING, 1)
	for currency in FOREIGN_CURRENCIES:
		frappe.db.set_value("Currency", currency, "enabled", 1)
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
	endpoints |= {f"{connection}:square": SQUARE_FAKE for connection in SQUARE_STORED_VALUE}
	update_site_config("idempotent_importer_test_endpoints", endpoints)
	update_site_config(GIFT_CARD_NUMBER_KEY, "e2e-gift-card-key")
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


def signing_key() -> str:
	"""A new P-256 private key, PEM: the e2e certificate's (the fake NetSuite reads its assertions' claims
	without the public key)."""
	key = ec.generate_private_key(ec.SECP256R1())
	return key.private_bytes(
		serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
	).decode()


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
	if not frappe.db.exists("Address", f"{company}-Billing"):
		# The address its shipments leave from.
		frappe.get_doc(
			{
				"doctype": "Address",
				"address_title": company,
				"address_type": "Billing",
				"address_line1": "100 Warehouse Road",
				"city": "Edmonton",
				"country": COUNTRY,
				"is_your_company_address": 1,
				"links": [{"link_doctype": "Company", "link_name": company}],
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
	holidays = f"{company} Holidays"
	if not frappe.db.exists("Holiday List", holidays):
		# HRMS counts leave days against the company's holiday list (none: every day is a working day).
		frappe.get_doc(
			{
				"doctype": "Holiday List",
				"holiday_list_name": holidays,
				"from_date": FISCAL_YEAR_START,
				"to_date": FISCAL_YEAR_END,
			}
		).insert()
		frappe.db.set_value("Company", company, "default_holiday_list", holidays)
		frappe.get_doc(
			{
				"doctype": "Holiday List Assignment",
				"applicable_for": "Company",
				"assigned_to": company,
				"holiday_list": holidays,
				"from_date": FISCAL_YEAR_START,
			}
		).submit()
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
			**({"api_secret": signing_key()} if spec.signs_with_key else {}),
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
			**SQUARE_STORED_VALUE.get(spec.connection, {}),
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
		"assert_oauth_connect": assert_oauth_connect,
		"assert_square_online": assert_square_online,
		"assert_bigcommerce": assert_bigcommerce,
		"assert_squarespace": assert_squarespace,
		"assert_wix": assert_wix,
		"assert_magento": assert_magento,
		"assert_lightspeed": assert_lightspeed,
		"assert_ecwid": assert_ecwid,
		"assert_odoo": assert_odoo,
		"assert_business_central": assert_business_central,
		"assert_dynamics_sales": assert_dynamics_sales,
		"assert_finance_operations": assert_finance_operations,
		"assert_quickbooks_online": assert_quickbooks_online,
		"assert_xero": assert_xero,
		"assert_zoho_books": assert_zoho_books,
		"assert_zoho_inventory": assert_zoho_inventory,
		"assert_netsuite": assert_netsuite,
		"assert_sap_business_one": assert_sap_business_one,
		"assert_acumatica": assert_acumatica,
		"assert_sage_intacct": assert_sage_intacct,
		"assert_agendrix": assert_agendrix,
		"assert_highlevel": assert_highlevel,
		"assert_peoplesoft": assert_peoplesoft,
		"assert_hubspot": assert_hubspot,
		"assert_salesforce": assert_salesforce,
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


def assert_discount(
	connection: str,
	type_: str,
	source_id: str,
	cards: str | None,
	kinds: list[str],
	what: str,
	held_codes: dict[str, str] | None = None,
) -> None:
	"""A mirrored discount: held awaiting ``cards`` (disabled, its conditions ``kinds`` recorded, none of its
	codes redeemable), or, when ``cards`` is None, enabled with none recorded and its codes redeemable but
	for ``held_codes`` (a code held by its own conditions, and the cards it awaits)."""
	name = target(connection, "discount", type_, source_id)
	doctype = frappe.db.get_value(
		"Importer Link", {"connection": connection, "kind": "discount", "target_name": name}, "target_doctype"
	)
	record = frappe.db.get_value(
		doctype, name, ["disable", "importer_hold", "importer_conditions"], as_dict=True
	)
	recorded = [c["kind"] for c in json.loads(record.importer_conditions or "[]")]
	hold = f"held: awaiting enforcement ({cards})" if cards else None
	expect((record.disable, record.importer_hold, recorded), (int(bool(cards)), hold, kinds), what)
	if doctype == "Promotional Scheme":
		return
	live = frappe.get_all(
		"Importer Link",
		filters={
			"connection": connection,
			"kind": "coupon",
			"gid": ["like", f"%/{type_}/{source_id}#%"],
			"retired": 0,
		},
		pluck="target_name",
	)
	for coupon in frappe.get_all(
		"Coupon Code", filters={"name": ["in", live or [""]]}, fields=["name", "coupon_code", "importer_hold"]
	):
		try:
			validate_coupon_code(coupon.name)
			redeemable = True
		except frappe.ValidationError:
			redeemable = False
		own = (held_codes or {}).get(coupon.coupon_code)
		code_hold = f"held: awaiting enforcement ({own})" if own and not cards else hold
		expect(
			(redeemable, coupon.importer_hold),
			(not cards and not own, code_hold),
			f"{what}: code {coupon.coupon_code}",
		)


def assert_fork_limits(
	connection: str,
	type_: str,
	source_id: str,
	rule: dict[str, Any],
	codes: dict[str, dict[str, Any]],
	what: str,
) -> None:
	"""The Webshop fork's fields (card 405) on a mirrored discount's Pricing Rule (``rule``) and on its Coupon
	Codes (``codes``, by code)."""
	name = target(connection, "discount", type_, source_id)
	expect(frappe.db.get_value("Pricing Rule", name, list(rule), as_dict=True), rule, f"{what}: its rule")
	for code, wanted in codes.items():
		coupon = frappe.get_doc("Coupon Code", target(connection, "coupon", type_, f"{source_id}#{code}"))
		got = {
			field: [row.customer for row in coupon.eligible_customers]
			if field == "eligible_customers"
			else coupon.get(field)
			for field in wanted
		}
		expect(got, wanted, f"{what}: code {code}")


def coupon_order(connection: str, coupon: str, customer: str, item: str) -> Any:
	"""An unsaved Sales Order of ``customer``'s for one ``item`` at 100, with ``coupon`` (a Coupon Code)."""
	company = business_of(connection).company
	warehouse = frappe.get_all(
		"Warehouse", filters={"company": company, "is_group": 0}, pluck="name", limit=1
	)
	return frappe.get_doc(
		{
			"doctype": "Sales Order",
			"company": company,
			"customer": customer,
			"currency": CURRENCY,
			"delivery_date": frappe.utils.add_days(frappe.utils.nowdate(), 7),
			"coupon_code": coupon,
			"items": [{"item_code": item, "qty": 1, "rate": 100, "warehouse": warehouse[0]}],
		}
	)


def refuses(order: Any, error: type[Exception]) -> bool:
	"""Whether saving ``order`` raises ``error``."""
	try:
		order.insert(ignore_permissions=True)
	except error:
		return True
	return False


def assert_one_use_per_customer(connection: str, coupon: str, customer: str, item: str, what: str) -> None:
	"""The Webshop fork lets ``customer`` use ``coupon`` (a Coupon Code) once: a second order with it is refused.
	Rolled back, so the store's records stay as the importer wrote them."""
	frappe.db.savepoint("fork_limit")
	try:
		coupon_order(connection, coupon, customer, item).insert(ignore_permissions=True).submit()
		refused = refuses(coupon_order(connection, coupon, customer, item), CouponLimitReachedError)
		expect(refused, True, f"{what}: a second order with the code")
	finally:
		frappe.db.rollback(save_point="fork_limit")
	print(f"{what}: one use per customer, enforced")


def assert_new_customers_only(
	connection: str, coupon: str, returning: str, new: str, item: str, what: str
) -> None:
	"""The Webshop fork (card 407) lets only a customer with no order yet use ``coupon``: ``new`` gets its
	discount, ``returning`` (with mirrored orders) is refused. Rolled back."""
	expect(customer_uses(returning, None) > 0, True, f"{what}: the returning customer has mirrored orders")
	expect(customer_uses(new, None), 0, f"{what}: the new customer has none")
	frappe.db.savepoint("fork_rule_limit")
	try:
		allowed = coupon_order(connection, coupon, new, item).insert(ignore_permissions=True)
		expect(amount(allowed.discount_amount) > 0, True, f"{what}: the new customer's discount")
		refused = refuses(coupon_order(connection, coupon, returning, item), CouponNotEligibleError)
		expect(refused, True, f"{what}: a returning customer's order with the code")
	finally:
		frappe.db.rollback(save_point="fork_rule_limit")
	print(f"{what}: new customers only, enforced")


@check
def assert_woocommerce(connection: str, state: str) -> None:
	"""The WooCommerce fixtures' state ``state`` as it should land in ERPNext."""
	if connection == "woo-a":
		used = state != "v1"
		assert_gift_cards(
			"gid://woocommerce/woo-a/GiftCard/177",
			"5QY5E4AXTAVRLCPK",
			"GC_W177",
			"60" if used else "100",
			"60" if used else "100",
			["issued", "used", "activate", "adjust_decrement"] if used else ["issued", "activate"],
			"the WooCommerce gift card (redeemed into Ingrid's account; partly used in v2)",
		)
		assert_gift_cards(
			"gid://woocommerce/woo-a/GiftCard/178",
			"Z9KQW2LPNM4RXT7B",
			None,
			"100",
			"0",
			["issued"],
			"the switched-off gift card (nothing to spend: no Square card)",
		)
	else:
		expect(
			frappe.db.count("Importer Gift Card", {"connection": connection}),
			0,
			f"{connection} mirrors no gift cards",
		)
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
	assert_discount(
		connection,
		"Coupon",
		"304",
		None,
		["emails"],
		"the email-restricted coupon, enabled: the fork enforces it",
	)
	assert_fork_limits(
		connection,
		"Coupon",
		"304",
		{"maximum_use_per_customer": 0, "shipping_discount": ""},
		{"staffonly": {"eligible_emails": "*@atelier-north.test", "maximum_use_per_customer": 0}},
		"the email-restricted coupon",
	)
	assert_discount(connection, "Coupon", "301", None, [], "the unlimited coupon, enabled")
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
	assert_tax_inclusive(
		connection, "805", "31.50", {"GST": "1.44", "PST": "1.31"}, ["GST at 5, PST at 7", "GST at 5"]
	)
	assert_shipment(
		connection,
		"801",
		"Fulfillment/8301",
		{
			"carrier": "Canada Post",
			"awb_number": "1234567890",
			"tracking_url": "https://www.canadapost-postescanada.ca/track/1234567890",
			"tracking_status": "In Progress",
		},
	)
	assert_discount(
		connection,
		"DiscountCodeNode",
		"701",
		"card 405",
		["once_per_customer", "shared_use_limit"],
		"discount 701 (once per customer, one limit for two codes), held",
	)
	assert_discount(
		connection,
		"DiscountCodeNode",
		"702",
		"card 405",
		["free_shipping"],
		"free-shipping discount 702, held",
	)
	assert_discount(
		connection, "DiscountCodeNode", "704", None, [], "discount 704 (a use limit only), enabled"
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
		0,
		"receipt 801's comments: its shipment is a Shipment",
	)
	assert_shipment(
		connection,
		"801",
		"Shipment/3001",
		{"carrier": "Canada Post", "awb_number": "CP123456789CA"},
		"Receipt",
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
def assert_square_online(state: str) -> None:
	connection = "square-q"
	assert_square_discounts(connection, state)
	mug = target(connection, "product", "Item", "ITEM_MUG")
	expect(
		(
			mug,
			frappe.db.get_value("Item", mug, "item_name"),
			frappe.db.get_value("Item", mug, "has_variants"),
		),
		("SQ-PP-MUG-OCEAN", "Ocean mug", 0),
		"the mug, one item",
	)
	expect(stock_of(connection, mug), amount("4"), "the mug's main-location stock, not reduced by orders")
	expect(frappe.db.get_value("Item Barcode", {"parent": mug}, "barcode"), "0629104150021", "UPC → barcode")
	expect(frappe.db.get_value("Item", mug, "item_group"), "Mugs - SQ", "category → Item Group")
	charcoal = part(connection, "variant", "ItemVariation/VAR_APRON_CHAR")
	expect(
		(item_price(connection, charcoal), stock_of(connection, charcoal)),
		(amount("48"), amount("0")),
		"the main location's price override, and sold out",
	)
	expect(
		frappe.db.get_value("Importer Link", {"connection": connection, "gid": ["like", "%/Item/ITEM_GIFT"]}),
		None,
		"the gift card",
	)
	order = target(connection, "order", "Order", "ORDER_1")
	invoice = frappe.db.get_value(
		"Sales Invoice", order, ["docstatus", "grand_total", "discount_amount", "customer"], as_dict=True
	)
	expect(
		(invoice.docstatus, amount(invoice.grand_total), amount(invoice.discount_amount), invoice.customer),
		(1, amount("123.06"), amount("4.80"), target(connection, "customer", "Customer", "CUST_ADA")),
		"order 1's invoice",
	)
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "order", "Order", "ORDER_2"), "docstatus"),
		0,
		"unpaid order 2",
	)
	expect(
		frappe.db.get_value(
			"Web Page", target(connection, "policy", "ReturnPolicy", "LOCMAIN1"), "published"
		),
		1,
		"the return policy page",
	)
	if state == "v1":
		expect(item_price(connection, mug), amount("32"), "the mug's price")
		expect(
			published(target(connection, "product", "Item", "ITEM_SAMPLE")),
			0,
			"an archived item, unpublished",
		)
		return
	expect(item_price(connection, mug), amount("34"), "the mug's new price")
	expect(
		frappe.db.get_value("Item", target(connection, "product", "Item", "ITEM_SAMPLE"), "disabled"),
		1,
		"the deleted item, disabled",
	)
	credit = part(connection, "refund", "Refund/REF_1")
	expect(
		amount(frappe.db.get_value("Sales Invoice", credit, "grand_total")), amount("-10"), "order 1's refund"
	)
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "order", "Order", "ORDER_3"), "customer"),
		target(connection, "customer", "Customer", "CUST_GRACE"),
		"new order 3, linked to the new customer",
	)


@check
def assert_bigcommerce(state: str) -> None:
	connection = "bigcommerce-c"
	spent = state != "v1"
	certificate = "gid://bigcommerce/bigcommerce-c/GiftCertificate"
	assert_gift_cards(
		f"{certificate}/901",
		"4KZX0OUHIJHC",
		"GC_BC901",
		"30" if spent else "50",
		"30" if spent else "50",
		["issue", "observed", "activate", "adjust_decrement"] if spent else ["issue", "activate"],
		"the gift certificate (its own code; spent at BigCommerce in v2)",
	)
	credit = gift_card("gid://bigcommerce/bigcommerce-c/StoreCredit/41")
	expect(
		(credit.kind, credit.square_id, credit.loaded_balance, credit.gan),
		("store_credit", "GC_SC41", "15", "9489748124649236")
		if spent
		else ("store_credit", None, "0", credit.gan),
		"the customer's store credit (none in v1: no Square card yet; 15 in v2: a Square card with a new number)",
	)
	unpaid = assert_gift_cards(
		f"{certificate}/902",
		"9034877072048837",
		None,
		"25",
		"0",
		["issue"],
		"the unpaid certificate (a new number; nothing to spend, so no Square card yet)",
	)
	expect(unpaid.state, "disabled", "the unpaid certificate's state")
	usd = frappe.get_doc("Sales Invoice", target(connection, "order", "Order", "504"))
	expect(
		(
			usd.docstatus,
			usd.currency,
			amount(usd.conversion_rate),
			amount(usd.grand_total),
			amount(usd.base_grand_total),
		),
		(1, "USD", amount("1.33"), amount("31.50"), amount("42.00")),
		"order 504, paid in USD at BigCommerce's rate (0.75 USD per CAD)",
	)
	expect(amount(usd.outstanding_amount), amount("0"), "order 504, paid")
	assert_shipment(
		connection,
		"501",
		"Shipment/1",
		{
			"carrier": "Canada Post",
			"carrier_service": "Canada Post Expedited",
			"awb_number": "CP123456789CA",
			"description_of_content": "2 x Enamel trail mug, 1 x Wool toque",
		},
	)
	mug = target(connection, "product", "Product", "111")
	expect(
		(mug, frappe.db.get_value("Item", mug, "brand"), frappe.db.get_value("Item", mug, "item_group")),
		("BC-BC-MUG-1", "Northern Trail", "Camp Kitchen - BC"),
		"the mug: its base variant's SKU, brand and category",
	)
	expect(stock_of(connection, mug), amount("12"), "product-level stock, not reduced by orders")
	expect(frappe.db.get_value("Item Barcode", {"parent": mug}, "barcode"), "0778899100018", "GTIN → barcode")
	navy = part(connection, "variant", "Variant/1122")
	red = part(connection, "variant", "Variant/1121")
	expect(
		(
			item_price(connection, navy),
			stock_of(connection, navy),
			item_price(connection, red),
			stock_of(connection, red),
		),
		(amount("32.5"), amount("0"), amount("30"), amount("5")),
		"the navy variant's own price and stock; the red one inherits the product's price",
	)
	order = target(connection, "order", "Order", "501")
	invoice = frappe.db.get_value(
		"Sales Invoice", order, ["docstatus", "grand_total", "discount_amount", "customer"], as_dict=True
	)
	expect(
		(invoice.docstatus, amount(invoice.grand_total), amount(invoice.discount_amount), invoice.customer),
		(1, amount("92.40"), amount("4.50"), target(connection, "customer", "Customer", "41")),
		"order 501's invoice",
	)
	expect(
		frappe.db.count(
			"Comment",
			{"reference_doctype": "Sales Invoice", "reference_name": order, "comment_type": "Comment"},
		),
		1,
		"order 501's staff notes (its shipment is a Shipment)",
	)
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "order", "Order", "502"), "docstatus"),
		0,
		"pending guest order 502",
	)
	assert_bigcommerce_discounts(connection, state)
	expect(
		frappe.db.get_value("Web Page", target(connection, "page", "Page", "82"), "published"),
		0,
		"a hidden page",
	)
	post = target(connection, "article", "Post", "91")
	expect(
		frappe.db.get_value("Blog Post", post, "blog_category"),
		target(connection, "blog", "Blog", "bcfix123"),
		"the post in the store's blog",
	)
	expect(
		frappe.db.count("Importer Link", {"connection": connection, "kind": "review", "retired": 0}),
		1 if state == "v1" else 2,
		"approved reviews",
	)
	if state == "v1":
		expect(item_price(connection, mug), amount("24"), "the mug's price")
		expect(
			published(target(connection, "product", "Product", "113")), 0, "the hidden product, unpublished"
		)
		return
	expect(item_price(connection, mug), amount("26"), "the mug's new price")
	expect(
		frappe.db.get_value("Item", target(connection, "product", "Product", "113"), "disabled"),
		1,
		"the deleted product",
	)
	credit = part(connection, "refund", "Refund/9001")
	expect(
		amount(frappe.db.get_value("Sales Invoice", credit, "grand_total")),
		amount("-10"),
		"order 501's refund",
	)
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "order", "Order", "503"), "docstatus"),
		1,
		"new paid order 503",
	)


@check
def assert_squarespace(state: str) -> None:
	connection = "squarespace-p"
	assert_tax_inclusive(
		connection,
		"6d4000000000000000000034",
		"31.50",
		{"GST": "1.44", "PST": "1.31"},
		["GST at 5, PST at 7", "GST at 5"],
	)
	board = target(connection, "product", "Product", "6b2000000000000000000011")
	expect(
		(board, frappe.db.get_value("Item", board, "item_group")),
		("SS-SQS-BOARD-1", "Kitchen - SS"),
		"the board: its variant's SKU, its store page",
	)
	expect(stock_of(connection, board), amount("9"), "the board's stock, not reduced by orders")
	expect(
		frappe.db.get_value("Item Barcode", {"parent": board}, "barcode"), "0662233440017", "GTIN → barcode"
	)
	expect(tags_of("Item", board), ["cedar", "kitchen"], "the board's tags")
	moss = part(connection, "variant", "ProductVariant/a1b2c3d4-0000-4000-8000-000000000122")
	expect(
		(item_price(connection, moss), stock_of(connection, moss)),
		(amount("65"), amount("0")),
		"the moss scarf",
	)
	ebook = target(connection, "product", "Product", "6b2000000000000000000014")
	expect(
		(frappe.db.get_value("Item", ebook, "is_stock_item"), item_price(connection, ebook)),
		(0, amount("12")),
		"a download: not a stock item, priced at the product",
	)
	expect(
		frappe.db.get_value(
			"Importer Link", {"connection": connection, "gid": ["like", "%/Product/6b2000000000000000000015"]}
		),
		None,
		"the gift card",
	)
	order = target(connection, "order", "Order", "6d4000000000000000000031")
	invoice = frappe.db.get_value(
		"Sales Invoice", order, ["docstatus", "grand_total", "discount_amount", "customer"], as_dict=True
	)
	expect(
		(invoice.docstatus, amount(invoice.grand_total), amount(invoice.discount_amount), invoice.customer),
		(
			1,
			amount("165.90"),
			amount("5.00"),
			target(connection, "customer", "Profile", "6c3000000000000000000021"),
		),
		"order 1001's invoice",
	)
	expect(
		frappe.db.count(
			"Comment",
			{"reference_doctype": "Sales Invoice", "reference_name": order, "comment_type": "Comment"},
		),
		1,
		"order 1001's internal note (its fulfillment is a Shipment)",
	)
	assert_shipment(
		connection,
		"6d4000000000000000000031",
		"Fulfillment/6d4000000000000000000031-0",
		{"carrier": "Canada Post", "awb_number": "SQS1234567890CA"},
	)
	expect(
		frappe.db.get_value(
			"Sales Invoice", target(connection, "order", "Order", "6d4000000000000000000032"), "docstatus"
		),
		0,
		"unpaid guest order 1002",
	)
	expect(
		frappe.db.get_value(
			"Importer Link", {"connection": connection, "gid": ["like", "%/Profile/6c3000000000000000000023"]}
		),
		None,
		"a mailing-list subscriber",
	)
	assert_squarespace_discounts(connection, state)
	if state == "v1":
		expect(item_price(connection, board), amount("48"), "the board's price")
		expect(
			published(target(connection, "product", "Product", "6b2000000000000000000013")),
			0,
			"a hidden product",
		)
		return
	expect(item_price(connection, board), amount("52"), "the board's new price")
	expect(
		frappe.db.get_value(
			"Item", target(connection, "product", "Product", "6b2000000000000000000013"), "disabled"
		),
		1,
		"the deleted product, disabled",
	)
	credit = part(connection, "refund", "Refund/b7c8d9e0-0000-4000-8000-000000009001")
	expect(
		amount(frappe.db.get_value("Sales Invoice", credit, "grand_total")),
		amount("-10"),
		"order 1001's refund",
	)
	expect(
		frappe.db.get_value(
			"Sales Invoice", target(connection, "order", "Order", "6d4000000000000000000033"), "customer"
		),
		target(connection, "customer", "Profile", "6c3000000000000000000024"),
		"new order 1003, linked to the new customer",
	)


@check
def assert_wix(state: str) -> None:
	connection = "wix-x"
	redeemed = state != "v1"
	assert_gift_cards(
		"gid://wix/wix-x/LoyaltyAccount/5b0e7c40-0001-4a1b-8c2d-3e4f5a6b0001",
		None,
		"LA_MAYA",
		"70" if redeemed else "120",
		"70" if redeemed else "120",
		["earn", "adjust", "redeem", "adjust_points", "adjust_points"]
		if redeemed
		else ["earn", "adjust", "adjust_points"],
		"the Wix loyalty account, enrolled in Square Loyalty by the customer's phone",
	)
	assert_tax_inclusive(
		connection,
		"8f1e2d30-0005-4c5b-a69d-7e8f9a0b0005",
		"31.50",
		{"GST": "1.44", "PST": "1.31"},
		["GST at 5, PST at 7", "GST at 5"],
	)
	mug = target(connection, "product", "Product", "1a7e5c10-0001-4c2e-9b41-6f0d2a1e0001")
	expect(
		(mug, frappe.db.get_value("Item", mug, "item_group")),
		("WX-WIX-MUG-1", "Mugs - WX"),
		"the mug: its default variant's SKU, its category",
	)
	expect(stock_of(connection, mug), amount("14"), "the mug's tracked stock, not reduced by orders")
	expect(frappe.db.get_value("Item Barcode", {"parent": mug}, "barcode"), "0845511200019", "barcode")
	sage = part(
		connection,
		"variant",
		"Variant/1a7e5c10-0002-4c2e-9b41-6f0d2a1e0002:7c1d3e20-0001-4f8a-a3b2-1d9c4e5f0001",
	)
	expect(
		(item_price(connection, sage), stock_of(connection, sage)),
		(amount("22"), amount("6")),
		"the sage towel: its compare-at price (the sale is a pricing rule) and stock",
	)
	order = target(connection, "order", "Order", "8f1e2d30-0001-4c5b-a69d-7e8f9a0b0001")
	invoice = frappe.db.get_value(
		"Sales Invoice", order, ["docstatus", "grand_total", "discount_amount", "customer"], as_dict=True
	)
	expect(
		(invoice.docstatus, amount(invoice.grand_total), amount(invoice.discount_amount), invoice.customer),
		(
			1,
			amount("69.93"),
			amount("6.40"),
			target(connection, "customer", "Contact", "2c4e6a80-0001-4b3d-9e1f-5a6b7c8d0001"),
		),
		"order 10001's invoice",
	)
	expect(
		frappe.db.count(
			"Comment",
			{"reference_doctype": "Sales Invoice", "reference_name": order, "comment_type": "Comment"},
		),
		0,
		"order 10001's comments: its fulfillment is a Shipment",
	)
	assert_shipment(
		connection,
		"8f1e2d30-0001-4c5b-a69d-7e8f9a0b0001",
		"Fulfillment/ff000000-0000-4000-8000-000000000001",
		{"carrier": "Canada Post", "awb_number": "7023210039414604"},
	)
	expect(
		frappe.db.get_value(
			"Sales Invoice",
			target(connection, "order", "Order", "8f1e2d30-0002-4c5b-a69d-7e8f9a0b0002"),
			"docstatus",
		),
		0,
		"unpaid order 10002",
	)
	assert_wix_discounts(connection, state)
	post = target(connection, "article", "Post", "0e1f2a30-0001-4b5c-9d6e-7f8a9b0c0001")
	expect(
		frappe.db.get_value("Blog Post", post, "blog_category"),
		target(connection, "blog", "BlogCategory", "4d3c2b10-0001-4e5f-8a9b-0c1d2e3f0001"),
		"the post in its first category",
	)
	expect(
		'<img src="/files/kiln' in (frappe.db.get_value("Blog Post", post, "content_html") or ""),
		True,
		"the post's image, copied",
	)
	expect(
		frappe.db.count("Importer Link", {"connection": connection, "kind": "review", "retired": 0}),
		1,
		"the approved review",
	)
	if state == "v1":
		expect(item_price(connection, mug), amount("28"), "the mug's price")
		bowl = target(connection, "product", "Product", "1a7e5c10-0003-4c2e-9b41-6f0d2a1e0003")
		expect(published(bowl), 0, "a hidden product")
		return
	expect(item_price(connection, mug), amount("30"), "the mug's new price")
	credit = part(connection, "refund", "Refund/b0000000-0000-4000-8000-000000000001")
	expect(
		amount(frappe.db.get_value("Sales Invoice", credit, "grand_total")),
		amount("-17.01"),
		"order 10001's refund",
	)
	expect(
		frappe.db.get_value(
			"Sales Invoice",
			target(connection, "order", "Order", "8f1e2d30-0003-4c5b-a69d-7e8f9a0b0003"),
			"customer",
		),
		target(connection, "customer", "Contact", "2c4e6a80-0003-4b3d-9e1f-5a6b7c8d0003"),
		"new order 10003, linked to the new contact",
	)


@check
def assert_magento(state: str) -> None:
	connection = "magento-m"
	kettle = target(connection, "product", "Product", "11")
	expect(
		(kettle, frappe.db.get_value("Item", kettle, "item_group")),
		("MG-MAG-KETTLE-1", "Galley - MG"),
		"the kettle: its SKU, its category (not the store root)",
	)
	expect(stock_of(connection, kettle), amount("7"), "the kettle's stock, not reduced by orders")
	navy = part(connection, "variant", "Variant/122")
	red = part(connection, "variant", "Variant/121")
	expect(
		(
			item_price(connection, navy),
			stock_of(connection, navy),
			item_price(connection, red),
			stock_of(connection, red),
		),
		(amount("159.5"), amount("0"), amount("149"), amount("4")),
		"the jacket's child products: their prices and stock",
	)
	tide = target(connection, "product", "Product", "14")
	expect(frappe.db.get_value("Item", tide, "is_stock_item"), 0, "a downloadable product isn't a stock item")
	expect(
		frappe.db.get_value("Importer Link", {"connection": connection, "gid": ["like", "%/Product/15"]}),
		None,
		"the bundle",
	)
	order = target(connection, "order", "Order", "501")
	invoice = frappe.db.get_value(
		"Sales Invoice", order, ["docstatus", "grand_total", "discount_amount", "customer"], as_dict=True
	)
	expect(
		(invoice.docstatus, amount(invoice.grand_total), amount(invoice.discount_amount), invoice.customer),
		(1, amount("258.82"), amount("5.00"), target(connection, "customer", "Customer", "41")),
		"order 501's invoice",
	)
	expect(
		frappe.db.get_value("Sales Invoice Item", {"parent": order, "idx": 2}, "item_code"),
		navy,
		"the jacket line is the navy child product bought",
	)
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "order", "Order", "502"), "docstatus"),
		0,
		"new guest order 502",
	)
	assert_magento_discounts(connection, state)
	expect(
		frappe.db.get_value("Web Page", target(connection, "page", "CmsPage", "3"), "published"),
		0,
		"an inactive CMS page",
	)
	if state == "v1":
		expect(item_price(connection, kettle), amount("89.99"), "the kettle's price")
		expect(published(target(connection, "product", "Product", "13")), 0, "a disabled product")
		return
	expect(item_price(connection, kettle), amount("94.99"), "the kettle's new price")
	credit = part(connection, "refund", "CreditMemo/901")
	expect(
		amount(frappe.db.get_value("Sales Invoice", credit, "grand_total")),
		amount("-10"),
		"order 501's credit memo",
	)
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "order", "Order", "503"), "customer"),
		target(connection, "customer", "Customer", "43"),
		"new order 503, linked to the new customer",
	)


def assert_lightspeed_discounts(connection: str, state: str) -> None:
	"""Every active Lightspeed discount code and rule: what ERPNext enforces enabled, the rest held."""
	expect(
		frappe.db.count("Importer Link", {"connection": connection, "kind": "discount", "retired": 0}),
		6,
		"four active codes and two active rules (not the inactive code or rule)",
	)
	assert_discount(connection, "Discount", "1001", None, [], "the amount-off code, enabled")
	assert_discount(connection, "Discount", "1002", None, [], "the towels code, enabled")
	# The Webshop fork enforces free shipping and a percentage off shipping (card 405).
	assert_discount(connection, "Discount", "1003", None, ["free_shipping"], "the shipping code, enabled")
	assert_fork_limits(
		connection, "Discount", "1003", {"shipping_discount": "Free Shipping"}, {}, "the shipping code"
	)
	assert_discount(
		connection, "Discount", "1005", None, ["shipping_discount"], "the shipping-discount code, enabled"
	)
	assert_fork_limits(
		connection,
		"Discount",
		"1005",
		{"shipping_discount": "Percentage", "shipping_discount_value": 10},
		{},
		"the shipping-discount code",
	)
	assert_discount(connection, "DiscountRule", "2001", None, [], "the kitchen rule, automatic and enabled")
	kitchen = target(connection, "discount", "DiscountRule", "2001")
	expect(
		(
			frappe.db.get_value("Pricing Rule", kitchen, "apply_on"),
			amount(frappe.db.get_value("Pricing Rule", kitchen, "discount_amount")),
		),
		("Item Group", amount("5" if state == "v1" else "6")),
		"the kitchen rule's amount off its category",
	)
	assert_discount(
		connection, "DiscountRule", "2002", "card 407", ["rule_condition"], "the order-minimum rule, held"
	)


def assert_ecwid_discounts(connection: str, state: str) -> None:
	"""Every active Ecwid coupon and wholesale price tier: what ERPNext enforces enabled, the rest held."""
	expect(
		frappe.db.count("Importer Link", {"connection": connection, "kind": "discount", "retired": 0}),
		6,
		"four active coupons and two products' wholesale prices (not the paused coupon)",
	)
	assert_discount(connection, "DiscountCoupon", "9501", None, [], "the amount-off coupon, enabled")
	# The Webshop fork enforces a code's once-per-customer limit and free shipping (card 405).
	assert_discount(
		connection,
		"DiscountCoupon",
		"9502",
		None,
		["once_per_customer"],
		"the once-per-customer coupon, enabled",
	)
	assert_fork_limits(
		connection,
		"DiscountCoupon",
		"9502",
		{"maximum_use_per_customer": 1},
		{},
		"the once-per-customer coupon",
	)
	assert_discount(
		connection, "DiscountCoupon", "9503", None, ["free_shipping"], "the shipping coupon, enabled"
	)
	assert_fork_limits(
		connection,
		"DiscountCoupon",
		"9503",
		{"shipping_discount": "Free Shipping"},
		{},
		"the shipping coupon",
	)
	# Held until the Webshop fork enforced customer histories (card 407): now enabled, new customers only on its
	# rule, and the fork refuses it to a customer with an order.
	assert_discount(
		connection,
		"DiscountCoupon",
		"9505",
		None,
		["customer_history"],
		"the new-customer coupon, enabled",
	)
	assert_fork_limits(
		connection,
		"DiscountCoupon",
		"9505",
		{"disable": 0, "new_customers_only": 1, "minimum_customer_orders": 0},
		{},
		"the new-customer coupon",
	)
	if state == "v1":
		assert_new_customers_only(
			connection,
			target(connection, "coupon", "DiscountCoupon", "9505#ECW-NEWONLY"),
			target(connection, "customer", "Customer", "9401"),
			target(connection, "customer", "Customer", "9402"),
			target(connection, "product", "Product", "9201"),
			"the new-customer coupon",
		)
	assert_discount(connection, "WholesalePrice", "9201", None, [], "the candle's wholesale prices, enabled")
	scheme = target(connection, "discount", "WholesalePrice", "9201")
	live = frappe.get_all(
		"Pricing Rule", filters={"promotional_scheme": scheme, "disable": 0}, fields=["min_qty", "rate"]
	)
	expect(
		sorted((amount(r.min_qty), amount(r.rate)) for r in live),
		[(amount("5"), amount("25")), (amount("10"), amount("22"))][: 2 if state == "v1" else 1],
		"the candle's wholesale tiers",
	)
	assert_discount(
		connection,
		"WholesalePrice",
		"9202",
		"card 407",
		["rule_condition"],
		"wholesale prices with variations' own, held",
	)


def assert_wix_discounts(connection: str, state: str) -> None:
	"""Every active Wix coupon and discount rule: what ERPNext enforces enabled, the rest held."""
	expect(
		frappe.db.count("Importer Link", {"connection": connection, "kind": "discount", "retired": 0}),
		6,
		"four active coupons and two active rules (not the deactivated coupon or the expired rule)",
	)
	coupon = "c0000000-0000-4000-8000-00000000000{}"
	assert_discount(connection, "Coupon", coupon.format(4), None, [], "the one-product coupon, enabled")
	# The Webshop fork enforces a per-customer limit and free shipping (card 405).
	assert_discount(
		connection,
		"Coupon",
		coupon.format(1),
		None,
		["per_customer_limit"],
		"the per-customer coupon, enabled",
	)
	assert_fork_limits(
		connection, "Coupon", coupon.format(1), {"maximum_use_per_customer": 1}, {}, "the per-customer coupon"
	)
	assert_discount(
		connection, "Coupon", coupon.format(2), None, ["free_shipping"], "the shipping coupon, enabled"
	)
	assert_fork_limits(
		connection,
		"Coupon",
		coupon.format(2),
		{"shipping_discount": "Free Shipping"},
		{},
		"the shipping coupon",
	)
	assert_discount(
		connection, "Coupon", coupon.format(3), "card 407", ["spread_amount"], "the amount off mugs, held"
	)
	rule = "d0000000-0000-4000-8000-00000000000{}"
	assert_discount(
		connection, "DiscountRule", rule.format(1), None, [], "the two-mugs rule, automatic and enabled"
	)
	mugs = target(connection, "discount", "DiscountRule", rule.format(1))
	expect(
		(
			frappe.db.get_value("Pricing Rule", mugs, "min_qty"),
			amount(frappe.db.get_value("Pricing Rule", mugs, "discount_percentage")),
		),
		(2, amount("10" if state == "v1" else "15")),
		"the two-mugs rule's quantity and percentage",
	)
	assert_discount(
		connection,
		"DiscountRule",
		rule.format(2),
		"card 407",
		["fixed_price_set"],
		"the three-for-$20 rule, held",
	)


def assert_squarespace_discounts(connection: str, state: str) -> None:
	"""Every Squarespace discount: what ERPNext enforces enabled, the rest held."""
	expect(
		frappe.db.count("Importer Link", {"connection": connection, "kind": "discount", "retired": 0}),
		7,
		"every discount",
	)
	discount = "6e500000000000000000004{}"
	for number, what in (
		(2, "the amount-off code"),
		(4, "the scarf code"),
		(5, "the automatic 5%"),
		(6, "buy two scarves, get one"),
	):
		assert_discount(connection, "Discount", discount.format(number), None, [], f"{what}, enabled")
	assert_discount(
		connection,
		"Discount",
		discount.format(1),
		None,
		["once_per_customer"],
		"the once-per-customer code, enabled: the fork enforces it",
	)
	assert_fork_limits(
		connection,
		"Discount",
		discount.format(1),
		{"maximum_use_per_customer": 1},
		{},
		"the once-per-customer code",
	)
	assert_discount(
		connection,
		"Discount",
		discount.format(3),
		"card 405",
		["free_shipping"],
		"the shipping code, held: free on one of Squarespace's fulfilment options",
	)
	assert_discount(
		connection,
		"Discount",
		discount.format(7),
		"card 407",
		["item_limit", "item_choice"],
		"the half-price board, held",
	)
	third = frappe.get_doc("Pricing Rule", target(connection, "discount", "Discount", discount.format(6)))
	expect(
		(
			third.coupon_code_based,
			third.price_or_product_discount,
			third.same_item,
			third.is_recursive,
			third.recurse_for,
		),
		(0, "Product", 1, 1, 2),
		"the third scarf free, for every two",
	)
	mug = target(connection, "discount", "Discount", discount.format(7))
	expect(
		amount(frappe.db.get_value("Pricing Rule", mug, "discount_percentage")),
		amount("50" if state == "v1" else "40"),
		"the half-price board's percentage",
	)


def assert_square_discounts(connection: str, state: str) -> None:
	"""Square's pricing rules and staff discounts: what ERPNext enforces enabled, the rest held."""
	expect(
		frappe.db.count("Importer Link", {"connection": connection, "kind": "discount", "retired": 0}),
		3,
		"two pricing rules and the discount no rule applies",
	)
	assert_discount(
		connection, "PricingRule", "PR_MUGS", None, [], "two mugs, ten percent off, automatic and enabled"
	)
	mugs = target(connection, "discount", "PricingRule", "PR_MUGS")
	expect(
		(
			frappe.db.get_value("Pricing Rule", mugs, "coupon_code_based"),
			frappe.db.get_value("Pricing Rule", mugs, "min_qty"),
			amount(frappe.db.get_value("Pricing Rule", mugs, "discount_percentage")),
		),
		(0, 2, amount("10" if state == "v1" else "15")),
		"the mugs rule's quantity and percentage",
	)
	assert_discount(
		connection, "PricingRule", "PR_APRON", "card 405", ["schedule"], "the weekend apron rule, held"
	)
	assert_discount(
		connection,
		"Discount",
		"DISC_STAFF",
		"an operator decision",
		["applied_by_staff"],
		"the staff discount, held",
	)


def assert_magento_discounts(connection: str, state: str) -> None:
	"""Every active Magento cart price rule and tier price: what ERPNext enforces enabled, the rest held."""
	expect(
		frappe.db.count("Importer Link", {"connection": connection, "kind": "discount", "retired": 0}),
		8,
		"five active rules and three customer groups' tier prices (not the inactive rule)",
	)
	assert_discount(connection, "SalesRule", "62", None, [], "the percentage rule, enabled")
	assert_discount(
		connection,
		"SalesRule",
		"61",
		None,
		["per_customer_limit"],
		"the per-customer rule, enabled: the fork enforces it",
	)
	assert_fork_limits(
		connection,
		"SalesRule",
		"61",
		{"maximum_use_per_customer": 1},
		{"MAG-WELCOME5": {"maximum_use_per_customer": 1}},
		"the per-customer rule",
	)
	assert_discount(
		connection, "SalesRule", "63", "card 405", ["free_shipping"], "the free-shipping rule, held"
	)
	assert_discount(
		connection, "SalesRule", "64", None, [], "the rule without a coupon, automatic and enabled"
	)
	assert_discount(connection, "SalesRule", "65", None, [], "the jackets' amount off each item, enabled")
	expect(
		frappe.db.get_value("Pricing Rule", target(connection, "discount", "SalesRule", "65"), "apply_on"),
		"Item Group",
		"the jackets' rule applies to their category",
	)
	wholesale = target(connection, "customer_group", "CustomerGroup", "2")
	scheme = target(connection, "discount", "TierPrice", "11-2")
	expect(
		frappe.db.get_value("Promotional Scheme", scheme, ["applicable_for", "disable"]),
		("Customer Group", 0),
		"the wholesale tier price on the kettle",
	)
	expect(
		frappe.get_all("Pricing Rule", filters={"promotional_scheme": scheme}, pluck="customer_group"),
		[wholesale],
		"one rule, for the wholesale group",
	)
	every = target(connection, "discount", "TierPrice", "11-32000")
	live = frappe.get_all(
		"Pricing Rule", filters={"promotional_scheme": every, "disable": 0}, pluck="min_qty"
	)
	expect(
		sorted(amount(q) for q in live),
		[amount("3"), amount("10")][: 2 if state == "v1" else 1],
		"kettle tiers",
	)


def assert_bigcommerce_discounts(connection: str, state: str) -> None:
	"""Every BigCommerce discount: what ERPNext enforces written enabled, the rest recorded and held."""
	# Held until the Webshop fork enforced per-customer limits and shipping discounts (card 405): now enabled,
	# with the source's limits on the fork's fields, and the fork refuses a customer's second use.
	assert_discount(connection, "Coupon", "61", None, ["per_customer_limit"], "coupon 61, enabled")
	assert_fork_limits(
		connection,
		"Coupon",
		"61",
		{"disable": 0, "maximum_use_per_customer": 1},
		{"BC-WELCOME": {"maximum_use_per_customer": 0}},
		"coupon 61 (once per customer, over its code)",
	)
	if state == "v1":
		assert_one_use_per_customer(
			connection,
			target(connection, "coupon", "Coupon", "61#BC-WELCOME"),
			target(connection, "customer", "Customer", "41"),
			target(connection, "product", "Product", "111"),
			"coupon 61",
		)
	assert_discount(connection, "Coupon", "62", None, ["free_shipping"], "free-shipping coupon 62, enabled")
	assert_fork_limits(
		connection, "Coupon", "62", {"shipping_discount": "Free Shipping"}, {}, "free-shipping coupon 62"
	)
	assert_discount(connection, "Coupon", "64", None, ["shipping_discount"], "shipping coupon 64, enabled")
	assert_fork_limits(
		connection,
		"Coupon",
		"64",
		{"shipping_discount": "Amount", "shipping_discount_value": 50},
		{},
		"shipping coupon 64 ($50 off shipping)",
	)
	assert_discount(connection, "Promotion", "71", None, [], "promotion 71, enabled, and its code")
	assert_fork_limits(
		connection,
		"Promotion",
		"71",
		{"maximum_use_per_customer": 0},
		{"BC-FALL15": {"maximum_use_per_customer": 1}},
		"promotion 71 (its code once per customer)",
	)
	assert_discount(connection, "Promotion", "72", None, [], "automatic promotion 72, enabled")
	assert_discount(connection, "Promotion", "73", "card 407", ["cap", "item_limit"], "promotion 73, held")
	assert_discount(
		connection, "Promotion", "74", "card 405", ["shared_use_limit"], "gift promotion 74, held"
	)
	wholesale = frappe.db.get_value(
		"Customer", target(connection, "customer", "Customer", "41"), "customer_group"
	)
	expect(wholesale, target(connection, "customer_group", "CustomerGroup", "3"), "customer 41 in its group")
	everything = target(connection, "discount", "CustomerGroupRule", "3-all")
	expect(
		frappe.db.get_value("Pricing Rule", everything, ["applicable_for", "customer_group", "disable"]),
		("Customer Group", wholesale, 0),
		"the wholesale group's discount on everything",
	)
	assert_discount(connection, "CustomerGroupRule", "3-all", None, [], "the group's discount, enabled")
	price = target(connection, "discount", "CustomerGroupRule", "3-product-112")
	expect(
		amount(frappe.db.get_value("Pricing Rule", price, "rate")),
		amount("18" if state == "v1" else "17"),
		"the wholesale group's fixed price on the toque",
	)
	scheme = target(connection, "discount", "BulkPricing", "111")
	live = frappe.get_all(
		"Pricing Rule",
		filters={"promotional_scheme": scheme, "disable": 0},
		fields=["min_qty", "discount_percentage"],
	)
	expect(
		sorted((amount(r.min_qty), amount(r.discount_percentage)) for r in live),
		[(amount("5"), amount("5")), (amount("10"), amount("10"))][: 2 if state == "v1" else 1],
		"the mug's bulk pricing tiers",
	)


def assert_invoice(
	connection: str, order_id: str, wanted: tuple[int, str, str, str | None], what: str
) -> str:
	"""An order's invoice: its docstatus, grand total, discount and customer; returns its name."""
	order = target(connection, "order", "Order", order_id)
	invoice = frappe.db.get_value(
		"Sales Invoice", order, ["docstatus", "grand_total", "discount_amount", "customer"], as_dict=True
	)
	docstatus, total, discount, customer = wanted
	expect(
		(invoice.docstatus, amount(invoice.grand_total), amount(invoice.discount_amount), invoice.customer),
		(docstatus, amount(total), amount(discount), customer),
		what,
	)
	return str(order)


def assert_shipment(
	connection: str, order_id: str, fulfilment: str, wanted: dict[str, Any], order_type: str = "Order"
) -> None:
	"""An order's fulfilment as a draft Shipment from the company's address to the order's customer, with the
	source's carrier and tracking (``wanted``: Shipment fields)."""
	order = target(connection, "order", order_type, order_id)
	shipment = frappe.get_doc("Shipment", part(connection, "shipment", fulfilment))
	expect(
		(shipment.docstatus, shipment.pickup_company, shipment.delivery_customer),
		(0, business_of(connection).company, frappe.db.get_value("Sales Invoice", order, "customer")),
		f"order {order_id}'s shipment",
	)
	expect(
		{field: shipment.get(field) for field in wanted}, wanted, f"order {order_id}'s shipment's tracking"
	)


def assert_tax_inclusive(
	connection: str, order_id: str, total: str, taxes: dict[str, str], templates: list[str | None]
) -> None:
	"""A tax-inclusive order whose lines are taxed at different rates: a submitted invoice at the source's
	total, each included tax a row of its own (on its own account) at the source's amount, and each line
	taxed through the Item Tax Template of its own rates (by title, e.g. "GST at 5, PST at 7")."""
	order = target(connection, "order", "Order", order_id)
	invoice = frappe.get_doc("Sales Invoice", order)
	expect(
		(invoice.docstatus, amount(invoice.grand_total)), (1, amount(total)), f"order {order_id}'s invoice"
	)
	expect(
		{row.description: amount(row.tax_amount) for row in invoice.taxes if row.included_in_print_rate},
		{title: amount(value) for title, value in taxes.items()},
		f"order {order_id}'s included taxes",
	)
	expect(
		len({row.account_head for row in invoice.taxes if row.included_in_print_rate}),
		len(taxes),
		f"order {order_id}'s included taxes, each on its own account",
	)
	abbr = frappe.db.get_value("Importer Connection", connection, "abbreviation")
	expect(
		[
			frappe.db.get_value("Item Tax Template", line.item_tax_template, "title")
			if line.item_tax_template
			else None
			for line in invoice.items
		],
		[f"{template} - {abbr}" if template else None for template in templates],
		f"order {order_id}'s lines' own rates",
	)


@check
def assert_lightspeed(state: str) -> None:
	connection = "lightspeed-l"
	mug = target(connection, "product", "Product", "501")
	expect(
		(mug, frappe.db.get_value("Item", mug, "item_group"), frappe.db.get_value("Item", mug, "brand")),
		("LS-LS-MUG-1", "Tableware - LS", "Harbour Lane"),
		"the mug: its SKU, category and brand",
	)
	expect(stock_of(connection, mug), amount("14"), "the mug's stock, not reduced by orders")
	slate = part(connection, "variant", "Variant/5022")
	expect(
		(item_price(connection, slate), stock_of(connection, slate)),
		(amount("21"), amount("0")),
		"the slate towel",
	)
	customer = target(connection, "customer", "Customer", "801")
	order = assert_invoice(connection, "701", (1, "78.75", "5.00", customer), "order ORD00001's invoice")
	expect(
		frappe.db.count(
			"Comment",
			{"reference_doctype": "Sales Invoice", "reference_name": order, "comment_type": "Comment"},
		),
		1,
		"order ORD00001's staff memo (its shipment is a Shipment)",
	)
	assert_shipment(connection, "701", "Shipment/901", {"awb_number": "CP987654321CA"})
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "order", "Order", "702"), "docstatus"),
		0,
		"unpaid guest order",
	)
	assert_lightspeed_discounts(connection, state)
	expect(
		frappe.db.get_value("Web Page", target(connection, "page", "Textpage", "1102"), "published"),
		0,
		"a hidden page",
	)
	expect(
		frappe.db.get_value("Blog Post", target(connection, "article", "Article", "1301"), "blog_category"),
		target(connection, "blog", "Blog", "1201"),
		"the article in its blog",
	)
	if state == "v1":
		expect(item_price(connection, mug), amount("26.25"), "the mug's price, with tax (a retail shop)")
		expect(published(target(connection, "product", "Product", "503")), 0, "a hidden product")
		return
	expect(item_price(connection, mug), amount("27.3"), "the mug's new price")
	credit = part(connection, "refund", "CreditNote/9102")
	expect(
		amount(frappe.db.get_value("Sales Invoice", credit, "grand_total")),
		amount("-26.25"),
		"the credit note",
	)
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "order", "Order", "703"), "customer"),
		target(connection, "customer", "Customer", "803"),
		"new order ORD00003, linked to the new customer",
	)


@check
def assert_odoo(state: str) -> None:
	"""Odoo's books and business: the chart, products and stock, partners, people, the pipeline, orders,
	invoices with their credit note, payments that settle them, and journal entries."""
	connection = "odoo-o"
	sales = target(connection, "account", "Account", "106")
	expect(
		frappe.db.get_value("Account", sales, ["account_number", "root_type", "account_type"]),
		("400000", "Income", "Income Account"),
		"the product sales account",
	)
	expect(
		frappe.db.get_value("Account", target(connection, "account", "Account", "104"), "account_type"),
		"Bank",
		"outstanding receipts, which payments go into",
	)
	expect(
		frappe.db.get_value("Importer Link", {"connection": connection, "gid": ["like", "%/Account/110"]}),
		None,
		"an off-balance account",
	)
	chair = target(connection, "product", "Product", "10")
	expect((chair, stock_of(connection, chair)), ("OD-CHAIR", amount("12")), "the chair: its code and stock")
	expect(
		frappe.db.get_value("Item", part(connection, "variant", "Variant/22"), "item_code"),
		"OD-DESK-WAL",
		"the walnut desk",
	)
	deco = target(connection, "customer", "Partner", "7")
	expect(frappe.db.get_value("Customer", deco, "tax_id"), "CA123456789", "Deco Addict's tax id")
	invoice = target(connection, "sales_invoice", "Move", "31")
	expect(
		[
			amount(value)
			for value in frappe.db.get_value(
				"Sales Invoice", invoice, ["docstatus", "grand_total", "outstanding_amount"]
			)
		],
		[amount("1"), amount("315"), amount("0")],
		"INV/2026/00001: posted, and settled by its credit note and payment",
	)
	credit = target(connection, "sales_invoice", "Move", "32")
	expect(
		frappe.db.get_value("Sales Invoice", credit, ["is_return", "return_against"]),
		(1, invoice),
		"the credit note, against the invoice",
	)
	bill = target(connection, "purchase_invoice", "Move", "33")
	expect(
		amount(frappe.db.get_value("Purchase Invoice", bill, "outstanding_amount")),
		amount("0"),
		"the paid bill",
	)
	expect(
		frappe.db.get_value(
			"Payment Entry", target(connection, "payment_entry", "Payment", "41"), "docstatus"
		),
		1,
		"the customer's payment",
	)
	expect(
		frappe.db.get_value("Sales Order", target(connection, "sales_order", "SaleOrder", "1"), "docstatus"),
		1,
		"the confirmed sale order",
	)
	employee = target(connection, "employee", "Employee", "2")
	expect(
		frappe.db.get_value("Employee", employee, "reports_to"),
		target(connection, "employee", "Employee", "1"),
		"Marc's manager",
	)
	opportunity = target(connection, "opportunity", "CrmLead", "3")
	expect(
		frappe.db.get_value("Opportunity", opportunity, ["opportunity_from", "party_name"]),
		("Lead", target(connection, "lead", "CrmLead", "3")),
		"an opportunity with no customer, with its lead",
	)
	rent = target(connection, "journal_entry", "Move", "34")
	if state == "v1":
		expect(item_price(connection, chair), amount("150"), "the chair's price")
		expect(
			amount(frappe.db.get_value("Journal Entry", rent, "total_debit")), amount("1000"), "March's rent"
		)
		expect(
			frappe.db.get_value(
				"Quotation", target(connection, "draft_order", "SaleOrder", "2"), "docstatus"
			),
			0,
			"the quotation",
		)
		expect(
			frappe.db.get_value(
				"Purchase Order", target(connection, "purchase_order", "PurchaseOrder", "1"), "docstatus"
			),
			1,
			"the confirmed purchase order",
		)
		return
	expect(item_price(connection, chair), amount("155"), "the chair's new price")
	expect(
		(
			amount(frappe.db.get_value("Journal Entry", rent, "total_debit")),
			frappe.db.get_value("Journal Entry", rent, "amended_from") is not None,
		),
		(amount("1100"), True),
		"the corrected rent, amended",
	)
	expect(
		frappe.db.get_value("Sales Order", target(connection, "sales_order", "SaleOrder", "2"), "docstatus"),
		1,
		"the confirmed quotation's order",
	)
	expect(
		frappe.db.get_value(
			"Purchase Order", target(connection, "purchase_order", "PurchaseOrder", "1"), "docstatus"
		),
		2,
		"the cancelled purchase order",
	)
	expect(
		frappe.db.get_value("Employee", employee, ["status", "relieving_date"]),
		("Left", frappe.utils.getdate("2026-03-31")),
		"Marc left",
	)
	expect(
		frappe.db.get_value("Customer", target(connection, "customer", "Partner", "16"), "disabled"),
		1,
		"an archived customer",
	)
	expect(frappe.db.get_value("Opportunity", opportunity, "status"), "Lost", "the lost opportunity")
	lead = frappe.db.get_value(
		"Importer Link",
		{"connection": connection, "kind": "lead", "gid": ["like", "%/CrmLead/1"]},
		"target_name",
	)
	expect(frappe.db.get_value("Lead", lead, "disabled"), 1, "a lead deleted at the source")
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "sales_invoice", "Move", "36"), "docstatus"),
		0,
		"a draft invoice",
	)


def bc(n: int) -> str:
	"""A Business Central fixture id (tools/bc_fixtures.py)."""
	return f"{n:08x}-0000-4000-8000-{n:012x}"


@check
def assert_business_central(state: str) -> None:
	"""Business Central's chart, items, parties, people, pipeline, orders, invoices and credit memos."""
	connection = "business-central-b"
	expect(
		frappe.db.get_value("Account", target(connection, "account", "Account", bc(102)), "account_type"),
		"Bank",
		"the checking account",
	)
	expect(
		frappe.db.get_value(
			"Importer Link", {"connection": connection, "gid": ["like", f"%/Account/{bc(101)}"]}
		),
		None,
		"a heading of the chart",
	)
	desk = target(connection, "product", "Item", bc(201))
	expect(
		(desk, stock_of(connection, desk)), ("DB-1896-S", amount("4")), "the desk: its number and inventory"
	)
	expect(
		frappe.db.get_value("Item", part(connection, "variant", f"ItemVariant/{bc(204)}"), "item_code"),
		"DB-1900-S-RED",
		"the red chair",
	)
	invoice = target(connection, "sales_invoice", "SalesInvoice", bc(421))
	expect(
		[amount(v) for v in frappe.db.get_value("Sales Invoice", invoice, ["docstatus", "grand_total"])],
		[amount("1"), amount("1155")],
		"S-INV1001",
	)
	expect(
		frappe.db.get_value(
			"Sales Invoice",
			target(connection, "sales_invoice", "SalesCreditMemo", bc(441)),
			["is_return", "return_against"],
		),
		(1, invoice),
		"the credit memo, against its invoice",
	)
	expect(
		amount(
			frappe.db.get_value(
				"Purchase Invoice",
				target(connection, "purchase_invoice", "PurchaseInvoice", bc(461)),
				"grand_total",
			)
		),
		amount("3150"),
		"P-INV1001",
	)
	draft = target(connection, "sales_invoice", "SalesInvoice", bc(431))
	order = target(connection, "sales_order", "SalesOrder", bc(411))
	employee = target(connection, "employee", "Employee", bc(321))
	opportunity = target(connection, "opportunity", "Opportunity", bc(351))
	if state == "v1":
		expect(item_price(connection, desk), amount("1000"), "the desk's price")
		expect(frappe.db.get_value("Sales Invoice", draft, "docstatus"), 0, "a draft invoice")
		expect(frappe.db.get_value("Sales Order", order, "docstatus"), 1, "an open order")
		expect(
			frappe.db.get_value("Opportunity", opportunity, "status"), "Open", "an opportunity in progress"
		)
		return
	expect(item_price(connection, desk), amount("1050"), "the desk's new price")
	expect(
		frappe.db.get_value(
			"Sales Invoice", target(connection, "sales_invoice", "SalesInvoice", bc(431)), "docstatus"
		),
		1,
		"the draft invoice, posted",
	)
	expect(
		frappe.db.get_value("Sales Order", order, "docstatus"),
		2,
		"an order Business Central deleted once invoiced",
	)
	expect(frappe.db.get_value("Employee", employee, "status"), "Left", "a terminated employee")
	expect(frappe.db.get_value("Opportunity", opportunity, "status"), "Converted", "a won opportunity")
	expect(
		frappe.db.get_value("Customer", target(connection, "customer", "Customer", bc(302)), "disabled"),
		1,
		"a blocked customer",
	)


def dataverse(n: int) -> str:
	"""A Dynamics 365 Sales fixture id (tools/dataverse_fixtures.py)."""
	return f"d{n:07x}-0000-4000-8000-{n:012x}"


@check
def assert_dynamics_sales(state: str) -> None:
	"""Dynamics 365 Sales' products, customers, pipeline, quotes, orders and invoices (one in US dollars)."""
	connection = "dynamics-sales-d"
	desk = target(connection, "product", "Product", dataverse(11))
	expect((desk, stock_of(connection, desk)), ("DS-SD-100", amount("6")), "the standing desk")
	contoso = target(connection, "customer", "Account", dataverse(21))
	expect(frappe.db.get_value("Customer", contoso, "customer_type"), "Company", "Contoso")
	usd = target(connection, "sales_invoice", "Invoice", dataverse(81))
	expect(
		[
			amount(v)
			for v in frappe.db.get_value(
				"Sales Invoice", usd, ["grand_total", "conversion_rate", "base_grand_total"]
			)
		],
		[amount("630"), amount("1.37"), amount("863.1")],
		"INV-01001 in US dollars, at the rate its totals imply",
	)
	expect(
		frappe.db.get_value("Sales Invoice", usd, "customer"),
		target(connection, "customer", "Contact", dataverse(32)),
		"an invoice to a contact",
	)
	order = target(connection, "sales_order", "SalesOrder", dataverse(71))
	expect(
		amount(frappe.db.get_value("Sales Order", order, "discount_amount")),
		amount("90"),
		"the order's discount",
	)
	expect(
		frappe.db.get_value(
			"Quotation", target(connection, "draft_order", "Quote", dataverse(61)), "docstatus"
		),
		0,
		"the draft quote",
	)
	opportunity = target(connection, "opportunity", "Opportunity", dataverse(52))
	expect(
		frappe.db.get_value("Opportunity", opportunity, ["opportunity_from", "party_name"]),
		("Lead", target(connection, "lead", "Lead", dataverse(41))),
		"an opportunity with the lead it came from",
	)
	if state == "v1":
		expect(item_price(connection, desk), amount("800"), "the desk's price")
		expect(frappe.db.get_value("Sales Order", order, "docstatus"), 1, "a submitted order")
		return
	expect(item_price(connection, desk), amount("850"), "the desk's new price")
	expect(frappe.db.get_value("Sales Order", order, "docstatus"), 2, "a cancelled order")
	expect(
		frappe.db.get_value("Customer", target(connection, "customer", "Account", dataverse(22)), "disabled"),
		1,
		"a deactivated account",
	)
	expect(
		frappe.db.get_value(
			"Opportunity", target(connection, "opportunity", "Opportunity", dataverse(51)), "status"
		),
		"Converted",
		"a won opportunity",
	)


@check
def assert_finance_operations(state: str) -> None:
	"""Finance & Operations' documented entities: products, customers and draft sales orders; vendors and
	purchase orders wait for the field map to name their entities."""
	connection = "finance-operations-f"
	speaker = target(connection, "product", "Item", "D0001")
	expect(speaker, "DF-D0001", "the speaker's item code")
	expect(
		frappe.db.get_value("Importer Link", {"connection": connection, "gid": ["like", "%/Item/D0004"]}),
		None,
		"a product master (planned)",
	)
	expect(
		frappe.db.get_value(
			"Customer", target(connection, "customer", "Customer", "US-027"), "customer_type"
		),
		"Individual",
		"a person customer",
	)
	order = target(connection, "sales_order", "SalesOrder", "000701")
	expect(
		[amount(v) for v in frappe.db.get_value("Sales Order", order, ["docstatus", "grand_total"])],
		[amount("0"), amount("360")],
		"a draft sales order totalling its lines",
	)
	expect(
		frappe.db.count(
			"Importer Link", {"connection": connection, "kind": ["in", ["supplier", "purchase_order"]]}
		),
		0,
		"vendors and purchase orders, not yet mapped",
	)
	if state == "v1":
		expect(item_price(connection, speaker), amount("180"), "the speaker's price")
		return
	expect(item_price(connection, speaker), amount("189"), "the speaker's new price")
	removed = frappe.db.get_value(
		"Importer Link",
		{"connection": connection, "gid": ["like", "%/SalesOrder/000702"]},
		["target_name", "retired"],
		as_dict=True,
	)
	expect(removed.retired, 1, "a sales order removed at the source")
	title = str(frappe.db.get_value("Sales Order", removed.target_name, "title"))
	if not title.startswith("[Removed at source]"):
		raise AssertionError(f"the removed draft order isn't marked: {title}")


@check
def assert_ecwid(state: str) -> None:
	connection = "ecwid-c"
	assert_tax_inclusive(
		connection, "CW-1004", "31.50", {"GST": "1.44", "PST": "1.31"}, ["GST at 5, PST at 7", "GST at 5"]
	)
	candle = target(connection, "product", "Product", "9201")
	expect(
		(
			candle,
			frappe.db.get_value("Item", candle, "item_group"),
			frappe.db.get_value("Item", candle, "brand"),
		),
		("EC-ECW-CANDLE-1", "Candles - EC", "Cedarwick"),
		"the candle: its SKU, category and brand",
	)
	expect(stock_of(connection, candle), amount("12"), "the candle's stock, not reduced by orders")
	fir = part(connection, "variant", "Variant/920201")
	expect(
		(item_price(connection, fir), stock_of(connection, fir)), (amount("16"), amount("20")), "the fir melt"
	)
	customer = target(connection, "customer", "Customer", "9401")
	assert_invoice(connection, "CW-1001", (1, "80.85", "5.00", customer), "order CW-1001's invoice")
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "order", "Order", "CW-1002"), "docstatus"),
		0,
		"unpaid order",
	)
	assert_ecwid_discounts(connection, state)
	expect(
		frappe.db.count("Importer Link", {"connection": connection, "kind": "review", "retired": 0}),
		1 if state == "v1" else 2,
		"published reviews",
	)
	if state == "v1":
		expect(item_price(connection, candle), amount("28"), "the candle's price")
		expect(published(target(connection, "product", "Product", "9203")), 0, "a disabled product")
		return
	expect(item_price(connection, candle), amount("29.5"), "the candle's new price")
	credit = part(connection, "refund", "Refund/CW-1001-1")
	expect(
		amount(frappe.db.get_value("Sales Invoice", credit, "grand_total")),
		amount("-29.4"),
		"order CW-1001's refund",
	)
	expect(
		frappe.db.get_value("Sales Invoice", target(connection, "order", "Order", "CW-1003"), "customer"),
		target(connection, "customer", "Customer", "9403"),
		"new order CW-1003, linked to the new customer",
	)


@dataclass(frozen=True)
class OAuthConnect:
	"""What Connect must do for one OAuth connection: the approval URL's query (``redirect`` names the
	callback method), what the source's redirect back carries beside the code, the refresh token the fake
	source issues for it, and the connection fields the approval settles."""

	query: dict[str, str]
	redirect: str
	params: dict[str, str]
	refresh_token: str
	settled: dict[str, str]


OAUTH_CONNECTS: dict[str, OAuthConnect] = {
	"etsy-e": OAuthConnect(
		{"client_id": "e2e-keystring", "code_challenge_method": "S256"},
		"etsy_callback",
		{},
		ETSY_ROTATED_REFRESH_TOKEN,
		{},
	),
	"quickbooks-q": OAuthConnect(
		{"client_id": "e2e-oauth-client", "scope": "com.intuit.quickbooks.accounting"},
		"oauth_callback",
		{"realmId": "9341452345"},
		OAUTH_ROTATED_REFRESH_TOKEN,
		{"api_account": "9341452345"},
	),
	"xero-x": OAuthConnect(
		{"client_id": "e2e-oauth-client"},
		"oauth_callback",
		{},
		OAUTH_ROTATED_REFRESH_TOKEN,
		{"api_account": "70784a63-d24b-46a9-a4db-0e70a274b056"},
	),
	"acumatica-a": OAuthConnect(
		{"client_id": "e2e-oauth-client", "scope": "api offline_access"},
		"oauth_callback",
		{},
		OAUTH_ROTATED_REFRESH_TOKEN,
		{},
	),
	"agendrix-a": OAuthConnect(
		{"client_id": "e2e-oauth-client", "scope": "read"},
		"oauth_callback",
		{},
		OAUTH_ROTATED_REFRESH_TOKEN,
		{},
	),
	"highlevel-h": OAuthConnect(
		{"client_id": "e2e-oauth-client", "response_type": "code"},
		"oauth_callback",
		{},
		OAUTH_ROTATED_REFRESH_TOKEN,
		{"api_account": HIGHLEVEL_LOCATION},
	),
	"zoho-books-b": OAuthConnect(
		{"client_id": "e2e-oauth-client", "access_type": "offline"},
		"oauth_callback",
		{"location": "us", "accounts-server": "https://accounts.zoho.com"},
		OAUTH_ROTATED_REFRESH_TOKEN,
		{"api_account": "10234695"},
	),
	"hubspot-s": OAuthConnect(
		{"client_id": "e2e-oauth-client", "response_type": "code"},
		"oauth_callback",
		{},
		OAUTH_ROTATED_REFRESH_TOKEN,
		{},
	),
	"salesforce-f": OAuthConnect(
		{"client_id": "e2e-oauth-client", "scope": "api refresh_token"},
		"oauth_callback",
		{},
		OAUTH_ROTATED_REFRESH_TOKEN,
		{},
	),
}


@check
def assert_oauth_connect(connection: str) -> None:
	"""Connect through OAuth 2: the approval URL asks for the source's read scopes, and the source's redirect
	back with a code is exchanged for the refresh token the connection keeps (and settles what it names)."""
	want = OAUTH_CONNECTS[connection]
	frappe.db.set_value("Importer Connection", connection, "credential_hold", "held for the check")
	frappe.db.commit()
	url = api.connect(connection)
	query = parse_qs(urlsplit(url).query)
	expect(
		{name: query.get(name, [""])[0] for name in [*want.query, "redirect_uri"]},
		{**want.query, "redirect_uri": frappe.utils.get_url(CALLBACK_PATH + want.redirect)},
		"the approval URL",
	)
	callback = getattr(api, want.redirect)
	callback(code="e2e-authorization-code", state=query["state"][0], **want.params)
	expect(
		frappe.get_doc("Importer Connection", connection).get_password("refresh_token"),
		want.refresh_token,
		"the refresh token the source issued for the code",
	)
	for fieldname, value in want.settled.items():
		expect(
			frappe.db.get_value("Importer Connection", connection, fieldname),
			value,
			f"the settled {fieldname}",
		)
	expect(
		frappe.db.get_value("Importer Connection", connection, "credential_hold"), None, "the hold, released"
	)
	expect(
		frappe.local.response.get("location"),
		f"/app/importer-connection/{connection}?oauth=connected",
		"back on the connection",
	)
	try:
		callback(code="e2e-authorization-code", state=query["state"][0], **want.params)
	except frappe.ValidationError:
		print("a used approval can't be replayed")
	else:
		raise AssertionError("an approval was accepted twice")


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


@dataclass(frozen=True)
class Booked:
	"""One mirrored record of an accounting source and what one of its fields must be (a document's
	``grand_total``, a journal's ``total_debit``), in a fixture state; ``retired`` when the source deleted
	or voided it, so its copy is cancelled."""

	kind: str
	type_: str
	source_id: str
	field: str = ""
	value: Any = None
	retired: bool = False


def assert_booked(connection: str, records: list[Booked]) -> None:
	for record in records:
		gid = f"gid://{business_of(connection).source_id}/{connection}/{record.type_}/{record.source_id}"
		link = frappe.db.get_value(
			"Importer Link",
			{"connection": connection, "kind": record.kind, "gid": gid},
			["target_doctype", "target_name", "retired"],
			as_dict=True,
		)
		if link is None:
			raise AssertionError(f"{gid} ({record.kind}) is not mirrored")
		expect(bool(link.retired), record.retired, f"{gid} retired")
		if record.retired:
			status = frappe.db.get_value(link.target_doctype, link.target_name, "docstatus")
			if status is not None and frappe.get_meta(link.target_doctype).is_submittable:
				expect(status, 2, f"{gid}'s {link.target_doctype}, cancelled")
			continue
		if record.field:
			actual = frappe.db.get_value(link.target_doctype, link.target_name, record.field)
			wanted = record.value
			if isinstance(wanted, Decimal):
				actual = amount(actual)
			expect(actual, wanted, f"{gid}'s {record.field}")


@check
def assert_quickbooks_online(state: str) -> None:
	"""The QuickBooks Online company of state ``state``: its chart, parties, items, documents, payments and
	money movements."""
	changed = state == "v2"
	records = [
		Booked("account", "Account", "35", "account_type", "Bank"),
		Booked("cost_center", "Class", "5000000000000012345"),
		Booked("customer", "Customer", "1", "customer_type", "Company"),
		Booked("supplier", "Vendor", "10"),
		Booked("employee", "Employee", "20", "first_name", "Sam"),
		Booked("project", "Customer", "3"),
		Booked("product", "Item", "2", "is_stock_item", 1),
		Booked("draft_order", "Estimate", "40", "grand_total", amount("542.4")),
		Booked("purchase_order", "PurchaseOrder", "50", "grand_total", amount(678)),
		Booked("sales_invoice", "Invoice", "130", "grand_total", amount("367.25")),
		Booked("sales_invoice", "Invoice", "131", "grand_total", amount("50.85"), retired=changed),
		Booked("sales_invoice", "SalesReceipt", "140", "grand_total", amount("135.6")),
		Booked("sales_invoice", "CreditMemo", "145", "grand_total", amount("-96.05")),
		Booked("sales_invoice", "RefundReceipt", "146", "grand_total", amount("-10.17")),
		Booked("purchase_invoice", "Bill", "160", "grand_total", amount(728)),
		Booked("purchase_invoice", "VendorCredit", "165", "grand_total", amount(-20)),
		Booked("payment_entry", "Payment", "150", "paid_amount", amount("367.25")),
		Booked("payment_entry", "BillPayment", "170", "paid_amount", amount(728)),
		Booked("payment_entry", "SalesReceiptPayment", "140", "paid_amount", amount("135.6")),
		Booked("journal_entry", "JournalEntry", "180", "total_debit", amount(35 if changed else 30)),
		Booked("journal_entry", "Deposit", "190", "total_debit", amount(100)),
		Booked("journal_entry", "Transfer", "195", "total_debit", amount(200), retired=changed),
		Booked("journal_entry", "Purchase", "200", "total_debit", amount("45.2")),
	]
	if changed:
		records += [
			Booked("sales_invoice", "Invoice", "132", "grand_total", amount("192.1")),
			Booked("payment_entry", "Payment", "151", "paid_amount", amount("192.1")),
			Booked("account", "Account", "16", "account_name", "Postage and Courier - QB"),
		]
	assert_booked("quickbooks-q", records)


@check
def assert_xero(state: str) -> None:
	"""The Xero organisation of state ``state``."""
	changed = state == "v2"

	def g(n: int) -> str:
		return f"00000000-0000-4000-8000-{n:012d}"

	records = [
		Booked("account", "Account", g(1), "account_type", "Bank"),
		Booked("cost_center", "TrackingOption", g(302)),
		Booked("product", "Item", g(201), "is_stock_item", 1),
		Booked("customer", "Contact", g(101), "customer_type", "Company"),
		Booked("supplier", "Contact", g(103)),
		Booked("customer", "Contact", g(104)),
		Booked("supplier", "Contact", g(104)),
		Booked("project", "Project", g(1201)),
		Booked("draft_order", "Quote", g(1001), "grand_total", amount(756)),
		Booked("purchase_order", "PurchaseOrder", g(1101), "grand_total", amount(525)),
		Booked("sales_invoice", "Invoice", g(401), "grand_total", amount(315)),
		Booked("sales_invoice", "Invoice", g(402), "grand_total", amount("37.8"), retired=changed),
		Booked("sales_invoice", "CreditNote", g(501), "grand_total", amount("-18.9")),
		Booked("purchase_invoice", "Invoice", g(451), "grand_total", amount(210)),
		Booked("payment_entry", "Payment", g(601), "paid_amount", amount(315)),
		Booked("payment_entry", "Payment", g(602), "paid_amount", amount(210)),
		Booked("payment_entry", "Prepayment", g(701), "paid_amount", amount(50)),
		Booked("journal_entry", "ManualJournal", g(801), "total_debit", amount(400), retired=changed),
		Booked("journal_entry", "BankTransaction", g(901), "total_debit", amount(42)),
		Booked("journal_entry", "BankTransfer", g(951), "total_debit", amount(250)),
	]
	if changed:
		records.append(Booked("purchase_invoice", "Invoice", g(452), "grand_total", amount("94.5")))
	assert_booked("xero-x", records)


@check
def assert_zoho_books(state: str) -> None:
	"""The Zoho Books organisation of state ``state``."""
	changed = state == "v2"
	records = [
		Booked("account", "Account", "460000000000362", "account_type", "Bank"),
		Booked("warehouse", "Location", "460000000000901"),
		Booked("product", "Item", "460000000000501"),
		Booked("customer", "Contact", "460000000000101", "customer_type", "Company"),
		Booked("supplier", "Contact", "460000000000103"),
		Booked("project", "Project", "460000000000701"),
		Booked("draft_order", "Estimate", "460000000006001", "grand_total", amount("472.5")),
		Booked("sales_invoice", "Invoice", "460000000001001", "grand_total", amount("236.25")),
		Booked(
			"sales_invoice",
			"SalesReceipt",
			"460000000002001",
			"grand_total",
			amount("47.25"),
			retired=changed,
		),
		Booked("purchase_invoice", "Bill", "460000000004001", "grand_total", amount(126)),
		Booked("payment_entry", "CustomerPayment", "460000000003001", "paid_amount", amount("236.25")),
		Booked("payment_entry", "VendorPayment", "460000000005001", "paid_amount", amount(126)),
		Booked("journal_entry", "Journal", "460000000007001", "total_debit", amount(40 if changed else 25)),
		Booked("journal_entry", "Expense", "460000000008001", "total_debit", amount(15)),
	]
	if changed:
		records.append(Booked("purchase_invoice", "Bill", "460000000004002", "grand_total", amount(126)))
	assert_booked("zoho-books-b", records)


@check
def assert_zoho_inventory(state: str) -> None:
	"""The Zoho Inventory organisation of state ``state``."""
	changed = state == "v2"
	assert_booked(
		"zoho-inventory-i",
		[
			Booked("warehouse", "Location", "2000000000000901"),
			Booked("collection", "Category", "2000000000000301"),
			Booked("product", "ItemGroup", "2000000000000401", "has_variants", 1),
			Booked("product", "Item", "2000000000000421"),
			Booked("customer", "Contact", "2000000000000101"),
			Booked("supplier", "Contact", "2000000000000103"),
			Booked(
				"sales_order", "SalesOrder", "2000000000005001", "grand_total", amount(693), retired=changed
			),
			Booked("sales_invoice", "Invoice", "2000000000006001", "grand_total", amount(693)),
			Booked("purchase_order", "PurchaseOrder", "2000000000008001", "grand_total", amount(315)),
			Booked("purchase_invoice", "Bill", "2000000000009001", "grand_total", amount(315)),
			Booked("payment_entry", "CustomerPayment", "2000000000007001", "paid_amount", amount(693)),
		],
	)


@check
def assert_netsuite(state: str) -> None:
	"""The NetSuite account of state ``state``: its chart, departments and locations, items, parties and
	their contacts, people, documents, payments and journals. The USD cash sale's and deposit's payments
	wait for payments in other currencies (card 386)."""
	changed = state == "v2"
	records = [
		Booked("account", "Account", "100", "account_type", "Bank"),
		Booked("account", "Account", "600", "disabled", 1 if changed else 0),
		Booked("cost_center", "Department", "2", "is_group", 0),
		Booked("department", "Department", "3"),
		Booked("warehouse", "Location", "2"),
		Booked("product", "Item", "501", "is_stock_item", 1),
		Booked("product", "Item", "503", retired=changed),
		Booked("customer", "Customer", "101", "customer_type", "Company"),
		Booked("customer", "Customer", "102", "disabled", 1 if changed else 0),
		Booked("supplier", "Vendor", "201"),
		Booked("lead", "Customer", "104", "company_name", "Pine Ridge Lodge"),
		Booked("employee", "Employee", "302", "status", "Left" if changed else "Active"),
		Booked("project", "Job", "401", "status", "Completed" if changed else "Open"),
		Booked("opportunity", "Transaction", "602"),
		Booked("draft_order", "Transaction", "601", "grand_total", amount("37.8")),
		Booked("sales_order", "Transaction", "603", "grand_total", amount("147.55")),
		Booked("purchase_order", "Transaction", "608", "grand_total", amount(90)),
		Booked("sales_invoice", "Transaction", "604", "grand_total", amount(126)),
		Booked("sales_invoice", "Transaction", "605", "grand_total", amount(23)),
		Booked("sales_invoice", "Transaction", "606", "grand_total", amount("-18.9")),
		Booked("purchase_invoice", "Transaction", "609", "grand_total", amount("120.75")),
		Booked("payment_entry", "Transaction", "607", "paid_amount", amount("107.1")),
		Booked("payment_entry", "Transaction", "610", "paid_amount", amount("120.75")),
		Booked("journal_entry", "Transaction", "611", "total_debit", amount(1500), retired=changed),
		Booked("journal_entry", "Transaction", "612", "total_debit", amount(42)),
	]
	if changed:
		records += [
			Booked("customer", "Customer", "103", "customer_name", "Harbor Yoga"),
			Booked("sales_invoice", "Transaction", "614", "docstatus", 0),
		]
	assert_booked("netsuite-n", records)
	expect(
		frappe.db.get_value("Account", target("netsuite-n", "account", "Account", "131"), "parent_account"),
		target("netsuite-n", "account", "Account", "130"),
		"an account under its summary account",
	)
	customer = target("netsuite-n", "customer", "Customer", "101")
	people = frappe.get_all(
		"Dynamic Link", filters={"link_name": customer, "parenttype": "Contact"}, pluck="parent"
	)
	expect(
		{
			(
				frappe.db.get_value("Contact", name, "first_name"),
				frappe.db.get_value("Contact", name, "designation"),
			)
			for name in people
		}
		>= {("Rosa Diaz", "Head Chef")},
		True,
		"the customer's contact",
	)


@check
def assert_sap_business_one(state: str) -> None:
	"""The SAP Business One company of state ``state``: its chart, profit centers, warehouses, items,
	business partners and their contact persons, people, documents, payments and its manual journal entry
	(the document postings the Service Layer also lists are never booked twice). The USD invoice is
	cancelled in v2, and its cancellation is never booked."""
	changed = state == "v2"
	records = [
		Booked("account", "Account", "1100", "account_type", "Cash"),
		Booked(
			"account",
			"Account",
			"5200",
			"account_name",
			"Office Supplies & Postage - SB" if changed else "Office Supplies - SB",
		),
		Booked("cost_center", "ProfitCenter", "OPS"),
		Booked("warehouse", "Warehouse", "02", "disabled", 1 if changed else 0),
		Booked("collection", "ItemGroup", "100"),
		Booked("product", "Item", "A1000", "is_stock_item", 1),
		Booked("product", "Item", "K200", retired=changed),
		Booked("customer", "BusinessPartner", "C001", "customer_type", "Company"),
		Booked("supplier", "BusinessPartner", "V001"),
		Booked("lead", "BusinessPartner", "L002", "company_name", "Cedar Hollow Lodge"),
		Booked("department", "Department", "2"),
		Booked("designation", "Position", "1"),
		Booked("employee", "Employee", "2", "status", "Left" if changed else "Active"),
		Booked("project", "Project", "P01"),
		Booked("opportunity", "Opportunity", "1"),
		Booked("draft_order", "Quotation", "1", "grand_total", amount("37.8")),
		Booked("sales_order", "Order", "2", "grand_total", amount("148.05")),
		Booked("purchase_order", "PurchaseOrder", "3", "grand_total", amount(90)),
		Booked("sales_invoice", "Invoice", "4", "grand_total", amount(126)),
		Booked("sales_invoice", "Invoice", "5", "grand_total", amount(23), retired=changed),
		Booked("sales_invoice", "CreditNote", "6", "grand_total", amount("-18.9")),
		Booked("purchase_invoice", "PurchaseInvoice", "7", "grand_total", amount("120.75")),
		Booked("payment_entry", "IncomingPayment", "8", "paid_amount", amount("107.1")),
		Booked("payment_entry", "VendorPayment", "9", "paid_amount", amount("120.75")),
		Booked("journal_entry", "JournalEntry", "10", "total_debit", amount(1500)),
	]
	if changed:
		records += [
			Booked("customer", "BusinessPartner", "L001", "customer_name", "Summit Pilates"),
			Booked("sales_invoice", "Invoice", "13", "grand_total", amount("94.5")),
		]
	assert_booked("sap-b", records)
	for absent in ("JournalEntry/11", "Invoice/12"):
		expect(
			frappe.db.exists(
				"Importer Link", {"connection": "sap-b", "gid": f"gid://sap_business_one/sap-b/{absent}"}
			),
			None,
			f"{absent}, never booked",
		)


@check
def assert_acumatica(state: str) -> None:
	"""The Acumatica tenant of state ``state``: its accounts, subaccounts, warehouses, items, parties and
	their contacts, the employees' departments and titles, documents, payments and its general ledger batch.
	The USD invoice waits for an exchange rate (the Default endpoint's invoice records none) and is voided in
	v2."""
	changed = state == "v2"
	records = [
		Booked("account", "Account", "10100", "account_type", "Bank"),
		Booked("account", "Account", "60000", "disabled", 1 if changed else 0),
		Booked("cost_center", "Subaccount", "OPS-000"),
		Booked("warehouse", "Warehouse", "RETAIL", "disabled", 1 if changed else 0),
		Booked("collection", "ItemClass", "BOOTS"),
		Booked("product", "Item", "HIKER-BOOT", "is_stock_item", 1),
		Booked("product", "Item", "TRAIL-SOCK", retired=changed),
		Booked("product", "Item", "FITTING", "is_stock_item", 0),
		Booked("customer", "Customer", "ALPINE", "customer_type", "Company"),
		Booked("supplier", "Vendor", "LEATHERCO"),
		Booked("employee", "Employee", "EP002", "status", "Inactive" if changed else "Active"),
		Booked("project", "Project", "FITOUT"),
		Booked("lead", "Lead", "202", "company_name", "Ridge Runners Club"),
		Booked("opportunity", "Opportunity", "OP000001"),
		Booked("draft_order", "SalesOrder", "QT:QT000001", "grand_total", amount(252)),
		Booked("sales_order", "SalesOrder", "SO:SO000001", "grand_total", amount("283.5")),
		Booked("purchase_order", "PurchaseOrder", "Normal:PO000001", "grand_total", amount(600)),
		Booked("sales_invoice", "Invoice", "Invoice:AR000001", "grand_total", amount("262.5")),
		Booked("sales_invoice", "Invoice", "Credit Memo:AR000003", "grand_total", amount(-42)),
		Booked("sales_invoice", "CashSale", "Cash Sale:CS000001", "grand_total", amount("31.5")),
		Booked("purchase_invoice", "Bill", "Bill:AP000001", "grand_total", amount(651)),
		Booked("payment_entry", "Payment", "Payment:PM000001", "paid_amount", amount("220.5")),
		Booked("payment_entry", "Check", "Payment:CHK00001", "paid_amount", amount(651)),
		Booked("payment_entry", "CashSale", "Cash Sale:CS000001", "paid_amount", amount("31.5")),
		Booked("journal_entry", "JournalTransaction", "GL:GL000001", "total_debit", amount(1800)),
	]
	if changed:
		records.append(Booked("sales_invoice", "Invoice", "Invoice:AR000004", "docstatus", 0))
	assert_booked("acumatica-a", records)


@check
def assert_sage_intacct(state: str) -> None:
	"""The Sage Intacct company of state ``state``: its accounts, departments, items, parties, people,
	documents, payments and its general journal transaction (the AR journal's postings and the order entry
	invoice are never booked twice)."""
	changed = state == "v2"
	records = [
		Booked("account", "GLAccount", "3000", "root_type", "Equity"),
		Booked("account", "GLAccount", "6000", "disabled", 1 if changed else 0),
		Booked("cost_center", "Department", "ADV-TAX"),
		Booked("department", "Department", "ADV-TAX"),
		Booked("warehouse", "Warehouse", "WH1"),
		Booked("product", "Item", "GUIDE", "is_stock_item", 1),
		Booked("customer", "Customer", "C-NORTH", "customer_type", "Company"),
		Booked("supplier", "Vendor", "V-SOFT"),
		Booked("employee", "Employee", "E-02", "status", "Left" if changed else "Active"),
		Booked("project", "Project", "P-100"),
		Booked("draft_order", "SOTransaction", "11", "grand_total", amount(4500)),
		Booked("sales_order", "SOTransaction", "12", "grand_total", amount(4620)),
		Booked("purchase_order", "POTransaction", "21", "grand_total", amount(250)),
		Booked("sales_invoice", "Invoice", "31", "grand_total", amount(4620)),
		Booked("sales_invoice", "Invoice", "32", "grand_total", amount(800), retired=changed),
		Booked("purchase_invoice", "Bill", "41", "grand_total", amount(350)),
		Booked("payment_entry", "ARPayment", "51", "paid_amount", amount(4620)),
		Booked("payment_entry", "APPayment", "61", "paid_amount", amount(350)),
		Booked("journal_entry", "GLTransaction", "GJ:7", "total_debit", amount(2200)),
	]
	if changed:
		records.append(Booked("purchase_invoice", "Bill", "42", "grand_total", amount(90)))
	assert_booked("intacct-s", records)
	for absent in ("GLTransaction/ARJ:8", "SOTransaction/13"):
		expect(
			frappe.db.exists(
				"Importer Link", {"connection": "intacct-s", "gid": f"gid://sage_intacct/intacct-s/{absent}"}
			),
			None,
			f"{absent}, never booked",
		)


def agendrix_id(group: int, n: int) -> str:
	return f"00000000-0000-4000-{group:04d}-{n:012d}"


@check
def assert_agendrix(state: str) -> None:
	"""The Agendrix café of state ``state`` (tools/agendrix_fixtures.py): its sites as branches, positions
	as designations, members as employees (one archived in v2), time off types, shifts on the Shift Type of
	their hours (one moved, a draft removed in v2), punches, a day's attendance from its time entries (a
	longer afternoon in v2), and time off requests (one declined in v2); an approved leave's day is marked
	by HRMS itself, and the open shift and the part-day request aren't mirrored."""
	changed = state == "v2"
	connection = "agendrix-a"
	site, position, member, leave, shift, clock, _, request = range(1, 9)
	avery = agendrix_id(member, 1)
	downtown = "Downtown Café - AG" if changed else "Downtown - AG"
	assert_booked(
		connection,
		[
			Booked("branch", "Site", agendrix_id(site, 1), "branch", downtown),
			Booked(
				"designation", "Position", agendrix_id(position, 3), "designation_name", "Kitchen - Prep - AG"
			),
			Booked("employee", "Member", avery, "branch", downtown),
			Booked("employee", "Member", avery, "designation", "Barista - AG"),
			Booked("employee", "Member", agendrix_id(member, 3), "status", "Left" if changed else "Active"),
			Booked("leave_type", "LeaveType", agendrix_id(leave, 2), "is_lwp", 1),
			Booked(
				"shift_assignment",
				"Shift",
				agendrix_id(shift, 1),
				"shift_type",
				"09:00-17:00 - AG" if changed else "08:00-16:00 - AG",
			),
			Booked("shift_assignment", "Shift", agendrix_id(shift, 2), "shift_type", "22:00-06:00 - AG"),
			Booked("employee_checkin", "ClockLog", agendrix_id(clock, 1), "log_type", "IN"),
			Booked(
				"attendance", "Attendance", f"{avery}:2026-10-05", "working_hours", 7.75 if changed else 7.5
			),
			Booked("attendance", "Attendance", f"{agendrix_id(member, 2)}:2026-10-05", "docstatus", 0),
			Booked("leave_application", "LeaveRequest", agendrix_id(request, 1), "docstatus", 1),
			Booked(
				"leave_application",
				"LeaveRequest",
				agendrix_id(request, 2),
				"status",
				"Rejected" if changed else "Open",
			),
		],
	)
	draft = target(connection, "shift_assignment", "Shift", agendrix_id(shift, 3))
	expect(
		frappe.db.get_value("Shift Assignment", draft, ["docstatus", "status"]),
		(0, "Inactive" if changed else "Active"),
		"the draft shift, removed in v2",
	)
	employee = target(connection, "employee", "Member", avery)
	expect(
		frappe.db.get_value("Attendance", {"employee": employee, "attendance_date": "2026-10-09"}, "status"),
		"On Leave",
		"the approved leave's day, marked by HRMS",
	)
	for absent in (f"Shift/{agendrix_id(shift, 4)}", f"LeaveRequest/{agendrix_id(request, 3)}"):
		expect(
			frappe.db.count(
				"Importer Link", {"connection": connection, "gid": f"gid://agendrix/{connection}/{absent}"}
			),
			0,
			f"{absent}, which has no first-party home",
		)
	expect(
		frappe.db.count(
			"Employee Checkin",
			{
				"employee": (
					"in",
					[employee, target(connection, "employee", "Member", agendrix_id(member, 2))],
				)
			},
		),
		4 if changed else 3,
		"successful punches",
	)


@check
def assert_highlevel(state: str) -> None:
	"""The HighLevel studio of state ``state`` (tools/highlevel_fixtures.py): its users, products, contacts
	as leads (the billed one a customer too), its business as a prospect, its pipeline and opportunity,
	messages, appointment and task, its invoices and payments (v2: a user removed, a new price, a won
	opportunity, a sent invoice, a new message). A void invoice and a failed transaction aren't kept."""
	changed = state == "v2"
	connection = "highlevel-h"
	assert_booked(
		connection,
		[
			Booked("employee", "User", "user001", "status", "Active"),
			Booked("employee", "User", "user002", "status", "Inactive" if changed else "Active"),
			Booked("lead", "Contact", "contact003", "status", "Do Not Contact"),
			Booked("customer", "Contact", "contact001", "customer_name", "Riley River"),
			Booked("prospect", "Business", "business001", "company_name", "River Co - HL"),
			Booked("opportunity_type", "Pipeline", "pipeline001"),
			Booked(
				"opportunity",
				"Opportunity",
				"opportunity001",
				"status",
				"Converted" if changed else "Open",
			),
			Booked("opportunity", "Opportunity", "opportunity001", "opportunity_type", "Sales - HL"),
			Booked("communication", "Message", "message001", "communication_medium", "Email"),
			Booked("communication", "Message", "message003", "communication_medium", "Phone"),
			Booked("event", "Appointment", "appointment001", "status", "Open"),
			Booked("todo", "Task", "task001", "status", "Open"),
			Booked("sales_invoice", "Invoice", "invoice001", "grand_total", amount(113)),
			Booked("sales_invoice", "Invoice", "invoice002", "docstatus", 1 if changed else 0),
			Booked("payment_entry", "Transaction", "transaction001", "paid_amount", amount(113)),
			Booked("payment_entry", "TransactionRefund", "transaction002", "payment_type", "Pay"),
		],
	)
	if changed:
		assert_booked(connection, [Booked("communication", "Message", "message004")])
	consultation = target(connection, "product", "Product", "product001")
	expect(item_price(connection, consultation), amount(175 if changed else 150), "the consultation's price")
	tee = target(connection, "product", "Product", "product002")
	expect(frappe.db.count("Item", {"variant_of": tee}), 2, "the T-shirt's variants")
	riley = target(connection, "lead", "Contact", "contact001")
	expect(
		[row.note for row in frappe.get_doc("Lead", riley).notes],
		["Follow-up\n\nWants the spring catalogue"],
		"the contact's note",
	)
	for absent in ("Invoice/invoice003", "Transaction/transaction003"):
		expect(
			frappe.db.count(
				"Importer Link",
				{"connection": connection, "gid": f"gid://highlevel/{connection}/{absent}", "retired": 0},
			),
			0,
			f"{absent}, void or failed",
		)


@check
def assert_peoplesoft(state: str) -> None:
	"""The PeopleSoft installation of state ``state`` (tools/peoplesoft_fixtures.py), read through the queries
	its field map names: departments, jobs and employees in HRMS; customers, suppliers, accounts, an AR
	invoice, a voucher, payments and journals in the books (v2: a department inactive, an employee
	terminated, a supplier renamed, the voucher posted and paid, a new journal)."""
	changed = state == "v2"
	connection = "peoplesoft-p"
	records = [
		Booked("department", "Department", "10100", "disabled", 1 if changed else 0),
		Booked("designation", "Job", "KC001", "designation_name", "Field Technician - PS"),
		Booked("employee", "Employee", "KU0002", "status", "Left" if changed else "Active"),
		Booked("employee", "Employee", "KU0002", "gender", "Male"),
		Booked(
			"supplier",
			"Supplier",
			"VEND01",
			"supplier_name",
			"Northern Tools & Supply Inc" if changed else "Northern Tools Inc",
		),
		Booked("customer", "Customer", "CUST01", "customer_type", "Company"),
		Booked("account", "Account", "400000", "root_type", "Income"),
		Booked("sales_invoice", "ArInvoice", "US001:INV0000101", "grand_total", amount(640)),
		Booked("purchase_invoice", "Voucher", "US001:00000201", "docstatus", 1 if changed else 0),
		Booked("payment_entry", "ArPayment", "US001:DEP0001:1", "paid_amount", amount(640)),
		Booked("journal_entry", "Journal", "US001:JRN0000301:2026-09-30", "total_debit", amount(125)),
	]
	if changed:
		records += [
			Booked("payment_entry", "ApPayment", "RBC01:0000000451", "paid_amount", amount(450)),
			Booked("journal_entry", "Journal", "US001:JRN0000302:2026-10-15", "total_debit", amount(80)),
		]
	assert_booked(connection, records)
	avery = target(connection, "employee", "Employee", "KU0002")
	expect(
		frappe.db.get_value("Employee", avery, "reports_to"),
		target(connection, "employee", "Employee", "KU0001"),
		"the supervisor",
	)


@check
def assert_hubspot(state: str) -> None:
	"""The HubSpot studio of state ``state`` (tools/hubspot_fixtures.py): owners, products, contacts as leads
	(an unqualified one closed), companies as prospects, the customers, the pipeline and deals at their stage,
	the calls, emails, meeting and task, a quote to an organisation, an order, invoices and payments, and the
	studio's own properties (v2: an owner archived, a new price, a won deal, a completed task, an opened
	invoice, a new email). A voided invoice and a failed payment aren't kept."""
	changed = state == "v2"
	connection = "hubspot-s"
	assert_booked(
		connection,
		[
			Booked("employee", "Owner", "1", "status", "Active"),
			Booked("employee", "Owner", "2", "status", "Inactive" if changed else "Active"),
			Booked("lead", "Contact", "101", "status", "Converted"),
			Booked("lead", "Contact", "103", "disabled", 1),
			Booked("customer", "Company", "201", "customer_type", "Company"),
			Booked("customer", "Contact", "102", "customer_name", "Sam Shore"),
			Booked("prospect", "Company", "202", "company_name", "Delta Works - HS"),
			Booked("opportunity_type", "Pipeline", "default"),
			Booked("opportunity", "Deal", "301", "status", "Converted" if changed else "Open"),
			Booked(
				"opportunity",
				"Deal",
				"301",
				"sales_stage",
				"Closed won - HS" if changed else "Qualified to buy - HS",
			),
			Booked("opportunity", "Deal", "301", "opportunity_from", "Prospect"),
			Booked("opportunity", "Deal", "302", "status", "Lost"),
			Booked("communication", "Call", "501", "communication_medium", "Phone"),
			Booked("communication", "Email", "502", "sent_or_received", "Received"),
			Booked("event", "Meeting", "601", "status", "Completed"),
			Booked("todo", "Task", "701", "status", "Closed" if changed else "Open"),
			Booked("draft_order", "Quote", "801", "quotation_to", "Prospect"),
			Booked("draft_order", "Quote", "801", "grand_total", amount("310.5")),
			Booked("sales_order", "Order", "851", "grand_total", amount(55)),
			Booked("sales_invoice", "Invoice", "861", "grand_total", amount(345)),
			Booked("sales_invoice", "Invoice", "862", "docstatus", 1 if changed else 0),
			Booked("payment_entry", "Payment", "871", "paid_amount", amount(345)),
			Booked("payment_entry", "PaymentRefund", "872", "payment_type", "Pay"),
		],
	)
	if changed:
		assert_booked(
			connection, [Booked("communication", "Email", "503", "reference_doctype", "Opportunity")]
		)
	session = target(connection, "product", "Product", "401")
	expect(item_price(connection, session), amount(175 if changed else 150), "the design session's price")
	riley = target(connection, "lead", "Contact", "101")
	expect(
		json.loads(frappe.db.get_value("Lead", riley, "importer_custom_fields")),
		{"Preferred contact time": "Mornings"},
		"the contact's own property",
	)
	expect(
		[row.note for row in frappe.get_doc("Lead", riley).notes],
		["Wants the spring catalogue"],
		"the contact's note",
	)
	for absent in ("Invoice/863", "Payment/873"):
		expect(
			frappe.db.count(
				"Importer Link",
				{"connection": connection, "gid": f"gid://hubspot/{connection}/{absent}", "retired": 0},
			),
			0,
			f"{absent}, voided or failed",
		)


@check
def assert_salesforce(state: str) -> None:
	"""The Salesforce studio org of state ``state`` (tools/salesforce_fixtures.py): users, products, accounts as
	prospects with their contacts (the customer one a customer too), leads, opportunity types and
	opportunities at their stage, logged activities, an email message, a task, an event, a quote to an
	organisation and orders, and the org's custom fields (v2: a user deactivated, a new price, a lead closed,
	a won opportunity, a closed task, an activated order, a new email)."""
	changed = state == "v2"
	connection = "salesforce-f"
	river, delta = "0015e000002AaAAAA1", "0015e000002AbBBAA2"
	assert_booked(
		connection,
		[
			Booked("employee", "User", "0055e000001AbBBAA2", "status", "Inactive" if changed else "Active"),
			Booked("customer", "Account", river, "customer_type", "Company"),
			Booked("prospect", "Account", delta, "company_name", "Delta Works - SF"),
			Booked("lead", "Lead", "00Q5e000003AaAAAA1", "disabled", 1 if changed else 0),
			Booked("lead", "Lead", "00Q5e000003AcCCAA3", "status", "Converted"),
			Booked("opportunity_type", "OpportunityType", "New%20Customer"),
			Booked(
				"opportunity",
				"Opportunity",
				"0065e000004AaAAAA1",
				"status",
				"Converted" if changed else "Open",
			),
			Booked(
				"opportunity",
				"Opportunity",
				"0065e000004AaAAAA1",
				"sales_stage",
				"Closed Won - SF" if changed else "Proposal/Price Quote - SF",
			),
			Booked(
				"opportunity", "Opportunity", "0065e000004AaAAAA1", "opportunity_type", "New Customer - SF"
			),
			Booked("communication", "Task", "00T5e000005AaAAAA1", "reference_doctype", "Opportunity"),
			Booked("communication", "EmailMessage", "02s5e000007AaAAAA1", "sent_or_received", "Received"),
			Booked("todo", "Task", "00T5e000005AbBBAA2", "status", "Closed" if changed else "Open"),
			Booked("event", "Event", "00U5e000006AaAAAA1", "subject", "Concept review"),
			Booked("draft_order", "Quote", "0Q05e000008AaAAAA1", "grand_total", amount("310.5")),
			Booked("sales_order", "Order", "8015e000009AaAAAA1", "grand_total", amount(135)),
			Booked("sales_order", "Order", "8015e000009AbBBAA2", "docstatus", 1 if changed else 0),
		],
	)
	if changed:
		assert_booked(
			connection,
			[Booked("communication", "EmailMessage", "02s5e000007AbBBAA2", "reference_doctype", "Prospect")],
		)
	session = target(connection, "product", "Product2", "01t5e000000AaAAAA1")
	expect(item_price(connection, session), amount(175 if changed else 150), "the design session's price")
	prospect = target(connection, "prospect", "Account", river)
	expect(
		json.loads(frappe.db.get_value("Prospect", prospect, "importer_custom_fields")),
		{"Account Tier": "Gold"},
		"the account's custom field",
	)
	expect(
		frappe.db.count(
			"Dynamic Link", {"link_doctype": "Prospect", "link_name": prospect, "parenttype": "Contact"}
		),
		2,
		"the account's contacts",
	)


@check
def assert_listed_sources_refuse(template: str) -> None:
	"""A connection to a listed source (the first still listed) can be saved, but its run refuses to start,
	says why, and touches nothing."""
	if not LISTED:
		print("every source is built: none is listed")
		return
	listed = LISTED[0]
	connection = "listed-e2e"
	if not frappe.db.exists("Importer Connection", connection):
		doc = frappe.copy_doc(frappe.get_doc("Importer Connection", template))
		doc.update(
			{
				"connection_id": connection,
				"source": listed.label,
				"store_url": "https://listed.example.test",
				"abbreviation": "LI",
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
