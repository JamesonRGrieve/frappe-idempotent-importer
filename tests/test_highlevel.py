# SPDX-License-Identifier: AGPL-3.0-or-later
"""The HighLevel adapter over the recorded studio (tests/fixtures/highlevel, proven against HighLevel's API
v2 OpenAPI documents): OAuth and the sub-account the token names, each endpoint's paging, the version
header, conversations read in windows, the billed contacts as customers, and every mapper."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.oauth import TokenGrant
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.highlevel.adapter import GRANT_PATH, HighLevelAdapter
from idempotent_importer.sources.highlevel.reader import HighLevelReader
from tests.support.transport import (
	OAUTH_ACCESS_TOKEN,
	FakeTransport,
	RecordingSleeper,
	oauth_source,
	scripted,
)

ADAPTER = HighLevelAdapter()
LOCATION = "loc0harbour0studio01"
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()


def gid(type_: str, id_: str) -> str:
	return f"gid://highlevel/highlevel-h/{type_}/{id_}"


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="highlevel",
		connection="highlevel-h",
		store_url="https://app.gohighlevel.com",
		api_user="e2e-highlevel-client",
		api_secret="e2e-highlevel-secret",
		account=LOCATION,
		refresh_token="e2e-highlevel-refresh",
		save_refresh_token=lambda token: None,
		endpoint_override="http://highlevel-fake:8080",
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[HighLevelReader, FakeTransport]:
	fake = oauth_source("highlevel", state, GRANT_PATH)
	built = ADAPTER.reader(config, fake, RecordingSleeper(NOW))
	assert isinstance(built, HighLevelReader)
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


def desired(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> object:
	mapper = {m.kind: m for m in ADAPTER.mappers(config)}[kind]
	return mapper.map(nodes(config, kind, state)[key])


def values(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> Values:
	mapped = desired(config, kind, key, state)
	assert mapped is not None and hasattr(mapped, "values")
	result = mapped.values()
	assert isinstance(result, Values)
	return result


def query_of(url: str) -> dict[str, list[str]]:
	return parse_qs(urlsplit(url).query)


class TestOAuth:
	def test_the_approval_chooses_a_sub_account_and_asks_to_read(self, config: ConnectionConfig) -> None:
		url = ADAPTER.oauth(config, scripted()).authorization_url("https://erp/cb", "s", "v")
		assert url.startswith("https://marketplace.gohighlevel.com/v2/oauth/chooselocation?")
		scopes = query_of(url)["scope"][0].split(" ")
		assert "contacts.readonly" in scopes and all(scope.endswith(".readonly") for scope in scopes)

	def test_the_token_names_the_sub_account(self, config: ConnectionConfig) -> None:
		grant = TokenGrant("a", {"locationId": LOCATION, "userType": "Location"})
		assert ADAPTER.connected(config, scripted(), grant, {}) == {"api_account": LOCATION}
		with pytest.raises(SourceError, match="sub-account"):
			ADAPTER.connected(config, scripted(), TokenGrant("a"), {})

	def test_requests_carry_the_token_and_their_version(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.EMPLOYEE, None, None, "")
		source.page(Kind.COMMUNICATION, None, None, "")
		users = next(r for r in fake.requests if "/users/" in r.url)
		assert users.headers["Authorization"] == f"Bearer {OAUTH_ACCESS_TOKEN}"
		assert users.headers["Version"] == "2021-07-28"
		search = next(r for r in fake.requests if "/conversations/search" in r.url)
		assert search.headers["Version"] == "2021-04-15"

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="client ID and secret"):
			ADAPTER.oauth(replace(config, api_user=""), scripted())
		with pytest.raises(SourceError, match="Connect to HighLevel"):
			ADAPTER.reader(replace(config, account=""), scripted(), RecordingSleeper(NOW))


class TestReader:
	def test_nothing_is_incremental(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		assert not any(source.is_incremental(kind) for kind in ADAPTER.kinds())

	def test_contacts_page_after_the_last_one(self, config: ConnectionConfig) -> None:
		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if method == "POST":
				return HttpResponse(200, b'{"access_token": "t", "refresh_token": "r"}')
			if "/contacts/" in url and "/tasks" not in url and urlsplit(url).path == "/contacts/":
				listed = [{"id": f"c{n}", "dateAdded": "2026-08-03T15:00:00.000Z"} for n in range(100)]
				return HttpResponse(200, json.dumps({"contacts": listed, "count": 250}).encode())
			return HttpResponse(200, b'{"tasks": []}')

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		page = source.page(Kind.TODO, None, None, "")
		assert page.has_next_page and page.end_cursor == "c99|1785769200000"
		fake = FakeTransport(route)
		ADAPTER.reader(config, fake, RecordingSleeper(NOW)).page(Kind.TODO, page.end_cursor, None, "")
		assert query_of(fake.requests[1].url)["startAfterId"] == ["c99"]

	def test_offset_lists_page_until_a_short_page(self, config: ConnectionConfig) -> None:
		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if method == "POST":
				return HttpResponse(200, b'{"access_token": "t", "refresh_token": "r"}')
			invoices = [{"_id": f"i{n}", "status": "void"} for n in range(100)]
			return HttpResponse(200, json.dumps({"invoices": invoices, "total": 101}).encode())

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		page = source.page(Kind.SALES_INVOICE, None, None, "")
		assert page.has_next_page and page.end_cursor == "100"

	def test_conversation_windows_halve_until_a_page_holds_them(self, config: ConnectionConfig) -> None:
		windows: list[tuple[int, int]] = []

		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			if method == "POST":
				return HttpResponse(200, b'{"access_token": "t", "refresh_token": "r"}')
			query = query_of(url)
			start, end = int(query["startDate"][0]), int(query["endDate"][0])
			windows.append((start, end))
			total = 150 if len(windows) == 1 else 0
			return HttpResponse(200, json.dumps({"conversations": [], "total": total}).encode())

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		page = source.page(Kind.COMMUNICATION, None, None, "")
		(first_start, first_end), (second_start, second_end) = windows
		assert first_start == second_start and second_end == first_start + (first_end - first_start) // 2
		assert page.has_next_page and page.end_cursor == f"{second_end + 1}|{first_end}"

	def test_the_billed_contacts_are_customers(self, config: ConnectionConfig) -> None:
		assert set(nodes(config, Kind.CUSTOMER)) == {gid("Contact", "contact001")}

	def test_kinds_read_their_records(self, config: ConnectionConfig) -> None:
		assert len(nodes(config, Kind.LEAD)) == 3
		assert set(nodes(config, Kind.COMMUNICATION)) == {gid("Message", f"message00{n}") for n in (1, 2, 3)}
		assert set(nodes(config, Kind.PAYMENT_ENTRY)) == {
			gid("Transaction", "transaction001"),
			gid("Transaction", "transaction002"),
			gid("TransactionRefund", "transaction002"),
			gid("Transaction", "transaction003"),
		}
		business = nodes(config, Kind.PROSPECT)[gid("Business", "business001")]
		assert business.strings("contact_ids") == ["contact001"]

	def test_one_object_by_its_identity(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		lead = source.node(Kind.LEAD, gid("Contact", "contact001"))
		assert lead is not None and lead.children("notes")
		assert source.node(Kind.CUSTOMER, gid("Contact", "contact009")) is None
		with pytest.raises(SourceError, match="by itself"):
			source.node(Kind.EVENT, gid("Appointment", "appointment001"))
		with pytest.raises(SourceError, match="no supplier"):
			source.page(Kind.SUPPLIER, None, None, "")

	def test_every_kind_has_a_mapper(self, config: ConnectionConfig) -> None:
		assert {mapper.kind for mapper in ADAPTER.mappers(config)} == set(ADAPTER.kinds())


class TestMappers:
	def test_users(self, config: ConnectionConfig) -> None:
		morgan = values(config, Kind.EMPLOYEE, gid("User", "user001"))
		assert (morgan.string("first_name"), morgan.optional_string("company_email")) == (
			"Morgan",
			"morgan@studio.example",
		)
		assert values(config, Kind.EMPLOYEE, gid("User", "user002"), "v2").string("status") == "Inactive"

	def test_products(self, config: ConnectionConfig) -> None:
		consultation = values(config, Kind.PRODUCT, gid("Product", "product001"))
		assert (consultation.string("type"), consultation.decimal("price")) == ("simple", Decimal(150))
		assert values(config, Kind.PRODUCT, gid("Product", "product001"), "v2").decimal("price") == 175
		tee = values(config, Kind.PRODUCT, gid("Product", "product002"))
		assert tee.string("type") == "variable" and tee.flag("is_stock_item")
		assert [
			(v.string("gid"), v.required_record("attributes").all(), v.decimal("stock_qty"))
			for v in tee.records("variants")
		] == [
			(gid("Price", "price002"), {"Size": "S"}, Decimal(10)),
			(gid("Price", "price003"), {"Size": "M"}, Decimal(4)),
		]
		assert tee.records("images")[0].string("url") == "http://highlevel-fake:8080/media/studio-tee.png"
		plan = desired(config, Kind.PRODUCT, gid("Product", "product003"))
		assert isinstance(plan, Desired) and "subscription plans" in plan.warnings[0]
		products = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.PRODUCT]
		two = Node({"id": "x", "name": "Two", "prices": [{"_id": "a", "type": "one_time", "amount": 1}] * 2})
		with pytest.raises(Unsupported, match="price lists"):
			products.map(two)

	def test_leads_customers_and_prospects(self, config: ConnectionConfig) -> None:
		riley = values(config, Kind.LEAD, gid("Contact", "contact001"))
		assert (riley.string("status"), riley.optional_string("company")) == ("Lead", "River Co")
		assert riley.records("crm_notes")[0].all() == {
			"gid": gid("Note", "note001"),
			"date": "2026-09-02T14:30:00Z",
			"text": "Follow-up\n\nWants the spring catalogue",
		}
		assert values(config, Kind.LEAD, gid("Contact", "contact003")).string("status") == "Do Not Contact"
		customer = values(config, Kind.CUSTOMER, gid("Contact", "contact001"))
		assert (customer.string("name"), customer.string("type")) == ("Riley River", "Individual")
		assert customer.records("addresses")[0].string("country_code") == "CA"
		river = values(config, Kind.PROSPECT, gid("Business", "business001"))
		assert river.strings("leads") == [gid("Contact", "contact001")]
		assert river.records("addresses")[0].string("line1") == "1 Harbour Rd"

	def test_pipelines_and_opportunities(self, config: ConnectionConfig) -> None:
		assert values(config, Kind.OPPORTUNITY_TYPE, gid("Pipeline", "pipeline001")).string("name") == "Sales"
		deal = values(config, Kind.OPPORTUNITY, gid("Opportunity", "opportunity001"))
		assert (
			deal.string("party_kind"),
			deal.string("party"),
			deal.string("status"),
			deal.decimal("amount"),
			deal.optional_string("opportunity_type"),
		) == ("lead", gid("Contact", "contact002"), "Open", Decimal(1200), gid("Pipeline", "pipeline001"))
		assert values(config, Kind.OPPORTUNITY, gid("Opportunity", "opportunity001"), "v2").string(
			"status"
		) == ("Converted")

	def test_messages(self, config: ConnectionConfig) -> None:
		email = values(config, Kind.COMMUNICATION, gid("Message", "message001"))
		assert (email.string("medium"), email.flag("sent"), email.optional_string("email")) == (
			"Email",
			False,
			"riley@example.com",
		)
		sms = values(config, Kind.COMMUNICATION, gid("Message", "message002"))
		assert (sms.string("medium"), sms.flag("sent"), sms.optional_string("phone")) == (
			"SMS",
			True,
			"+19025550001",
		)
		call = values(config, Kind.COMMUNICATION, gid("Message", "message003"))
		assert (call.string("medium"), call.string("content")) == ("Phone", "185 seconds, completed")

	def test_appointments_and_tasks(self, config: ConnectionConfig) -> None:
		call = values(config, Kind.EVENT, gid("Appointment", "appointment001"))
		assert (call.string("starts_on"), call.string("status"), call.records("participants")[0].all()) == (
			"2026-10-22 10:00:00",
			"Open",
			{"kind": "lead", "party": gid("Contact", "contact002")},
		)
		task = values(config, Kind.TODO, gid("Task", "task001"))
		assert (task.string("status"), task.optional_string("date")) == ("Open", "2026-10-30")

	def test_invoices_and_payments(self, config: ConnectionConfig) -> None:
		invoice = values(config, Kind.SALES_INVOICE, gid("Invoice", "invoice001"))
		assert (invoice.string("number"), invoice.decimal("total"), invoice.flag("submitted")) == (
			"1001",
			Decimal(113),
			True,
		)
		line = invoice.records("lines")[0]
		assert (line.decimal("qty"), line.decimal("rate"), line.optional_string("variant")) == (
			Decimal(2),
			Decimal(50),
			gid("Price", "price001"),
		)
		assert [tax.decimal("amount") for tax in invoice.records("taxes")] == [Decimal(13)]
		assert not values(config, Kind.SALES_INVOICE, gid("Invoice", "invoice002")).flag("submitted")
		assert values(config, Kind.SALES_INVOICE, gid("Invoice", "invoice002"), "v2").flag("submitted")
		assert desired(config, Kind.SALES_INVOICE, gid("Invoice", "invoice003")) is None, "void"
		paid = values(config, Kind.PAYMENT_ENTRY, gid("Transaction", "transaction001"))
		assert [a.all() for a in paid.records("allocations")] == [
			{"kind": "sales_invoice", "document": gid("Invoice", "invoice001"), "amount": Decimal(113)}
		]
		advance = values(config, Kind.PAYMENT_ENTRY, gid("Transaction", "transaction002"))
		assert (advance.decimal("amount"), advance.records("allocations")) == (Decimal(50), [])
		refund = values(config, Kind.PAYMENT_ENTRY, gid("TransactionRefund", "transaction002"))
		assert (refund.string("payment_type"), refund.decimal("amount")) == ("Pay", Decimal(10))
		assert desired(config, Kind.PAYMENT_ENTRY, gid("Transaction", "transaction003")) is None, "failed"
