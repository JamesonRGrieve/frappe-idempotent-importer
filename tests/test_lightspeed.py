# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Lightspeed eCom adapter over the recorded shop (tests/fixtures/lightspeed, copied from
wp-woo-idempotent-importer): the reader's enveloped page-numbered lists, assembly and incremental phases, and
the mappers."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.lightspeed.adapter import LightspeedAdapter
from idempotent_importer.sources.lightspeed.mapping import slug_of, text
from idempotent_importer.sources.lightspeed.reader import (
	RATE_LIMIT,
	LightspeedReader,
	api_base,
	changed_since,
	linked_id,
	phase_of,
)
from tests.support.discounts import condition_kinds, held, terms
from tests.support.transport import FakeTransport, RecordingSleeper, rest_source

ADAPTER = LightspeedAdapter()
CONFIG = ConnectionConfig(
	"lightspeed",
	"lightspeed-l",
	"https://harbourlane.example",
	api_user="e2e-lightspeed-key",
	api_secret="e2e-lightspeed-secret",
	account="us1/en",
	endpoint_override="http://lightspeed-fake:8080",
)
GID = "gid://lightspeed/lightspeed-l"


def reader(state: str = "v1") -> tuple[LightspeedReader, FakeTransport]:
	transport = rest_source("lightspeed", state)
	built = ADAPTER.reader(CONFIG, transport, RecordingSleeper())
	assert isinstance(built, LightspeedReader)
	return built, transport


def nodes(kind: Kind, state: str = "v1") -> dict[str, Node]:
	source, _ = reader(state)
	found: dict[str, Node] = {}
	cursor: str | None = None
	while True:
		page = source.page(kind, cursor, None, "")
		found |= {node.id().removeprefix(f"{GID}/"): node for node in page.nodes}
		if not page.has_next_page:
			return found
		cursor = page.end_cursor


def mapper_of(kind: Kind) -> Mapper:
	return {m.kind: m for m in ADAPTER.mappers(CONFIG)}[kind]


def mapped(kind: Kind, key: str, state: str = "v1") -> Desired | None:
	return mapper_of(kind).map(nodes(kind, state)[key])


def values(kind: Kind, key: str, state: str = "v1") -> Values:
	desired = mapped(kind, key, state)
	assert desired is not None
	return desired.values()


def totals(order: Values) -> Decimal:
	goods = sum((line.decimal("qty") * line.decimal("rate") for line in order.records("lines")), Decimal(0))
	charges = sum((charge.decimal("amount") for charge in order.records("charges")), Decimal(0))
	return goods - order.decimal("discount") + charges


class TestReader:
	def test_requests_carry_the_key_under_the_shop_language(self) -> None:
		source, transport = reader()
		source.page(Kind.COLLECTION, None, None, "")
		assert transport.requests[0].url.startswith("http://lightspeed-fake:8080/en/categories.json?")
		assert transport.requests[0].headers["Authorization"].startswith("Basic ")
		assert RATE_LIMIT.min_interval() == pytest.approx(1.2)

	def test_the_account_names_the_cluster_and_language(self) -> None:
		assert (
			api_base(ConnectionConfig("lightspeed", "l", "https://x", account="eu1/nl"))
			== "https://api.webshopapp.com/nl"
		)
		with pytest.raises(SourceError, match="cluster and shop language"):
			api_base(ConnectionConfig("lightspeed", "l", "https://x", account="us1/xx"))
		with pytest.raises(SourceError, match="cluster is one of"):
			api_base(ConnectionConfig("lightspeed", "l", "https://x", account="ap1/en"))
		with pytest.raises(SourceError, match="API key"):
			ADAPTER.reader(
				ConnectionConfig("lightspeed", "l", "https://x", account="us1/en"),
				rest_source("lightspeed", "v1"),
				RecordingSleeper(),
			)

	def test_products_arrive_with_variants_images_categories_and_brand(self) -> None:
		towel = nodes(Kind.PRODUCT)["Product/502"]
		assert towel.children("variants") and towel.integers("category_ids")
		mug = nodes(Kind.PRODUCT)["Product/501"]
		assert mug.optional_string("brand") and mug.children("images")
		assert mug.get("shop_is_b2b") is False

	def test_orders_arrive_with_lines_shipments_credit_notes_and_currency(self) -> None:
		order = nodes(Kind.ORDER, "v2")["Order/701"]
		assert order.children("products") and order.children("shipments")
		assert [note.integer("id") for note in order.children("credit_notes")] == [9102]
		assert order.string("currency") == "CAD"

	def test_an_incremental_run_rereads_the_parents_of_changed_children(self) -> None:
		since = datetime(2026, 9, 2, tzinfo=UTC)
		source, transport = reader("v2")
		first = source.page(Kind.PRODUCT, None, since, "")
		assert first.has_next_page and first.end_cursor == "variants:"
		changed = source.page(Kind.PRODUCT, first.end_cursor, since, "")
		assert not changed.has_next_page and changed.nodes
		assert any("updated_at_min=2026-09-01" in r.url for r in transport.requests)
		orders = source.page(Kind.ORDER, None, since, "")
		assert orders.end_cursor == "shipments:"
		shipments = source.page(Kind.ORDER, orders.end_cursor, since, "")
		assert shipments.end_cursor == "invoices:"
		invoices = source.page(Kind.ORDER, shipments.end_cursor, since, "")
		assert not invoices.has_next_page
		assert changed_since(None) == {} and phase_of("orders:3", "x") == ("orders", 3)

	def test_articles_carry_their_blogs_url_and_redirects_the_shops_domain(self) -> None:
		article = nodes(Kind.ARTICLE)["Article/1301"]
		assert article.optional_string("blog_url") == "journal"
		redirect = nodes(Kind.URL_REDIRECT)["Redirect/1401"]
		assert redirect.optional_string("shop_domain") == "harbourlane.example"

	def test_single_objects(self) -> None:
		source, _ = reader()
		for kind, key in (
			(Kind.PRODUCT, "Product/501"),
			(Kind.ORDER, "Order/701"),
			(Kind.ARTICLE, "Article/1301"),
			(Kind.URL_REDIRECT, "Redirect/1401"),
			(Kind.CUSTOMER, "Customer/801"),
		):
			found = source.node(kind, f"{GID}/{key}")
			assert found is None or found.id() == f"{GID}/{key}"
		with pytest.raises(SourceError, match="Not a Lightspeed eCom identity"):
			source.node(Kind.PRODUCT, f"{GID}/Product/x")
		with pytest.raises(SourceError):
			source.node(Kind.POLICY, f"{GID}/Policy/1")
		with pytest.raises(SourceError):
			source.page(Kind.POLICY, None, None, "")

	def test_links_and_unset_fields(self) -> None:
		assert linked_id(Node({"parent": False}, "x"), "parent") is None
		assert linked_id(Node({"items": {"resource": {"id": False}}}, "x"), "items") is None
		assert linked_id(Node({"brand": {"resource": {"id": 6}}}, "x"), "brand") == 6
		assert (
			text(Node({"a": False, "b": " x "}, "x"), "a") is None
			and text(Node({"b": " x "}, "x"), "b") == "x"
		)
		assert slug_of("clothes/womens", "W") == "womens" and slug_of("", "Two words") == "two-words"


class TestMappers:
	def test_categories(self) -> None:
		tableware = mapped(Kind.COLLECTION, "Category/401")
		assert tableware is not None and tableware.legacy_paths == ("/tableware",)
		towels = values(Kind.COLLECTION, "Category/402")
		assert (towels.optional_string("parent"), towels.optional_string("description")) == (
			f"{GID}/Category/401",
			"Tea towels short description",
		)

	def test_a_retail_shops_prices_include_tax(self) -> None:
		mug = mapped(Kind.PRODUCT, "Product/501")
		assert mug is not None and mug.legacy_paths == ("/stoneware-mug.html",)
		fields = mug.values()
		assert (fields.string("type"), fields.decimal("price"), fields.decimal("stock_qty")) == (
			"simple",
			Decimal("26.25"),
			Decimal(14),
		)

	def test_matrix_variants(self) -> None:
		towel = values(Kind.PRODUCT, "Product/502")
		variants = {v.string("gid"): v for v in towel.records("variants")}
		slate = variants[f"{GID}/Variant/5022"]
		assert slate.required_record("weight").all() == {"value": Decimal(120), "unit": "g"}
		assert slate.optional_string("barcode") == "0881122330028"
		assert towel.records("attributes")

	def test_b2b_prices_old_prices_and_plain_variants(self) -> None:
		mug = nodes(Kind.PRODUCT)["Product/501"]
		variant = mug.children("variants")[0].raw()
		on_sale: dict[str, JsonValue] = {**variant, "oldPriceExcl": 30, "options": False}
		b2b = mapper_of(Kind.PRODUCT).map(mug.with_fields(shop_is_b2b=True, variants=[on_sale]))
		assert b2b is not None
		assert (b2b.values().decimal("price"), b2b.values().required_record("sale").decimal("price")) == (
			Decimal(30),
			Decimal(25),
		)
		second: dict[str, JsonValue] = {
			**variant,
			"id": 5012,
			"title": "Large",
			"weightUnit": "stone",
			"weightValue": "1",
		}
		plain = mapper_of(Kind.PRODUCT).map(mug.with_fields(variants=[variant, second]))
		assert plain is not None and plain.warnings
		assert plain.values().records("attributes")[0].string("name") == "Variant"
		with pytest.raises(SourceError, match="no variants"):
			mapper_of(Kind.PRODUCT).map(mug.with_fields(variants=[]))
		with pytest.raises(SourceError, match="no price"):
			mapper_of(Kind.PRODUCT).map(mug.with_fields(variants=[{**variant, "priceIncl": None}]))

	def test_customers(self) -> None:
		marguerite = values(Kind.CUSTOMER, "Customer/801")
		addresses = marguerite.records("addresses")
		assert len(addresses) == 1, "the shipping address is the billing one"
		assert (addresses[0].optional_string("line1"), addresses[0].optional_string("country_code")) == (
			"Spring Garden Road 12",
			"CA",
		)
		assert marguerite.optional_string("email") == "marguerite@example.com"

	def test_discounts(self) -> None:
		welcome = values(Kind.DISCOUNT, "Discount/1001")
		assert (
			welcome.string("discount"),
			welcome.decimal("min_amount"),
			welcome.optional_integer("max_uses"),
		) == (
			"amount",
			Decimal(30),
			100,
		)
		towels = values(Kind.DISCOUNT, "Discount/1002")
		assert (towels.string("applies"), towels.strings("items")) == ("items", [f"{GID}/Product/502"])
		assert mapped(Kind.DISCOUNT, "Discount/1004") is None, "an inactive code"
		shipping = mapped(Kind.DISCOUNT, "Discount/1003")
		assert (condition_kinds(shipping), held(shipping)) == (["free_shipping"], ["card 405"])
		assert held(mapped(Kind.DISCOUNT, "Discount/1001")) == []

	def test_discount_rules(self) -> None:
		source, _ = reader()
		codes = source.page(Kind.DISCOUNT, None, None, "")
		assert codes.end_cursor == "rules:"
		with pytest.raises(SourceError, match="discount cursor"):
			source.page(Kind.DISCOUNT, "elsewhere:", None, "")
		kitchen = values(Kind.DISCOUNT, "DiscountRule/2001")
		assert (kitchen.flag("automatic"), kitchen.string("applies"), kitchen.strings("groups")) == (
			True,
			"groups",
			[f"{GID}/Category/401"],
		)
		assert (kitchen.string("discount"), kitchen.decimal("value")) == ("amount", Decimal(5))
		assert values(Kind.DISCOUNT, "DiscountRule/2001", "v2").decimal("value") == Decimal(6)
		assert held(mapped(Kind.DISCOUNT, "DiscountRule/2001")) == []
		big = mapped(Kind.DISCOUNT, "DiscountRule/2002")
		assert terms(big, "rule_condition") == {
			"source": {"type": "get_x_discount_on_orders_over_y", "minCategoryAmount": Decimal(75)}
		}
		assert mapped(Kind.DISCOUNT, "DiscountRule/2003") is None, "an inactive rule"

	@pytest.mark.parametrize(
		("changes", "kinds"),
		[
			(
				{
					"type": "get_x_discount_in_y_customer_groups",
					"customer_groups": [{"resource": {"id": 9, "url": "groups/9"}}],
				},
				["several_customer_groups"],
			),
			({"type": "get_x_discount_for_repeat_customers"}, ["customer_history"]),
			({"type": "get_free_shipping_on_orders_over_x", "minCategoryAmount": "50"}, ["free_shipping"]),
			(
				{
					"type": "buy_x_items_get_y_items_free",
					"freeProducts": [{"resource": {"id": 502, "url": "products/502"}}],
				},
				["rule_condition"],
			),
			({"type": "buy_x_items_from_cat_get_cheapest_free"}, ["item_choice"]),
			({"type": "buy_x_items_from_cat_get_y_discount_on_cheapest"}, ["item_choice"]),
			({"minCategoryProducts": "2"}, ["rule_condition"]),
			({"maximumUses": 10}, ["shared_use_limit"]),
			({"discountType": "points"}, ["rule_condition"]),
		],
	)
	def test_discount_rule_conditions(self, changes: dict[str, JsonValue], kinds: list[str]) -> None:
		desired = mapper_of(Kind.DISCOUNT).map(
			nodes(Kind.DISCOUNT)["DiscountRule/2001"].with_fields(**changes)
		)
		assert condition_kinds(desired) == kinds
		assert held(desired)

	def test_unknown_discount_shapes(self) -> None:
		with pytest.raises(SourceError, match="rule of type"):
			mapper_of(Kind.DISCOUNT).map(nodes(Kind.DISCOUNT)["DiscountRule/2001"].with_fields(type="other"))
		with pytest.raises(SourceError, match="code of type"):
			mapper_of(Kind.DISCOUNT).map(nodes(Kind.DISCOUNT)["Discount/1001"].with_fields(type="other"))
		source, _ = reader()
		assert source.node(Kind.DISCOUNT, f"{GID}/DiscountRule/2999") is None
		assert source.node(Kind.DISCOUNT, f"{GID}/Discount/1999") is None

	def test_discount_edges(self) -> None:
		code = nodes(Kind.DISCOUNT)["Discount/1001"]
		mapper = mapper_of(Kind.DISCOUNT)
		for changes, kinds in (
			({"applyTo": "products", "products": [{"id": 502}]}, ["spread_amount"]),
			({"shipment": "discount"}, ["shipping_discount"]),
			({"allowCombination": False}, ["combination"]),
			({"before_tax": False}, ["rule_condition"]),
			({"minimum_after": True}, ["rule_condition"]),
			(
				{
					"type": "percentage",
					"applyTo": "productscategories",
					"products": [{"id": 1}],
					"categories": [{"id": 2}],
				},
				["groups_beside_items"],
			),
		):
			assert condition_kinds(mapper.map(code.with_fields(**changes))) == kinds
		on_category = mapper.map(
			code.with_fields(type="percentage", applyTo="categories", categories=[{"id": 401}])
		)
		assert on_category is not None and on_category.values().strings("groups") == [f"{GID}/Category/401"]

	def test_a_paid_order_balances_to_lightspeeds_total(self) -> None:
		order = values(Kind.ORDER, "Order/701")
		assert totals(order) == order.decimal("total") == Decimal("78.75")
		assert order.decimal("discount") == Decimal(5)
		lines = order.records("lines")
		assert lines[1].string("name") == "Waffle dish towel (Colour Slate)"
		assert lines[1].optional_string("variant") == f"{GID}/Variant/5022"
		assert order.optional_string("customer") == f"{GID}/Customer/801"
		assert [s.optional_string("tracking_number") for s in order.records("shipments")] == ["CP987654321CA"]
		cancelled = mapper_of(Kind.ORDER).map(
			nodes(Kind.ORDER)["Order/701"].with_fields(
				shipments=[{"id": 1, "status": "cancelled", "order": {}}]
			)
		)
		assert cancelled is not None and cancelled.values().records("shipments") == []

	def test_an_unpaid_guest_order_and_a_credit_note(self) -> None:
		guest = values(Kind.ORDER, "Order/702")
		assert not guest.flag("submitted") and guest.optional_string("customer") is None
		refund = values(Kind.ORDER, "Order/701", "v2").records("refunds")[0]
		lines = [(line.decimal("qty"), line.decimal("rate")) for line in refund.records("lines")]
		assert (refund.decimal("amount"), lines) == (
			Decimal("26.25"),
			[(Decimal(-1), Decimal(25)), (Decimal(-1), Decimal("1.25"))],
		)

	def test_order_edges(self) -> None:
		order = nodes(Kind.ORDER)["Order/701"]
		mapper = mapper_of(Kind.ORDER)
		assert mapper.map(order.with_fields(status="cancelled")) is None
		untaxed = mapper.map(
			order.with_fields(taxRates=[], paymentPriceExcl=2, paymentTitle="Invoice", memo="Gift")
		)
		assert untaxed is not None
		charges = [
			(c.string("description"), c.decimal("amount")) for c in untaxed.values().records("charges")
		]
		assert charges[0] == ("Tax", Decimal("3.75")) and ("Invoice", Decimal(2)) in charges
		assert untaxed.values().records("notes")[-1].string("text") == "Gift"

	def test_pages_blogs_articles_reviews_and_redirects(self) -> None:
		workshop = mapped(Kind.PAGE, "Textpage/1101")
		assert workshop is not None and workshop.legacy_paths == ("/service/our-workshop",)
		assert not values(Kind.PAGE, "Textpage/1102").flag("published")
		journal = mapped(Kind.BLOG, "Blog/1201")
		assert journal is not None and journal.legacy_paths == ("/blogs/journal",)
		linen = mapped(Kind.ARTICLE, "Article/1301")
		assert linen is not None and linen.legacy_paths == ("/blogs/journal/caring-for-linen.html",)
		assert linen.values().string("category") == f"{GID}/Blog/1201"
		with pytest.raises(SourceError, match="no blog"):
			mapper_of(Kind.ARTICLE).map(nodes(Kind.ARTICLE)["Article/1301"].with_fields(blog=False))
		assert values(Kind.REVIEW, "Review/1501").string("product") == f"{GID}/Product/501"
		assert mapped(Kind.REVIEW, "Review/1502") is None, "a hidden review"
		with pytest.raises(SourceError, match="names no product"):
			mapper_of(Kind.REVIEW).map(nodes(Kind.REVIEW)["Review/1501"].with_fields(product=False))
		assert values(Kind.URL_REDIRECT, "Redirect/1401").string("target") == "/stoneware-mug.html"
		assert (
			values(Kind.URL_REDIRECT, "Redirect/1402").string("target") == "https://sale.example.com/harbour"
		)


def test_the_adapter() -> None:
	assert {m.kind for m in ADAPTER.mappers(CONFIG)} == set(ADAPTER.kinds())
	assert ADAPTER.filter_hint() is None
