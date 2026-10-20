# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Zoho Books and Zoho Inventory adapters over their recorded organisations (tests/fixtures/zoho_books and
zoho_inventory, proven against Zoho's published OpenAPI modules): the data centre, OAuth and the
organisation the approval opens, paging and documents read whole, the lookups each product's reader makes,
and every mapper."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.oauth import TokenGrant
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.zoho import client
from idempotent_importer.sources.zoho.adapter import ZohoAdapter
from idempotent_importer.sources.zoho.client import GRANT_PATH, every, one
from idempotent_importer.sources.zoho_books.adapter import adapter as books_adapter
from idempotent_importer.sources.zoho_inventory.adapter import adapter as inventory_adapter
from tests.support.transport import (
	OAUTH_ACCESS_TOKEN,
	FakeTransport,
	RecordingSleeper,
	oauth_source,
	scripted,
)

BOOKS = books_adapter()
INVENTORY = inventory_adapter()


def config_of(source: str, account: str, store_url: str) -> ConnectionConfig:
	return ConnectionConfig(
		source=source,
		connection=f"{source}-z",
		store_url=store_url,
		api_user="e2e-zoho-client",
		api_secret="e2e-zoho-secret",
		account=account,
		refresh_token="e2e-zoho-refresh",
		save_refresh_token=lambda token: None,
		endpoint_override="http://zoho-fake:8080",
	)


BOOKS_CONFIG = config_of("zoho_books", "10234695", "https://books.zoho.com")
INVENTORY_CONFIG = config_of("zoho_inventory", "20000001", "https://inventory.zoho.com")


def reader(
	adapter: ZohoAdapter, config: ConnectionConfig, state: str = "v1"
) -> tuple[SourceReader, FakeTransport]:
	fake = oauth_source(adapter.id, state, GRANT_PATH, rotates=False)
	return adapter.reader(config, fake, RecordingSleeper()), fake


def nodes(adapter: ZohoAdapter, config: ConnectionConfig, kind: Kind, state: str = "v1") -> dict[str, Node]:
	source, _ = reader(adapter, config, state)
	found: dict[str, Node] = {}
	cursor: str | None = None
	while True:
		page = source.page(kind, cursor, None, "")
		found |= {node.id().split("/", 4)[-1]: node for node in page.nodes}
		if not page.has_next_page:
			return found
		cursor = page.end_cursor


def mapped(
	adapter: ZohoAdapter, config: ConnectionConfig, kind: Kind, key: str, state: str = "v1"
) -> Values | None:
	mapper = {m.kind: m for m in adapter.mappers(config)}[kind]
	desired = mapper.map(nodes(adapter, config, kind, state)[key])
	return None if desired is None else desired.values()


def values(adapter: ZohoAdapter, config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> Values:
	found = mapped(adapter, config, kind, key, state)
	assert found is not None
	return found


def books(kind: Kind, key: str, state: str = "v1") -> Values:
	return values(BOOKS, BOOKS_CONFIG, kind, key, state)


def inventory(kind: Kind, key: str, state: str = "v1") -> Values:
	return values(INVENTORY, INVENTORY_CONFIG, kind, key, state)


def gid(source: str, type_: str, id_: str) -> str:
	return f"gid://{source}/{source}-z/{type_}/{id_}"


class TestDataCentresAndOAuth:
	def test_the_store_url_names_the_data_centre(self) -> None:
		eu = replace(BOOKS_CONFIG, store_url="https://books.zoho.eu", endpoint_override=None)
		assert client.accounts_url(eu) == "https://accounts.zoho.eu"
		assert client.api_url(replace(eu, store_url="inventory.zoho.com.au")) == "https://www.zohoapis.com.au"
		with pytest.raises(SourceError, match="data centre"):
			client.data_centre(replace(eu, store_url="https://example.com"))

	def test_the_approval_asks_for_offline_read_access(self) -> None:
		url = BOOKS.oauth(replace(BOOKS_CONFIG, endpoint_override=None), scripted()).authorization_url(
			"https://erp/cb", "s", "v"
		)
		query = parse_qs(urlsplit(url).query)
		assert url.startswith("https://accounts.zoho.com/oauth/v2/auth?")
		assert query["access_type"] == ["offline"] and "ZohoBooks.invoices.READ" in query["scope"][0].split(
			","
		)

	def test_requests_carry_the_token_and_the_organisation(self) -> None:
		source, fake = reader(BOOKS, BOOKS_CONFIG)
		source.page(Kind.ACCOUNT, None, None, "")
		grant = parse_qs(fake.requests[0].body.decode())
		assert (grant["client_id"], grant["client_secret"]) == (["e2e-zoho-client"], ["e2e-zoho-secret"])
		api = fake.requests[1]
		assert api.headers["Authorization"] == f"Zoho-oauthtoken {OAUTH_ACCESS_TOKEN}"
		assert parse_qs(urlsplit(api.url).query)["organization_id"] == ["10234695"]

	def test_the_approval_opens_an_organisation(self) -> None:
		listed = scripted(
			HttpResponse(200, json.dumps({"organizations": [{"organization_id": "1", "name": "A"}]}).encode())
		)
		assert BOOKS.connected(replace(BOOKS_CONFIG, account=""), listed, TokenGrant("a"), {}) == {
			"api_account": "1"
		}
		assert listed.requests[0].url == "http://zoho-fake:8080/books/v3/organizations"
		elsewhere = replace(BOOKS_CONFIG, endpoint_override=None)
		with pytest.raises(SourceError, match="data centre"):
			BOOKS.connected(
				elsewhere, listed, TokenGrant("a"), {"accounts-server": "https://accounts.zoho.eu"}
			)

	def test_configuration(self) -> None:
		with pytest.raises(SourceError, match="client's ID and secret"):
			BOOKS.oauth(replace(BOOKS_CONFIG, api_secret=""), scripted())
		with pytest.raises(SourceError, match="Connect to Zoho Inventory"):
			INVENTORY.reader(replace(INVENTORY_CONFIG, account=""), scripted(), RecordingSleeper())


class TestReader:
	def test_one_or_a_list_of_one(self) -> None:
		node = Node({"a": {"x": 1}, "b": [{"x": 2}], "c": [{"x": 1}, {"x": 2}]})
		assert one(node, "a") is not None and one(node, "b") is not None and one(node, "z") is None
		assert len(every(node, "c")) == 2
		with pytest.raises(SourceError, match="one was expected"):
			one(node, "c")

	def test_documents_are_read_whole_and_kinds_in_turn(self) -> None:
		sales = nodes(BOOKS, BOOKS_CONFIG, Kind.SALES_INVOICE)
		assert sorted(sales) == ["Invoice/460000000001001", "SalesReceipt/460000000002001"]
		assert sales["Invoice/460000000001001"].children("line_items")[0].string("name") == "Desk Lamp"
		assert sorted(nodes(BOOKS, BOOKS_CONFIG, Kind.PAYMENT_ENTRY)) == [
			"CustomerPayment/460000000003001",
			"SalesReceiptPayment/460000000002001",
			"VendorPayment/460000000005001",
		]
		assert sorted(nodes(BOOKS, BOOKS_CONFIG, Kind.SUPPLIER)) == ["Contact/460000000000103"]

	def test_incremental_runs_ask_for_what_changed(self) -> None:
		source, fake = reader(BOOKS, BOOKS_CONFIG)
		assert source.is_incremental(Kind.SALES_INVOICE) and not source.is_incremental(Kind.CUSTOMER)
		source.page(Kind.PURCHASE_INVOICE, None, datetime(2026, 10, 1, tzinfo=UTC), "")
		listed = next(r for r in fake.requests if urlsplit(r.url).path == "/books/v3/bills")
		assert parse_qs(urlsplit(listed.url).query)["last_modified_time"] == ["2026-10-01T00:00:00+0000"]

	def test_a_full_page_without_a_page_context_reads_the_next(self) -> None:
		full = Node({"journals": [{"journal_id": str(n)} for n in range(client.PER_PAGE)]})
		assert client.more_pages(full, client.PER_PAGE)
		assert not client.more_pages(full, client.PER_PAGE - 1)
		said = Node({"page_context": [{"has_more_page": False}]})
		assert not client.more_pages(said, client.PER_PAGE)

	def test_a_page_reads_its_documents_whole(self) -> None:
		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if method == "POST":
				return HttpResponse(200, b'{"access_token": "t"}')
			if urlsplit(url).path.endswith("/journals"):
				return HttpResponse(200, json.dumps({"journals": [{"journal_id": "1"}]}).encode())
			return HttpResponse(
				200, json.dumps({"journal": {"journal_id": "1", "entry_number": "7"}}).encode()
			)

		source = BOOKS.reader(BOOKS_CONFIG, FakeTransport(route), RecordingSleeper())
		page = source.page(Kind.JOURNAL_ENTRY, None, None, "")
		assert [node.string("entry_number") for node in page.nodes] == ["7"]
		assert page.end_cursor == "1:"

	def test_an_expense_gets_its_paid_through_account(self) -> None:
		expense = nodes(BOOKS, BOOKS_CONFIG, Kind.JOURNAL_ENTRY)["Expense/460000000008001"]
		assert expense.string("paid_through_account_id") == "460000000000361"

	def test_inventory_lines_name_their_item_group(self) -> None:
		order = nodes(INVENTORY, INVENTORY_CONFIG, Kind.SALES_ORDER)["SalesOrder/2000000000005001"]
		lines = order.children("line_items")
		assert lines[0].string("group_id") == "2000000000000401" and not lines[1].has("group_id")
		assert sorted(nodes(INVENTORY, INVENTORY_CONFIG, Kind.PRODUCT)) == [
			"Item/2000000000000421",
			"ItemGroup/2000000000000401",
		]

	def test_one_object_by_its_identity(self) -> None:
		source, _ = reader(BOOKS, BOOKS_CONFIG)
		found = source.node(Kind.SALES_INVOICE, gid("zoho_books", "Invoice", "460000000001001"))
		assert found is not None and found.string("invoice_number") == "INV-000001"
		item = source.node(Kind.PRODUCT, gid("zoho_books", "Item", "460000000000501"))
		assert item is None  # Books' items are read from their list; this fixture has no read by id.
		with pytest.raises(SourceError, match="can't read"):
			source.node(Kind.SALES_INVOICE, gid("zoho_books", "Bill", "1"))

	def test_every_kind_has_a_mapper(self) -> None:
		for adapter, config in ((BOOKS, BOOKS_CONFIG), (INVENTORY, INVENTORY_CONFIG)):
			assert {mapper.kind for mapper in adapter.mappers(config)} == set(adapter.kinds())


class TestBooksMappers:
	def test_accounts_locations_and_items(self) -> None:
		bank = books(Kind.ACCOUNT, "Account/460000000000362")
		assert (bank.string("root_type"), bank.optional_string("account_type")) == ("Asset", "Bank")
		assert books(Kind.ACCOUNT, "Account/460000000000368").optional_string("parent") is None
		office = books(Kind.WAREHOUSE, "Location/460000000000901")
		assert (
			office.string("name") == "Head Office"
			and office.required_record("address").string("country_code") == "CA"
		)
		lamp = books(Kind.PRODUCT, "Item/460000000000501")
		assert (
			lamp.decimal("price") == Decimal(45)
			and books(Kind.PRODUCT, "Item/460000000000501", "v2").decimal("price") == 49
		)

	def test_contacts_and_projects(self) -> None:
		clinic = books(Kind.CUSTOMER, "Contact/460000000000101")
		assert (clinic.string("type"), clinic.optional_string("first_name")) == ("Company", "Priya")
		assert len(clinic.records("addresses")) == 2
		assert books(Kind.CUSTOMER, "Contact/460000000000102").string("type") == "Individual"
		assert books(Kind.PROJECT, "Project/460000000000701").optional_string("customer") == gid(
			"zoho_books", "Contact", "460000000000101"
		)

	def test_documents(self) -> None:
		invoice = books(Kind.SALES_INVOICE, "Invoice/460000000001001")
		assert [(row.decimal("qty"), row.decimal("rate")) for row in invoice.records("lines")] == [
			(Decimal(3), Decimal(45)),
			(Decimal(1), Decimal(90)),
		]
		assert (
			invoice.decimal("total") == Decimal("236.25") and invoice.optional_string("reference") == "PO 77"
		)
		bill = books(Kind.PURCHASE_INVOICE, "Bill/460000000004001")
		assert bill.records("lines")[0].optional_string("account") == gid(
			"zoho_books", "Account", "460000000000368"
		)
		assert bill.decimal("total") == Decimal(126)
		receipt = books(Kind.SALES_INVOICE, "SalesReceipt/460000000002001")
		assert [tax.decimal("amount") for tax in receipt.records("taxes")] == [Decimal("2.25")]
		assert receipt.decimal("total") == Decimal("47.25")
		assert books(Kind.PURCHASE_INVOICE, "Bill/460000000004002", "v2").string("number") == "NW-4502"
		estimate = books(Kind.DRAFT_ORDER, "Estimate/460000000006001")
		assert estimate.decimal("total") == Decimal("472.5")

	def test_payments(self) -> None:
		payment = books(Kind.PAYMENT_ENTRY, "CustomerPayment/460000000003001")
		assert payment.records("allocations")[0].string("document") == gid(
			"zoho_books", "Invoice", "460000000001001"
		)
		vendor = books(Kind.PAYMENT_ENTRY, "VendorPayment/460000000005001")
		assert (vendor.string("payment_type"), vendor.optional_string("account")) == (
			"Pay",
			gid("zoho_books", "Account", "460000000000362"),
		)
		receipt = books(Kind.PAYMENT_ENTRY, "SalesReceiptPayment/460000000002001")
		assert receipt.decimal("amount") == Decimal("47.25")

	def test_journals_and_expenses(self) -> None:
		journal = books(Kind.JOURNAL_ENTRY, "Journal/460000000007001")
		assert [(row.decimal("debit"), row.decimal("credit")) for row in journal.records("lines")] == [
			(Decimal(25), Decimal(0)),
			(Decimal(0), Decimal(25)),
		]
		assert (
			books(Kind.JOURNAL_ENTRY, "Journal/460000000007001", "v2").records("lines")[0].decimal("debit")
			== 40
		)
		expense = books(Kind.JOURNAL_ENTRY, "Expense/460000000008001")
		assert [row.string("account") for row in expense.records("lines")] == [
			gid("zoho_books", "Account", "460000000000367"),
			gid("zoho_books", "Account", "460000000000361"),
		]

	def test_a_discount_and_shipping_are_the_documents_own(self) -> None:
		mapper = {m.kind: m for m in BOOKS.mappers(BOOKS_CONFIG)}[Kind.SALES_INVOICE]
		invoice = nodes(BOOKS, BOOKS_CONFIG, Kind.SALES_INVOICE)["Invoice/460000000001001"]
		desired = mapper.map(invoice.with_fields(discount=10, shipping_charge="5.00", total="231.25"))
		assert desired is not None
		fields = desired.values()
		assert fields.decimal("discount") == Decimal(10)
		assert fields.records("taxes")[-1].string("role") == "shipping"

	def test_what_is_planned(self) -> None:
		mapper = {m.kind: m for m in BOOKS.mappers(BOOKS_CONFIG)}[Kind.SALES_INVOICE]
		invoice = nodes(BOOKS, BOOKS_CONFIG, Kind.SALES_INVOICE)["Invoice/460000000001001"]
		with pytest.raises(Unsupported, match="tax-inclusive"):
			mapper.map(invoice.with_fields(is_inclusive_tax=True))
		assert mapper.map(invoice.with_fields(status="void")) is None


class TestInventoryMappers:
	def test_item_groups_are_templates_with_variants(self) -> None:
		jacket = inventory(Kind.PRODUCT, "ItemGroup/2000000000000401")
		assert jacket.string("type") == "variable"
		variant = jacket.records("variants")[0]
		assert variant.required_record("attributes").all() == {"Size": "M"}
		assert variant.decimal("stock_qty") == Decimal(8)
		assert inventory(Kind.PRODUCT, "Item/2000000000000421", "v2").decimal("price") == Decimal(19)

	def test_categories_and_locations(self) -> None:
		assert inventory(Kind.COLLECTION, "Category/2000000000000301").string("name") == "Apparel"
		assert inventory(Kind.WAREHOUSE, "Location/2000000000000901").string("name") == "Main Warehouse"

	def test_orders_name_the_variant_they_sold(self) -> None:
		order = inventory(Kind.SALES_ORDER, "SalesOrder/2000000000005001")
		first = order.records("lines")[0]
		assert first.optional_string("product") == gid("zoho_inventory", "ItemGroup", "2000000000000401")
		assert first.optional_string("variant") == gid("zoho_inventory", "Item", "2000000000000411")
		assert order.decimal("total") == Decimal(693)
		assert (
			mapped(INVENTORY, INVENTORY_CONFIG, Kind.SALES_ORDER, "SalesOrder/2000000000005001", "v2") is None
		)

	def test_purchases_and_payments(self) -> None:
		order = inventory(Kind.PURCHASE_ORDER, "PurchaseOrder/2000000000008001")
		assert order.records("lines")[0].decimal("rate") == Decimal(6)
		bill = inventory(Kind.PURCHASE_INVOICE, "Bill/2000000000009001")
		assert bill.decimal("total") == Decimal(315)
		payment = inventory(Kind.PAYMENT_ENTRY, "CustomerPayment/2000000000007001")
		# Inventory reads no chart of accounts: the connection's deposit account and the items' accounts.
		assert payment.optional_string("account") is None
		assert bill.records("lines")[0].optional_string("account") is None
