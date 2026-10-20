# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Ecwid adapter over the recorded store (tests/fixtures/ecwid, copied from wp-woo-idempotent-importer):
the reader's offset paging and assembly, and the mappers."""

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
from idempotent_importer.sources.ecwid.adapter import EcwidAdapter
from idempotent_importer.sources.ecwid.mapping import legacy, taxes_of
from idempotent_importer.sources.ecwid.reader import RATE_LIMIT, EcwidAuthorizer, EcwidReader, api_base
from tests.support.discounts import condition_kinds, held, terms
from tests.support.transport import FakeTransport, RecordingSleeper, rest_source

ADAPTER = EcwidAdapter()
CONFIG = ConnectionConfig(
	"ecwid",
	"ecwid-e",
	"https://cedarwick.example",
	api_secret="e2e-ecwid-token",
	account="7400100",
	endpoint_override="http://ecwid-fake:8080",
)
GID = "gid://ecwid/ecwid-e"


def reader(state: str = "v1") -> tuple[EcwidReader, FakeTransport]:
	transport = rest_source("ecwid", state)
	built = ADAPTER.reader(CONFIG, transport, RecordingSleeper())
	assert isinstance(built, EcwidReader)
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
	def test_requests_carry_the_token_under_the_store_id(self) -> None:
		source, transport = reader()
		source.page(Kind.COLLECTION, None, None, "")
		assert transport.requests[0].url.startswith("http://ecwid-fake:8080/api/v3/7400100/categories?")
		assert transport.requests[0].headers["Authorization"] == "Bearer e2e-ecwid-token"
		assert RATE_LIMIT.min_interval() == pytest.approx(0.1)
		with pytest.raises(SourceError, match="secret access token"):
			EcwidAuthorizer("")
		assert "e2e-ecwid-token" not in repr(EcwidAuthorizer("e2e-ecwid-token"))
		with pytest.raises(SourceError, match="store ID"):
			api_base(ConnectionConfig("ecwid", "e", "https://x", account="store"))

	def test_items_carry_what_their_mappers_need_from_the_profile(self) -> None:
		assert nodes(Kind.PRODUCT)["Product/9201"].optional_string("weight_unit")
		assert nodes(Kind.ORDER)["Order/CW-1001"].string("currency") == "CAD"
		assert (
			nodes(Kind.URL_REDIRECT)["Redirect/rd-41a"].optional_string("store_url")
			== "https://cedarwick.example"
		)

	def test_incremental_kinds_send_a_unix_timestamp(self) -> None:
		source, transport = reader()
		assert source.is_incremental(Kind.ORDER) and not source.is_incremental(Kind.COLLECTION)
		source.page(Kind.ORDER, None, datetime(2026, 9, 1, tzinfo=UTC), "")
		assert "updatedFrom=1788220800" in transport.requests[0].url

	def test_single_objects(self) -> None:
		source, _ = reader()
		for kind, key in (
			(Kind.PRODUCT, "Product/9201"),
			(Kind.ORDER, "Order/CW-1001"),
			(Kind.CUSTOMER, "Customer/9401"),
			(Kind.COLLECTION, "Category/9101"),
		):
			found = source.node(kind, f"{GID}/{key}")
			assert found is not None and found.id() == f"{GID}/{key}"
		review = source.node(Kind.REVIEW, f"{GID}/Review/9701")
		assert review is not None and review.id() == f"{GID}/Review/9701"
		assert source.node(Kind.REVIEW, f"{GID}/Review/1") is None
		with pytest.raises(SourceError, match="Not an Ecwid identity"):
			source.node(Kind.PRODUCT, f"{GID}/Product/x")
		with pytest.raises(SourceError):
			source.node(Kind.DISCOUNT, f"{GID}/DiscountCoupon/9501")
		with pytest.raises(SourceError):
			source.page(Kind.PAGE, None, None, "")


class TestMappers:
	def test_categories(self) -> None:
		candles = mapped(Kind.COLLECTION, "Category/9101")
		assert candles is not None and candles.legacy_paths == ("/products/candles",)
		assert (
			candles.values().optional_string("image") == "http://ecwid-fake:8080/images/ecw-cat-candles.png"
		)

	def test_a_simple_product(self) -> None:
		candle = values(Kind.PRODUCT, "Product/9201")
		assert (
			candle.optional_string("sku"),
			candle.optional_string("barcode"),
			candle.optional_string("brand"),
		) == (
			"ECW-CANDLE-1",
			"0774455660013",
			"Cedarwick",
		)
		assert (candle.decimal("stock_qty"), candle.strings("collections")) == (
			Decimal(12),
			[f"{GID}/Category/9101"],
		)

	def test_variations_with_a_compare_to_price(self) -> None:
		sampler = values(Kind.PRODUCT, "Product/9202")
		fir = sampler.records("variants")[0]
		assert fir.string("gid") == f"{GID}/Variant/920201"
		assert (fir.decimal("price"), fir.required_record("sale").decimal("price")) == (
			Decimal(16),
			Decimal(14),
		)
		assert fir.required_record("attributes").all() == {"Scent": "Fir"}

	def test_hidden_gift_card_and_odd_products(self) -> None:
		assert not values(Kind.PRODUCT, "Product/9203").flag("published")
		candle = nodes(Kind.PRODUCT)["Product/9201"]
		with pytest.raises(Unsupported, match="gift cards"):
			mapper_of(Kind.PRODUCT).map(candle.with_fields(isGiftCard=True))
		odd = mapper_of(Kind.PRODUCT).map(
			candle.with_fields(
				weight_unit="CARAT",
				options=[{"name": "Wick", "type": "SELECT"}],
				wholesalePrices=[{"quantity": 5}],
			)
		)
		assert odd is not None and len(odd.warnings) == 3

	def test_customers(self) -> None:
		rowan = values(Kind.CUSTOMER, "Customer/9401")
		billing = rowan.records("addresses")[0]
		assert (
			billing.optional_string("line1"),
			billing.optional_string("line2"),
			billing.optional_string("state"),
		) == (
			"14 Spray Avenue",
			"Apt 2",
			"AB",
		)
		assert rowan.optional_string("email") == "rowan.pike@example.com"

	def test_coupons(self) -> None:
		welcome = values(Kind.DISCOUNT, "DiscountCoupon/9501")
		assert (
			welcome.string("discount"),
			welcome.decimal("min_amount"),
			welcome.optional_string("valid_upto"),
		) == (
			"amount",
			Decimal(30),
			"2026-12-31",
		)
		assert mapped(Kind.DISCOUNT, "DiscountCoupon/9504") is None, "a paused coupon"
		assert held(mapped(Kind.DISCOUNT, "DiscountCoupon/9501")) == []
		for key, kinds, cards in (
			("9502", ["once_per_customer"], ["card 405"]),
			("9503", ["free_shipping"], ["card 405"]),
			("9505", ["customer_history"], ["card 407"]),
		):
			desired = mapped(Kind.DISCOUNT, f"DiscountCoupon/{key}")
			assert (condition_kinds(desired), held(desired)) == (kinds, cards)
		assert terms(mapped(Kind.DISCOUNT, "DiscountCoupon/9505"), "customer_history") == {"new": True}

	def test_wholesale_prices(self) -> None:
		source, _ = reader()
		coupons = source.page(Kind.DISCOUNT, None, None, "")
		assert coupons.end_cursor == "wholesale:"
		assert source.page(Kind.DISCOUNT, "0", None, "").end_cursor == "wholesale:", "an offset saved before"
		with pytest.raises(SourceError, match="discount cursor"):
			source.page(Kind.DISCOUNT, "elsewhere:0", None, "")
		candle = values(Kind.DISCOUNT, "WholesalePrice/9201")
		assert (candle.flag("automatic"), candle.strings("items")) == (True, [f"{GID}/Product/9201"])
		assert [t.all() for t in candle.records("tiers")] == [
			{"discount": "rate", "value": Decimal(25), "min_qty": Decimal(5), "max_qty": Decimal(9)},
			{"discount": "rate", "value": Decimal(22), "min_qty": Decimal(10)},
		]
		assert held(mapped(Kind.DISCOUNT, "WholesalePrice/9201")) == []
		assert len(values(Kind.DISCOUNT, "WholesalePrice/9201", "v2").records("tiers")) == 1
		melts = mapped(Kind.DISCOUNT, "WholesalePrice/9202")
		assert terms(melts, "rule_condition") == {
			"source": {
				"variations": [{"variant": f"{GID}/Variant/920202", "tiers": [{"quantity": 3, "price": 12}]}]
			}
		}
		assert "WholesalePrice/9203" not in nodes(Kind.DISCOUNT)
		node = nodes(Kind.DISCOUNT)["WholesalePrice/9201"]
		assert mapper_of(Kind.DISCOUNT).map(node.with_fields(wholesalePrices=[])) is None

	def test_coupon_edges(self) -> None:
		coupon = nodes(Kind.DISCOUNT)["DiscountCoupon/9501"]
		mapper = mapper_of(Kind.DISCOUNT)
		single = mapper.map(
			coupon.with_fields(
				discountType="PERCENT", usesLimit="SINGLE", catalogLimit={"categories": [9101]}
			)
		)
		assert single is not None
		assert (single.values().optional_integer("max_uses"), single.values().strings("groups")) == (
			1,
			[f"{GID}/Category/9101"],
		)
		for changes, kinds in (
			({"catalogLimit": {"products": [9201]}}, ["spread_amount"]),
			({"discountType": "PERCENT", "catalogLimit": {"categories": [0]}}, ["item_matcher"]),
			(
				{"discountType": "PERCENT", "catalogLimit": {"products": [1], "categories": [2]}},
				["groups_beside_items"],
			),
			({"shippingLimit": {"shippingMethods": ["pickup-1"]}}, ["shipping_limits"]),
			({"discountType": "ABS_AND_SHIPPING"}, ["free_shipping"]),
			({"applicationLimit": "REPEAT_CUSTOMER_ONLY"}, ["customer_history"]),
			({"applicationLimit": "SOMETHING_NEW"}, ["customer_segments"]),
		):
			assert condition_kinds(mapper.map(coupon.with_fields(**changes))) == kinds
		with pytest.raises(SourceError, match="type"):
			mapper.map(coupon.with_fields(discountType="BOGUS"))

	def test_a_paid_order_balances_to_ecwids_total(self) -> None:
		order = values(Kind.ORDER, "Order/CW-1001")
		assert totals(order) == order.decimal("total") == Decimal("80.85")
		assert order.string("number") == "CW-1001"
		lines = order.records("lines")
		assert lines[1].optional_string("variant") == f"{GID}/Variant/920201"
		assert lines[0].optional_string("variant") == f"{GID}/Variant/9201-base"
		taxes = [c for c in order.records("charges") if c.string("kind") == "tax"]
		assert [(t.string("description"), t.decimal("amount")) for t in taxes] == [("GST", Decimal("3.85"))]

	def test_an_unpaid_guest_order_and_a_refund(self) -> None:
		guest = values(Kind.ORDER, "Order/CW-1002")
		assert not guest.flag("submitted") and guest.optional_string("customer") is None
		refund = values(Kind.ORDER, "Order/CW-1001", "v2").records("refunds")[0]
		assert (refund.string("gid"), refund.decimal("amount")) == (
			f"{GID}/Refund/CW-1001-1",
			Decimal("29.4"),
		)

	def test_a_gross_price_order_includes_its_taxes(self) -> None:
		order = nodes(Kind.ORDER)["Order/CW-1001"]
		items: list[JsonValue] = [item.raw() for item in order.children("items")]
		second = order.children("items")[1].raw()
		items[1] = {**second, "taxes": []}
		surcharge: list[JsonValue] = [
			{
				"description": "Gift wrap",
				"total": 3.15,
				"totalWithoutTax": 3,
				"taxes": [{"name": "GST", "value": 5, "total": 0.15}],
			}
		]
		desired = mapper_of(Kind.ORDER).map(
			order.with_fields(pricesIncludeTax=True, items=items, customSurcharges=surcharge)
		)
		assert desired is not None
		found = desired.values()
		assert found.flag("taxes_included")
		assert [line.all().get("taxes") for line in found.records("lines")] == [
			[{"title": "GST", "rate": Decimal(5)}],
			[],
		]
		assert [c.all() for c in found.records("charges")] == [
			{"kind": "included_tax", "description": "GST", "amount": Decimal("2.6")},
			{"kind": "shipping", "description": "Canada Post Expedited", "amount": Decimal(12)},
			{
				"kind": "shipping_tax",
				"description": "GST (Canada Post Expedited)",
				"amount": Decimal("0.6"),
				"rate": Decimal(5),
			},
			{"kind": "fee", "description": "Gift wrap", "amount": Decimal(3)},
			{
				"kind": "shipping_tax",
				"description": "GST (Gift wrap)",
				"amount": Decimal("0.15"),
				"rate": Decimal(5),
			},
		]

	def test_order_edges(self) -> None:
		order = nodes(Kind.ORDER)["Order/CW-1001"]
		mapper = mapper_of(Kind.ORDER)
		assert mapper.map(order.with_fields(paymentStatus="CANCELLED")) is None
		surcharge: list[JsonValue] = [{"description": "Gift wrap", "totalWithoutTax": 3, "taxes": []}]
		extra = mapper.map(
			order.with_fields(
				handlingFee={"name": "Handling", "value": 2},
				customSurcharges=surcharge,
				trackingNumber="CP123",
				privateAdminNotes="Fragile",
				shipments=[
					{
						"id": "s1",
						"shippingService": {"carrierName": "Canada Post"},
						"tracking": {"tracking_number": "CP123"},
					}
				],
			)
		)
		assert extra is not None
		descriptions = [c.string("description") for c in extra.values().records("charges")]
		assert {"Handling", "Gift wrap"} <= set(descriptions)
		assert len(extra.values().records("notes")) == 1, "the private notes"
		assert [
			(s.optional_string("carrier"), s.optional_string("tracking_number"))
			for s in extra.values().records("shipments")
		] == [("Canada Post", "CP123")], "the label, and no second shipment for the same tracking number"
		unlabelled = mapper.map(order.with_fields(trackingNumber="CP999", fulfillmentStatus="DELIVERED"))
		assert unlabelled is not None
		shipment = unlabelled.values().records("shipments")[-1]
		assert (
			shipment.optional_string("service"),
			shipment.optional_string("tracking_number"),
			shipment.flag("delivered"),
		) == (
			"Canada Post Expedited",
			"CP999",
			True,
		)
		assert taxes_of(Node({"t": [{"name": "GST", "total": 0}]}, "x"), "t") == []

	def test_reviews_and_redirects(self) -> None:
		review = values(Kind.REVIEW, "Review/9701")
		assert (review.string("product"), review.optional_string("author_email")) == (
			f"{GID}/Product/9201",
			"rowan.pike@example.com",
		)
		assert mapped(Kind.REVIEW, "Review/9702") is None, "a moderated review"
		with pytest.raises(SourceError, match="names no product"):
			mapper_of(Kind.REVIEW).map(nodes(Kind.REVIEW)["Review/9701"].with_fields(productId=0))
		assert values(Kind.URL_REDIRECT, "Redirect/rd-41a").string("target") == "/products/cedar-candle"
		assert legacy("https://shop.example/#!/candles") == ()


def test_the_adapter() -> None:
	assert {m.kind for m in ADAPTER.mappers(CONFIG)} == set(ADAPTER.kinds())
	assert ADAPTER.filter_hint() is None
