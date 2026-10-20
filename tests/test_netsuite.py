# SPDX-License-Identifier: AGPL-3.0-or-later
"""The NetSuite adapter over the recorded account (tests/fixtures/netsuite, proven against the REST API
Browser 2026.1 and SuiteQL's documented response): the machine-to-machine grant, SuiteQL's keyset listings
and incremental condition, records read whole and annotated, and every mapper."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import CredentialsRejected, SourceError, Unsupported
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.netsuite.adapter import GRANT_PATH, NetSuiteAdapter
from idempotent_importer.sources.netsuite.mapping import designation_key
from idempotent_importer.sources.netsuite.reader import (
	KINDS,
	LISTINGS,
	PAGE_SIZE,
	SUITEQL_PATH,
	NetSuiteReader,
	account_host,
	item_record,
	transaction_record,
)
from tests.support.transport import (
	OAUTH_ACCESS_TOKEN,
	FakeTransport,
	RecordingSleeper,
	oauth_source,
	scripted,
)

ADAPTER = NetSuiteAdapter()
ENDPOINT = "http://netsuite-fake:8080"
CANONICAL_TOKEN_URL = "https://1234567.suitetalk.api.netsuite.com" + GRANT_PATH


@pytest.fixture(scope="module")
def key() -> ec.EllipticCurvePrivateKey:
	return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture
def config(key: ec.EllipticCurvePrivateKey) -> ConnectionConfig:
	pem = key.private_bytes(
		serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
	).decode()
	return ConnectionConfig(
		source="netsuite",
		connection="netsuite-n",
		store_url="https://1234567.app.netsuite.com",
		api_user="e2e-netsuite-client",
		api_secret=pem,
		account="1234567",
		key_id="e2e-certificate",
		endpoint_override=ENDPOINT,
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[NetSuiteReader, FakeTransport]:
	fake = oauth_source("netsuite", state, GRANT_PATH, rotates=False)
	built = ADAPTER.reader(config, fake, RecordingSleeper())
	assert isinstance(built, NetSuiteReader)
	return built, fake


def nodes(config: ConnectionConfig, kind: Kind, state: str = "v1") -> dict[str, Node]:
	source, _ = reader(config, state)
	found: dict[str, Node] = {}
	cursor: str | None = None
	while True:
		page = source.page(kind, cursor, None, "")
		found |= {node.id().rsplit("/", 1)[-1]: node for node in page.nodes}
		if not page.has_next_page:
			return found
		cursor = page.end_cursor


def mapped(config: ConnectionConfig, kind: Kind, node: Node) -> Desired | None:
	return {m.kind: m for m in ADAPTER.mappers(config)}[kind].map(node)


def fields(config: ConnectionConfig, kind: Kind, key_: str, state: str = "v1") -> dict[str, Any]:
	desired = mapped(config, kind, nodes(config, kind, state)[key_])
	assert desired is not None
	return desired.fields


def changed(node: Node, **values: Any) -> Node:
	return Node({**node.raw(), **values}, node.path)


def editable(node: Node, field: str) -> dict[str, Any]:
	"""A copy of one of the node's sublists, to change."""
	copied: dict[str, Any] = json.loads(json.dumps(node.raw()[field]))
	return copied


def fields_of(desired: Desired | None) -> dict[str, Any]:
	assert desired is not None
	return desired.fields


def suiteql_bodies(fake: FakeTransport) -> list[dict[str, Any]]:
	return [json.loads(r.body) for r in fake.requests if r.url.split("?", 1)[0].endswith(SUITEQL_PATH)]


class TestAuth:
	def test_the_token_request_carries_a_signed_assertion_for_the_accounts_token_endpoint(
		self, config: ConnectionConfig, key: ec.EllipticCurvePrivateKey
	) -> None:
		source, fake = reader(config)
		source.page(Kind.WAREHOUSE, None, None, "")
		grant = fake.requests[0]
		assert grant.url == ENDPOINT + GRANT_PATH
		form = parse_qs(grant.body.decode())
		assert form["grant_type"] == ["client_credentials"]
		assertion = form["client_assertion"][0]
		assert jwt.get_unverified_header(assertion)["kid"] == "e2e-certificate"
		claims = jwt.decode(
			assertion,
			key.public_key(),
			algorithms=["ES256"],
			audience=CANONICAL_TOKEN_URL,
			options={"verify_iat": False, "verify_exp": False},
		)
		assert (claims["iss"], claims["scope"]) == ("e2e-netsuite-client", "rest_webservices")
		assert all(r.headers["Authorization"] == f"Bearer {OAUTH_ACCESS_TOKEN}" for r in fake.requests[1:])
		suiteql = next(r for r in fake.requests if r.url.split("?", 1)[0].endswith(SUITEQL_PATH))
		assert suiteql.headers["Prefer"] == "transient"

	def test_a_refused_assertion_stops_the_run(self, config: ConnectionConfig) -> None:
		source = ADAPTER.reader(
			config, scripted(HttpResponse(401, b'{"error":"invalid_client"}')), RecordingSleeper()
		)
		with pytest.raises(CredentialsRejected, match="certificate ID"):
			source.page(Kind.ACCOUNT, None, None, "")

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="certificate ID and its private key"):
			ADAPTER.reader(replace(config, key_id=""), scripted(), RecordingSleeper())
		with pytest.raises(SourceError, match="account ID"):
			ADAPTER.reader(replace(config, account="acme corp"), scripted(), RecordingSleeper())
		assert account_host(replace(config, account="1234567_SB1")) == (
			"https://1234567-sb1.suitetalk.api.netsuite.com"
		)
		assert NetSuiteReader.base_url(replace(config, endpoint_override=None)) == (
			"https://1234567.suitetalk.api.netsuite.com"
		)
		assert ADAPTER.filter_hint() is None
		assert ADAPTER.kinds() == KINDS and Kind.DESIGNATION in KINDS


class TestReader:
	def test_a_full_listing_starts_the_keyset_at_zero(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		page = source.page(Kind.CUSTOMER, None, None, "")
		assert [node.string("source_id") for node in page.nodes] == ["101", "102"]
		assert not page.has_next_page and page.end_cursor is None
		body = suiteql_bodies(fake)[0]
		assert body == {
			"q": "SELECT id FROM customer WHERE id > ? AND searchstage = 'Customer' ORDER BY id",
			"params": ["0"],
		}
		assert "limit=100" in fake.requests[1].url

	def test_an_incremental_listing_asks_from_the_day_before_the_cursor(
		self, config: ConnectionConfig
	) -> None:
		source, fake = reader(config)
		source.page(Kind.ACCOUNT, None, datetime(2026, 10, 1, 9, 30, tzinfo=UTC), "")
		body = suiteql_bodies(fake)[0]
		assert body["q"].endswith("AND lastmodifieddate >= TO_DATE(?, 'YYYY-MM-DD') ORDER BY id")
		assert body["params"] == ["0", "2026-09-30"]
		assert all(source.is_incremental(kind) for kind in LISTINGS)
		assert not source.is_incremental(Kind.DESIGNATION)

	def test_the_next_page_starts_after_the_last_id(self, config: ConnectionConfig) -> None:
		def listing(*ids: str, more: bool) -> HttpResponse:
			items = [{"links": [], "id": id_} for id_ in ids]
			return HttpResponse(200, json.dumps({"items": items, "hasMore": more}).encode())

		vendor = json.dumps({"id": "7", "companyName": "Bay"}).encode()
		fake = scripted(
			HttpResponse(200, b'{"access_token":"t"}'), listing("7", more=True), HttpResponse(200, vendor)
		)
		source = ADAPTER.reader(config, fake, RecordingSleeper())
		page = source.page(Kind.SUPPLIER, None, None, "")
		assert (page.has_next_page, page.end_cursor) == (True, "7")
		source.page(Kind.SUPPLIER, "7", None, "")
		assert suiteql_bodies(fake)[-1]["params"] == ["7"]
		assert PAGE_SIZE == 100

	def test_records_are_read_whole_and_annotated(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		invoice = next(
			n for n in source.page(Kind.SALES_INVOICE, None, None, "").nodes if n.string("source_id") == "604"
		)
		assert invoice.id() == "gid://netsuite/netsuite-n/Transaction/604"
		assert (invoice.string("record_type"), invoice.string("currency_code")) == ("invoice", "CAD")
		assert (invoice.string("posting"), invoice.string("voided")) == ("T", "F")
		types = [
			line.optional_string("item_type") for line in invoice.required_child("item").children("items")
		]
		assert types == ["InvtPart", "Service", "Subtotal", "Discount"]
		gets = [r.url for r in fake.requests if "/record/v1/" in r.url]
		assert all(url.endswith("?expandSubResources=true") for url in gets)
		# A currency is read once per run however many records name it.
		assert sum("/currency/1?" in url for url in gets) == 1

	def test_trees_know_their_groups_and_accounts_their_summary_parents(
		self, config: ConnectionConfig
	) -> None:
		departments = nodes(config, Kind.DEPARTMENT)
		assert [departments[i].optional_flag("has_children") for i in ("1", "2", "3")] == [False, True, False]
		accounts = nodes(config, Kind.ACCOUNT)
		assert accounts["131"].optional_flag("parent_is_summary") is True
		assert accounts["410"].optional_flag("parent_is_summary") is False
		assert accounts["100"].raw()["parent_is_summary"] is None

	def test_opportunities_know_their_customers_stage(self, config: ConnectionConfig) -> None:
		assert nodes(config, Kind.OPPORTUNITY)["602"].string("entity_stage") == "Prospect"

	def test_job_titles_are_designations(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		page = source.page(Kind.DESIGNATION, None, None, "")
		assert [node.string("title") for node in page.nodes] == ["Sales Manager", "Technician"]
		gid = page.nodes[0].id()
		assert gid.endswith(designation_key("Sales Manager"))
		found = source.node(Kind.DESIGNATION, gid)
		assert found is not None and found.raw() == page.nodes[0].raw()

	def test_one_record_is_looked_up_by_id(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		found = source.node(Kind.CUSTOMER, "gid://netsuite/netsuite-n/Customer/101")
		assert found is not None and found.string("companyName") == "Lakeside Cafe"
		with pytest.raises(SourceError, match="Not a NetSuite customer"):
			source.node(Kind.CUSTOMER, "gid://netsuite/netsuite-n/Vendor/201")
		with pytest.raises(SourceError, match="NetSuite has no page"):
			source.page(Kind.PAGE, None, None, "")

	def test_a_record_deleted_since_its_listing_is_left_out(self, config: ConnectionConfig) -> None:
		listing = {"items": [{"links": [], "id": "9"}], "hasMore": False}
		fake = scripted(
			HttpResponse(200, b'{"access_token":"t"}'),
			HttpResponse(200, json.dumps(listing).encode()),
			HttpResponse(404, b'{"title":"Not Found"}'),
		)
		source = ADAPTER.reader(config, fake, RecordingSleeper())
		assert source.page(Kind.SUPPLIER, None, None, "").nodes == []

	def test_record_types(self) -> None:
		def row(**values: str) -> Node:
			return Node({"id": "1", **values})

		assert item_record(row(itemtype="Service", subtype="Purchase")) == "servicePurchaseItem"
		assert item_record(row(itemtype="NonInvtPart")) == "nonInventorySaleItem"
		assert item_record(row(itemtype="InvtPart", islotitem="T")) == "lotNumberedInventoryItem"
		assert item_record(row(itemtype="Assembly", isserialitem="T")) == "serializedAssemblyItem"
		assert item_record(row(itemtype="Kit")) == "kitItem"
		with pytest.raises(SourceError, match="unknown subtype"):
			item_record(row(itemtype="OthCharge", subtype="Lease"))
		with pytest.raises(SourceError, match="unread type"):
			item_record(row(itemtype="Payment"))
		assert transaction_record(row(type="CustCred")) == "creditMemo"
		with pytest.raises(SourceError, match="unread type"):
			transaction_record(row(type="ItemShip"))


class TestBooks:
	def test_accounts(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.ACCOUNT, "110") == {
			"number": "1100",
			"name": "Accounts Receivable",
			"root_type": "Asset",
			"account_type": "Receivable",
			"parent": None,
			"is_group": False,
			"currency": "CAD",
		}
		assert fields(config, Kind.ACCOUNT, "210")["account_type"] == "Tax"
		assert fields(config, Kind.ACCOUNT, "130")["is_group"] is True
		assert fields(config, Kind.ACCOUNT, "131")["parent"] == "gid://netsuite/netsuite-n/Account/130"
		under_posting = mapped(config, Kind.ACCOUNT, nodes(config, Kind.ACCOUNT)["410"])
		assert under_posting is not None and under_posting.fields["parent"] is None
		assert "takes postings" in under_posting.warnings[0]
		with pytest.raises(Unsupported, match="NonPosting"):
			mapped(config, Kind.ACCOUNT, nodes(config, Kind.ACCOUNT)["900"])

	def test_an_inactive_account_is_disabled_not_retired(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.ACCOUNT, "600", "v2")["disabled"] is True
		assert fields(config, Kind.ACCOUNT, "610", "v2")["name"] == "Office Supplies & Postage"
		assert "disabled" not in fields(config, Kind.ACCOUNT, "610", "v2")

	def test_departments_are_cost_centers_and_departments(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.COST_CENTER, "3") == {
			"name": "Field Services",
			"parent": None,
			"is_group": False,
		}
		assert fields(config, Kind.COST_CENTER, "2")["is_group"] is False
		assert fields(config, Kind.DEPARTMENT, "2")["is_group"] is True
		assert fields(config, Kind.DEPARTMENT, "3")["parent"] == "gid://netsuite/netsuite-n/Department/2"
		node = changed(nodes(config, Kind.DEPARTMENT)["1"], isInactive=True)
		desired = mapped(config, Kind.DEPARTMENT, node)
		assert desired is not None and desired.fields["disabled"] is True

	def test_locations_are_warehouses(self, config: ConnectionConfig) -> None:
		main = fields(config, Kind.WAREHOUSE, "1")
		assert (main["is_group"], main["address"]["city"], main["address"]["country_code"]) == (
			True,
			"Vancouver",
			"CA",
		)
		bay = fields(config, Kind.WAREHOUSE, "2")
		assert (bay["parent"], bay["address"]) == ("gid://netsuite/netsuite-n/Location/1", None)
		inactive = mapped(
			config, Kind.WAREHOUSE, changed(nodes(config, Kind.WAREHOUSE)["2"], isInactive=True)
		)
		assert inactive is not None and inactive.fields["disabled"] is True


class TestCatalog:
	def test_an_inventory_item(self, config: ConnectionConfig) -> None:
		mug = fields(config, Kind.PRODUCT, "501")
		assert (mug["name"], mug["sku"], mug["barcode"], mug["is_stock_item"]) == (
			"Trail Mug",
			"TRAIL-MUG",
			"0627843001234",
			True,
		)
		assert (mug["price"], mug["stock_qty"], mug["weight"]) == (
			Decimal(18),
			Decimal(40),
			{"value": Decimal("0.5"), "unit": "lbs"},
		)
		assert (
			fields(config, Kind.PRODUCT, "501", "v2")["price"],
			fields(config, Kind.PRODUCT, "501", "v2")["stock_qty"],
		) == (
			Decimal("19.5"),
			Decimal(35),
		)

	def test_services_kits_and_web_items(self, config: ConnectionConfig) -> None:
		service = fields(config, Kind.PRODUCT, "502")
		assert (service["is_stock_item"], service["stock_qty"], service["published"]) == (False, None, False)
		assert fields(config, Kind.PRODUCT, "503")["published"] is True
		assert fields(config, Kind.PRODUCT, "506")["name"] == "Mug Set"
		assert mapped(config, Kind.PRODUCT, nodes(config, Kind.PRODUCT, "v2")["503"]) is None

	def test_base_prices_in_several_currencies_warn(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.PRODUCT)["502"]
		prices = editable(node, "price")
		usd = {**prices["items"][0], "currencyPage": {"id": "2", "refName": "US Dollar"}, "price": 70.0}
		desired = mapped(
			config, Kind.PRODUCT, changed(node, price={**prices, "items": [*prices["items"], usd]})
		)
		assert desired is not None and desired.fields["price"] == Decimal(95)
		assert "several currencies" in desired.warnings[0]
		assert fields_of(mapped(config, Kind.PRODUCT, changed(node, price=None)))["price"] is None


class TestParties:
	def test_a_company_customer_with_its_address_book_and_contacts(self, config: ConnectionConfig) -> None:
		cafe = fields(config, Kind.CUSTOMER, "101")
		assert (cafe["name"], cafe["type"], cafe["company"], cafe["phone"]) == (
			"Lakeside Cafe",
			"Company",
			"Lakeside Cafe",
			"+1 604 555 0101",
		)
		assert [(a["address_type"], a["line1"]) for a in cafe["addresses"]] == [
			("Billing", "12 Lakeshore Rd"),
			("Shipping", "14 Lakeshore Rd"),
		]
		assert cafe["contacts"] == [
			{
				"gid": "gid://netsuite/netsuite-n/Contact/701",
				"name": "Rosa Diaz",
				"email": "rosa@lakeside.example",
				"phone": "+1 250 555 0112",
				"job_title": "Head Chef",
			}
		]
		assert fields(config, Kind.CUSTOMER, "101", "v2")["phone"] == "+1 604 555 0199"

	def test_a_person_in_another_currency_and_an_inactive_customer(self, config: ConnectionConfig) -> None:
		maya = fields(config, Kind.CUSTOMER, "102")
		assert (maya["type"], maya["first_name"], maya["currency"]) == ("Individual", "Maya", "USD")
		assert "disabled" not in maya
		assert fields(config, Kind.CUSTOMER, "102", "v2")["disabled"] is True

	def test_a_lead_become_a_customer_is_listed_as_one(self, config: ConnectionConfig) -> None:
		assert "103" not in nodes(config, Kind.CUSTOMER)
		assert fields(config, Kind.CUSTOMER, "103", "v2")["name"] == "Harbor Yoga"
		assert "103" not in nodes(config, Kind.LEAD, "v2")

	def test_suppliers(self, config: ConnectionConfig) -> None:
		paper = fields(config, Kind.SUPPLIER, "201")
		assert (paper["tax_id"], paper["contacts"][0]["job_title"]) == (
			"123456789RT0001",
			"Accounts Receivable Clerk",
		)
		assert fields(config, Kind.SUPPLIER, "202", "v2")["email"] == "accounts@bolt.example"

	def test_leads_and_prospects(self, config: ConnectionConfig) -> None:
		harbor = fields(config, Kind.LEAD, "103")
		assert (harbor["status"], harbor["company"], harbor["notes"]) == (
			"Lead",
			"Harbor Yoga",
			"Met at the spring trade show",
		)
		assert fields(config, Kind.LEAD, "104")["status"] == "Opportunity"
		inactive = mapped(config, Kind.LEAD, changed(nodes(config, Kind.LEAD)["104"], isInactive=True))
		assert inactive is not None and inactive.fields["disabled"] is True


class TestPeople:
	def test_employees(self, config: ConnectionConfig) -> None:
		jordan = fields(config, Kind.EMPLOYEE, "302")
		assert jordan == {
			"first_name": "Jordan",
			"middle_name": None,
			"last_name": "Lee",
			"gender": "Female",
			"date_of_birth": "1992-11-02",
			"date_of_joining": "2023-01-09",
			"relieving_date": None,
			"status": "Active",
			"employee_number": "E302",
			"department": "gid://netsuite/netsuite-n/Department/3",
			"designation": f"gid://netsuite/netsuite-n/Designation/{designation_key('Technician')}",
			"reports_to": "gid://netsuite/netsuite-n/Employee/301",
			"company_email": "jordan@northwind.example",
			"personal_email": None,
			"phone": "+1 604 555 0100",
		}
		left = fields(config, Kind.EMPLOYEE, "302", "v2")
		assert (left["status"], left["relieving_date"]) == ("Left", "2026-10-01")
		node = changed(nodes(config, Kind.EMPLOYEE)["301"], isInactive=True)
		assert fields_of(mapped(config, Kind.EMPLOYEE, node))["status"] == "Inactive"

	def test_designations(self, config: ConnectionConfig) -> None:
		key_ = designation_key("Sales Manager")
		assert fields(config, Kind.DESIGNATION, key_) == {"name": "Sales Manager"}

	def test_projects(self, config: ConnectionConfig) -> None:
		job = fields(config, Kind.PROJECT, "401")
		assert (job["name"], job["status"], job["customer"], job["start"], job["end"]) == (
			"Lakeside Cafe Renovation",
			"Open",
			"gid://netsuite/netsuite-n/Customer/101",
			"2026-08-01",
			"2026-11-30",
		)
		assert fields(config, Kind.PROJECT, "401", "v2")["status"] == "Completed"
		inactive = changed(nodes(config, Kind.PROJECT)["401"], isInactive=True)
		assert fields_of(mapped(config, Kind.PROJECT, inactive))["status"] == "Cancelled"


class TestSales:
	def test_an_opportunity_with_a_prospect(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.OPPORTUNITY, "602") == {
			"party_kind": "lead",
			"party": "gid://netsuite/netsuite-n/Customer/104",
			"title": "Lodge mugs",
			"status": "Open",
			"date": "2026-09-11",
			"currency": "CAD",
			"amount": Decimal(900),
			"probability": Decimal(40),
			"expected_closing": "2026-12-15",
			"notes": "Mugs for every cabin",
		}
		node = nodes(config, Kind.OPPORTUNITY)["602"]
		customer = mapped(config, Kind.OPPORTUNITY, changed(node, entity_stage="Customer"))
		assert customer is not None and customer.fields["party_kind"] == "customer"
		with pytest.raises(Unsupported, match="no ERPNext status"):
			mapped(config, Kind.OPPORTUNITY, changed(node, status={"id": "Z", "refName": "?"}))

	def test_an_estimate_is_a_draft_quotation(self, config: ConnectionConfig) -> None:
		estimate = fields(config, Kind.DRAFT_ORDER, "601")
		assert (estimate["number"], estimate["created_at"], estimate["total"]) == (
			"EST-1001",
			"2026-09-10T12:00:00Z",
			Decimal("37.8"),
		)
		assert estimate["charges"] == [{"kind": "tax", "description": "Tax", "amount": Decimal("1.8")}]
		voided = changed(nodes(config, Kind.DRAFT_ORDER)["601"], status={"id": "V", "refName": "Voided"})
		assert mapped(config, Kind.DRAFT_ORDER, voided) is None

	def test_a_sales_order_charges_shipping_as_freight(self, config: ConnectionConfig) -> None:
		order = fields(config, Kind.SALES_ORDER, "603")
		assert order["taxes"] == [
			{"description": "Tax", "amount": Decimal("6.55"), "account": None},
			{"description": "Shipping", "amount": Decimal(10), "account": None, "role": "shipping"},
		]
		assert (order["submitted"], order["due_date"], order["total"]) == (
			True,
			"2026-09-30",
			Decimal("147.55"),
		)
		node = nodes(config, Kind.SALES_ORDER)["603"]
		draft = mapped(
			config, Kind.SALES_ORDER, changed(node, status={"id": "A", "refName": "Pending Approval"})
		)
		assert draft is not None and draft.fields["submitted"] is False
		assert (
			mapped(config, Kind.SALES_ORDER, changed(node, status={"id": "C", "refName": "Cancelled"}))
			is None
		)

	def test_an_invoice_books_its_discount_lines_off_the_net_total(self, config: ConnectionConfig) -> None:
		invoice = fields(config, Kind.SALES_INVOICE, "604")
		assert [(line["source_line"], line["qty"], line["rate"]) for line in invoice["lines"]] == [
			("item:1", Decimal(2), Decimal(18)),
			("item:2", Decimal(1), Decimal(95)),
		]
		assert invoice["lines"][1]["project"] == "gid://netsuite/netsuite-n/Job/401"
		assert invoice["lines"][1]["cost_center"] == "gid://netsuite/netsuite-n/Department/3"
		assert (invoice["discount"], invoice["total"]) == (Decimal(11), Decimal(126))
		assert invoice["party_account"] == "gid://netsuite/netsuite-n/Account/110"
		assert (invoice["reference"], invoice["remarks"]) == ("PO-7781", "Thanks for your business")
		assert fields(config, Kind.SALES_ORDER, "603", "v2")["remarks"] == "Deliver to the kitchen door"

	def test_a_document_discount_comes_off_too(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.SALES_INVOICE)["604"]
		desired = mapped(config, Kind.SALES_INVOICE, changed(node, discountTotal=-6.0, total=120.0))
		assert desired is not None and desired.fields["discount"] == Decimal(17)

	def test_an_invoice_pending_approval_is_a_draft_and_a_voided_one_is_gone(
		self, config: ConnectionConfig
	) -> None:
		assert fields(config, Kind.SALES_INVOICE, "614", "v2")["submitted"] is False
		node = changed(nodes(config, Kind.SALES_INVOICE)["604"], voided="T")
		assert mapped(config, Kind.SALES_INVOICE, node) is None

	def test_a_credit_memo_returns_against_the_invoice_it_is_applied_to(
		self, config: ConnectionConfig
	) -> None:
		memo = fields(config, Kind.SALES_INVOICE, "606")
		assert (memo["is_return"], memo["return_against"], memo["total"], memo["return_settles"]) == (
			True,
			"gid://netsuite/netsuite-n/Transaction/604",
			Decimal("-18.9"),
			True,
		)
		assert memo["lines"][0]["qty"] == Decimal(-1)
		assert memo["taxes"] == [{"description": "Tax", "amount": Decimal("-0.9"), "account": None}]

	def test_a_cash_sale_and_a_cash_refund(self, config: ConnectionConfig) -> None:
		sale = fields(config, Kind.SALES_INVOICE, "605")
		assert (sale["currency"], sale["exchange_rate"], sale["party_account"]) == (
			"USD",
			Decimal("1.35"),
			None,
		)
		refund = changed(nodes(config, Kind.SALES_INVOICE)["605"], record_type="cashRefund")
		desired = mapped(config, Kind.SALES_INVOICE, refund)
		assert desired is not None
		assert (desired.fields["is_return"], desired.fields["return_settles"]) == (True, False)


class TestPurchases:
	def test_a_purchase_order(self, config: ConnectionConfig) -> None:
		order = fields(config, Kind.PURCHASE_ORDER, "608")
		assert (order["party"], order["total"], order["submitted"]) == (
			"gid://netsuite/netsuite-n/Vendor/201",
			Decimal(90),
			True,
		)
		node = nodes(config, Kind.PURCHASE_ORDER)["608"]
		planned = mapped(config, Kind.PURCHASE_ORDER, changed(node, status={"id": "P", "refName": "Planned"}))
		assert planned is not None and planned.fields["submitted"] is False
		assert (
			mapped(config, Kind.PURCHASE_ORDER, changed(node, status={"id": "C", "refName": "Rejected"}))
			is None
		)

	def test_a_vendor_bill_with_items_and_expenses(self, config: ConnectionConfig) -> None:
		bill = fields(config, Kind.PURCHASE_INVOICE, "609")
		assert bill["reference"] == "NP-3321"
		assert bill["party_account"] == "gid://netsuite/netsuite-n/Account/200"
		expense = bill["lines"][1]
		assert (expense["name"], expense["product"], expense["account"], expense["cost_center"]) == (
			"Packing paper",
			None,
			"gid://netsuite/netsuite-n/Account/610",
			"gid://netsuite/netsuite-n/Department/2",
		)

	def test_a_vendor_credit_is_a_return(self, config: ConnectionConfig) -> None:
		node = changed(nodes(config, Kind.PURCHASE_INVOICE)["609"], record_type="vendorCredit")
		desired = mapped(config, Kind.PURCHASE_INVOICE, node)
		assert desired is not None and desired.fields["is_return"] is True
		assert desired.fields["total"] == Decimal("-120.75")

	def test_a_document_whose_lines_dont_make_its_total_is_unsupported(
		self, config: ConnectionConfig
	) -> None:
		node = changed(nodes(config, Kind.PURCHASE_INVOICE)["609"], total=1.0)
		with pytest.raises(Unsupported, match="not its total"):
			mapped(config, Kind.PURCHASE_INVOICE, node)


class TestPayments:
	def test_a_customer_payment_allocated_to_its_invoice(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.PAYMENT_ENTRY, "607") == {
			"payment_type": "Receive",
			"party_type": "Customer",
			"party": "gid://netsuite/netsuite-n/Customer/101",
			"date": "2026-09-20",
			"amount": Decimal("107.1"),
			"currency": "CAD",
			"exchange_rate": None,
			"account": "gid://netsuite/netsuite-n/Account/100",
			"party_account": "gid://netsuite/netsuite-n/Account/110",
			"submitted": True,
			"reference": "PMT-7001",
			"remarks": None,
			"allocations": [
				{
					"kind": "sales_invoice",
					"document": "gid://netsuite/netsuite-n/Transaction/604",
					"amount": Decimal("107.1"),
				}
			],
		}

	def test_a_vendor_payment(self, config: ConnectionConfig) -> None:
		payment = fields(config, Kind.PAYMENT_ENTRY, "610")
		assert (payment["payment_type"], payment["party_type"], payment["party_account"]) == (
			"Pay",
			"Supplier",
			"gid://netsuite/netsuite-n/Account/200",
		)
		assert payment["allocations"][0]["kind"] == "purchase_invoice"

	def test_a_cash_sale_is_its_own_payment_and_a_deposit_an_advance(self, config: ConnectionConfig) -> None:
		sale = fields(config, Kind.PAYMENT_ENTRY, "605")
		assert sale["allocations"] == [
			{
				"kind": "sales_invoice",
				"document": "gid://netsuite/netsuite-n/Transaction/605",
				"amount": Decimal(23),
			}
		]
		refund = mapped(
			config,
			Kind.PAYMENT_ENTRY,
			changed(nodes(config, Kind.PAYMENT_ENTRY)["605"], record_type="cashRefund"),
		)
		assert refund is not None and refund.fields["payment_type"] == "Pay"
		deposit = fields(config, Kind.PAYMENT_ENTRY, "613")
		assert (deposit["amount"], deposit["allocations"], deposit["exchange_rate"]) == (
			Decimal(50),
			[],
			Decimal("1.35"),
		)

	def test_payments_that_cant_be_payment_entries(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.PAYMENT_ENTRY)["607"]
		with pytest.raises(Unsupported, match="pending approval"):
			mapped(config, Kind.PAYMENT_ENTRY, changed(node, posting="F"))
		credit = {"items": [{"apply": True, "amount": 18.9, "doc": {"id": "606"}}]}
		with pytest.raises(Unsupported, match="applies credits"):
			mapped(config, Kind.PAYMENT_ENTRY, changed(node, credit=credit))
		assert mapped(config, Kind.PAYMENT_ENTRY, changed(node, voided="T")) is None
		with pytest.raises(SourceError, match="has no customer"):
			mapped(config, Kind.PAYMENT_ENTRY, changed(node, customer=None))


class TestJournals:
	def test_a_journal_entry_line_by_line(self, config: ConnectionConfig) -> None:
		journal = fields(config, Kind.JOURNAL_ENTRY, "611")
		assert (journal["number"], journal["date"], journal["remarks"], journal["submitted"]) == (
			"JE-1101",
			"2026-09-30",
			"September rent",
			True,
		)
		assert [(line["debit"], line["credit"], line["party_type"]) for line in journal["lines"]] == [
			(Decimal(1500), Decimal(0), None),
			(Decimal(0), Decimal(1500), "Supplier"),
		]
		assert mapped(config, Kind.JOURNAL_ENTRY, nodes(config, Kind.JOURNAL_ENTRY, "v2")["611"]) is None

	def test_a_check_pays_its_expenses_out_of_its_bank_account(self, config: ConnectionConfig) -> None:
		check = fields(config, Kind.JOURNAL_ENTRY, "612")
		assert [
			(line["account"].rsplit("/", 1)[1], line["debit"], line["credit"]) for line in check["lines"]
		] == [
			("610", Decimal(42), Decimal(0)),
			("100", Decimal(0), Decimal(42)),
		]
		with pytest.raises(Unsupported, match="item lines or tax"):
			mapped(
				config, Kind.JOURNAL_ENTRY, changed(nodes(config, Kind.JOURNAL_ENTRY)["612"], taxTotal=3.0)
			)

	def test_journals_that_cant_be_journal_entries(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.JOURNAL_ENTRY)["611"]
		with pytest.raises(Unsupported, match="another currency"):
			mapped(config, Kind.JOURNAL_ENTRY, changed(node, exchangeRate=1.35))
		lines = editable(node, "line")
		unbalanced = lines["items"][:1]
		with pytest.raises(Unsupported, match="doesn't balance"):
			mapped(config, Kind.JOURNAL_ENTRY, changed(node, line={**lines, "items": unbalanced}))
		partner = {
			**lines["items"][1],
			"entity": {"id": "5", "links": [{"rel": "self", "href": "x/record/v1/partner/5"}]},
		}
		with pytest.raises(Unsupported, match="partner"):
			mapped(
				config,
				Kind.JOURNAL_ENTRY,
				changed(node, line={**lines, "items": [lines["items"][0], partner]}),
			)
