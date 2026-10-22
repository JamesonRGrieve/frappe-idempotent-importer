# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Wave adapter over the recorded bakery business (tests/fixtures/wave, proven against Wave's schema in
tests/schemas/wave.json): OAuth and the business the approval opens, GraphQL paging, incremental filters
and error answers, Wave's global ids, and every mapper."""

from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from idempotent_importer.core.errors import CredentialsRejected, SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.oauth import TokenGrant
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.wave import operations
from idempotent_importer.sources.wave.adapter import GRANT_PATH, WaveAdapter
from idempotent_importer.sources.wave.reader import WaveReader, local_id
from tests.support.fixtures import rest_fixtures
from tests.support.transport import FakeTransport, RecordingSleeper, oauth_source, rest_source, scripted
from tools.proofs import invalid_wave_operations, mismatched_wave_fixtures, served_differently
from tools.schemas import wave_schema
from tools.wave_fixtures import BUSINESS, global_id

ADAPTER = WaveAdapter()
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()


def gid(type_: str, id_: int | str) -> str:
	return f"gid://wave/wave-v/{type_}/{id_}"


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="wave",
		connection="wave-v",
		store_url="https://next.waveapps.com",
		api_user="e2e-wave-client",
		api_secret="e2e-wave-secret",
		account=BUSINESS,
		refresh_token="e2e-wave-refresh",
		save_refresh_token=lambda token: None,
		site_url="https://erp.example",
		endpoint_override="http://wave-fake:8080",
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[WaveReader, FakeTransport]:
	fake = oauth_source("wave", state, GRANT_PATH, rotates=False)
	built = ADAPTER.reader(config, fake, RecordingSleeper(NOW))
	assert isinstance(built, WaveReader)
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


def graphql_body(fake: FakeTransport, name: str) -> dict[str, Any]:
	sent = [
		json.loads(r.body) for r in fake.requests if r.method == "POST" and r.url.endswith("/graphql/public")
	]
	return next(body for body in sent if body["operationName"] == name)


class TestProofs:
	def test_every_operation_and_fixture_matches_wave_schema(self) -> None:
		schema = wave_schema()
		assert invalid_wave_operations(schema) == []
		assert mismatched_wave_fixtures(schema) == []

	def test_a_fixture_shaped_otherwise_fails(self) -> None:
		fixture = next(f for f in rest_fixtures("wave", "v1") if f.file.name == "products.json")
		data = json.loads(json.dumps(fixture.response["data"]))
		data["business"]["products"]["edges"][0]["node"]["sku"] = "LOAF"
		failures = served_differently(
			wave_schema(), operations.PRODUCTS.document, data, dict(fixture.body["variables"]), "x"
		)
		assert failures == [
			"x: $.business.products.edges[0].node.sku is in the fixture but not selected by the operation"
		]


class TestOAuth:
	def test_refreshes_name_the_redirect_and_keep_the_refresh_token(self, config: ConnectionConfig) -> None:
		kept: list[str] = []
		source, fake = reader(replace(config, save_refresh_token=kept.append))
		source.page(Kind.ACCOUNT, None, None, "")
		grant = next(r for r in fake.requests if r.method == "POST" and r.url.endswith(GRANT_PATH))
		form = grant.body.decode()
		assert "grant_type=refresh_token" in form and "redirect_uri=https%3A%2F%2Ferp.example" in form
		assert "client_secret=e2e-wave-secret" in form
		assert kept == []

	def test_the_business_the_approval_opens_becomes_the_account(self, config: ConnectionConfig) -> None:
		fake = rest_source("wave", "v1")
		assert ADAPTER.connected(replace(config, account=""), fake, TokenGrant("a"), {}) == {
			"api_account": BUSINESS
		}
		assert ADAPTER.connected(config, fake, TokenGrant("a"), {}) == {}
		empty = {"data": {"businesses": {"pageInfo": {"currentPage": 1, "totalPages": 1}, "edges": []}}}
		with pytest.raises(SourceError, match="several businesses or none"):
			ADAPTER.connected(
				replace(config, account=""),
				scripted(HttpResponse(200, json.dumps(empty).encode())),
				TokenGrant("a"),
				{},
			)
		with pytest.raises(CredentialsRejected):
			ADAPTER.connected(config, scripted(HttpResponse(401, b"{}")), TokenGrant("a"), {})

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="Connect to Wave"):
			ADAPTER.reader(replace(config, account=""), scripted(), RecordingSleeper(NOW))
		with pytest.raises(SourceError, match="client ID and secret"):
			ADAPTER.oauth(replace(config, api_secret=""), scripted())


class TestReader:
	def test_global_ids_become_their_own_ids(self) -> None:
		assert local_id(global_id("Customer", 60592896)) == "60592896"
		assert local_id("not/base64+id") == "not_base64-id"
		assert (
			local_id(base64.b64encode(b"no type here").decode()) == base64.b64encode(b"no type here").decode()
		)

	def test_pages_follow_total_pages_and_incremental_runs_ask_for_changes(
		self, config: ConnectionConfig
	) -> None:
		sent: list[dict[str, Any]] = []

		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if url.endswith(GRANT_PATH):
				return HttpResponse(200, b'{"access_token": "t", "refresh_token": "r"}')
			request = json.loads(body)
			sent.append(request)
			page = request["variables"]["page"]
			listing = {"pageInfo": {"currentPage": page, "totalPages": 2}, "edges": []}
			return HttpResponse(200, json.dumps({"data": {"business": {"customers": listing}}}).encode())

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		assert source.is_incremental(Kind.CUSTOMER) and not source.is_incremental(Kind.ACCOUNT)
		first = source.page(Kind.CUSTOMER, None, datetime(2026, 10, 1, 9, tzinfo=UTC), "")
		assert (first.has_next_page, first.end_cursor) == (True, "2")
		last = source.page(Kind.CUSTOMER, "2", None, "")
		assert not last.has_next_page
		assert sent[0]["variables"] == {
			"business": BUSINESS,
			"page": 1,
			"pageSize": 45,
			"since": "2026-10-01T09:00:00Z",
		}
		assert "since" not in sent[1]["variables"]

	def test_graphql_errors_fail_the_read(self, config: ConnectionConfig) -> None:
		def answering(errors: list[dict[str, Any]]) -> FakeTransport:
			def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
				if url.endswith(GRANT_PATH):
					return HttpResponse(200, b'{"access_token": "t"}')
				return HttpResponse(200, json.dumps({"data": None, "errors": errors}).encode())

			return FakeTransport(route)

		refused = ADAPTER.reader(
			config,
			answering([{"message": "no", "extensions": {"code": "UNAUTHENTICATED"}}]),
			RecordingSleeper(NOW),
		)
		with pytest.raises(CredentialsRejected, match="no"):
			refused.page(Kind.ACCOUNT, None, None, "")
		broken = ADAPTER.reader(config, answering([{"message": "bad field"}]), RecordingSleeper(NOW))
		with pytest.raises(SourceError, match="bad field"):
			broken.page(Kind.ACCOUNT, None, None, "")

	def test_payments_come_with_their_invoice(self, config: ConnectionConfig) -> None:
		payments = nodes(config, Kind.PAYMENT_ENTRY)
		assert list(payments) == [gid("InvoicePayment", 1)]
		assert payments[gid("InvoicePayment", 1)].string("invoice") == global_id("Invoice", 1)

	def test_nothing_is_read_by_itself(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		with pytest.raises(SourceError, match="by itself"):
			source.node(Kind.CUSTOMER, gid("Customer", 1))
		with pytest.raises(SourceError, match="no timesheet"):
			source.page(Kind.TIMESHEET, None, None, "")


class TestMappers:
	def test_accounts_and_taxes(self, config: ConnectionConfig) -> None:
		chequing = values(config, Kind.ACCOUNT, gid("Account", 1))
		assert (chequing.string("root_type"), chequing.string("account_type"), chequing.string("number")) == (
			"Asset",
			"Bank",
			"1010",
		)
		assert values(config, Kind.ACCOUNT, gid("Account", 4)).string("account_type") == "Tax"
		hst = values(config, Kind.TAX_RATE, gid("SalesTax", 1))
		assert (hst.string("name"), hst.decimal("rate"), hst.string("number")) == (
			"HST",
			Decimal(15),
			"123456789RT0001",
		)
		assert mapped(config, Kind.TAX_RATE, gid("SalesTax", 3)) is None, "archived"
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.TAX_RATE]
		with pytest.raises(Unsupported, match="compound"):
			mapper.map(nodes(config, Kind.TAX_RATE)[gid("SalesTax", 2)])

	def test_products_customers_and_vendors(self, config: ConnectionConfig) -> None:
		assert values(config, Kind.PRODUCT, gid("Product", 1)).decimal("price") == Decimal("8.5")
		assert values(config, Kind.PRODUCT, gid("Product", 1), "v2").decimal("price") == Decimal(9)
		river = values(config, Kind.CUSTOMER, gid("Customer", 1))
		assert (
			river.string("type"),
			river.string("company"),
			river.records("addresses")[0].string("country_code"),
		) == ("Company", "River Cafe", "CA")
		sam = values(config, Kind.CUSTOMER, gid("Customer", 2))
		assert (sam.string("type"), sam.has("disabled")) == ("Individual", False)
		assert values(config, Kind.CUSTOMER, gid("Customer", 2), "v2").flag("disabled")
		assert values(config, Kind.SUPPLIER, gid("Vendor", 1)).string("name") == "Mill Co"

	def test_estimates_invoices_and_payments(self, config: ConnectionConfig) -> None:
		estimate = values(config, Kind.DRAFT_ORDER, gid("Estimate", 1))
		assert (estimate.decimal("discount"), estimate.decimal("total")) == (Decimal(24), Decimal("248.4"))
		assert [c.decimal("amount") for c in estimate.records("charges")] == [Decimal("32.4")]
		paid = values(config, Kind.SALES_INVOICE, gid("Invoice", 1))
		assert (
			paid.decimal("total"),
			paid.flag("submitted"),
			paid.records("taxes")[0].string("description"),
		) == (Decimal("39.1"), True, "HST")
		assert paid.records("lines")[0].string("product") == gid("Product", 1)
		assert not values(config, Kind.SALES_INVOICE, gid("Invoice", 2)).flag("submitted")
		assert values(config, Kind.SALES_INVOICE, gid("Invoice", 2), "v2").flag("submitted")
		payment = values(config, Kind.PAYMENT_ENTRY, gid("InvoicePayment", 1))
		assert (payment.string("party"), payment.string("account"), payment.decimal("amount")) == (
			gid("Customer", 1),
			gid("Account", 1),
			Decimal("39.1"),
		)
		assert payment.records("allocations")[0].string("document") == gid("Invoice", 1)

	def test_what_cant_be_booked_exactly(self, config: ConnectionConfig) -> None:
		mappers = {m.kind: m for m in ADAPTER.mappers(config)}
		invoice = nodes(config, Kind.SALES_INVOICE)[gid("Invoice", 1)]
		with pytest.raises(Unsupported, match="not the total"):
			mappers[Kind.SALES_INVOICE].map(
				invoice.with_fields(total={"value": "39.11", "currency": {"code": "CAD"}})
			)
		payment = nodes(config, Kind.PAYMENT_ENTRY)[gid("InvoicePayment", 1)]
		assert mappers[Kind.PAYMENT_ENTRY].map(payment.with_fields(active=False)) is None
		with pytest.raises(Unsupported, match="refund"):
			mappers[Kind.PAYMENT_ENTRY].map(payment.with_fields(amount="-5.00"))
		estimate = nodes(config, Kind.DRAFT_ORDER)[gid("Estimate", 1)]
		assert mappers[Kind.DRAFT_ORDER].map(estimate.with_fields(status="DELETED")) is None
