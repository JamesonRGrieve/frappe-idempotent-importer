# SPDX-License-Identifier: AGPL-3.0-or-later
"""The WooCommerce adapter over the recorded store (tests/fixtures/woocommerce): what it reads, and how
each kind maps into the shared field contract."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.woocommerce.adapter import WooCommerceAdapter
from idempotent_importer.sources.woocommerce.reader import WooCommerceReader
from tests.support.transport import FakeTransport, RecordingSleeper, woocommerce

ADAPTER = WooCommerceAdapter()


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[WooCommerceReader, FakeTransport]:
	transport = woocommerce(state)
	return WooCommerceReader.connect(config, transport, RecordingSleeper()), transport


def nodes(config: ConnectionConfig, kind: Kind, state: str = "v1") -> dict[str, Node]:
	source, _ = reader(config, state)
	return {node.id().rsplit("/", 1)[-1]: node for node in source.page(kind, None, None, "").nodes}


def mapped(config: ConnectionConfig, kind: Kind, source_id: str, state: str = "v1") -> Desired | None:
	mapper = {m.kind: m for m in ADAPTER.mappers(config)}[kind]
	return mapper.map(nodes(config, kind, state)[source_id])


def values(config: ConnectionConfig, kind: Kind, source_id: str, state: str = "v1") -> Values:
	desired = mapped(config, kind, source_id, state)
	assert desired is not None
	return desired.values()


class TestReader:
	def test_identities_belong_to_the_connection(self, woo_config: ConnectionConfig) -> None:
		products = nodes(woo_config, Kind.PRODUCT)
		assert products["101"].id() == "gid://woocommerce/woo-a/Product/101"
		other = replace(woo_config, connection="woo-b")
		assert nodes(other, Kind.PRODUCT)["101"].id() == "gid://woocommerce/woo-b/Product/101"

	def test_variable_products_arrive_with_their_variations_and_the_weight_unit(
		self, woo_config: ConnectionConfig
	) -> None:
		hoodie = nodes(woo_config, Kind.PRODUCT)["102"]
		assert [v.integer("id") for v in hoodie.children("variation_nodes")] == [1021, 1022]
		assert hoodie.string("weight_unit") == "kg"

	def test_orders_arrive_with_refunds_and_notes(self, woo_config: ConnectionConfig) -> None:
		v2 = nodes(woo_config, Kind.ORDER, "v2")
		assert [r.integer("id") for r in v2["502"].children("refund_nodes")] == [601]
		assert len(nodes(woo_config, Kind.ORDER)["501"].children("note_nodes")) == 2

	def test_posts_arrive_with_their_authors_name(self, woo_config: ConnectionConfig) -> None:
		assert nodes(woo_config, Kind.ARTICLE)["901"].string("author_name") == "Ingrid Admin"

	def test_redirects_come_from_the_redirection_plugin(self, woo_config: ConnectionConfig) -> None:
		assert set(nodes(woo_config, Kind.URL_REDIRECT)) == {"961", "962"}

	def test_paging_follows_total_pages(self, woo_config: ConnectionConfig) -> None:
		source, _ = reader(woo_config)
		page = source.page(Kind.CUSTOMER, None, None, "")
		assert (page.has_next_page, page.end_cursor) == (False, None)

	def test_incremental_kinds_ask_for_changes_since_in_gmt(self, woo_config: ConnectionConfig) -> None:
		source, transport = reader(woo_config)
		source.page(Kind.ORDER, None, datetime(2026, 9, 1, 10, tzinfo=UTC), "")
		listing = transport.requests[0].url
		assert "modified_after=2026-09-01T10%3A00%3A00" in listing
		assert "dates_are_gmt=true" in listing
		assert source.is_incremental(Kind.ORDER)
		assert not source.is_incremental(Kind.CUSTOMER)

	def test_application_passwords_read_drafts_and_redirects(self, woo_config: ConnectionConfig) -> None:
		source, transport = reader(woo_config)
		source.page(Kind.PAGE, None, None, "")
		assert "status=publish%2Cfuture%2Cdraft%2Cpending%2Cprivate" in transport.requests[0].url
		assert transport.requests[0].headers["Authorization"].startswith("Basic ")

	def test_a_consumer_key_reads_published_content_anonymously(self, woo_config: ConnectionConfig) -> None:
		source, transport = reader(replace(woo_config, auth_method="Consumer Key"))
		source.page(Kind.PAGE, None, None, "")
		assert "status=" not in transport.requests[0].url
		assert "Authorization" not in transport.requests[0].headers
		assert source.page(Kind.URL_REDIRECT, None, None, "").nodes == []

	def test_single_objects(self, woo_config: ConnectionConfig) -> None:
		source, _ = reader(woo_config)
		toque = source.node(Kind.PRODUCT, "gid://woocommerce/woo-a/Product/101")
		assert toque is not None
		assert toque.string("sku") == "TOQUE-1"
		assert source.node(Kind.PRODUCT, "gid://woocommerce/woo-a/Product/999") is None
		with pytest.raises(SourceError):
			source.node(Kind.PRODUCT, "gid://woocommerce/woo-a/Order/1")

	@pytest.mark.parametrize(
		("change", "message"),
		[
			(lambda c: replace(c, store_url="http://woo.example"), "https"),
			(lambda c: replace(c, auth_method=""), "Application Password"),
		],
	)
	def test_configuration_is_checked(
		self,
		woo_config: ConnectionConfig,
		change: Callable[[ConnectionConfig], ConnectionConfig],
		message: str,
	) -> None:
		with pytest.raises(SourceError, match=message):
			WooCommerceReader.connect(change(woo_config), woocommerce("v1"), RecordingSleeper())


class TestProducts:
	def test_a_simple_product(self, woo_config: ConnectionConfig) -> None:
		toque = values(woo_config, Kind.PRODUCT, "101")
		assert toque.string("type") == "simple"
		assert toque.string("sku") == "TOQUE-1"
		assert toque.optional_string("barcode") == "0123456789012"
		assert toque.decimal("price") == Decimal("25.00")
		assert toque.record("sale") is None
		assert toque.decimal("stock_qty") == Decimal(12)
		assert toque.flag("is_stock_item")
		assert toque.flag("published")
		assert toque.required_record("weight").all() == {"value": Decimal("0.12"), "unit": "kg"}
		assert toque.strings("collections") == ["gid://woocommerce/woo-a/ProductCategory/13"]
		assert toque.records("images")[0].string("url").endswith("toque.png")

	def test_a_variable_product(self, woo_config: ConnectionConfig) -> None:
		hoodie = values(woo_config, Kind.PRODUCT, "102")
		assert hoodie.string("type") == "variable"
		assert [a.all() for a in hoodie.records("attributes")] == [
			{"name": "Size", "values": ["S", "M"]},
			{"name": "Colour", "values": ["Navy"]},
		]
		medium = hoodie.records("variants")[1]
		assert medium.required_record("attributes").all() == {"Size": "M", "Colour": "Navy"}
		assert medium.decimal("price") == Decimal("60.00")
		assert medium.required_record("sale").decimal("price") == Decimal("54.00")
		assert medium.decimal("stock_qty") == Decimal(2)
		assert hoodie.flag("is_stock_item"), "its variations track stock"

	def test_visibility_and_status(self, woo_config: ConnectionConfig) -> None:
		assert not values(woo_config, Kind.PRODUCT, "103").flag("published"), "catalog visibility hidden"
		assert not values(woo_config, Kind.PRODUCT, "105").flag("published"), "a draft"
		assert not values(woo_config, Kind.PRODUCT, "103").flag("is_stock_item"), "virtual"

	def test_grouped_products_have_no_erpnext_home(self, woo_config: ConnectionConfig) -> None:
		with pytest.raises(Unsupported, match="grouped"):
			mapped(woo_config, Kind.PRODUCT, "104")

	def test_old_urls(self, woo_config: ConnectionConfig) -> None:
		desired = mapped(woo_config, Kind.PRODUCT, "101")
		assert desired is not None
		assert desired.legacy_paths == ("/product/wool-toque",)

	def test_a_price_change_changes_the_mapping(self, woo_config: ConnectionConfig) -> None:
		assert values(woo_config, Kind.PRODUCT, "101", "v2").decimal("price") == Decimal("27.00")


class TestCategories:
	def test_tree_and_names(self, woo_config: ConnectionConfig) -> None:
		hoodies = values(woo_config, Kind.COLLECTION, "12")
		assert hoodies.optional_string("parent") == "gid://woocommerce/woo-a/ProductCategory/11"
		assert values(woo_config, Kind.COLLECTION, "13", "v2").string("name") == "Hats & Accessories"


class TestOrders:
	def test_a_paid_order_balances(self, woo_config: ConnectionConfig) -> None:
		order = values(woo_config, Kind.ORDER, "501")
		assert order.flag("submitted")
		lines = order.records("lines")
		assert [(line.decimal("qty"), line.decimal("rate")) for line in lines] == [
			(Decimal(2), Decimal("25.00")),
			(Decimal(1), Decimal("60.00")),
		]
		assert lines[1].optional_string("variant") == "gid://woocommerce/woo-a/ProductVariation/1021"
		charges = sum((c.decimal("amount") for c in order.records("charges")), Decimal(0))
		goods = sum((line.decimal("qty") * line.decimal("rate") for line in lines), Decimal(0))
		assert goods - order.decimal("discount") + charges == order.decimal("total") == Decimal("114.45")
		assert order.required_record("payment").optional_string("reference") == "ch_501"
		assert len(order.records("notes")) == 2

	def test_unpaid_and_cancelled_orders(self, woo_config: ConnectionConfig) -> None:
		assert not values(woo_config, Kind.ORDER, "503").flag("submitted")
		assert values(woo_config, Kind.ORDER, "503").record("payment") is None
		assert mapped(woo_config, Kind.ORDER, "504") is None
		assert values(woo_config, Kind.ORDER, "503", "v2").flag("submitted")

	def test_a_guest_order(self, woo_config: ConnectionConfig) -> None:
		order = values(woo_config, Kind.ORDER, "502")
		assert order.optional_string("customer") is None
		assert order.required_record("guest").optional_string("email") == "ana@example.test"

	def test_a_refund_balances(self, woo_config: ConnectionConfig) -> None:
		refund = values(woo_config, Kind.ORDER, "502", "v2").records("refunds")[0]
		lines = sum(
			(line.decimal("qty") * line.decimal("rate") for line in refund.records("lines")), Decimal(0)
		)
		charges = sum((c.decimal("amount") for c in refund.records("charges")), Decimal(0))
		assert lines + charges == -refund.decimal("amount") == Decimal("-26.25")


class TestCoupons:
	def test_a_percentage_coupon(self, woo_config: ConnectionConfig) -> None:
		welcome = values(woo_config, Kind.DISCOUNT, "301")
		assert welcome.strings("codes") == ["welcome10"]
		assert (welcome.string("discount"), welcome.string("applies")) == ("percentage", "transaction")
		assert welcome.decimal("min_amount") == Decimal("50.00")
		assert welcome.optional_string("valid_upto") == "2026-12-31"
		assert welcome.optional_integer("max_uses") == 100

	def test_a_product_coupon(self, woo_config: ConnectionConfig) -> None:
		hoodie = values(woo_config, Kind.DISCOUNT, "303")
		assert (hoodie.string("discount"), hoodie.string("applies")) == ("amount", "items")
		assert hoodie.strings("items") == ["gid://woocommerce/woo-a/Product/102"]

	def test_unenforceable_restrictions(self, woo_config: ConnectionConfig) -> None:
		with pytest.raises(Unsupported, match="email_restrictions"):
			mapped(woo_config, Kind.DISCOUNT, "304")


class TestContent:
	def test_pages(self, woo_config: ConnectionConfig) -> None:
		about = values(woo_config, Kind.PAGE, "701")
		assert about.flag("published")
		assert about.strings("images") == ["http://woo-fake:8080/wp-content/uploads/2026/08/workshop.png"]
		assert not values(woo_config, Kind.PAGE, "702").flag("published")

	def test_posts(self, woo_config: ConnectionConfig) -> None:
		post = values(woo_config, Kind.ARTICLE, "901")
		assert post.string("category") == "gid://woocommerce/woo-a/Category/801"
		assert post.required_record("author").all() == {"key": "1", "name": "Ingrid Admin"}
		assert post.string("published_on") == "2026-08-20"
		assert post.strings("tags") == ["Knitting"], "a post's tag ids, read by name"

	def test_product_tags(self, woo_config: ConnectionConfig) -> None:
		assert values(woo_config, Kind.PRODUCT, "101").strings("tags") == ["wool"]

	def test_a_posts_tags_are_read_once(self, woo_config: ConnectionConfig) -> None:
		source, transport = reader(woo_config)
		source.page(Kind.ARTICLE, None, None, "")
		source.page(Kind.ARTICLE, None, None, "")
		assert sum("/wp/v2/tags" in request.url for request in transport.requests) == 1

	def test_reviews(self, woo_config: ConnectionConfig) -> None:
		assert values(woo_config, Kind.REVIEW, "951").decimal("rating") == Decimal(1)
		assert mapped(woo_config, Kind.REVIEW, "952") is None, "spam"

	def test_redirects(self, woo_config: ConnectionConfig) -> None:
		assert values(woo_config, Kind.URL_REDIRECT, "961").all() == {
			"source": "/old-shop",
			"target": "/shop",
			"status": 301,
		}
		assert (
			values(woo_config, Kind.URL_REDIRECT, "962").string("target")
			== "https://outlet.example.test/sale"
		)


def test_the_adapter(woo_config: ConnectionConfig) -> None:
	assert ADAPTER.built
	assert {mapper.kind for mapper in ADAPTER.mappers(woo_config)} == set(ADAPTER.kinds())
	assert ADAPTER.filter_hint() is None
