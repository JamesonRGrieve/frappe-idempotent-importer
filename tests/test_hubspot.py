# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HubSpot adapter over the recorded studio (tests/fixtures/hubspot, proven against HubSpot's CRM v3
OpenAPI documents): OAuth, list paging, the properties the account defined (by label), notes, line items and
adjustments read in batches, incremental search after the last id, the customers, and every mapper."""

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
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.oauth import TokenGrant
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.hubspot.adapter import GRANT_PATH, HubSpotAdapter
from idempotent_importer.sources.hubspot.mapping import Amounts, Context
from idempotent_importer.sources.hubspot.reader import HubSpotReader
from tests.support.transport import (
	OAUTH_ACCESS_TOKEN,
	FakeTransport,
	RecordingSleeper,
	oauth_source,
	scripted,
)

ADAPTER = HubSpotAdapter()
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()


def gid(type_: str, id_: str) -> str:
	return f"gid://hubspot/hubspot-s/{type_}/{id_}"


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="hubspot",
		connection="hubspot-s",
		store_url="https://app.hubspot.com",
		api_user="e2e-hubspot-client",
		api_secret="e2e-hubspot-secret",
		refresh_token="e2e-hubspot-refresh",
		save_refresh_token=lambda token: None,
		endpoint_override="http://hubspot-fake:8080",
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[HubSpotReader, FakeTransport]:
	fake = oauth_source("hubspot", state, GRANT_PATH, rotates=False)
	built = ADAPTER.reader(config, fake, RecordingSleeper(NOW))
	assert isinstance(built, HubSpotReader)
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
	def test_the_approval_asks_to_read(self, config: ConnectionConfig) -> None:
		url = ADAPTER.oauth(config, scripted()).authorization_url("https://erp/cb", "s", "v")
		assert url.startswith("https://app.hubspot.com/oauth/authorize?")
		scopes = parse_qs(urlsplit(url).query)["scope"][0].split(" ")
		assert "crm.objects.contacts.read" in scopes
		assert all(scope == "oauth" or scope.endswith(".read") for scope in scopes)

	def test_the_refresh_token_is_kept_and_requests_carry_the_access_token(
		self, config: ConnectionConfig
	) -> None:
		kept: list[str] = []
		source, fake = reader(replace(config, save_refresh_token=kept.append))
		source.page(Kind.EMPLOYEE, None, None, "")
		grant = next(r for r in fake.requests if r.method == "POST")
		form = parse_qs(grant.body.decode())
		assert form["grant_type"] == ["refresh_token"] and form["client_secret"] == ["e2e-hubspot-secret"]
		assert kept == [], "HubSpot keeps its refresh token"
		owners = next(r for r in fake.requests if "/owners" in r.url)
		assert owners.headers["Authorization"] == f"Bearer {OAUTH_ACCESS_TOKEN}"

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="client ID and secret"):
			ADAPTER.oauth(replace(config, api_secret=""), scripted())
		assert ADAPTER.connected(config, scripted(), TokenGrant("a"), {}) == {}


class TestReader:
	def test_the_objects_kinds_are_incremental(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		assert source.is_incremental(Kind.LEAD) and source.is_incremental(Kind.SALES_INVOICE)
		assert not source.is_incremental(Kind.CUSTOMER) and not source.is_incremental(Kind.EMPLOYEE)

	def test_owners_active_then_archived(self, config: ConnectionConfig) -> None:
		assert set(nodes(config, Kind.EMPLOYEE, "v2")) == {gid("Owner", "1"), gid("Owner", "2")}

	def test_lists_page_after_the_cursor_with_the_properties_asked_for(
		self, config: ConnectionConfig
	) -> None:
		calls: list[str] = []

		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			calls.append(url)
			if method == "POST":
				return HttpResponse(200, b'{"access_token": "t"}')
			if "/properties/" in url:
				return HttpResponse(200, b'{"results": []}')
			after = parse_qs(urlsplit(url).query).get("after")
			body_ = {"results": [], "paging": {"next": {"after": "100"}}} if not after else {"results": []}
			return HttpResponse(200, json.dumps(body_).encode())

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		first = source.page(Kind.PRODUCT, None, None, "")
		assert first.has_next_page and first.end_cursor == "0:100"
		last = source.page(Kind.PRODUCT, first.end_cursor, None, "")
		assert not last.has_next_page
		listed = [url for url in calls if "/objects/products" in url]
		assert "properties=name%2Cdescription%2Cprice" in listed[0]
		assert parse_qs(urlsplit(listed[1]).query)["after"] == ["100"]

	def test_custom_properties_by_label_with_their_options(self, config: ConnectionConfig) -> None:
		riley = nodes(config, Kind.LEAD)[gid("Contact", "101")]
		assert riley.raw()["custom"] == {"Preferred contact time": "Mornings"}

	def test_notes_and_a_deals_stage_come_with_it(self, config: ConnectionConfig) -> None:
		deal = nodes(config, Kind.OPPORTUNITY)[gid("Deal", "301")]
		assert [note.string("id") for note in deal.children("notes")] == ["903"]
		assert deal.required_child("stage").string("label") == "Qualified to buy"

	def test_incremental_runs_search_what_changed_after_the_last_id(self, config: ConnectionConfig) -> None:
		bodies: list[dict[str, object]] = []

		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if url.endswith(GRANT_PATH):
				return HttpResponse(200, b'{"access_token": "t"}')
			if url.endswith("/search"):
				bodies.append(json.loads(body))
				ids = [
					{"id": str(n), "properties": {}, "createdAt": "x", "updatedAt": "x", "archived": False}
					for n in range(100)
				]
				return HttpResponse(200, json.dumps({"total": 250, "results": ids}).encode())
			if "/properties/" in url:
				return HttpResponse(200, b'{"results": []}')
			object_id = urlsplit(url).path.rsplit("/", 1)[1]
			record = {
				"id": object_id,
				"properties": {"name": f"P{object_id}"},
				"createdAt": "x",
				"updatedAt": "x",
				"archived": False,
			}
			return HttpResponse(200, json.dumps(record).encode())

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		since = datetime(2026, 10, 1, tzinfo=UTC)
		page = source.page(Kind.PRODUCT, "0:42", since, "")
		assert len(page.nodes) == 100 and page.end_cursor == "0:99"
		changed = str(int(since.timestamp() * 1000))
		assert bodies[0]["filterGroups"] == [
			{
				"filters": [
					{"propertyName": "hs_lastmodifieddate", "operator": "GTE", "value": changed},
					{"propertyName": "hs_object_id", "operator": "GT", "value": "42"},
				]
			}
		]

	def test_customers_are_the_customer_stage_and_who_is_billed(self, config: ConnectionConfig) -> None:
		found = nodes(config, Kind.CUSTOMER)
		assert set(found) == {gid("Contact", "101"), gid("Company", "201"), gid("Contact", "102")}
		people = found[gid("Company", "201")].children("people")
		assert [person.string("id") for person in people] == ["101", "102"]

	def test_a_payment_and_its_refund(self, config: ConnectionConfig) -> None:
		found = nodes(config, Kind.PAYMENT_ENTRY)
		assert gid("PaymentRefund", "872") in found and gid("PaymentRefund", "871") not in found

	def test_node_by_identity(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		contact = source.node(Kind.LEAD, gid("Contact", "103"))
		assert contact is not None and contact.id() == gid("Contact", "103")
		with pytest.raises(SourceError, match="by itself"):
			source.node(Kind.LEAD, gid("Owner", "1"))


class TestMappers:
	def test_owners(self, config: ConnectionConfig) -> None:
		assert values(config, Kind.EMPLOYEE, gid("Owner", "2"), "v2").string("status") == "Inactive"
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.EMPLOYEE]
		queue = Node({"id": gid("Owner", "9"), "type": "QUEUE", "archived": False})
		assert mapper.map(queue) is None

	def test_products(self, config: ConnectionConfig) -> None:
		session = values(config, Kind.PRODUCT, gid("Product", "401"), "v2")
		assert session.decimal("price") == Decimal(175) and not session.flag("is_stock_item")
		prints = values(config, Kind.PRODUCT, gid("Product", "402"))
		assert prints.flag("is_stock_item") and prints.all()["custom"] == {"Finish": "Matte"}

	def test_leads(self, config: ConnectionConfig) -> None:
		riley = values(config, Kind.LEAD, gid("Contact", "101"))
		assert riley.string("status") == "Converted"
		assert [note.string("text") for note in riley.records("crm_notes")] == ["Wants the spring catalogue"]
		assert values(config, Kind.LEAD, gid("Contact", "102")).string("mobile") == "+1 902 555 0102"
		quinn = values(config, Kind.LEAD, gid("Contact", "103"))
		assert quinn.flag("disabled") and quinn.string("status") == "Lead"

	def test_customers(self, config: ConnectionConfig) -> None:
		company = values(config, Kind.CUSTOMER, gid("Company", "201"))
		assert company.string("type") == "Company" and company.all()["custom"] == {"Account tier": "Gold"}
		assert [p.string("name") for p in company.records("contacts")] == ["Riley River", "Sam Shore"]
		assert company.records("addresses")[0].string("country_code") == "CA"
		assert values(config, Kind.CUSTOMER, gid("Contact", "101")).string("type") == "Individual"

	def test_prospects(self, config: ConnectionConfig) -> None:
		river = values(config, Kind.PROSPECT, gid("Company", "201"))
		assert river.strings("leads") == [gid("Contact", "101"), gid("Contact", "102")]
		assert river.string("website") == "https://river.example"
		assert [n.string("text") for n in river.records("crm_notes")] == ["Expanding in spring"]

	def test_deals_at_their_stage(self, config: ConnectionConfig) -> None:
		assert (
			values(config, Kind.OPPORTUNITY_TYPE, gid("Pipeline", "default")).string("name")
			== "Sales Pipeline"
		)
		deal = values(config, Kind.OPPORTUNITY, gid("Deal", "301"))
		assert (deal.string("party_kind"), deal.string("party")) == ("prospect", gid("Company", "201"))
		assert (deal.string("status"), deal.string("sales_stage")) == ("Open", "Qualified to buy")
		assert deal.decimal("probability") == Decimal(40) and deal.string("expected_closing") == "2026-11-30"
		assert deal.all()["custom"] == {"Lead source detail": "Expo 2026"}
		won = values(config, Kind.OPPORTUNITY, gid("Deal", "301"), "v2")
		assert won.string("status") == "Converted"
		lost = values(config, Kind.OPPORTUNITY, gid("Deal", "302"))
		assert (lost.string("status"), lost.string("party_kind")) == ("Lost", "lead")

	def test_calls_and_emails(self, config: ConnectionConfig) -> None:
		call = values(config, Kind.COMMUNICATION, gid("Call", "501"))
		assert (call.string("medium"), call.flag("sent")) == ("Phone", True)
		assert call.string("content") == "Walked through the brief.\n754 seconds, COMPLETED"
		assert call.required_record("with").string("kind") == "lead"
		email = values(config, Kind.COMMUNICATION, gid("Email", "502"))
		assert (email.flag("sent"), email.string("email")) == (False, "sam@river.example")
		about_deal = values(config, Kind.COMMUNICATION, gid("Email", "503"), "v2")
		assert about_deal.required_record("with").string("kind") == "opportunity"

	def test_meetings_and_tasks(self, config: ConnectionConfig) -> None:
		meeting = values(config, Kind.EVENT, gid("Meeting", "601"))
		assert (meeting.string("starts_on"), meeting.string("status")) == (
			"2026-09-10T16:00:00Z",
			"Completed",
		)
		assert [p.string("kind") for p in meeting.records("participants")] == ["lead", "opportunity"]
		task = values(config, Kind.TODO, gid("Task", "701"))
		assert (task.string("status"), task.string("date")) == ("Open", "2026-09-30")
		assert values(config, Kind.TODO, gid("Task", "701"), "v2").string("status") == "Closed"

	def test_a_quote_to_an_organisation(self, config: ConnectionConfig) -> None:
		quote = values(config, Kind.DRAFT_ORDER, gid("Quote", "801"))
		assert quote.required_record("quoted").all() == {"kind": "prospect", "party": gid("Company", "202")}
		assert (quote.decimal("discount"), quote.decimal("total")) == (Decimal(30), Decimal("310.5"))
		assert [c.decimal("amount") for c in quote.records("charges")] == [Decimal("40.5")]

	def test_orders_and_invoices(self, config: ConnectionConfig) -> None:
		order = values(config, Kind.SALES_ORDER, gid("Order", "851"))
		assert (order.decimal("total"), order.string("party")) == (Decimal(55), gid("Company", "201"))
		assert order.records("taxes")[0].string("role") == "fee"
		paid = values(config, Kind.SALES_INVOICE, gid("Invoice", "861"))
		assert (paid.decimal("total"), paid.flag("submitted"), paid.string("remarks")) == (
			Decimal(345),
			True,
			"PO PO-77",
		)
		draft = values(config, Kind.SALES_INVOICE, gid("Invoice", "862"))
		assert (draft.flag("submitted"), draft.string("party")) == (False, gid("Contact", "102"))
		assert values(config, Kind.SALES_INVOICE, gid("Invoice", "862"), "v2").flag("submitted")
		assert mapped(config, Kind.SALES_INVOICE, gid("Invoice", "863")) is None, "voided"

	def test_payments(self, config: ConnectionConfig) -> None:
		paid = values(config, Kind.PAYMENT_ENTRY, gid("Payment", "871"))
		assert paid.records("allocations")[0].string("document") == gid("Invoice", "861")
		refund = values(config, Kind.PAYMENT_ENTRY, gid("PaymentRefund", "872"))
		assert (refund.string("payment_type"), refund.decimal("amount")) == ("Pay", Decimal(20))
		assert mapped(config, Kind.PAYMENT_ENTRY, gid("Payment", "873")) is None, "failed"

	def test_amounts_that_cant_be_exact(self, config: ConnectionConfig) -> None:
		context = Context(config)

		def document(*parts: tuple[str, dict[str, JsonValue]]) -> Node:
			line: dict[str, JsonValue] = {
				"id": "1",
				"properties": {"name": "X", "quantity": "1", "price": "10.01", "amount": "10.01"},
			}
			raw: dict[str, JsonValue] = {"id": gid("Quote", "1"), "line_items": [line]}
			for name, properties in parts:
				raw[name] = [{"id": "2", "properties": properties}]
			return Node(raw)

		with pytest.raises(Unsupported, match="isn't an exact amount"):
			Amounts(context, document(("discounts", {"hs_type": "PERCENT", "hs_value": "5"})), "CAD")
		with pytest.raises(Unsupported, match="tax rate group"):
			taxed: dict[str, JsonValue] = {
				"id": "1",
				"properties": {"name": "X", "quantity": "1", "price": "1", "hs_tax_rate_group_id": "7"},
			}
			Amounts(context, Node({"id": gid("Quote", "1"), "line_items": [taxed]}), "CAD")
