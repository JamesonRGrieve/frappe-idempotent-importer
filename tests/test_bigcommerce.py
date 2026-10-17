# SPDX-License-Identifier: AGPL-3.0-or-later
"""The BigCommerce adapter over the recorded store (tests/fixtures/bigcommerce, copied from
wp-woo-idempotent-importer): the reader's v2/v3 paging, assembly and incremental filters, and the mappers."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.bigcommerce.adapter import BigCommerceAdapter
from idempotent_importer.sources.bigcommerce.reader import BigCommerceAuthorizer, BigCommerceReader
from tests.support.transport import FakeTransport, RecordingSleeper, rest_source

ADAPTER = BigCommerceAdapter()
CONFIG = ConnectionConfig(
	"bigcommerce",
	"bigcommerce-c",
	"https://bigcommerce-fake:8080",
	api_secret="e2e-bigcommerce-token",
	account="bcfix123",
)
GID = "gid://bigcommerce/bigcommerce-c"


def reader(state: str = "v1") -> tuple[BigCommerceReader, FakeTransport]:
	transport = rest_source("bigcommerce", state)
	built = ADAPTER.reader(CONFIG, transport, RecordingSleeper())
	assert isinstance(built, BigCommerceReader)
	return built, transport


def nodes(kind: Kind, state: str = "v1", since: datetime | None = None) -> dict[str, Node]:
	source, _ = reader(state)
	found: dict[str, Node] = {}
	cursor: str | None = None
	while True:
		page = source.page(kind, cursor, since, "")
		found |= {node.id().removeprefix(f"{GID}/"): node for node in page.nodes}
		if not page.has_next_page:
			return found
		cursor = page.end_cursor


def mapped(kind: Kind, key: str, state: str = "v1") -> Desired | None:
	return {m.kind: m for m in ADAPTER.mappers(CONFIG)}[kind].map(nodes(kind, state)[key])


def values(kind: Kind, key: str, state: str = "v1") -> Values:
	desired = mapped(kind, key, state)
	assert desired is not None
	return desired.values()


def totals(order: Values) -> Decimal:
	goods = sum((line.decimal("qty") * line.decimal("rate") for line in order.records("lines")), Decimal(0))
	charges = sum((charge.decimal("amount") for charge in order.records("charges")), Decimal(0))
	return goods - order.decimal("discount") + charges


class TestReader:
	def test_requests_carry_the_token_under_the_store_hash(self) -> None:
		source, transport = reader()
		source.page(Kind.COLLECTION, None, None, "")
		assert transport.requests[0].headers["X-Auth-Token"] == "e2e-bigcommerce-token"
		assert "/stores/bcfix123/v3/catalog/trees/categories" in transport.requests[0].url
		with pytest.raises(SourceError, match="access token"):
			BigCommerceAuthorizer("")
		assert "e2e-bigcommerce-token" not in repr(BigCommerceAuthorizer("e2e-bigcommerce-token"))

	def test_the_store_hash_is_checked(self) -> None:
		for bad in ("", "BC/../x", "has space"):
			with pytest.raises(SourceError, match="store hash"):
				BigCommerceReader.store_base(ConnectionConfig("bigcommerce", "c", "https://s", account=bad))

	def test_v3_lists_page_by_their_total(self) -> None:
		source, _ = reader()
		first = source.page(Kind.CUSTOMER, None, None, "")
		assert first.has_next_page and first.end_cursor == "2"
		assert sorted(nodes(Kind.CUSTOMER)) == ["Customer/41", "Customer/42"]

	def test_products_arrive_with_variants_brand_and_weight_unit(self) -> None:
		mug = nodes(Kind.PRODUCT)["Product/111"]
		assert mug.string("source_id") == "111"
		assert mug.optional_string("brand") == "Northern Trail"
		assert mug.children("variants")
		assert mug.optional_string("weight_units")

	def test_discounts_read_coupons_then_promotions_with_their_codes(self) -> None:
		source, _ = reader()
		coupons = source.page(Kind.DISCOUNT, None, None, "")
		assert coupons.has_next_page and coupons.end_cursor == "promotions:"
		assert all("/Coupon/" in node.id() for node in coupons.nodes)
		promotions = source.page(Kind.DISCOUNT, coupons.end_cursor, None, "")
		fall = next(node for node in promotions.nodes if node.id().endswith("/Promotion/71"))
		assert [code.string("code") for code in fall.children("codes")] == ["BC-FALL15"]

	def test_orders_arrive_with_their_parts_and_refunds(self) -> None:
		order = nodes(Kind.ORDER, "v2")["Order/501"]
		assert len(order.children("products")) == 2
		assert order.children("taxes") and order.children("shipments") and order.children("coupons")
		assert [refund.integer("id") for refund in order.children("refunds")] == [9001]

	def test_incremental_kinds_filter_by_modification_time(self) -> None:
		since = datetime(2026, 9, 1, tzinfo=UTC)
		source, transport = reader()
		assert source.is_incremental(Kind.ORDER) and not source.is_incremental(Kind.PAGE)
		source.page(Kind.ORDER, None, since, "")
		assert "min_date_modified=2026-09-01T00%3A00%3A00%2B00%3A00" in transport.requests[0].url

	def test_the_blog_page_is_the_blog_and_posts_carry_the_store_url(self) -> None:
		assert "Page/83" not in nodes(Kind.PAGE), "the blog page is not a content page"
		blog = nodes(Kind.BLOG)["Blog/bcfix123"]
		assert blog.required_child("page").string("type") == "blog"
		post = nodes(Kind.ARTICLE)["Post/91"]
		assert post.string("blog_id") == "bcfix123" and post.optional_string("store_url")

	def test_reviews_are_read_for_products_that_have_any(self) -> None:
		reviews = nodes(Kind.REVIEW)
		assert sorted(reviews) == ["Review/301", "Review/302"]
		assert reviews["Review/301"].integer("product_id") == 111

	def test_single_objects(self) -> None:
		source, _ = reader()
		product = source.node(Kind.PRODUCT, f"{GID}/Product/111")
		assert product is not None and product.children("variants")
		with pytest.raises(SourceError, match="Not a BigCommerce identity"):
			source.node(Kind.PRODUCT, f"{GID}/Product/abc")
		with pytest.raises(SourceError):
			source.page(Kind.POLICY, None, None, "")
		with pytest.raises(SourceError):
			source.node(Kind.REVIEW, f"{GID}/Review/301")

	@pytest.mark.parametrize(
		("kind", "gid"),
		[
			(Kind.COLLECTION, "Category/21"),
			(Kind.CUSTOMER, "Customer/41"),
			(Kind.DISCOUNT, "Coupon/61"),
			(Kind.DISCOUNT, "Promotion/71"),
			(Kind.ORDER, "Order/501"),
			(Kind.PAGE, "Page/81"),
			(Kind.ARTICLE, "Post/91"),
			(Kind.URL_REDIRECT, "Redirect/1"),
		],
	)
	def test_a_single_object_is_read_or_is_gone(self, kind: Kind, gid: str) -> None:
		"""Each kind's single read: the fixtures answer some, and a 404 means BigCommerce no longer has it."""
		source, transport = reader()
		found = source.node(kind, f"{GID}/{gid}")
		assert found is None or found.id() == f"{GID}/{gid}" or found.id().startswith(f"{GID}/")
		assert transport.requests, "one read at least"

	def test_incremental_products_and_customers(self) -> None:
		since = datetime(2026, 9, 1, tzinfo=UTC)
		source, transport = reader()
		source.page(Kind.PRODUCT, None, since, "")
		source.page(Kind.CUSTOMER, None, since, "")
		assert all(
			"date_modified%3Amin=2026-09-01" in r.url for r in transport.requests if "/products?" in r.url
		)
		assert any("/customers?" in r.url and "date_modified%3Amin" in r.url for r in transport.requests)


class TestMappers:
	def test_a_product_with_one_plain_variant_is_simple(self) -> None:
		mug = values(Kind.PRODUCT, "Product/111")
		assert (mug.string("type"), mug.optional_string("sku"), mug.optional_string("barcode")) == (
			"simple",
			"BC-MUG-1",
			"0778899100018",
		)
		assert (mug.decimal("price"), mug.decimal("stock_qty")) == (Decimal(24), Decimal(12))
		assert mug.strings("collections") == [f"{GID}/Category/21"]
		assert mug.required_record("weight").all()["unit"] == "kg"
		assert len(mug.records("images")) == 2

	def test_variants_inherit_the_products_price_and_track_their_own_stock(self) -> None:
		variants = {v.string("gid"): v for v in values(Kind.PRODUCT, "Product/112").records("variants")}
		red, navy = variants[f"{GID}/Variant/1121"], variants[f"{GID}/Variant/1122"]
		assert (red.decimal("price"), red.decimal("stock_qty")) == (Decimal(30), Decimal(5))
		assert (navy.decimal("price"), navy.decimal("stock_qty")) == (Decimal("32.5"), Decimal(0))

	def test_a_hidden_product_is_unpublished(self) -> None:
		assert not values(Kind.PRODUCT, "Product/113").flag("published")

	def test_a_paid_order_balances_to_bigcommerces_total(self) -> None:
		order = values(Kind.ORDER, "Order/501")
		assert totals(order) == order.decimal("total") == Decimal("92.4")
		assert order.decimal("discount") == Decimal("4.5")
		assert order.flag("submitted") and order.optional_string("customer") == f"{GID}/Customer/41"
		assert len(order.records("notes")) == 2, "the shipment and the staff notes"
		assert order.optional_string("remarks") == "Please leave it at the back door."

	def test_a_pending_order_is_a_draft_and_refunds_are_credit_notes(self) -> None:
		assert not values(Kind.ORDER, "Order/502").flag("submitted")
		refund = values(Kind.ORDER, "Order/501", "v2").records("refunds")[0]
		assert refund.decimal("amount") == Decimal(10)

	def test_an_order_in_another_currency_is_unsupported(self) -> None:
		order = nodes(Kind.ORDER)["Order/501"].with_fields(currency_code="USD", currency_exchange_rate="0.73")
		with pytest.raises(Unsupported, match="USD, not the store's CAD"):
			{m.kind: m for m in ADAPTER.mappers(CONFIG)}[Kind.ORDER].map(order)

	@pytest.mark.parametrize("status", [0, 5, 6])
	def test_abandoned_cancelled_and_declined_orders_map_to_nothing(self, status: int) -> None:
		order = nodes(Kind.ORDER)["Order/501"].with_fields(status_id=status)
		assert {m.kind: m for m in ADAPTER.mappers(CONFIG)}[Kind.ORDER].map(order) is None

	def test_coupons_erpnext_cannot_enforce_exactly_are_unsupported(self) -> None:
		with pytest.raises(Unsupported, match="per-customer"):
			mapped(Kind.DISCOUNT, "Coupon/61")
		with pytest.raises(Unsupported, match="free_shipping"):
			mapped(Kind.DISCOUNT, "Coupon/62")
		assert mapped(Kind.DISCOUNT, "Coupon/63") is None, "a disabled coupon"
		with pytest.raises(Unsupported, match="automatic"):
			mapped(Kind.DISCOUNT, "Promotion/72")

	def test_an_amount_off_the_order_coupon(self) -> None:
		welcome = nodes(Kind.DISCOUNT)["Coupon/61"].with_fields(max_uses_per_customer=0)
		desired = {m.kind: m for m in ADAPTER.mappers(CONFIG)}[Kind.DISCOUNT].map(welcome)
		assert desired is not None
		coupon = desired.values()
		assert (coupon.strings("codes"), coupon.string("discount"), coupon.string("applies")) == (
			["BC-WELCOME"],
			"amount",
			"transaction",
		)
		assert (coupon.decimal("value"), coupon.decimal("min_amount")) == (Decimal("4.5"), Decimal(50))

	def test_a_percentage_promotion_with_a_minimum_spend(self) -> None:
		promotion = nodes(Kind.DISCOUNT)["Promotion/71"]
		codes: list[JsonValue] = [
			{**code.raw(), "max_uses_per_customer": 0} for code in promotion.children("codes")
		]
		desired = {m.kind: m for m in ADAPTER.mappers(CONFIG)}[Kind.DISCOUNT].map(
			promotion.with_fields(codes=codes)
		)
		assert desired is not None
		rule = desired.values()
		assert (rule.strings("codes"), rule.string("discount"), rule.decimal("value")) == (
			["BC-FALL15"],
			"percentage",
			Decimal(15),
		)
		assert rule.decimal("min_amount") == Decimal(40)

	def test_pages_blog_posts_reviews_and_redirects(self) -> None:
		assert values(Kind.PAGE, "Page/81").flag("published")
		assert not values(Kind.PAGE, "Page/82").flag("published")
		with pytest.raises(Unsupported, match="contact_form"):
			mapped(Kind.PAGE, "Page/84")
		assert values(Kind.BLOG, "Blog/bcfix123").string("title") == "Trail Journal"
		post = values(Kind.ARTICLE, "Post/91")
		assert post.string("category") == f"{GID}/Blog/bcfix123" and post.flag("published")
		assert values(Kind.REVIEW, "Review/301").string("product") == f"{GID}/Product/111"
		assert mapped(Kind.REVIEW, "Review/302") is None, "a pending review"
		assert values(Kind.URL_REDIRECT, "Redirect/1").all() == {
			"source": "/old-trail-mug",
			"target": "/enamel-trail-mug",
			"status": 301,
		}
		assert (
			values(Kind.URL_REDIRECT, "Redirect/2").string("target") == "https://northerntrail.example/sale"
		)


def mapper_of(kind: Kind) -> Mapper:
	return {m.kind: m for m in ADAPTER.mappers(CONFIG)}[kind]


def coupon(**changes: JsonValue) -> Node:
	return nodes(Kind.DISCOUNT)["Coupon/61"].with_fields(max_uses_per_customer=0, **changes)


def promotion(**changes: JsonValue) -> Node:
	fall = nodes(Kind.DISCOUNT)["Promotion/71"]
	codes: list[JsonValue] = [{**code.raw(), "max_uses_per_customer": 0} for code in fall.children("codes")]
	return fall.with_fields(**{"codes": codes, **changes})


def rule(action: JsonValue, condition: JsonValue = None) -> list[JsonValue]:
	return [{"action": action, "condition": condition}]


class TestMoreMappers:
	def test_categories_and_customers(self) -> None:
		kitchen = values(Kind.COLLECTION, "Category/21")
		assert (kitchen.string("name"), kitchen.string("slug"), kitchen.optional_string("parent")) == (
			"Camp Kitchen",
			"camp-kitchen",
			None,
		)
		ingrid = values(Kind.CUSTOMER, "Customer/41")
		assert (ingrid.string("name"), ingrid.optional_string("email"), ingrid.string("type")) == (
			"Ingrid Johansson",
			"ingrid@example.com",
			"Individual",
		)
		assert ingrid.records("addresses")[0].optional_string("postcode") == "T1W 2B2"
		company = nodes(Kind.CUSTOMER)["Customer/41"].with_fields(first_name="", last_name="")
		mapped_company = mapper_of(Kind.CUSTOMER).map(company)
		assert mapped_company is not None and mapped_company.values().string("type") == "Company"

	def test_product_edges(self) -> None:
		toque = nodes(Kind.PRODUCT)["Product/112"]
		tracked_as_a_whole = mapper_of(Kind.PRODUCT).map(toque.with_fields(inventory_tracking="product"))
		assert tracked_as_a_whole is not None and tracked_as_a_whole.warnings
		odd_unit = mapper_of(Kind.PRODUCT).map(toque.with_fields(weight_units="Stone"))
		assert odd_unit is not None and any("Stone" in w for w in odd_unit.warnings)
		with pytest.raises(SourceError, match="no variants"):
			mapper_of(Kind.PRODUCT).map(toque.with_fields(variants=[]))
		with pytest.raises(SourceError, match="non-integer"):
			mapper_of(Kind.PRODUCT).map(toque.with_fields(categories=["21"]))
		with pytest.raises(SourceError, match="no price"):
			mapper_of(Kind.PRODUCT).map(
				toque.with_fields(
					price=None, variants=[{**toque.children("variants")[0].raw(), "price": None}]
				)
			)

	def test_legacy_coupon_shapes(self) -> None:
		on_a_product = mapper_of(Kind.DISCOUNT).map(
			coupon(type="percentage_discount", applies_to={"entity": "products", "ids": [112]})
		)
		assert on_a_product is not None and on_a_product.values().strings("items") == [f"{GID}/Product/112"]
		per_item = mapper_of(Kind.DISCOUNT).map(
			coupon(type="per_item_discount", applies_to={"entity": "categories", "ids": [22]})
		)
		assert per_item is not None and per_item.values().string("applies") == "groups"
		with pytest.raises(Unsupported, match="countries or shipping"):
			mapper_of(Kind.DISCOUNT).map(coupon(restricted_to={"countries": ["CA"]}))
		with pytest.raises(SourceError, match="unknown kind"):
			mapper_of(Kind.DISCOUNT).map(coupon(applies_to={"entity": "brands", "ids": [5]}))

	@pytest.mark.parametrize(
		("changes", "refusal"),
		[
			({"shipping_address": {"countries": [{"iso2_country_code": "CA"}]}}, "shipping_address"),
			({"customer": {"group_ids": [1]}}, "some customers"),
			({"rules": rule({}) * 2}, "2 rules"),
			({"rules": rule({"shipping": {}})}, "items, shipping or gifts"),
			(
				{
					"rules": rule(
						{
							"cart_value": {
								"discount": {"percentage_amount": "5"},
								"maximum_allowed_discount_amount": "9",
							}
						}
					)
				},
				"capped",
			),
			({"rules": rule({"cart_value": {"discount": {}}}), "max_uses": 0}, "no amount"),
			({"codes": [{"code": "A", "max_uses": 5}]}, "own schedule or use limit"),
			({"codes": [{"code": "A"}, {"code": "B"}], "max_uses": 5}, "shared across several codes"),
			(
				{
					"rules": rule(
						{"cart_value": {"discount": {"fixed_amount": "5"}}}, {"cart": {"minimum_quantity": 2}}
					)
				},
				"beyond a minimum spend",
			),
		],
	)
	def test_promotions_coupons_cannot_express(self, changes: dict[str, JsonValue], refusal: str) -> None:
		with pytest.raises((Unsupported, SourceError), match=refusal):
			mapper_of(Kind.DISCOUNT).map(promotion(**changes))

	def test_a_fixed_amount_promotion_without_conditions_and_a_disabled_one(self) -> None:
		fixed = mapper_of(Kind.DISCOUNT).map(
			promotion(rules=rule({"cart_value": {"discount": {"fixed_amount": "5"}}}))
		)
		assert fixed is not None
		assert (fixed.values().string("discount"), fixed.values().optional_decimal("min_amount")) == (
			"amount",
			None,
		)
		assert mapper_of(Kind.DISCOUNT).map(promotion(status="DISABLED")) is None

	def test_order_edges(self) -> None:
		order = nodes(Kind.ORDER, "v2")["Order/501"]
		refund = order.children("refunds")[0].raw()
		returned: dict[str, JsonValue] = {
			**refund,
			"items": [{"item_type": "PRODUCT", "item_id": 5011, "quantity": 1, "requested_amount": "10.00"}],
		}
		mapped_order = mapper_of(Kind.ORDER).map(order.with_fields(refunds=[returned]))
		assert mapped_order is not None
		lines = mapped_order.values().records("refunds")[0].records("lines")
		assert [(line.decimal("qty"), line.decimal("rate")) for line in lines] == [(Decimal(-1), Decimal(10))]
		stray: dict[str, JsonValue] = {
			**refund,
			"items": [{"item_type": "PRODUCT", "item_id": 9, "quantity": 1, "requested_amount": "1"}],
		}
		with pytest.raises(Unsupported, match="line the order doesn't have"):
			mapper_of(Kind.ORDER).map(order.with_fields(refunds=[stray]))
		fees: list[JsonValue] = [{"display_name_merchant": "Rush", "cost_ex_tax": "3.00"}]
		untaxed = mapper_of(Kind.ORDER).map(order.with_fields(taxes=[], fees=fees))
		assert untaxed is not None
		descriptions = [c.string("description") for c in untaxed.values().records("charges")]
		assert descriptions[0] == "Tax" and "Rush" in descriptions

	def test_content_edges(self) -> None:
		post = nodes(Kind.ARTICLE)["Post/91"]
		with pytest.raises(SourceError, match="no publication date"):
			mapper_of(Kind.ARTICLE).map(post.with_fields(published_date_iso8601=None, published_date=None))
		redirect = nodes(Kind.URL_REDIRECT)["Redirect/1"]
		assert mapper_of(Kind.URL_REDIRECT).map(redirect.with_fields(to_url=None)) is None


def test_the_adapter() -> None:
	assert {m.kind for m in ADAPTER.mappers(CONFIG)} == set(ADAPTER.kinds())
	assert ADAPTER.filter_hint() is None
