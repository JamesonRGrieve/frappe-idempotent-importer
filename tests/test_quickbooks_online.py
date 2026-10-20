# SPDX-License-Identifier: AGPL-3.0-or-later
"""The QuickBooks Online adapter over the recorded company (tests/fixtures/quickbooks_online, proven against
Intuit's published XML Schemas): OAuth and the company the approval names, the query reader's paging,
incremental condition and name lists, and every mapper."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.oauth import TokenGrant
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.quickbooks_online.adapter import GRANT_PATH, QuickBooksAdapter
from idempotent_importer.sources.quickbooks_online.reader import (
	ENTITIES,
	PAGE_SIZE,
	Entity,
	QuickBooksReader,
	api_base,
	query_text,
)
from tests.support.transport import (
	OAUTH_ACCESS_TOKEN,
	OAUTH_ROTATED_REFRESH_TOKEN,
	FakeTransport,
	RecordingSleeper,
	oauth_source,
	scripted,
)

ADAPTER = QuickBooksAdapter()
REALM = "9341452345"


@pytest.fixture
def saved() -> list[str]:
	return []


@pytest.fixture
def config(saved: list[str]) -> ConnectionConfig:
	return ConnectionConfig(
		source="quickbooks_online",
		connection="quickbooks-q",
		store_url="https://quickbooks.api.intuit.com",
		api_user="e2e-intuit-client",
		api_secret="e2e-intuit-secret",
		account=REALM,
		refresh_token="e2e-intuit-refresh",
		save_refresh_token=saved.append,
		endpoint_override="http://quickbooks_online-fake:8080",
	)


def transport(state: str = "v1") -> FakeTransport:
	return oauth_source("quickbooks_online", state, GRANT_PATH)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[QuickBooksReader, FakeTransport]:
	fake = transport(state)
	built = ADAPTER.reader(config, fake, RecordingSleeper())
	assert isinstance(built, QuickBooksReader)
	return built, fake


def nodes(config: ConnectionConfig, kind: Kind, state: str = "v1") -> dict[str, Node]:
	source, _ = reader(config, state)
	found: dict[str, Node] = {}
	cursor: str | None = None
	while True:
		page = source.page(kind, cursor, None, "")
		found |= {node.id().split("/", 4)[-1]: node for node in page.nodes}
		if not page.has_next_page:
			return found
		cursor = page.end_cursor


def values(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> Values:
	mapper = {m.kind: m for m in ADAPTER.mappers(config)}[kind]
	desired = mapper.map(nodes(config, kind, state)[key])
	assert desired is not None
	return desired.values()


class TestOAuth:
	def test_the_approval_asks_for_the_accounting_scope(self, config: ConnectionConfig) -> None:
		url = ADAPTER.oauth(config, scripted()).authorization_url("https://erp/cb", "s", "v")
		query = parse_qs(urlsplit(url).query)
		assert url.startswith("https://appcenter.intuit.com/connect/oauth2?")
		assert query["scope"] == ["com.intuit.quickbooks.accounting"]
		assert query["client_id"] == ["e2e-intuit-client"]

	def test_refreshes_authenticate_with_http_basic_and_keep_the_new_token(
		self, config: ConnectionConfig, saved: list[str]
	) -> None:
		source, fake = reader(config)
		source.page(Kind.ACCOUNT, None, None, "")
		grant = fake.requests[0]
		assert grant.url == "http://quickbooks_online-fake:8080" + GRANT_PATH
		assert (
			grant.headers["Authorization"]
			== "Basic " + base64.b64encode(b"e2e-intuit-client:e2e-intuit-secret").decode()
		)
		assert saved == [OAUTH_ROTATED_REFRESH_TOKEN]
		assert fake.requests[1].headers["Authorization"] == f"Bearer {OAUTH_ACCESS_TOKEN}"

	def test_the_approval_names_the_company(self, config: ConnectionConfig) -> None:
		grant = TokenGrant("token")
		assert ADAPTER.connected(config, scripted(), grant, {"realmId": "4620816365"}) == {
			"api_account": "4620816365"
		}
		with pytest.raises(SourceError, match="no company"):
			ADAPTER.connected(config, scripted(), grant, {})

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="client ID and secret"):
			ADAPTER.oauth(replace(config, api_secret=""), scripted())
		with pytest.raises(SourceError, match="Connect to QuickBooks Online"):
			ADAPTER.reader(replace(config, account=""), scripted(), RecordingSleeper())
		assert api_base(replace(config, endpoint_override=None)) == "https://quickbooks.api.intuit.com"
		sandbox = replace(
			config, endpoint_override=None, store_url="https://sandbox-quickbooks.api.intuit.com"
		)
		assert api_base(sandbox) == "https://sandbox-quickbooks.api.intuit.com"
		with pytest.raises(SourceError, match="Store URL"):
			api_base(replace(config, endpoint_override=None, store_url="https://qbo.intuit.com"))


class TestReader:
	def test_queries_read_name_lists_whole_and_in_creation_order(self) -> None:
		assert PAGE_SIZE == 1000
		assert query_text(Entity("Customer", True), None, 1) == (
			"SELECT * FROM Customer WHERE Active IN (true, false) ORDERBY MetaData.CreateTime "
			"STARTPOSITION 1 MAXRESULTS 1000"
		)
		since = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
		assert query_text(Entity("Invoice"), since, 1001) == (
			"SELECT * FROM Invoice WHERE MetaData.LastUpdatedTime >= '2026-10-01T09:30:00+00:00' "
			"ORDERBY MetaData.CreateTime STARTPOSITION 1001 MAXRESULTS 1000"
		)

	def test_incremental_runs_ask_for_what_changed(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		assert source.is_incremental(Kind.SALES_INVOICE)
		source.page(Kind.SALES_INVOICE, None, datetime(2026, 10, 1, tzinfo=UTC), "")
		sent = parse_qs(urlsplit(fake.requests[-1].url).query)
		assert "MetaData.LastUpdatedTime >= '2026-10-01T00:00:00+00:00'" in sent["query"][0]
		assert sent["minorversion"] == ["75"]

	def test_a_kind_reads_each_of_its_entities_in_turn(self, config: ConnectionConfig) -> None:
		sales = nodes(config, Kind.SALES_INVOICE)
		assert sorted(sales) == [
			"CreditMemo/145",
			"Invoice/130",
			"Invoice/131",
			"RefundReceipt/146",
			"SalesReceipt/140",
		]
		payments = nodes(config, Kind.PAYMENT_ENTRY)
		assert sorted(payments) == [
			"BillPayment/170",
			"Payment/150",
			"RefundReceiptPayment/146",
			"SalesReceiptPayment/140",
		]
		assert sales["Invoice/130"].id() == "gid://quickbooks_online/quickbooks-q/Invoice/130"

	def test_categories_projects_and_their_lists_are_told_apart(self, config: ConnectionConfig) -> None:
		assert sorted(nodes(config, Kind.COLLECTION)) == ["Item/1"]
		assert sorted(nodes(config, Kind.PRODUCT)) == ["Item/2", "Item/3", "Item/4"]
		assert sorted(nodes(config, Kind.CUSTOMER)) == ["Customer/1", "Customer/2"]
		assert sorted(nodes(config, Kind.PROJECT)) == ["Customer/3"]

	def test_a_full_page_reads_the_next(self, config: ConnectionConfig) -> None:
		classes = [{"Id": str(n), "Name": f"Class {n}"} for n in range(PAGE_SIZE)]
		full = HttpResponse(200, json.dumps({"QueryResponse": {"Class": classes}}).encode())
		fake = FakeTransport(lambda method, url, body, headers: _grant() if method == "POST" else full)
		source = ADAPTER.reader(config, fake, RecordingSleeper())
		page = source.page(Kind.COST_CENTER, None, None, "")
		assert page.has_next_page and page.end_cursor == f"0:{PAGE_SIZE + 1}"

	def test_one_object_by_its_identity(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		invoice = source.node(Kind.SALES_INVOICE, "gid://quickbooks_online/quickbooks-q/Invoice/130")
		assert invoice is not None and invoice.string("DocNumber") == "1001"
		receipt = source.node(
			Kind.PAYMENT_ENTRY, "gid://quickbooks_online/quickbooks-q/SalesReceiptPayment/140"
		)
		assert receipt is not None and receipt.string("Entity") == "SalesReceiptPayment"
		assert source.node(Kind.SALES_INVOICE, "gid://quickbooks_online/quickbooks-q/Invoice/999") is None
		with pytest.raises(SourceError, match="can't read"):
			source.node(Kind.SALES_INVOICE, "gid://quickbooks_online/quickbooks-q/Bill/160")

	def test_throttling_pauses_and_rejection_stops(self, config: ConnectionConfig) -> None:
		throttled = FakeTransport(
			lambda method, url, b, h: _grant() if method == "POST" else HttpResponse(429, b"")
		)
		with pytest.raises(RetryLater):
			ADAPTER.reader(config, throttled, RecordingSleeper()).page(Kind.ACCOUNT, None, None, "")
		refused = FakeTransport(
			lambda method, url, b, h: _grant() if method == "POST" else HttpResponse(403, b"")
		)
		with pytest.raises(CredentialsRejected):
			ADAPTER.reader(config, refused, RecordingSleeper()).page(Kind.ACCOUNT, None, None, "")

	def test_every_kind_has_a_mapper(self, config: ConnectionConfig) -> None:
		assert {mapper.kind for mapper in ADAPTER.mappers(config)} == set(ENTITIES)
		assert ADAPTER.kinds()[0] is Kind.ACCOUNT


def _grant() -> HttpResponse:
	return HttpResponse(200, b'{"access_token": "t", "refresh_token": "r"}')


def gid(entity: str, id_: str) -> str:
	return f"gid://quickbooks_online/quickbooks-q/{entity}/{id_}"


class TestMappers:
	def test_accounts(self, config: ConnectionConfig) -> None:
		bank = values(config, Kind.ACCOUNT, "Account/35")
		assert (bank.string("name"), bank.string("root_type"), bank.optional_string("account_type")) == (
			"Chequing",
			"Asset",
			"Bank",
		)
		assert values(config, Kind.ACCOUNT, "Account/89").optional_string("account_type") == "Tax"
		assert values(config, Kind.ACCOUNT, "Account/79").string("root_type") == "Income"
		postage = values(config, Kind.ACCOUNT, "Account/16")
		assert postage.optional_string("parent") is None and postage.optional_string("number") == "6010"
		assert values(config, Kind.ACCOUNT, "Account/16", "v2").string("name") == "Postage and Courier"

	def test_classes_and_departments_are_cost_centers(self, config: ConnectionConfig) -> None:
		assert values(config, Kind.COST_CENTER, "Class/5000000000000012345").string("name") == "Retail"
		assert values(config, Kind.COST_CENTER, "Department/1").string("name") == "Toronto"

	def test_items_and_categories(self, config: ConnectionConfig) -> None:
		assert values(config, Kind.COLLECTION, "Item/1").string("name") == "Hardware"
		shelf = values(config, Kind.PRODUCT, "Item/2")
		assert shelf.string("sku") == "OAK-1" and shelf.decimal("price") == Decimal(120)
		assert shelf.flag("is_stock_item") and shelf.decimal("stock_qty") == Decimal(12)
		assert shelf.strings("collections") == [gid("Item", "1")] and not shelf.flag("published")
		assert not values(config, Kind.PRODUCT, "Item/3").flag("is_stock_item")
		assert values(config, Kind.PRODUCT, "Item/2", "v2").decimal("price") == Decimal(125)

	def test_a_bundle_is_planned(self, config: ConnectionConfig) -> None:
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.PRODUCT]
		bundle = Node({"id": gid("Item", "9"), "Name": "Kit", "Type": "Group", "Active": True})
		with pytest.raises(Unsupported, match="Product Bundle"):
			mapper.map(bundle)
		assert (
			mapper.map(Node({"id": gid("Item", "9"), "Name": "Old", "Type": "Service", "Active": False}))
			is None
		)

	def test_parties(self, config: ConnectionConfig) -> None:
		cafe = values(config, Kind.CUSTOMER, "Customer/1")
		assert (cafe.string("name"), cafe.string("type"), cafe.optional_string("company")) == (
			"Birch Cafe",
			"Company",
			"Birch Cafe Inc.",
		)
		addresses = cafe.records("addresses")
		assert len(addresses) == 1 and addresses[0].string("country_code") == "CA"
		dana = values(config, Kind.CUSTOMER, "Customer/2")
		assert dana.string("type") == "Individual" and dana.optional_string("email") == "dana.lee@example.com"
		assert (
			values(config, Kind.CUSTOMER, "Customer/2", "v2").optional_string("email") == "dana@lee.example"
		)
		lumber = values(config, Kind.SUPPLIER, "Vendor/10")
		assert lumber.optional_string("tax_id") == "123456789RT0001"

	def test_employees_and_projects(self, config: ConnectionConfig) -> None:
		sam = values(config, Kind.EMPLOYEE, "Employee/20")
		assert (sam.string("first_name"), sam.optional_string("gender"), sam.string("status")) == (
			"Sam",
			"Male",
			"Active",
		)
		assert sam.optional_string("date_of_joining") == "2026-01-05"
		fit_out = values(config, Kind.PROJECT, "Customer/3")
		assert (
			fit_out.optional_string("customer") == gid("Customer", "1") and fit_out.string("status") == "Open"
		)

	def test_an_invoice_books_its_lines_and_taxes_to_its_total(self, config: ConnectionConfig) -> None:
		invoice = values(config, Kind.SALES_INVOICE, "Invoice/130")
		assert invoice.string("number") == "1001" and invoice.string("date") == "2026-02-10"
		assert invoice.string("party") == gid("Customer", "1")
		lines = invoice.records("lines")
		assert [
			(row.optional_string("product"), row.decimal("qty"), row.decimal("rate")) for row in lines
		] == [
			(gid("Item", "2"), Decimal(2), Decimal(120)),
			(gid("Item", "3"), Decimal(1), Decimal(85)),
		]
		assert lines[0].optional_string("cost_center") == gid("Class", "5000000000000012345")
		assert lines[0].optional_string("project") == gid("Customer", "3")
		assert [t.decimal("amount") for t in invoice.records("taxes")] == [Decimal("42.25")]
		assert invoice.decimal("total") == Decimal("367.25") and invoice.flag("submitted")

	def test_a_discount_and_shipping_are_the_documents_own(self, config: ConnectionConfig) -> None:
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.SALES_INVOICE]
		invoice = nodes(config, Kind.SALES_INVOICE)["Invoice/130"]
		lines: list[JsonValue] = [
			*(line.raw() for line in invoice.children("Line")),
			{"Amount": 25, "DetailType": "DiscountLineDetail", "DiscountLineDetail": {"PercentBased": False}},
			{
				"Amount": 15,
				"DetailType": "SalesItemLineDetail",
				"SalesItemLineDetail": {"ItemRef": {"value": "SHIPPING_ITEM_ID"}},
			},
		]
		desired = mapper.map(invoice.with_fields(Line=lines, TotalAmt=357.25))
		assert desired is not None
		fields = desired.values()
		assert fields.decimal("discount") == Decimal(25)
		shipping = fields.records("taxes")[-1]
		assert (shipping.string("role"), shipping.decimal("amount")) == ("shipping", Decimal(15))
		assert len(fields.records("lines")) == 2

	def test_returns_are_negative(self, config: ConnectionConfig) -> None:
		credit = values(config, Kind.SALES_INVOICE, "CreditMemo/145")
		assert credit.flag("is_return") and credit.decimal("total") == Decimal("-96.05")
		assert credit.records("lines")[0].decimal("qty") == Decimal(-1)
		vendor_credit = values(config, Kind.PURCHASE_INVOICE, "VendorCredit/165")
		assert vendor_credit.flag("is_return") and vendor_credit.decimal("total") == Decimal(-20)

	def test_a_bill_books_items_and_expenses(self, config: ConnectionConfig) -> None:
		bill = values(config, Kind.PURCHASE_INVOICE, "Bill/160")
		lines = bill.records("lines")
		assert lines[0].optional_string("product") == gid("Item", "2")
		assert lines[1].optional_string("product") is None and lines[1].optional_string("account") == gid(
			"Account", "16"
		)
		assert bill.decimal("total") == Decimal(728) and bill.optional_string("reference") == "NL-7781"
		assert values(config, Kind.PURCHASE_ORDER, "PurchaseOrder/50").decimal("total") == Decimal(678)

	def test_payments_settle_what_they_name(self, config: ConnectionConfig) -> None:
		payment = values(config, Kind.PAYMENT_ENTRY, "Payment/150")
		assert (payment.string("payment_type"), payment.string("party_type")) == ("Receive", "Customer")
		assert payment.optional_string("account") == gid("Account", "35")
		assert [
			(a.string("kind"), a.string("document"), a.decimal("amount"))
			for a in payment.records("allocations")
		] == [("sales_invoice", gid("Invoice", "130"), Decimal("367.25"))]
		bill = values(config, Kind.PAYMENT_ENTRY, "BillPayment/170")
		assert (bill.string("payment_type"), bill.string("party_type")) == ("Pay", "Supplier")
		assert bill.records("allocations")[0].string("document") == gid("Bill", "160")
		receipt = values(config, Kind.PAYMENT_ENTRY, "SalesReceiptPayment/140")
		assert receipt.records("allocations")[0].string("document") == gid("SalesReceipt", "140")
		refund = values(config, Kind.PAYMENT_ENTRY, "RefundReceiptPayment/146")
		assert refund.string("payment_type") == "Pay" and refund.decimal("amount") == Decimal("10.17")

	def test_applying_a_credit_is_planned(self, config: ConnectionConfig) -> None:
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.PAYMENT_ENTRY]
		node = nodes(config, Kind.PAYMENT_ENTRY)["Payment/150"].with_fields(
			Line=[{"Amount": 10, "LinkedTxn": [{"TxnId": "145", "TxnType": "CreditMemo"}]}]
		)
		with pytest.raises(Unsupported, match="CreditMemo"):
			mapper.map(node)

	def test_money_movements_are_balanced_journals(self, config: ConnectionConfig) -> None:
		def debits(key: str) -> list[tuple[str, Decimal, Decimal]]:
			journal = values(config, Kind.JOURNAL_ENTRY, key)
			return [
				(row.string("account"), row.decimal("debit"), row.decimal("credit"))
				for row in journal.records("lines")
			]

		assert debits("JournalEntry/180") == [
			(gid("Account", "16"), Decimal(30), Decimal(0)),
			(gid("Account", "15"), Decimal(0), Decimal(30)),
		]
		assert debits("Deposit/190") == [
			(gid("Account", "35"), Decimal(100), Decimal(0)),
			(gid("Account", "1"), Decimal(0), Decimal(100)),
		]
		assert debits("Transfer/195") == [
			(gid("Account", "41"), Decimal(200), Decimal(0)),
			(gid("Account", "35"), Decimal(0), Decimal(200)),
		]
		assert debits("Purchase/200") == [
			(gid("Account", "41"), Decimal(0), Decimal("45.2")),
			(gid("Account", "15"), Decimal("45.2"), Decimal(0)),
		]
		assert (
			values(config, Kind.JOURNAL_ENTRY, "JournalEntry/180", "v2").records("lines")[0].decimal("debit")
			== 35
		)

	def test_an_estimate_is_a_draft_quotation(self, config: ConnectionConfig) -> None:
		estimate = values(config, Kind.DRAFT_ORDER, "Estimate/40")
		assert estimate.string("created_at") == "2026-02-01T12:00:00Z"
		assert estimate.records("lines")[0].decimal("qty") == Decimal(4)
		assert estimate.decimal("total") == Decimal("542.4")
		assert [c.decimal("amount") for c in estimate.records("charges")] == [Decimal("62.4")]

	def test_the_second_state_drops_what_was_deleted(self, config: ConnectionConfig) -> None:
		assert "Invoice/131" not in nodes(config, Kind.SALES_INVOICE, "v2")
		assert "Transfer/195" not in nodes(config, Kind.JOURNAL_ENTRY, "v2")
		assert values(config, Kind.SALES_INVOICE, "Invoice/132", "v2").decimal("total") == Decimal("192.1")
