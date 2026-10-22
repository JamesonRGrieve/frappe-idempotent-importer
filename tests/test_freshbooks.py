# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FreshBooks adapter over the recorded studio business (tests/fixtures/freshbooks, proven against
tests/schemas/freshbooks.json): OAuth (JSON token requests, rotating refresh tokens) and the business users/me
names, paging, includes and incremental filters, the ledger grouped into expenses and other income, and every
mapper."""

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
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.freshbooks.adapter import GRANT_PATH, FreshBooksAdapter
from idempotent_importer.sources.freshbooks.reader import FreshBooksReader
from tests.support.transport import (
	OAUTH_ROTATED_REFRESH_TOKEN,
	FakeTransport,
	RecordingSleeper,
	oauth_source,
	rest_source,
	scripted,
)

ADAPTER = FreshBooksAdapter()
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()
ACCOUNT = "Zv8pR3|240340|046cc001-0002-e93e-1db1-1186b2983879"


def gid(type_: str, id_: str | int) -> str:
	return f"gid://freshbooks/freshbooks-f/{type_}/{id_}"


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="freshbooks",
		connection="freshbooks-f",
		store_url="https://my.freshbooks.com",
		api_user="e2e-freshbooks-client",
		api_secret="e2e-freshbooks-secret",
		account=ACCOUNT,
		refresh_token="e2e-freshbooks-refresh",
		save_refresh_token=lambda token: None,
		site_url="https://erp.example",
		endpoint_override="http://freshbooks-fake:8080",
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[FreshBooksReader, FakeTransport]:
	fake = oauth_source("freshbooks", state, GRANT_PATH)
	built = ADAPTER.reader(config, fake, RecordingSleeper(NOW))
	assert isinstance(built, FreshBooksReader)
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


def mapped(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> Values | None:
	mapper = {m.kind: m for m in ADAPTER.mappers(config)}[kind]
	desired = mapper.map(nodes(config, kind, state)[key])
	return desired.values() if desired else None


def values(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> Values:
	result = mapped(config, kind, key, state)
	assert result is not None
	return result


class TestOAuth:
	def test_token_requests_are_json_with_the_redirect_and_the_new_refresh_token_is_kept(
		self, config: ConnectionConfig
	) -> None:
		kept: list[str] = []
		source, fake = reader(replace(config, save_refresh_token=kept.append))
		source.page(Kind.TAX_RATE, None, None, "")
		grant = json.loads(next(r for r in fake.requests if r.method == "POST").body)
		assert grant["grant_type"] == "refresh_token"
		assert (
			grant["redirect_uri"] == "https://erp.example/api/method/idempotent_importer.api.oauth_callback"
		)
		assert kept == [OAUTH_ROTATED_REFRESH_TOKEN]

	def test_the_business_users_me_names_becomes_the_account(self, config: ConnectionConfig) -> None:
		fake = rest_source("freshbooks", "v1")
		assert ADAPTER.connected(replace(config, account=""), fake, TokenGrant("a"), {}) == {
			"api_account": ACCOUNT
		}
		assert ADAPTER.connected(config, fake, TokenGrant("a"), {}) == {}
		none = scripted(HttpResponse(200, b'{"response": {"business_memberships": []}}'))
		with pytest.raises(SourceError, match="several businesses or none"):
			ADAPTER.connected(replace(config, account=""), none, TokenGrant("a"), {})

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="Connect to FreshBooks"):
			ADAPTER.reader(replace(config, account="Zv8pR3"), scripted(), RecordingSleeper(NOW))
		with pytest.raises(SourceError, match="client ID and secret"):
			ADAPTER.oauth(replace(config, api_secret=""), scripted())


class TestReader:
	def test_accounting_lists_page_and_filter_by_the_day_changed(self, config: ConnectionConfig) -> None:
		urls: list[str] = []

		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if method == "POST":
				return HttpResponse(200, b'{"access_token": "t", "refresh_token": "r"}')
			urls.append(url)
			page = int(parse_qs(urlsplit(url).query)["page"][0])
			result = {"clients": [], "page": page, "pages": 2, "per_page": 100, "total": 150}
			return HttpResponse(200, json.dumps({"response": {"result": result}}).encode())

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		assert source.is_incremental(Kind.CUSTOMER) and not source.is_incremental(Kind.PRODUCT)
		first = source.page(Kind.CUSTOMER, None, datetime(2026, 10, 1, 9, tzinfo=UTC), "")
		assert first.has_next_page and first.end_cursor == "0:2"
		assert parse_qs(urlsplit(urls[0]).query)["search[updated_min]"] == ["2026-10-01"]

	def test_invoices_ask_for_their_lines(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.SALES_INVOICE, None, None, "")
		invoices = next(r for r in fake.requests if "/invoices/invoices" in r.url)
		assert parse_qs(urlsplit(invoices.url).query)["include[]"] == ["lines"]

	def test_the_ledger_groups_expenses_and_other_income(self, config: ConnectionConfig) -> None:
		journals = nodes(config, Kind.JOURNAL_ENTRY)
		assert set(journals) == {
			gid("JournalEntry", "005f5c22-8166-47e9-926d-098fe523f929"),
			gid("Expense", 77),
			gid("OtherIncome", 88),
		}
		assert len(journals[gid("Expense", 77)].children("postings")) == 3

	def test_a_bill_payment_carries_its_bills_vendor(self, config: ConnectionConfig) -> None:
		payment = nodes(config, Kind.PAYMENT_ENTRY)[gid("BillPayment", 1490)]
		assert payment.integer("vendorid") == 1563

	def test_nothing_is_read_by_itself(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		with pytest.raises(SourceError, match="by itself"):
			source.node(Kind.CUSTOMER, gid("Client", 220399))


class TestMappers:
	def test_accounts_and_taxes(self, config: ConnectionConfig) -> None:
		checking = values(config, Kind.ACCOUNT, gid("Account", "0f321fa9-78db-41ce-8c5f-11605718e5fb"))
		assert (checking.string("root_type"), checking.string("parent")) == (
			"Asset",
			gid("Account", "7a6c2b9e-1d2f-4c3a-8b5e-6f7a8b9c0d1e"),
		)
		assert values(config, Kind.ACCOUNT, gid("Account", "7a6c2b9e-1d2f-4c3a-8b5e-6f7a8b9c0d1e")).flag(
			"is_group"
		)
		hst = values(config, Kind.TAX_RATE, gid("Tax", 58728))
		assert (hst.decimal("rate"), hst.string("number")) == (Decimal(15), "123456789RT0001")
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.TAX_RATE]
		with pytest.raises(Unsupported, match="compound"):
			mapper.map(nodes(config, Kind.TAX_RATE)[gid("Tax", 58729)])

	def test_items_services_clients_and_vendors(self, config: ConnectionConfig) -> None:
		prints = values(config, Kind.PRODUCT, gid("Item", 201225), "v2")
		assert (prints.decimal("price"), prints.decimal("stock_qty"), prints.string("sku")) == (
			Decimal(50),
			Decimal(12),
			"PRINT",
		)
		assert values(config, Kind.PRODUCT, gid("Service", 4054453)).decimal("price") == Decimal(150)
		river = values(config, Kind.CUSTOMER, gid("Client", 220399))
		assert (river.string("name"), river.string("type"), river.string("tax_id")) == (
			"River Co",
			"Company",
			"987654321RT0001",
		)
		assert values(config, Kind.CUSTOMER, gid("Client", 220400)).flag("disabled"), "archived"
		assert mapped(config, Kind.CUSTOMER, gid("Client", 220400), "v2") is None, "deleted"
		vendor = values(config, Kind.SUPPLIER, gid("Vendor", 1563))
		assert [c.string("name") for c in vendor.records("contacts")] == ["Pat Mill"]

	def test_projects_and_time(self, config: ConnectionConfig) -> None:
		project = values(config, Kind.PROJECT, gid("Project", 779597))
		assert (project.string("status"), project.string("customer"), project.string("end")) == (
			"Open",
			gid("Client", 220399),
			"2026-11-30",
		)
		entry = values(config, Kind.TIMESHEET, gid("TimeEntry", 5095))
		assert (entry.decimal("hours"), entry.string("project"), entry.flag("billable")) == (
			Decimal("1.5"),
			gid("Project", 779597),
			True,
		)

	def test_estimates_invoices_and_credit_notes(self, config: ConnectionConfig) -> None:
		estimate = values(config, Kind.DRAFT_ORDER, gid("Estimate", 500))
		assert (estimate.decimal("discount"), estimate.decimal("total")) == (Decimal(30), Decimal("310.5"))
		assert [c.decimal("amount") for c in estimate.records("charges")] == [Decimal("40.5")]
		paid = values(config, Kind.SALES_INVOICE, gid("Invoice", 600))
		assert (
			paid.decimal("total"),
			paid.flag("submitted"),
			paid.records("taxes")[0].string("description"),
		) == (Decimal("103.5"), True, "HST 15%")
		assert not values(config, Kind.SALES_INVOICE, gid("Invoice", 601)).flag("submitted")
		assert values(config, Kind.SALES_INVOICE, gid("Invoice", 601), "v2").flag("submitted")
		credit = values(config, Kind.SALES_INVOICE, gid("CreditNote", 31131))
		assert (credit.flag("is_return"), credit.decimal("total")) == (True, Decimal(-25))

	def test_bills_and_payments(self, config: ConnectionConfig) -> None:
		bill = values(config, Kind.PURCHASE_INVOICE, gid("Bill", 1141))
		assert (
			bill.decimal("total"),
			bill.string("reference"),
			bill.records("taxes")[0].decimal("amount"),
		) == (Decimal(1725), "BN101", Decimal(225))
		paid = values(config, Kind.PAYMENT_ENTRY, gid("Payment", 900))
		assert paid.records("allocations")[0].string("document") == gid("Invoice", 600)
		out = values(config, Kind.PAYMENT_ENTRY, gid("BillPayment", 1490))
		assert (
			out.string("payment_type"),
			out.string("party"),
			out.records("allocations")[0].string("document"),
		) == ("Pay", gid("Vendor", 1563), gid("Bill", 1141))

	def test_journals_expenses_and_other_income(self, config: ConnectionConfig) -> None:
		manual = values(
			config, Kind.JOURNAL_ENTRY, gid("JournalEntry", "005f5c22-8166-47e9-926d-098fe523f929")
		)
		assert (manual.string("number"), manual.string("date")) == ("JE1", "2026-09-30")
		expense = values(config, Kind.JOURNAL_ENTRY, gid("Expense", 77))
		assert sorted(
			(line.decimal("debit"), line.decimal("credit")) for line in expense.records("lines")
		) == [
			(Decimal(0), Decimal("56.5")),
			(Decimal("6.5"), Decimal(0)),
			(Decimal(50), Decimal(0)),
		]
		assert (
			values(config, Kind.JOURNAL_ENTRY, gid("OtherIncome", 88)).string("number") == "Other income 88"
		)

	def test_amounts_that_dont_add_up(self, config: ConnectionConfig) -> None:
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.SALES_INVOICE]
		invoice = nodes(config, Kind.SALES_INVOICE)[gid("Invoice", 600)]
		with pytest.raises(Unsupported, match="not the amount"):
			mapper.map(invoice.with_fields(amount={"amount": "103.51", "code": "CAD"}))
