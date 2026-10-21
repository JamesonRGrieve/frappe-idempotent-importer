# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Pipedrive adapter over the recorded studio (tests/fixtures/pipedrive, proven against Pipedrive's v1 and
v2 OpenAPI documents): OAuth and the company domain the token names, v2 cursors and v1 offsets, custom
fields by name, the customers of won deals, incremental lists, and every mapper."""

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
from idempotent_importer.sources.crm import plain_text
from idempotent_importer.sources.pipedrive.adapter import GRANT_PATH, PipedriveAdapter
from idempotent_importer.sources.pipedrive.mapping import ends
from idempotent_importer.sources.pipedrive.reader import CustomField, PipedriveReader
from tests.support.transport import (
	OAUTH_ACCESS_TOKEN,
	FakeTransport,
	RecordingSleeper,
	oauth_source,
	scripted,
)

ADAPTER = PipedriveAdapter()
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()
DOMAIN = "https://harbour.pipedrive.com"


def gid(type_: str, id_: str | int) -> str:
	return f"gid://pipedrive/pipedrive-p/{type_}/{id_}"


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="pipedrive",
		connection="pipedrive-p",
		store_url="https://harbour.pipedrive.com",
		api_user="e2e-pipedrive-client",
		api_secret="e2e-pipedrive-secret",
		account=DOMAIN,
		refresh_token="e2e-pipedrive-refresh",
		save_refresh_token=lambda token: None,
		endpoint_override="http://pipedrive-fake:8080",
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[PipedriveReader, FakeTransport]:
	fake = oauth_source("pipedrive", state, GRANT_PATH, rotates=False)
	built = ADAPTER.reader(config, fake, RecordingSleeper(NOW))
	assert isinstance(built, PipedriveReader)
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
	def test_the_approval_and_the_company_domain(self, config: ConnectionConfig) -> None:
		url = ADAPTER.oauth(config, scripted()).authorization_url("https://erp/cb", "s", "v")
		assert url.startswith("https://oauth.pipedrive.com/oauth/authorize?")
		grant = TokenGrant("a", {"api_domain": DOMAIN + "/"})
		assert ADAPTER.connected(config, scripted(), grant, {}) == {"api_account": DOMAIN}
		with pytest.raises(SourceError, match="company domain"):
			ADAPTER.connected(config, scripted(), TokenGrant("a"), {})

	def test_the_client_authenticates_by_basic_and_requests_carry_the_token(
		self, config: ConnectionConfig
	) -> None:
		source, fake = reader(config)
		source.page(Kind.EMPLOYEE, None, None, "")
		grant = next(r for r in fake.requests if r.method == "POST")
		assert grant.headers["Authorization"].startswith("Basic ")
		users = next(r for r in fake.requests if "/users" in r.url)
		assert users.headers["Authorization"] == f"Bearer {OAUTH_ACCESS_TOKEN}"

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="Connect to Pipedrive"):
			ADAPTER.reader(
				replace(config, account="", endpoint_override=None), scripted(), RecordingSleeper(NOW)
			)
		with pytest.raises(SourceError, match="client ID and secret"):
			ADAPTER.oauth(replace(config, api_user=""), scripted())


class TestReader:
	def test_v2_pages_after_the_cursor_and_v1_by_offset(self, config: ConnectionConfig) -> None:
		calls: list[str] = []

		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if method == "POST":
				return HttpResponse(200, b'{"access_token": "t"}')
			calls.append(url)
			query = parse_qs(urlsplit(url).query)
			if "/api/v2/persons" in url:
				following = None if query.get("cursor") else "c2"
				return HttpResponse(
					200, json.dumps({"data": [], "additional_data": {"next_cursor": following}}).encode()
				)
			if "/api/v2/" in url:
				return HttpResponse(200, b'{"data": [], "additional_data": {"next_cursor": null}}')
			more = query["start"] == ["0"]
			records = [{"id": n, "name": f"U{n}"} for n in range(100)] if more else []
			pagination = {"start": 0, "limit": 100, "more_items_in_collection": more}
			return HttpResponse(200, json.dumps({"data": records, "additional_data": pagination}).encode())

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		first = source.page(Kind.LEAD, None, None, "")
		assert first.has_next_page and first.end_cursor == "0:c2"
		source.page(Kind.LEAD, first.end_cursor, None, "")
		assert parse_qs(urlsplit(calls[-1]).query)["cursor"] == ["c2"]
		users = source.page(Kind.EMPLOYEE, None, None, "")
		assert users.end_cursor == "0:100"

	def test_incremental_lists_ask_for_what_changed(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		assert source.is_incremental(Kind.OPPORTUNITY) and not source.is_incremental(Kind.EMPLOYEE)
		source.page(Kind.LEAD, None, datetime(2026, 10, 1, tzinfo=UTC), "")
		persons = next(r for r in fake.requests if "/api/v2/persons" in r.url)
		assert parse_qs(urlsplit(persons.url).query)["updated_since"] == ["2026-10-01T00:00:00Z"]

	def test_custom_fields_by_name_and_their_options(self, config: ConnectionConfig) -> None:
		river = nodes(config, Kind.PROSPECT)[gid("Organization", 201)]
		assert river.raw()["custom"] == {"Account tier": "Gold"}
		assert [p.integer("id") for p in river.children("persons")] == [101, 102]
		field = CustomField("Budget", "monetary", {})
		assert field.shown({"value": 1200.5, "currency": "CAD"}) == "1200.5 CAD"
		assert CustomField("Tags", "set", {"1": "A", "2": "B"}).shown("1,2") == "A; B"
		assert CustomField("Paid", "boolean", {}).shown(True) == "Yes"

	def test_customers_are_who_won_deals_are_with(self, config: ConnectionConfig) -> None:
		assert set(nodes(config, Kind.CUSTOMER)) == {gid("Organization", 201), gid("Person", 103)}

	def test_node_by_identity(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		person = source.node(Kind.LEAD, gid("Person", 103))
		assert person is not None and person.string("source_id") == "103"
		with pytest.raises(SourceError, match="by itself"):
			source.node(Kind.LEAD, gid("Note", 701))


class TestMappers:
	def test_users(self, config: ConnectionConfig) -> None:
		jamie = values(config, Kind.EMPLOYEE, gid("User", 2), "v2")
		assert (jamie.string("first_name"), jamie.string("last_name"), jamie.string("status")) == (
			"Jamie",
			"Front",
			"Inactive",
		)

	def test_products_and_their_variations(self, config: ConnectionConfig) -> None:
		session = values(config, Kind.PRODUCT, gid("Product", 401), "v2")
		assert session.decimal("price") == Decimal(175)
		prints = values(config, Kind.PRODUCT, gid("Product", 402))
		assert [v.decimal("price") for v in prints.records("variants")] == [Decimal(45), Decimal(60)]
		assert prints.all()["custom"] == {"Finish": "Matte"}

	def test_persons_organizations_and_customers(self, config: ConnectionConfig) -> None:
		riley = values(config, Kind.LEAD, gid("Person", 101))
		assert (riley.string("email"), riley.string("job_title")) == ("riley@river-co.example", "Director")
		assert riley.all()["custom"] == {"Preferred contact time": "Mornings"}
		river = values(config, Kind.PROSPECT, gid("Organization", 201))
		assert river.strings("leads") == [gid("Person", 101), gid("Person", 102)]
		assert river.records("addresses")[0].string("line1") == "1 Harbour Rd"
		customer = values(config, Kind.CUSTOMER, gid("Organization", 201))
		assert [c.string("name") for c in customer.records("contacts")] == ["Riley River", "Sam Shore"]
		assert values(config, Kind.CUSTOMER, gid("Person", 103)).string("type") == "Individual"

	def test_deals_and_leads(self, config: ConnectionConfig) -> None:
		deal = values(config, Kind.OPPORTUNITY, gid("Deal", 301))
		assert (deal.string("party_kind"), deal.string("party")) == ("prospect", gid("Organization", 201))
		assert (deal.string("sales_stage"), deal.decimal("probability")) == ("Proposal made", Decimal(60))
		assert deal.all()["custom"] == {"Lead source detail": "Expo 2026"}
		assert values(config, Kind.OPPORTUNITY, gid("Deal", 301), "v2").string("status") == "Converted"
		lost = values(config, Kind.OPPORTUNITY, gid("Deal", 302))
		assert (lost.string("status"), lost.string("party_kind"), lost.string("notes")) == (
			"Lost",
			"lead",
			"Budget",
		)
		assert values(config, Kind.OPPORTUNITY, gid("Deal", 303)).string("status") == "Closed"
		inbox = values(config, Kind.OPPORTUNITY, gid("Lead", "6f1b0a52-0000-4000-8000-000000000001"))
		assert (inbox.string("status"), inbox.decimal("amount")) == ("Open", Decimal(2000))
		archived = values(config, Kind.OPPORTUNITY, gid("Lead", "6f1b0a52-0000-4000-8000-000000000002"))
		assert archived.string("status") == "Closed"
		assert values(config, Kind.OPPORTUNITY_TYPE, gid("Pipeline", 1)).string("name") == "Sales"

	def test_activities_notes_and_call_logs(self, config: ConnectionConfig) -> None:
		call = values(config, Kind.COMMUNICATION, gid("Activity", 601))
		assert (call.string("medium"), call.string("content"), call.string("date")) == (
			"Phone",
			"Walked through the brief.",
			"2026-09-05T15:20:00Z",
		)
		email = values(config, Kind.COMMUNICATION, gid("Activity", 604))
		assert email.required_record("with").string("kind") == "prospect"
		assert mapped(config, Kind.COMMUNICATION, gid("Activity", 605)) is None, "a call not yet made"
		note = values(config, Kind.COMMUNICATION, gid("Note", 701))
		assert (note.string("medium"), note.required_record("with").string("kind")) == (
			"Other",
			"opportunity",
		)
		log = values(config, Kind.COMMUNICATION, gid("CallLog", "cl-0001"))
		assert log.string("content") == "Samples arrive Friday.\n125 seconds, connected"
		meeting = values(config, Kind.EVENT, gid("Activity", 602))
		assert (meeting.string("starts_on"), meeting.string("ends_on")) == (
			"2026-09-10T16:00:00Z",
			"2026-09-10T17:00:00Z",
		)
		assert [p.string("party") for p in meeting.records("participants")] == [
			gid("Person", 101),
			gid("Deal", 301),
			gid("Organization", 201),
			gid("Person", 102),
		]
		task = values(config, Kind.TODO, gid("Activity", 603))
		assert (task.string("status"), task.string("description")) == ("Open", "Task: Send samples")
		assert values(config, Kind.TODO, gid("Activity", 603), "v2").string("status") == "Closed"
		assert values(config, Kind.TODO, gid("Activity", 605)).string("status") == "Open"

	def test_what_cant_be_kept(self, config: ConnectionConfig) -> None:
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.PRODUCT]
		product = nodes(config, Kind.PRODUCT)[gid("Product", 401)]
		with pytest.raises(Unsupported, match="subscription"):
			mapper.map(product.with_fields(billing_frequency="monthly"))
		assert ends("2026-09-10 00:00:00", "01:00") is None, "an all-day event has no end time"
		assert plain_text("<p>One</p><div>Two<br>Three</div>") == "One\nTwo\nThree"
