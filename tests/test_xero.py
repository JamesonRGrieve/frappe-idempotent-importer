# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Xero adapter over the recorded organisation (tests/fixtures/xero, proven against XeroAPI/Xero-OpenAPI):
OAuth, the tenant on every request and the organisation the approval names, paging, If-Modified-Since,
what the reader looks up for the mappers, and every mapper."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from idempotent_importer.core.errors import CredentialsRejected, SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.oauth import TokenGrant
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.xero.adapter import GRANT_PATH, XeroAdapter
from idempotent_importer.sources.xero.mapping import base_rate, xero_day
from idempotent_importer.sources.xero.reader import COLLECTIONS, XeroReader
from tests.support.transport import (
	OAUTH_ACCESS_TOKEN,
	FakeTransport,
	RecordingSleeper,
	oauth_source,
	scripted,
)

ADAPTER = XeroAdapter()
TENANT = "70784a63-d24b-46a9-a4db-0e70a274b056"


def guid(n: int) -> str:
	return f"00000000-0000-4000-8000-{n:012d}"


def gid(type_: str, n: int) -> str:
	return f"gid://xero/xero-x/{type_}/{guid(n)}"


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="xero",
		connection="xero-x",
		store_url="https://go.xero.com",
		api_user="e2e-xero-client",
		api_secret="e2e-xero-secret",
		account=TENANT,
		refresh_token="e2e-xero-refresh",
		save_refresh_token=lambda token: None,
		endpoint_override="http://xero-fake:8080",
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[XeroReader, FakeTransport]:
	fake = oauth_source("xero", state, GRANT_PATH)
	built = ADAPTER.reader(config, fake, RecordingSleeper())
	assert isinstance(built, XeroReader)
	return built, fake


def nodes(config: ConnectionConfig, kind: Kind, state: str = "v1") -> dict[str, Node]:
	source, _ = reader(config, state)
	found: dict[str, Node] = {}
	cursor: str | None = None
	while True:
		page = source.page(kind, cursor, None, "")
		found |= {node.id(): node for node in page.nodes}
		if not page.has_next_page:
			return found
		cursor = page.end_cursor


def values(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> Values:
	mapper = {m.kind: m for m in ADAPTER.mappers(config)}[kind]
	desired = mapper.map(nodes(config, kind, state)[key])
	assert desired is not None
	return desired.values()


def mapped(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> object:
	mapper = {m.kind: m for m in ADAPTER.mappers(config)}[kind]
	return mapper.map(nodes(config, kind, state)[key])


class TestOAuth:
	def test_the_approval_asks_for_granular_read_scopes(self, config: ConnectionConfig) -> None:
		url = ADAPTER.oauth(config, scripted()).authorization_url("https://erp/cb", "s", "v")
		assert url.startswith("https://login.xero.com/identity/connect/authorize?")
		assert (
			"offline_access" in url
			and "accounting.invoices.read" in url
			and "accounting.transactions" not in url
		)

	def test_every_request_names_the_tenant(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.ACCOUNT, None, None, "")
		api = fake.requests[1]
		assert api.headers["xero-tenant-id"] == TENANT
		assert api.headers["Authorization"] == f"Bearer {OAUTH_ACCESS_TOKEN}"

	def test_the_approval_names_the_organisation(self, config: ConnectionConfig) -> None:
		one = [{"id": "c1", "tenantId": "t1", "tenantType": "ORGANISATION", "tenantName": "Kiln"}]
		answer = scripted(HttpResponse(200, json.dumps(one).encode()))
		assert ADAPTER.connected(replace(config, account=""), answer, TokenGrant("a"), {}) == {
			"api_account": "t1"
		}
		assert answer.requests[0].url == "http://xero-fake:8080/Connections"
		several = [*one, {"id": "c2", "tenantId": "t2", "tenantType": "ORGANISATION", "tenantName": "Shop"}]
		listed = scripted(HttpResponse(200, json.dumps(several).encode()))
		assert ADAPTER.connected(replace(config, account="t2"), listed, TokenGrant("a"), {}) == {}
		with pytest.raises(SourceError, match="several organisations"):
			ADAPTER.connected(replace(config, account=""), listed, TokenGrant("a"), {})
		with pytest.raises(CredentialsRejected):
			ADAPTER.connected(config, scripted(HttpResponse(401, b"")), TokenGrant("a"), {})

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="client ID and secret"):
			ADAPTER.oauth(replace(config, api_user=""), scripted())
		with pytest.raises(SourceError, match="Connect to Xero"):
			ADAPTER.reader(replace(config, account=""), scripted(), RecordingSleeper())


class TestReader:
	def test_incremental_runs_send_if_modified_since(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		assert source.is_incremental(Kind.SALES_INVOICE) and not source.is_incremental(Kind.COST_CENTER)
		source.page(Kind.SALES_INVOICE, None, datetime(2026, 10, 1, 8, tzinfo=UTC), "")
		invoices = next(r for r in fake.requests if "/Invoices" in r.url)
		assert invoices.headers["If-Modified-Since"] == "2026-10-01T08:00:00"
		accounts = next(r for r in fake.requests if r.url.endswith("/Accounts"))
		assert "If-Modified-Since" not in accounts.headers

	def test_kinds_read_their_collections_in_turn(self, config: ConnectionConfig) -> None:
		sales = nodes(config, Kind.SALES_INVOICE)
		assert sorted(sales) == sorted([gid("Invoice", 401), gid("Invoice", 402), gid("CreditNote", 501)])
		assert sorted(nodes(config, Kind.PURCHASE_INVOICE)) == [gid("Invoice", 451)]
		assert set(nodes(config, Kind.CUSTOMER)) == {
			gid("Contact", 101),
			gid("Contact", 102),
			gid("Contact", 104),
		}
		assert set(nodes(config, Kind.SUPPLIER)) == {gid("Contact", 103), gid("Contact", 104)}
		# The bank transaction of a prepayment is the prepayment (a payment), not a journal.
		assert gid("BankTransaction", 902) not in nodes(config, Kind.JOURNAL_ENTRY)
		assert set(nodes(config, Kind.COST_CENTER)) == {
			gid("TrackingCategory", 301),
			gid("TrackingOption", 302),
			gid("TrackingOption", 303),
		}

	def test_lines_get_their_account_ids_and_advances_their_bank(self, config: ConnectionConfig) -> None:
		invoice = nodes(config, Kind.SALES_INVOICE)[gid("Invoice", 401)]
		assert invoice.children("LineItems")[0].string("AccountID") == guid(2)
		spend = nodes(config, Kind.JOURNAL_ENTRY)[gid("BankTransaction", 901)]
		assert spend.string("TaxAccountID") == guid(4)
		prepayment = nodes(config, Kind.PAYMENT_ENTRY)[gid("Prepayment", 701)]
		assert prepayment.required_child("BankAccount").string("AccountID") == guid(1)

	def test_paging_follows_the_page_count(self, config: ConnectionConfig) -> None:
		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if method == "POST":
				return HttpResponse(200, b'{"access_token": "t", "refresh_token": "r"}')
			page = {
				"pagination": {"page": 1, "pageSize": 1000, "pageCount": 2, "itemCount": 1001},
				"ManualJournals": [],
			}
			return HttpResponse(200, json.dumps(page).encode())

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper())
		page = source.page(Kind.JOURNAL_ENTRY, None, None, "")
		assert page.has_next_page and page.end_cursor == "0:2"

	def test_one_object_by_its_identity(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		with pytest.raises(SourceError, match="can't read"):
			source.node(Kind.SALES_INVOICE, gid("Payment", 601))

	def test_every_kind_has_a_mapper(self, config: ConnectionConfig) -> None:
		assert {mapper.kind for mapper in ADAPTER.mappers(config)} == set(COLLECTIONS)


class TestMappers:
	def test_dates_and_rates(self) -> None:
		assert xero_day("/Date(1739145600000+0000)/") == "2025-02-10"
		assert xero_day("2026-02-10T00:00:00") == "2026-02-10"
		assert base_rate(Node({"CurrencyRate": 0.8})) == Decimal("1.250000000")
		assert base_rate(Node({"CurrencyRate": 1})) is None

	def test_accounts_and_tracking(self, config: ConnectionConfig) -> None:
		assert values(config, Kind.ACCOUNT, gid("Account", 5)).optional_string("account_type") == "Receivable"
		assert values(config, Kind.ACCOUNT, gid("Account", 2)).string("root_type") == "Income"
		region = values(config, Kind.COST_CENTER, gid("TrackingCategory", 301))
		assert region.flag("is_group")
		north = values(config, Kind.COST_CENTER, gid("TrackingOption", 302))
		assert north.optional_string("parent") == gid("TrackingCategory", 301)

	def test_items_and_contacts(self, config: ConnectionConfig) -> None:
		mug = values(config, Kind.PRODUCT, gid("Item", 201))
		assert (mug.string("sku"), mug.decimal("price"), mug.decimal("stock_qty")) == (
			"MUG-01",
			Decimal(18),
			Decimal(24),
		)
		riley = values(config, Kind.CUSTOMER, gid("Contact", 102))
		assert (
			riley.string("type") == "Individual"
			and riley.records("addresses")[0].string("country_code") == "CA"
		)
		bakery = values(config, Kind.CUSTOMER, gid("Contact", 101))
		assert bakery.string("type") == "Company"
		assert values(config, Kind.CUSTOMER, gid("Contact", 101), "v2").optional_string("email") == (
			"accounts@harbourbakery.example"
		)

	def test_invoices_and_credit_notes(self, config: ConnectionConfig) -> None:
		invoice = values(config, Kind.SALES_INVOICE, gid("Invoice", 401))
		lines = invoice.records("lines")
		assert [
			(row.optional_string("product"), row.decimal("qty"), row.decimal("rate")) for row in lines
		] == [
			(gid("Item", 201), Decimal(10), Decimal(18)),
			(gid("Item", 202), Decimal(2), Decimal(60)),
		]
		assert lines[0].optional_string("cost_center") == gid("TrackingOption", 302)
		assert lines[0].optional_string("account") == gid("Account", 2)
		assert [t.decimal("amount") for t in invoice.records("taxes")] == [Decimal(15)]
		assert invoice.decimal("total") == Decimal(315) and invoice.string("date") == "2026-02-10"
		credit = values(config, Kind.SALES_INVOICE, gid("CreditNote", 501))
		assert credit.flag("is_return") and credit.decimal("total") == Decimal("-18.9")
		assert mapped(config, Kind.SALES_INVOICE, gid("Invoice", 402), "v2") is None

	def test_inclusive_lines_are_net_of_tax(self, config: ConnectionConfig) -> None:
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.PURCHASE_INVOICE]
		node = Node(
			{
				"id": gid("Invoice", 9),
				"Type": "ACCPAY",
				"Status": "AUTHORISED",
				"InvoiceNumber": "X",
				"Date": "/Date(1739145600000+0000)/",
				"Contact": {"ContactID": guid(103)},
				"CurrencyCode": "CAD",
				"LineAmountTypes": "Inclusive",
				"LineItems": [
					{
						"Quantity": 1,
						"UnitAmount": 105,
						"LineAmount": 105,
						"TaxAmount": 5,
						"AccountID": guid(3),
						"TaxType": "INPUT",
					}
				],
				"Total": 105,
			}
		)
		desired = mapper.map(node)
		assert desired is not None
		assert desired.values().records("lines")[0].decimal("rate") == Decimal(100)

	def test_payments_and_advances(self, config: ConnectionConfig) -> None:
		payment = values(config, Kind.PAYMENT_ENTRY, gid("Payment", 601))
		assert (payment.string("payment_type"), payment.optional_string("account")) == (
			"Receive",
			gid("Account", 1),
		)
		assert payment.records("allocations")[0].string("document") == gid("Invoice", 401)
		paid = values(config, Kind.PAYMENT_ENTRY, gid("Payment", 602))
		assert (paid.string("payment_type"), paid.string("party_type")) == ("Pay", "Supplier")
		deposit = values(config, Kind.PAYMENT_ENTRY, gid("Prepayment", 701))
		assert deposit.decimal("amount") == Decimal(50) and deposit.optional_string("account") == gid(
			"Account", 1
		)
		assert [a.decimal("amount") for a in deposit.records("allocations")] == [Decimal("37.8")]
		assert values(config, Kind.PAYMENT_ENTRY, gid("Prepayment", 701), "v2").records("allocations") == []

	def test_journals(self, config: ConnectionConfig) -> None:
		def rows(key: str) -> list[tuple[str, Decimal, Decimal]]:
			journal = values(config, Kind.JOURNAL_ENTRY, key)
			return [
				(row.string("account"), row.decimal("debit"), row.decimal("credit"))
				for row in journal.records("lines")
			]

		assert rows(gid("ManualJournal", 801)) == [
			(gid("Account", 3), Decimal(400), Decimal(0)),
			(gid("Account", 8), Decimal(0), Decimal(400)),
		]
		assert rows(gid("BankTransaction", 901)) == [
			(gid("Account", 7), Decimal(0), Decimal(42)),
			(gid("Account", 3), Decimal(40), Decimal(0)),
			(gid("Account", 4), Decimal(2), Decimal(0)),
		]
		assert rows(gid("BankTransfer", 951)) == [
			(gid("Account", 7), Decimal(250), Decimal(0)),
			(gid("Account", 1), Decimal(0), Decimal(250)),
		]
		assert gid("ManualJournal", 801) not in nodes(config, Kind.JOURNAL_ENTRY, "v2")

	def test_orders_quotes_and_projects(self, config: ConnectionConfig) -> None:
		order = values(config, Kind.PURCHASE_ORDER, gid("PurchaseOrder", 1101))
		assert order.decimal("total") == Decimal(525) and order.optional_string("due_date") == "2026-02-18"
		quote = values(config, Kind.DRAFT_ORDER, gid("Quote", 1001))
		assert quote.decimal("total") == Decimal(756) and quote.string("created_at") == "2026-02-05T12:00:00Z"
		project = values(config, Kind.PROJECT, gid("Project", 1201))
		assert (
			project.optional_string("customer") == gid("Contact", 101)
			and project.optional_string("end") == "2026-04-30"
		)

	def test_a_second_tracking_category_is_planned(self, config: ConnectionConfig) -> None:
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.SALES_INVOICE]
		node = nodes(config, Kind.SALES_INVOICE)[gid("Invoice", 402)]
		line = node.children("LineItems")[0].raw() | {
			"Tracking": [
				{"TrackingOptionID": guid(302), "Option": "North"},
				{"TrackingOptionID": guid(999), "Option": "Online"},
			]
		}
		desired = mapper.map(node.with_fields(LineItems=[line]))
		assert desired is not None and "Online" in desired.warnings[0]

	def test_unknown_payment_types_are_planned(self, config: ConnectionConfig) -> None:
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.PAYMENT_ENTRY]
		node = nodes(config, Kind.PAYMENT_ENTRY)[gid("Payment", 601)].with_fields(
			PaymentType="ARPREPAYMENTPAYMENT"
		)
		with pytest.raises(Unsupported, match="planned"):
			mapper.map(node)
