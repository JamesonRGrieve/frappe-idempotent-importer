# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Salesforce adapter over the recorded studio org (tests/fixtures/salesforce, proven against
tests/schemas/salesforce.json): OAuth at the org's My Domain, the fields each describe offers (custom ones by
label), keyset paging after the last id, incremental queries, child relationships continued, the daily
allocation guard, and every mapper."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from idempotent_importer.core.errors import RetryLater, SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.oauth import TokenGrant
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.salesforce.adapter import GRANT_PATH, SalesforceAdapter
from idempotent_importer.sources.salesforce.reader import (
	ACCOUNT,
	PAGE_SIZE,
	LimitGuard,
	SalesforceReader,
	soql,
	type_identity,
)
from tests.support.transport import (
	OAUTH_ACCESS_TOKEN,
	FakeTransport,
	RecordingSleeper,
	oauth_source,
	scripted,
)

ADAPTER = SalesforceAdapter()
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()
ORG = "https://northwind.my.salesforce.com"
RIVER, DELTA = "0015e000002AaAAAA1", "0015e000002AbBBAA2"


def gid(type_: str, id_: str) -> str:
	return f"gid://salesforce/salesforce-f/{type_}/{id_}"


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="salesforce",
		connection="salesforce-f",
		store_url=ORG,
		api_user="e2e-salesforce-key",
		api_secret="e2e-salesforce-secret",
		refresh_token="e2e-salesforce-refresh",
		save_refresh_token=lambda token: None,
		endpoint_override="http://salesforce-fake:8080",
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[SalesforceReader, FakeTransport]:
	fake = oauth_source("salesforce", state, GRANT_PATH, rotates=False)
	built = ADAPTER.reader(config, fake, RecordingSleeper(NOW))
	assert isinstance(built, SalesforceReader)
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
	def test_the_approval_goes_to_the_orgs_my_domain(self, config: ConnectionConfig) -> None:
		url = ADAPTER.oauth(config, scripted()).authorization_url("https://erp/cb", "s", "v")
		assert url.startswith(f"{ORG}/services/oauth2/authorize?")
		assert parse_qs(urlsplit(url).query)["scope"] == ["api refresh_token"]

	def test_requests_carry_the_token(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.EMPLOYEE, None, None, "")
		query = next(r for r in fake.requests if "/query" in r.url)
		assert query.headers["Authorization"] == f"Bearer {OAUTH_ACCESS_TOKEN}"
		assert ADAPTER.connected(config, scripted(), TokenGrant("a"), {}) == {}

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="My Domain"):
			ADAPTER.oauth(replace(config, store_url="northwind"), scripted())
		with pytest.raises(SourceError, match="consumer key"):
			ADAPTER.oauth(replace(config, api_user=""), scripted())


class TestReader:
	def test_queries_select_what_the_describe_offers(self) -> None:
		query = soql(ACCOUNT, ["Name", "Account_Tier__c"], RIVER, datetime(2026, 10, 1, tzinfo=UTC))
		assert query.startswith("SELECT Id, Name, Account_Tier__c, (SELECT Id, FirstName")
		assert query.endswith(
			f"FROM Account WHERE SystemModstamp >= 2026-10-01T00:00:00Z AND Id > '{RIVER}' ORDER BY Id LIMIT 200"
		)
		with pytest.raises(SourceError, match="Not a Salesforce id"):
			soql(ACCOUNT, [], "x' OR Name != '", None)

	def test_a_full_page_continues_after_its_last_id(self, config: ConnectionConfig) -> None:
		queries: list[str] = []

		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if method == "POST":
				return HttpResponse(200, b'{"access_token": "t"}')
			if url.endswith("/describe"):
				return HttpResponse(
					200, json.dumps({"name": "Lead", "label": "Lead", "custom": False, "fields": []}).encode()
				)
			queries.append(parse_qs(urlsplit(url).query)["q"][0])
			records = [
				{"attributes": {"type": "Lead"}, "Id": f"00Q5e000003A{n:06d}"} for n in range(PAGE_SIZE)
			]
			return HttpResponse(
				200, json.dumps({"totalSize": PAGE_SIZE, "done": True, "records": records}).encode()
			)

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		page = source.page(Kind.LEAD, None, None, "")
		assert page.has_next_page and page.end_cursor == "0:00Q5e000003A000199"
		source.page(Kind.LEAD, page.end_cursor, None, "")
		assert "Id > '00Q5e000003A000199'" in queries[1]

	def test_a_long_child_list_continues(self, config: ConnectionConfig) -> None:
		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if method == "POST":
				return HttpResponse(200, b'{"access_token": "t"}')
			if url.endswith("/describe"):
				return HttpResponse(
					200, b'{"name": "Account", "label": "Account", "custom": false, "fields": []}'
				)
			if "/query/01g" in url:
				rest = {
					"totalSize": 2,
					"done": True,
					"records": [{"attributes": {"type": "Contact"}, "Id": "0035e000003AbBBAA2"}],
				}
				return HttpResponse(200, json.dumps(rest).encode())
			contacts = {
				"totalSize": 2,
				"done": False,
				"nextRecordsUrl": "/services/data/v62.0/query/01gD0000002HU6KIAW-2000",
				"records": [{"attributes": {"type": "Contact"}, "Id": "0035e000003AaAAAA1"}],
			}
			account = {
				"attributes": {"type": "Account"},
				"Id": RIVER,
				"Name": "River Co",
				"Contacts": contacts,
			}
			return HttpResponse(
				200, json.dumps({"totalSize": 1, "done": True, "records": [account]}).encode()
			)

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		account = source.page(Kind.PROSPECT, None, None, "").nodes[0]
		people = account.required_child("Contacts").children("records")
		assert [p.string("Id") for p in people] == ["0035e000003AaAAAA1", "0035e000003AbBBAA2"]

	def test_custom_fields_by_label_and_picklists_by_label(self, config: ConnectionConfig) -> None:
		river = nodes(config, Kind.PROSPECT)[gid("Account", RIVER)]
		assert river.raw()["custom"] == {"Account Tier": "Gold"}
		assert not any("CurrencyIsoCode" in n.raw() for n in nodes(config, Kind.OPPORTUNITY).values())

	def test_incremental_kinds(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		assert source.is_incremental(Kind.LEAD) and not source.is_incremental(Kind.CUSTOMER)
		assert not source.is_incremental(Kind.OPPORTUNITY_TYPE)
		page = source.page(Kind.LEAD, None, datetime(2026, 10, 1, tzinfo=UTC), "")
		assert page.nodes == [] and not page.has_next_page

	def test_customers_are_customer_accounts_and_those_ordered_for(self, config: ConnectionConfig) -> None:
		assert set(nodes(config, Kind.CUSTOMER)) == {gid("Account", RIVER)}

	def test_node_by_identity(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		with pytest.raises(SourceError, match="by itself"):
			source.node(Kind.LEAD, gid("Account", RIVER))

	def test_the_daily_allocation_pauses_the_run(self) -> None:
		calls: list[str] = []

		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			calls.append(url)
			return HttpResponse(200, b"{}", {"sforce-limit-info": "api-usage=9000/10000"})

		guard = LimitGuard(FakeTransport(route))
		assert guard.get("https://x/1", {}, 1).status == 200
		paused = guard.get("https://x/2", {}, 1)
		assert paused.status == 429 and paused.header("retry-after") == "3600" and len(calls) == 1

	def test_a_request_refused_for_the_limit_pauses_rather_than_holds(self, config: ConnectionConfig) -> None:
		refused = HttpResponse(
			403, b'[{"errorCode":"REQUEST_LIMIT_EXCEEDED","message":"TotalRequests Limit exceeded."}]'
		)
		transport = scripted(HttpResponse(200, b'{"access_token": "t"}'), refused)
		source = ADAPTER.reader(config, transport, RecordingSleeper(NOW))
		with pytest.raises(RetryLater):
			source.page(Kind.LEAD, None, None, "")


class TestMappers:
	def test_users(self, config: ConnectionConfig) -> None:
		jamie = values(config, Kind.EMPLOYEE, gid("User", "0055e000001AbBBAA2"))
		assert jamie.string("reports_to") == gid("User", "0055e000001AaAAAA1")
		assert jamie.string("employee_number") == "E-2"
		assert (
			values(config, Kind.EMPLOYEE, gid("User", "0055e000001AbBBAA2"), "v2").string("status")
			== "Inactive"
		)

	def test_products(self, config: ConnectionConfig) -> None:
		session = values(config, Kind.PRODUCT, gid("Product2", "01t5e000000AaAAAA1"), "v2")
		assert session.decimal("price") == Decimal(175) and session.string("sku") == "SESSION"

	def test_accounts(self, config: ConnectionConfig) -> None:
		river = values(config, Kind.PROSPECT, gid("Account", RIVER))
		assert [p.string("name") for p in river.records("contacts")] == ["Riley River", "Sam Shore"]
		assert [n.string("text") for n in river.records("crm_notes")] == ["Spring\n\nExpanding in spring"]
		customer = values(config, Kind.CUSTOMER, gid("Account", RIVER))
		assert customer.string("type") == "Company" and customer.all()["custom"] == {"Account Tier": "Gold"}
		assert customer.records("addresses")[0].string("country_code") == "CA"

	def test_leads(self, config: ConnectionConfig) -> None:
		quinn = values(config, Kind.LEAD, gid("Lead", "00Q5e000003AaAAAA1"))
		assert quinn.string("status") == "Open" and quinn.all()["custom"] == {
			"Preferred Contact Time": "Mornings"
		}
		closed = values(config, Kind.LEAD, gid("Lead", "00Q5e000003AaAAAA1"), "v2")
		assert closed.flag("disabled")
		assert values(config, Kind.LEAD, gid("Lead", "00Q5e000003AbBBAA2")).string("status") == "Replied"
		assert values(config, Kind.LEAD, gid("Lead", "00Q5e000003AcCCAA3")).string("status") == "Converted"

	def test_opportunities_at_their_stage(self, config: ConnectionConfig) -> None:
		types = nodes(config, Kind.OPPORTUNITY_TYPE)
		assert type_identity(config, "Existing Customer - Upgrade") in types
		deal = values(config, Kind.OPPORTUNITY, gid("Opportunity", "0065e000004AaAAAA1"))
		assert (deal.string("party_kind"), deal.string("party")) == ("prospect", gid("Account", RIVER))
		assert (deal.string("status"), deal.string("sales_stage")) == ("Open", "Proposal/Price Quote")
		assert deal.string("opportunity_type") == type_identity(config, "New Customer")
		assert (deal.string("currency"), deal.decimal("amount")) == ("CAD", Decimal(4800))
		assert (
			values(config, Kind.OPPORTUNITY, gid("Opportunity", "0065e000004AaAAAA1"), "v2").string("status")
			== "Converted"
		)
		assert (
			values(config, Kind.OPPORTUNITY, gid("Opportunity", "0065e000004AbBBAA2")).string("status")
			== "Lost"
		)

	def test_activities(self, config: ConnectionConfig) -> None:
		call = values(config, Kind.COMMUNICATION, gid("Task", "00T5e000005AaAAAA1"))
		assert (call.string("medium"), call.flag("sent")) == ("Phone", True)
		assert call.required_record("with").string("kind") == "opportunity"
		assert call.string("content") == "Walked through the brief.\n754 seconds, Connected"
		email = values(config, Kind.COMMUNICATION, gid("Task", "00T5e000005AcCCAA3"))
		assert (email.string("medium"), email.required_record("with").string("kind")) == ("Email", "prospect")
		assert mapped(config, Kind.COMMUNICATION, gid("Task", "00T5e000005AbBBAA2")) is None, "a to-do"
		message = values(config, Kind.COMMUNICATION, gid("EmailMessage", "02s5e000007AaAAAA1"))
		assert (message.flag("sent"), message.string("email")) == (False, "sam@river.example")
		task = values(config, Kind.TODO, gid("Task", "00T5e000005AbBBAA2"))
		assert (task.string("status"), task.required_record("about").string("kind")) == ("Open", "lead")
		assert mapped(config, Kind.TODO, gid("Task", "00T5e000005AaAAAA1")) is None, "a logged call"
		meeting = values(config, Kind.EVENT, gid("Event", "00U5e000006AaAAAA1"))
		assert meeting.string("starts_on") == "2026-09-10T16:00:00Z"
		assert [p.string("kind") for p in meeting.records("participants")] == ["opportunity", "prospect"]

	def test_quotes_and_orders(self, config: ConnectionConfig) -> None:
		quote = values(config, Kind.DRAFT_ORDER, gid("Quote", "0Q05e000008AaAAAA1"))
		assert quote.required_record("quoted").all() == {"kind": "prospect", "party": gid("Account", DELTA)}
		assert (quote.decimal("discount"), quote.decimal("total")) == (Decimal(30), Decimal("310.5"))
		order = values(config, Kind.SALES_ORDER, gid("Order", "8015e000009AaAAAA1"))
		assert (order.decimal("total"), order.flag("submitted"), order.string("remarks")) == (
			Decimal(135),
			True,
			"PO PO-77",
		)
		draft = values(config, Kind.SALES_ORDER, gid("Order", "8015e000009AbBBAA2"))
		assert not draft.flag("submitted")

	def test_a_quote_that_doesnt_add_up(self, config: ConnectionConfig) -> None:
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.DRAFT_ORDER]
		quote = nodes(config, Kind.DRAFT_ORDER)[gid("Quote", "0Q05e000008AaAAAA1")]
		with pytest.raises(Unsupported, match="grand total"):
			mapper.map(quote.with_fields(GrandTotal=311.0))
