# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Etsy adapter over the recorded shop (tests/fixtures/etsy, copied from wp-woo-idempotent-importer):
OAuth and its rotating refresh token, the reader's paging and the mappers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.common import plain_html, shifted
from idempotent_importer.sources.etsy.adapter import EtsyAdapter, oauth
from idempotent_importer.sources.etsy.auth import EtsyApp, EtsyOAuth, pkce_challenge
from idempotent_importer.sources.etsy.mapping import variant_key
from idempotent_importer.sources.etsy.reader import EtsyReader
from tests.support.transport import (
	ETSY_ROTATED_REFRESH_TOKEN,
	FakeTransport,
	RecordingSleeper,
	etsy,
	scripted,
)

ADAPTER = EtsyAdapter()


@dataclass
class Tokens:
	"""Where the connection keeps its refresh token: every refresh saves the new one."""

	saved: list[str] = field(default_factory=list)

	def save(self, token: str) -> None:
		self.saved.append(token)


@pytest.fixture
def tokens() -> Tokens:
	return Tokens()


@pytest.fixture
def config(tokens: Tokens) -> ConnectionConfig:
	return ConnectionConfig(
		source="etsy",
		connection="etsy-e",
		store_url="https://www.etsy.com/shop/ClayAndCloth",
		api_user="e2e-keystring",
		api_secret="e2e-shared-secret",
		refresh_token="e2e-refresh-token",
		save_refresh_token=tokens.save,
		endpoint_override="http://etsy-fake:8080",
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[EtsyReader, FakeTransport]:
	transport = etsy(state)
	built = ADAPTER.reader(config, transport, RecordingSleeper())
	assert isinstance(built, EtsyReader)
	return built, transport


def nodes(config: ConnectionConfig, kind: Kind, state: str = "v1") -> dict[str, Node]:
	source, _ = reader(config, state)
	found: dict[str, Node] = {}
	cursor: str | None = None
	while True:
		page = source.page(kind, cursor, None, "")
		found |= {node.id().rsplit("/", 1)[-1]: node for node in page.nodes}
		if not page.has_next_page:
			return found
		cursor = page.end_cursor


def mapped(config: ConnectionConfig, kind: Kind, source_id: str, state: str = "v1") -> Desired | None:
	mapper = {m.kind: m for m in ADAPTER.mappers(config)}[kind]
	return mapper.map(nodes(config, kind, state)[source_id])


def values(config: ConnectionConfig, kind: Kind, source_id: str, state: str = "v1") -> Values:
	desired = mapped(config, kind, source_id, state)
	assert desired is not None
	return desired.values()


class TestOAuth:
	def test_the_connect_url_asks_for_read_scopes_with_pkce(self, config: ConnectionConfig) -> None:
		url = oauth(config, scripted()).authorization_url("https://erp.example/cb", "state-1", "verifier")
		query = parse_qs(urlsplit(url).query)
		assert url.startswith("https://www.etsy.com/oauth/connect?")
		assert query["scope"] == ["listings_r transactions_r shops_r email_r"]
		assert query["code_challenge"] == [pkce_challenge("verifier")]
		assert (query["client_id"], query["code_challenge_method"]) == (["e2e-keystring"], ["S256"])

	def test_pkce_challenge_is_an_unpadded_base64url_sha256(self) -> None:
		challenge = pkce_challenge("verifier")
		assert len(challenge) == 43 and "=" not in challenge
		assert set(challenge) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
		assert challenge != pkce_challenge("other verifier")

	def test_every_token_response_rotates_the_refresh_token_and_saves_it(
		self, config: ConnectionConfig, tokens: Tokens
	) -> None:
		transport = etsy("v1")
		flow = oauth(config, transport)
		assert flow.refresh() == "7001.fixture-access-token"
		grant = parse_qs(transport.requests[0].body.decode())
		assert grant["refresh_token"] == ["e2e-refresh-token"]
		assert tokens.saved == [ETSY_ROTATED_REFRESH_TOKEN]
		flow.refresh()
		assert parse_qs(transport.requests[1].body.decode())["refresh_token"] == [ETSY_ROTATED_REFRESH_TOKEN]
		flow.exchange("code-1", "verifier", "https://erp.example/cb")
		assert parse_qs(transport.requests[2].body.decode())["code_verifier"] == ["verifier"]

	def test_an_unconnected_or_refused_shop_stops_the_run(self, config: ConnectionConfig) -> None:
		with pytest.raises(CredentialsRejected, match="not connected"):
			oauth(replace(config, refresh_token=""), scripted()).refresh()
		with pytest.raises(CredentialsRejected, match="connect the shop again"):
			oauth(config, scripted(HttpResponse(400, b'{"error":"invalid_grant"}'))).refresh()
		with pytest.raises(RetryLater):
			oauth(config, scripted(HttpResponse(503, b""))).refresh()
		with pytest.raises(SourceError, match="without tokens"):
			oauth(config, scripted(HttpResponse(200, b'{"access_token":"7001.x"}'))).refresh()
		with pytest.raises(SourceError, match="unreadable"):
			oauth(config, scripted(HttpResponse(200, b"nope"))).refresh()

	def test_configuration_and_secrets(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="keystring and shared secret"):
			EtsyApp("", "secret")
		with pytest.raises(SourceError, match="refresh token"):
			oauth(replace(config, save_refresh_token=None), scripted())
		flow = oauth(config, scripted())
		assert "e2e" not in repr(flow) and "e2e-shared-secret" not in repr(EtsyApp("k", "e2e-shared-secret"))
		assert isinstance(flow, EtsyOAuth)


class TestReader:
	def test_requests_carry_the_api_key_and_the_owners_token(self, config: ConnectionConfig) -> None:
		source, transport = reader(config)
		source.page(Kind.COLLECTION, None, None, "")
		api = [r for r in transport.requests if "/v3/application/" in r.url]
		assert api[0].headers["x-api-key"] == "e2e-keystring:e2e-shared-secret"
		assert api[0].headers["Authorization"] == "Bearer 7001.fixture-access-token"
		assert urlsplit(api[0].url).path == "/v3/application/users/7001/shops"

	def test_listings_are_read_in_every_state(self, config: ConnectionConfig) -> None:
		listings = nodes(config, Kind.PRODUCT)
		assert sorted(listings) == ["601", "602", "603", "604"]
		assert listings["601"].id() == "gid://etsy/etsy-e/Listing/601"

	def test_buyers_come_from_receipts_once_each(self, config: ConnectionConfig) -> None:
		buyers = nodes(config, Kind.CUSTOMER)
		assert list(buyers) == ["9001", "9002"]

	def test_incremental_kinds_ask_for_changes_since(self, config: ConnectionConfig) -> None:
		source, transport = reader(config)
		since = datetime(2026, 9, 1, tzinfo=UTC)
		assert source.is_incremental(Kind.ORDER) and not source.is_incremental(Kind.PRODUCT)
		source.page(Kind.REVIEW, None, since, "")
		source.page(Kind.ORDER, None, since, "")
		queries = [
			parse_qs(urlsplit(r.url).query)
			for r in transport.requests
			if "/reviews" in r.url or "/receipts" in r.url
		]
		assert queries[0]["min_created"] == [str(int(since.timestamp()))]
		assert queries[1]["min_last_modified"] == [str(int(since.timestamp()))]

	def test_policies_are_pages(self, config: ConnectionConfig) -> None:
		policies = nodes(config, Kind.POLICY)
		assert sorted(policies) == ["41", "5001-policy_privacy", "5001-policy_shipping"]
		assert policies["5001-policy_shipping"].string("html") == (
			"<p>Ships within 3 days.</p>\n<p>Tracked shipping only.</p>"
		)
		assert policies["41"].string("html") == "<p>Returns and exchanges are accepted within 30 days.</p>"

	def test_single_objects(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		assert source.node(Kind.PRODUCT, "gid://etsy/etsy-e/Listing/999") is None
		with pytest.raises(SourceError):
			source.node(Kind.REVIEW, "gid://etsy/etsy-e/Review/1")
		with pytest.raises(SourceError):
			source.page(Kind.DISCOUNT, None, None, "")


class TestMappers:
	def test_a_listing_without_variations_is_one_item(self, config: ConnectionConfig) -> None:
		mug = values(config, Kind.PRODUCT, "601")
		assert (mug.string("type"), mug.string("name"), mug.optional_string("sku")) == (
			"simple",
			'Hand-thrown mug "Ocean"',
			"MUG-OCEAN",
		)
		assert (mug.decimal("price"), mug.decimal("stock_qty")) == (Decimal(32), Decimal(4))
		assert mug.strings("collections") == ["gid://etsy/etsy-e/Section/51"]
		assert len(mug.records("images")) == 2
		assert mug.flag("published")

	def test_a_listing_with_variations(self, config: ConnectionConfig) -> None:
		apron = values(config, Kind.PRODUCT, "602")
		variants = apron.records("variants")
		assert [v.string("gid") for v in variants] == [
			"gid://etsy/etsy-e/ListingVariant/602-1001",
			"gid://etsy/etsy-e/ListingVariant/602-1002",
		]
		assert variants[1].decimal("stock_qty") == Decimal(0)
		assert apron.records("attributes")[0].all() == {"name": "Color", "values": ["Natural", "Charcoal"]}
		assert variants[0].required_record("weight").all() == {"value": Decimal("0.4"), "unit": "kg"}

	def test_hidden_listings_are_unpublished(self, config: ConnectionConfig) -> None:
		assert not values(config, Kind.PRODUCT, "603").flag("published")
		assert not values(config, Kind.PRODUCT, "604").flag("published")

	def test_a_paid_receipt_balances(self, config: ConnectionConfig) -> None:
		receipt = values(config, Kind.ORDER, "801")
		lines = receipt.records("lines")
		goods = sum((line.decimal("qty") * line.decimal("rate") for line in lines), Decimal(0))
		charges = sum((c.decimal("amount") for c in receipt.records("charges")), Decimal(0))
		assert goods - receipt.decimal("discount") + charges == receipt.decimal("total") == Decimal("134.4")
		assert lines[1].optional_string("variant") == "gid://etsy/etsy-e/ListingVariant/602-1002"
		assert receipt.optional_string("customer") == "gid://etsy/etsy-e/User/9001"
		assert receipt.flag("submitted")
		shipment = receipt.records("shipments")[0]
		assert (shipment.optional_string("carrier"), shipment.optional_string("tracking_number")) == (
			"Canada Post",
			"CP123456789CA",
		)

	def test_an_unpaid_receipt_is_a_draft_and_a_refund_one_adjustment(self, config: ConnectionConfig) -> None:
		assert not values(config, Kind.ORDER, "802").flag("submitted")
		refund = values(config, Kind.ORDER, "801", "v2").records("refunds")[0]
		assert (refund.decimal("amount"), refund.optional_string("reason")) == (Decimal(10), "Chipped handle")
		assert refund.records("lines")[0].decimal("rate") == Decimal(10)
		cancelled = nodes(config, Kind.ORDER)["801"].with_fields(status="canceled")
		assert {m.kind: m for m in ADAPTER.mappers(config)}[Kind.ORDER].map(cancelled) is None

	def test_buyers_reviews_and_sections(self, config: ConnectionConfig) -> None:
		ada = values(config, Kind.CUSTOMER, "9001")
		assert (ada.string("name"), ada.optional_string("email")) == ("ada@example.com", "ada@example.com")
		review = values(config, Kind.REVIEW, "70011")
		assert (review.string("product"), review.decimal("rating")) == (
			"gid://etsy/etsy-e/Listing/601",
			Decimal(1),
		)
		assert values(config, Kind.COLLECTION, "51").string("name") == "Mugs"

	def test_a_listing_without_inventory(self, config: ConnectionConfig) -> None:
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.PRODUCT]
		mug = nodes(config, Kind.PRODUCT)["601"]
		with pytest.raises(SourceError, match="without its inventory"):
			mapper.map(mug.with_fields(inventory=None))
		with pytest.raises(Unsupported, match="nothing to sell"):
			mapper.map(mug.with_fields(inventory={"products": []}))


def test_shared_helpers() -> None:
	assert variant_key(1, {}) == "1"
	assert variant_key(1, {9: "5", 2: "7.8"}) == "1-7.8.5"
	assert shifted(1999, 100, "x") == Decimal("19.99")
	with pytest.raises(SourceError, match="power of ten"):
		shifted(1, 3, "x")
	assert plain_html("a &amp; b\nc\n\nd") == "<p>a &amp; b<br>c</p>\n<p>d</p>"
	assert plain_html(None) == ""


def test_the_adapter(config: ConnectionConfig) -> None:
	assert {mapper.kind for mapper in ADAPTER.mappers(config)} == set(ADAPTER.kinds())
	assert ADAPTER.filter_hint() is None
	assert json.dumps(ADAPTER.kinds())
