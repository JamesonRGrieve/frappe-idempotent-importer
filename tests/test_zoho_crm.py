# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Zoho CRM adapter over the recorded studio (tests/fixtures/zoho_crm, proven against Zoho's CRM v8 OpenAPI
documents): OAuth in the organisation's data centre, the fields each module offers (custom ones by label),
more than 50 fields read in parts, paging by page then token, If-Modified-Since, activities on contacts, the
customers, and every mapper."""

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
from idempotent_importer.sources.zoho.client import GRANT_PATH
from idempotent_importer.sources.zoho_crm.adapter import ZohoCrmAdapter
from idempotent_importer.sources.zoho_crm.reader import Field, ZohoCrmReader
from tests.support.transport import (
	OAUTH_ACCESS_TOKEN,
	FakeTransport,
	RecordingSleeper,
	oauth_source,
	scripted,
)

ADAPTER = ZohoCrmAdapter()
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()
RIVER, DELTA = "5725767000000417001", "5725767000000417002"
REBRAND = "5725767000000419001"


def gid(type_: str, id_: str) -> str:
	return f"gid://zoho_crm/zoho-crm-z/{type_}/{id_}"


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="zoho_crm",
		connection="zoho-crm-z",
		store_url="https://crm.zoho.com",
		api_user="e2e-zoho-client",
		api_secret="e2e-zoho-secret",
		refresh_token="e2e-zoho-refresh",
		save_refresh_token=lambda token: None,
		endpoint_override="http://zoho_crm-fake:8080",
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[ZohoCrmReader, FakeTransport]:
	fake = oauth_source("zoho_crm", state, GRANT_PATH, rotates=False)
	built = ADAPTER.reader(config, fake, RecordingSleeper(NOW))
	assert isinstance(built, ZohoCrmReader)
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
	def test_the_approval_is_in_the_data_centre_and_asks_to_read(self, config: ConnectionConfig) -> None:
		eu = replace(config, store_url="https://crm.zoho.eu", endpoint_override=None)
		url = ADAPTER.oauth(eu, scripted()).authorization_url("https://erp/cb", "s", "v")
		assert url.startswith("https://accounts.zoho.eu/oauth/v2/auth?")
		scopes = parse_qs(urlsplit(url).query)["scope"][0].split(",")
		assert all(scope.endswith(".READ") for scope in scopes)
		with pytest.raises(SourceError, match="data centre"):
			ADAPTER.connected(
				eu, scripted(), TokenGrant("a"), {"accounts-server": "https://accounts.zoho.com"}
			)
		assert (
			ADAPTER.connected(
				eu, scripted(), TokenGrant("a"), {"accounts-server": "https://accounts.zoho.eu"}
			)
			== {}
		)

	def test_requests_carry_zohos_token_scheme(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.EMPLOYEE, None, None, "")
		users = next(r for r in fake.requests if "/users" in r.url)
		assert users.headers["Authorization"] == f"Zoho-oauthtoken {OAUTH_ACCESS_TOKEN}"


class TestReader:
	def test_a_module_needing_more_than_50_fields_reads_the_rest_by_id(
		self, config: ConnectionConfig
	) -> None:
		calls: list[dict[str, list[str]]] = []
		many = [
			{
				"api_name": f"Field_{n}",
				"field_label": f"Field {n}",
				"data_type": "text",
				"custom_field": True,
				"pick_list_values": [],
			}
			for n in range(60)
		]

		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if method == "POST":
				return HttpResponse(200, b'{"access_token": "t"}')
			path, query = urlsplit(url).path, parse_qs(urlsplit(url).query)
			if path.endswith("/settings/modules"):
				return HttpResponse(200, b'{"modules": [{"api_name": "Leads", "api_supported": true}]}')
			if path.endswith("/settings/fields"):
				return HttpResponse(200, json.dumps({"fields": many}).encode())
			calls.append(query)
			fields = query["fields"][0].split(",")
			record = {name: "x" for name in fields} | {"id": "1"}
			info = {"more_records": True, "next_page_token": "abc"} if "page" in query else {}
			return HttpResponse(200, json.dumps({"data": [record], "info": info}).encode())

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		page = source.page(Kind.LEAD, None, datetime(2026, 10, 1, tzinfo=UTC), "")
		assert len(calls) == 2 and calls[1]["ids"] == ["1"]
		custom = page.nodes[0].required_child("custom")
		assert len(custom.raw()) == 60, "every field, from both requests"
		assert page.has_next_page and page.end_cursor == "0:t:abc"
		source.page(Kind.LEAD, page.end_cursor, None, "")
		assert calls[2]["page_token"] == ["abc"]

	def test_incremental_runs_ask_what_changed(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		assert source.is_incremental(Kind.LEAD) and not source.is_incremental(Kind.CUSTOMER)
		source.page(Kind.LEAD, None, datetime(2026, 10, 1, tzinfo=UTC), "")
		leads = next(r for r in fake.requests if "/crm/v8/Leads" in r.url)
		assert leads.headers["If-Modified-Since"] == "2026-10-01T00:00:00+00:00"

	def test_custom_fields_and_picklists_by_label(self, config: ConnectionConfig) -> None:
		river = nodes(config, Kind.PROSPECT)[gid("Accounts", RIVER)]
		assert river.raw()["custom"] == {"Account Tier": "Gold"}
		assert [c.string("First_Name") for c in river.children("contacts")] == ["Riley", "Sam"]
		assert Field("Tags", "multiselectpicklist", {"a": "A"}).shown(["a", "b"]) == "A; b"
		assert Field("Owner", "lookup", {}).shown({"name": "Morgan", "id": "1"}) == "Morgan"

	def test_an_activity_on_a_contact_carries_its_account(self, config: ConnectionConfig) -> None:
		call = nodes(config, Kind.COMMUNICATION)[gid("Calls", "5725767000000423002")]
		assert call.string("contact_account") == RIVER

	def test_customers_and_modules_the_organisation_lacks(self, config: ConnectionConfig) -> None:
		assert set(nodes(config, Kind.CUSTOMER)) == {gid("Accounts", RIVER)}
		source = ADAPTER.reader(
			config,
			scripted(HttpResponse(200, b'{"access_token": "t"}'), HttpResponse(200, b'{"modules": []}')),
			RecordingSleeper(NOW),
		)
		page = source.page(Kind.DRAFT_ORDER, None, None, "")
		assert page.nodes == [] and not page.has_next_page

	def test_node_by_identity(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		deal = source.node(Kind.OPPORTUNITY, gid("Deals", REBRAND))
		assert deal is not None and deal.string("Deal_Name") == "River rebrand"
		with pytest.raises(SourceError, match="by itself"):
			source.node(Kind.OPPORTUNITY, gid("Accounts", RIVER))


class TestMappers:
	def test_users_products_and_vendors(self, config: ConnectionConfig) -> None:
		jamie = values(config, Kind.EMPLOYEE, gid("User", "5725767000000401002"), "v2")
		assert (jamie.string("status"), jamie.string("reports_to")) == (
			"Inactive",
			gid("User", "5725767000000401001"),
		)
		assert values(config, Kind.PRODUCT, gid("Products", "5725767000000420001"), "v2").decimal(
			"price"
		) == Decimal(175)
		prints = values(config, Kind.PRODUCT, gid("Products", "5725767000000420002"))
		assert (prints.flag("is_stock_item"), prints.decimal("stock_qty")) == (True, Decimal(12))
		vendor = values(config, Kind.SUPPLIER, gid("Vendors", "5725767000000421001"))
		assert vendor.records("addresses")[0].string("country_code") == "CA"

	def test_leads_accounts_and_customers(self, config: ConnectionConfig) -> None:
		quinn = values(config, Kind.LEAD, gid("Leads", "5725767000000422001"))
		assert quinn.string("status") == "Replied" and quinn.all()["custom"] == {
			"Preferred Contact Time": "Mornings"
		}
		assert values(config, Kind.LEAD, gid("Leads", "5725767000000422002")).flag("disabled")
		river = values(config, Kind.PROSPECT, gid("Accounts", RIVER))
		assert [c.string("name") for c in river.records("contacts")] == ["Riley River", "Sam Shore"]
		customer = values(config, Kind.CUSTOMER, gid("Accounts", RIVER))
		assert customer.all()["custom"] == {"Account Tier": "Gold"}

	def test_deals(self, config: ConnectionConfig) -> None:
		assert values(config, Kind.OPPORTUNITY_TYPE, gid("Pipeline", "Standard")).string("name") == "Standard"
		deal = values(config, Kind.OPPORTUNITY, gid("Deals", REBRAND))
		assert (deal.string("status"), deal.string("sales_stage"), deal.string("currency")) == (
			"Open",
			"Proposal/Price Quote",
			"CAD",
		)
		assert deal.string("opportunity_type") == gid("Pipeline", "Standard")
		assert values(config, Kind.OPPORTUNITY, gid("Deals", REBRAND), "v2").string("status") == "Converted"
		assert (
			values(config, Kind.OPPORTUNITY, gid("Deals", "5725767000000419002")).string("status") == "Lost"
		)

	def test_activities(self, config: ConnectionConfig) -> None:
		call = values(config, Kind.COMMUNICATION, gid("Calls", "5725767000000423001"))
		assert (call.string("date"), call.required_record("with").string("kind")) == (
			"2026-09-05T15:00:00Z",
			"opportunity",
		)
		assert call.string("content") == "Walked through the brief.\n754 seconds, Interested"
		inbound = values(config, Kind.COMMUNICATION, gid("Calls", "5725767000000423002"))
		assert (inbound.flag("sent"), inbound.required_record("with").string("party")) == (
			False,
			gid("Accounts", RIVER),
		)
		note = values(config, Kind.COMMUNICATION, gid("Notes", "5725767000000424001"))
		assert (note.string("subject"), note.string("content")) == ("Budget", "Budget approved")
		event = values(config, Kind.EVENT, gid("Events", "5725767000000425001"))
		assert [p.string("kind") for p in event.records("participants")] == ["opportunity", "lead"]
		assert event.string("starts_on") == "2026-09-10T16:00:00Z"
		task = values(config, Kind.TODO, gid("Tasks", "5725767000000426001"))
		assert (task.string("status"), task.required_record("about").string("kind")) == ("Open", "prospect")
		assert (
			values(config, Kind.TODO, gid("Tasks", "5725767000000426001"), "v2").string("status") == "Closed"
		)

	def test_quotes_orders_invoices_and_purchases(self, config: ConnectionConfig) -> None:
		quote = values(config, Kind.DRAFT_ORDER, gid("Quotes", "5725767000000427001"))
		assert (quote.decimal("discount"), quote.decimal("total")) == (Decimal(30), Decimal("310.5"))
		assert quote.required_record("quoted").string("party") == gid("Accounts", DELTA)
		order = values(config, Kind.SALES_ORDER, gid("SalesOrders", "5725767000000428001"))
		assert (order.decimal("total"), order.records("taxes")[0].string("role")) == (Decimal(145), "fee")
		paid = values(config, Kind.SALES_INVOICE, gid("Invoices", "5725767000000429001"))
		assert (paid.decimal("total"), paid.flag("submitted")) == (Decimal(345), True)
		draft = values(config, Kind.SALES_INVOICE, gid("Invoices", "5725767000000429002"))
		assert (draft.flag("submitted"), draft.decimal("discount")) == (False, Decimal(5))
		purchase = values(config, Kind.PURCHASE_ORDER, gid("PurchaseOrders", "5725767000000430001"))
		assert (purchase.string("party"), purchase.decimal("total")) == (
			gid("Vendors", "5725767000000421001"),
			Decimal(230),
		)

	def test_a_document_that_doesnt_add_up(self, config: ConnectionConfig) -> None:
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.SALES_INVOICE]
		invoice = nodes(config, Kind.SALES_INVOICE)[gid("Invoices", "5725767000000429001")]
		with pytest.raises(Unsupported, match="grand total"):
			mapper.map(invoice.with_fields(Grand_Total=346))
