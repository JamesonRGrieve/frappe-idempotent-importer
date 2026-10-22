# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Webflow adapter over the recorded studio site (tests/fixtures/webflow, proven against Webflow's Data API
v2 OpenAPI document): OAuth with a lasting access token and the site it opens, offset paging, e-commerce
collections told from the CMS, inventory per SKU, page content, incremental CMS reads, and every mapper."""

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
from idempotent_importer.sources.webflow.adapter import GRANT_PATH, WebflowAdapter
from idempotent_importer.sources.webflow.reader import WebflowReader
from tests.support.transport import FakeTransport, RecordingSleeper, oauth_source, rest_source, scripted

ADAPTER = WebflowAdapter()
NOW = datetime(2026, 10, 20, 12, tzinfo=UTC).timestamp()
SITE = "580e63e98c9a982ac9b8b741"
PRINT, MUG = "6a10000000000000000000a1", "6a10000000000000000000a2"
TOKEN = "e2e-webflow-lasting-token"


def gid(type_: str, id_: str) -> str:
	return f"gid://webflow/webflow-w/{type_}/{id_}"


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="webflow",
		connection="webflow-w",
		store_url="http://webflow-fake:8080",
		api_user="e2e-webflow-client",
		api_secret="e2e-webflow-secret",
		account=SITE,
		refresh_token=TOKEN,
		save_refresh_token=lambda token: None,
		endpoint_override="http://webflow-fake:8080",
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[WebflowReader, FakeTransport]:
	fake = oauth_source("webflow", state, GRANT_PATH, rotates=False)
	built = ADAPTER.reader(config, fake, RecordingSleeper(NOW))
	assert isinstance(built, WebflowReader)
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
	def test_the_approval_asks_to_read_and_the_token_is_kept(self, config: ConnectionConfig) -> None:
		url = ADAPTER.oauth(config, scripted()).authorization_url("https://erp/cb", "s", "v")
		assert url.startswith("https://webflow.com/oauth/authorize?")
		assert all(scope.endswith(":read") for scope in parse_qs(urlsplit(url).query)["scope"][0].split(" "))
		source, fake = reader(config)
		source.page(Kind.URL_REDIRECT, None, None, "")
		assert all(r.method == "GET" for r in fake.requests), "a lasting token needs no token request"
		assert fake.requests[0].headers["Authorization"] == f"Bearer {TOKEN}"

	def test_the_site_the_token_opens_becomes_the_account(self, config: ConnectionConfig) -> None:
		fake = rest_source("webflow", "v1")
		assert ADAPTER.connected(replace(config, account=""), fake, TokenGrant("a"), {}) == {
			"api_account": SITE
		}
		assert ADAPTER.connected(config, fake, TokenGrant("a"), {}) == {}
		several = scripted(HttpResponse(200, json.dumps({"sites": [{"id": "a"}, {"id": "b"}]}).encode()))
		with pytest.raises(SourceError, match="several sites"):
			ADAPTER.connected(replace(config, account=""), several, TokenGrant("a"), {})

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="choose the site"):
			ADAPTER.reader(replace(config, account=""), scripted(), RecordingSleeper(NOW))


class TestReader:
	def test_lists_page_by_offset_until_the_total(self, config: ConnectionConfig) -> None:
		def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
			offset = int(parse_qs(urlsplit(url).query)["offset"][0])
			redirects = [{"id": f"r{offset + n}", "fromUrl": "/a", "toUrl": "/b"} for n in range(100)]
			page = {"redirects": redirects, "pagination": {"limit": 100, "offset": offset, "total": 200}}
			return HttpResponse(200, json.dumps(page).encode())

		source = ADAPTER.reader(config, FakeTransport(route), RecordingSleeper(NOW))
		first = source.page(Kind.URL_REDIRECT, None, None, "")
		assert first.has_next_page and first.end_cursor == "100"
		assert not source.page(Kind.URL_REDIRECT, first.end_cursor, None, "").has_next_page

	def test_cms_items_are_incremental(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		assert source.is_incremental(Kind.ARTICLE) and not source.is_incremental(Kind.PRODUCT)
		source.page(Kind.ARTICLE, None, datetime(2026, 10, 1, tzinfo=UTC), "")
		items = next(r for r in fake.requests if "/items" in r.url)
		assert parse_qs(urlsplit(items.url).query)["lastUpdated[gte]"] == ["2026-10-01T00:00:00.000Z"]

	def test_products_carry_their_skus_inventory(self, config: ConnectionConfig) -> None:
		product = nodes(config, Kind.PRODUCT)[gid("Product", PRINT)]
		inventory = [sku.required_child("inventory").integer("quantity") for sku in product.children("skus")]
		assert inventory == [12, 4]

	def test_e_commerce_collections_arent_blogs(self, config: ConnectionConfig) -> None:
		assert set(nodes(config, Kind.BLOG)) == {gid("Collection", "6a10000000000000000000c4")}

	def test_customers_are_who_orders_name(self, config: ConnectionConfig) -> None:
		assert set(nodes(config, Kind.CUSTOMER)) == {
			gid("Customer", "riley@river.example"),
			gid("Customer", "sam@shore.example"),
		}

	def test_node_by_identity(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		with pytest.raises(SourceError, match="by itself"):
			source.node(Kind.ORDER, gid("Order", "fc7-128"))


class TestMappers:
	def test_assets_and_categories(self, config: ConnectionConfig) -> None:
		asset = values(config, Kind.ASSET, gid("Asset", "6a10000000000000000000e1"))
		assert asset.string("alt") == "The studio"
		category = values(config, Kind.COLLECTION, gid("Category", "6a10000000000000000000d1"))
		assert (category.string("name"), category.string("description")) == ("Prints", "Art prints")

	def test_products(self, config: ConnectionConfig) -> None:
		prints = values(config, Kind.PRODUCT, gid("Product", PRINT))
		assert prints.string("type") == "variable" and prints.strings("collections") == [
			gid("Category", "6a10000000000000000000d1")
		]
		a4, a3 = prints.records("variants")
		assert (a4.decimal("price"), a4.decimal("stock_qty"), a4.required_record("attributes").all()) == (
			Decimal(45),
			Decimal(12),
			{"Size": "A4"},
		)
		assert (a3.decimal("price"), a3.required_record("sale").decimal("price")) == (
			Decimal(75),
			Decimal(60),
		)
		assert prints.all()["custom"] == {"Care instructions": "Keep out of direct sun"}
		mug = values(config, Kind.PRODUCT, gid("Product", MUG), "v2")
		assert (mug.string("type"), mug.decimal("price"), mug.optional_decimal("stock_qty")) == (
			"simple",
			Decimal(24),
			None,
		)

	def test_orders(self, config: ConnectionConfig) -> None:
		order = values(config, Kind.ORDER, gid("Order", "fc7-128"))
		assert (order.decimal("discount"), order.decimal("total"), order.flag("submitted")) == (
			Decimal(9),
			Decimal("103.15"),
			True,
		)
		assert [c.string("kind") for c in order.records("charges")] == ["shipping", "tax"]
		assert order.string("customer") == gid("Customer", "riley@river.example")
		assert order.records("shipments")[0].string("tracking_number") == "CP123"
		assert order.string("remarks") == "Please wrap it"
		line = order.records("lines")[0]
		assert (line.string("variant"), line.decimal("rate")) == (
			gid("Sku", "6a10000000000000000000b1"),
			Decimal(45),
		)
		refunded = values(config, Kind.ORDER, gid("Order", "fc7-129"), "v2")
		assert refunded.records("refunds")[0].decimal("amount") == Decimal("25.3")
		assert (
			values(config, Kind.ORDER, gid("Order", "fc7-129")).records("notes")[0].string("text")
			== "Gift for a client"
		)
		assert not values(config, Kind.ORDER, gid("Order", "fc7-130")).flag("submitted")

	def test_customers(self, config: ConnectionConfig) -> None:
		riley = values(config, Kind.CUSTOMER, gid("Customer", "riley@river.example"))
		assert (riley.string("first_name"), riley.records("addresses")[0].string("country_code")) == (
			"Riley",
			"CA",
		)

	def test_pages(self, config: ConnectionConfig) -> None:
		about = values(config, Kind.PAGE, gid("Page", "6a10000000000000000000f1"), "v2")
		assert "the harbour and the hills" in about.string(
			"html"
		) and '<img src="http://webflow-fake' in about.string("html")
		assert about.strings("images") == [
			"http://webflow-fake:8080/media/580e63e98c9a982ac9b8b741/studio-hero.png"
		]
		assert mapped(config, Kind.PAGE, gid("Page", "6a10000000000000000000f2")) is None, "a template"

	def test_collections_and_items(self, config: ConnectionConfig) -> None:
		assert (
			values(config, Kind.BLOG, gid("Collection", "6a10000000000000000000c4")).string("title")
			== "Journal"
		)
		post = values(config, Kind.ARTICLE, gid("Item", "6a10000000000000000000g1"))
		assert post.string("intro") == "How the prints are made" and "Every print starts" in post.string(
			"html"
		)
		assert post.strings("images") == ["http://webflow-fake:8080/media/580e63e98c9a982ac9b8b741/press.png"]
		assert post.all()["custom"] == {"Featured": "Yes", "Topic": "Process"}
		assert post.string("category") == gid("Collection", "6a10000000000000000000c4")

	def test_redirects(self, config: ConnectionConfig) -> None:
		inside = values(config, Kind.URL_REDIRECT, gid("Redirect", "6a10000000000000000000h1"))
		assert (inside.string("source"), inside.string("target")) == ("/old-about", "/about")
		mapper = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.URL_REDIRECT]
		with pytest.raises(Unsupported, match="pattern"):
			mapper.map(Node({"id": gid("Redirect", "x"), "fromUrl": "/blog/(.*)", "toUrl": "/news/%1"}))
