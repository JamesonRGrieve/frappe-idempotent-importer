# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Squarespace adapter over the recorded site (tests/fixtures/squarespace, copied from
wp-woo-idempotent-importer): the reader's cursor and offset paging, order assembly and modification window,
and the mappers."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.squarespace.adapter import SquarespaceAdapter
from idempotent_importer.sources.squarespace.mapping import currency_of
from idempotent_importer.sources.squarespace.reader import (
	RATE_LIMIT,
	SquarespaceAuthorizer,
	SquarespaceReader,
	squarespace_time,
)
from tests.support.discounts import condition_kinds, held, terms
from tests.support.transport import FakeTransport, RecordingSleeper, rest_source

ADAPTER = SquarespaceAdapter()
CONFIG = ConnectionConfig(
	"squarespace", "squarespace-p", "https://harbourpine.example", api_secret="e2e-squarespace-key"
)
GID = "gid://squarespace/squarespace-p"
BOARD, SCARF, MUG, EBOOK, GIFT = (f"Product/6b200000000000000000001{n}" for n in range(1, 6))
MARIN, NADIA, LEE = (f"Profile/6c300000000000000000002{n}" for n in range(1, 4))
WELCOME, FIVE_OFF, SHIP_FREE, SCARF_15, AUTO, THIRD_FREE, MUG_HALF = (
	f"Discount/6e500000000000000000004{n}" for n in range(1, 8)
)
ORDER_1001, ORDER_1002, ORDER_1003 = (f"Order/6d400000000000000000003{n}" for n in range(1, 4))
NOW = datetime(2026, 10, 1, tzinfo=UTC)


def reader(state: str = "v1") -> tuple[SquarespaceReader, FakeTransport]:
	transport = rest_source("squarespace", state)
	client = RestClient(
		"Squarespace",
		"https://api.squarespace.com",
		SquarespaceAuthorizer("e2e-squarespace-key"),
		transport,
		RecordingSleeper(),
		RATE_LIMIT,
	)
	return SquarespaceReader(CONFIG, client, NOW), transport


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
	def test_requests_carry_the_key_and_the_user_agent(self) -> None:
		source, transport = reader()
		source.page(Kind.COLLECTION, None, None, "")
		headers = transport.requests[0].headers
		assert headers["Authorization"] == "Bearer e2e-squarespace-key"
		assert headers["User-Agent"] == "frappe-idempotent-importer"
		with pytest.raises(SourceError, match="API key"):
			SquarespaceAuthorizer("")
		assert "e2e-squarespace-key" not in repr(SquarespaceAuthorizer("e2e-squarespace-key"))
		assert RATE_LIMIT.min_interval() == pytest.approx(0.2)

	def test_products_page_by_cursor(self) -> None:
		source, _ = reader()
		first = source.page(Kind.PRODUCT, None, None, "")
		assert first.has_next_page and first.end_cursor
		assert sorted(nodes(Kind.PRODUCT)) == sorted([BOARD, SCARF, MUG, EBOOK, GIFT])
		assert nodes(Kind.PRODUCT)[BOARD].string("source_id") == "6b2000000000000000000011"

	def test_discounts_page_by_offset(self) -> None:
		source, transport = reader()
		page = source.page(Kind.DISCOUNT, None, None, "")
		assert not page.has_next_page and len(page.nodes) == 7
		assert "offset=0" in transport.requests[0].url and "limit=1000" in transport.requests[0].url

	def test_orders_arrive_with_transactions_and_their_customers_profile(self) -> None:
		orders = nodes(Kind.ORDER, "v2")
		paid = orders[ORDER_1001]
		assert paid.children("transactions")[0].children("payments")
		profile = paid.required_child("customer_profile")
		assert profile.string("id") == "6c3000000000000000000021"
		assert orders[ORDER_1002].child("customer_profile") is None, "a guest"
		assert (
			orders[ORDER_1003].required_child("customer_profile").string("id") == "6c3000000000000000000024"
		)

	def test_an_incremental_read_sends_both_ends_of_the_window(self) -> None:
		source, transport = reader()
		assert source.is_incremental(Kind.ORDER) and not source.is_incremental(Kind.CUSTOMER)
		source.page(Kind.ORDER, None, datetime(2026, 9, 1, tzinfo=UTC), "")
		url = transport.requests[0].url
		assert "modifiedAfter=2026-09-01T00%3A00%3A00.000Z" in url
		assert "modifiedBefore=2026-10-01T00%3A00%3A00.000Z" in url
		assert (
			squarespace_time(datetime(2026, 9, 1, 1, 2, 3, 456789, tzinfo=UTC)) == "2026-09-01T01:02:03.456Z"
		)

	def test_single_objects(self) -> None:
		source, _ = reader()
		board = source.node(Kind.PRODUCT, f"{GID}/{BOARD}")
		assert board is not None and board.id() == f"{GID}/{BOARD}"
		welcome = source.node(Kind.DISCOUNT, f"{GID}/{WELCOME}")
		assert welcome is not None and welcome.id() == f"{GID}/{WELCOME}"
		order = source.node(Kind.ORDER, f"{GID}/{ORDER_1001}")
		assert order is not None and order.children("transactions")
		assert source.node(Kind.CUSTOMER, f"{GID}/Profile/gone") is None
		with pytest.raises(SourceError, match="Not a Squarespace identity"):
			source.node(Kind.PRODUCT, f"{GID}/Product/a.b")
		with pytest.raises(SourceError):
			source.node(Kind.PAGE, f"{GID}/Page/1")
		with pytest.raises(SourceError):
			source.page(Kind.PAGE, None, None, "")


class TestMappers:
	def test_store_pages(self) -> None:
		kitchen = mapped(Kind.COLLECTION, "StorePage/6a1000000000000000000001")
		assert kitchen is not None and kitchen.values().string("slug") == "kitchen"
		assert kitchen.legacy_paths == ("/kitchen",)

	def test_a_single_variant_product_is_simple(self) -> None:
		board = values(Kind.PRODUCT, BOARD)
		assert (board.string("type"), board.optional_string("sku"), board.optional_string("barcode")) == (
			"simple",
			"SQS-BOARD-1",
			"0662233440017",
		)
		assert (board.decimal("price"), board.decimal("stock_qty")) == (Decimal(48), Decimal(9))
		assert board.required_record("weight").all() == {"value": Decimal("1.2"), "unit": "kg"}
		assert [image.string("url").rsplit("/", 1)[-1] for image in board.records("images")] == [
			"board.png",
			"board-side.png",
		]
		assert board.strings("tags") == ["kitchen", "cedar"]
		assert board.strings("collections") == [f"{GID}/StorePage/6a1000000000000000000001"]

	def test_variants_in_attribute_order_with_their_sale(self) -> None:
		scarf = values(Kind.PRODUCT, SCARF)
		charcoal, moss = scarf.records("variants")
		assert charcoal.string("gid") == f"{GID}/ProductVariant/a1b2c3d4-0000-4000-8000-000000000121"
		assert charcoal.required_record("sale").decimal("price") == Decimal(55)
		assert (moss.record("sale"), moss.decimal("stock_qty")) == (None, Decimal(0))
		assert scarf.records("attributes")[0].all() == {"name": "Colour", "values": ["Charcoal", "Moss"]}

	def test_hidden_unlimited_downloads_and_gift_cards(self) -> None:
		mug = values(Kind.PRODUCT, MUG)
		assert not mug.flag("published") and mug.optional_decimal("stock_qty") is None
		assert not mug.flag("is_stock_item")
		ebook = mapped(Kind.PRODUCT, EBOOK)
		assert ebook is not None and ebook.warnings
		assert ebook.values().decimal("price") == Decimal(12) and not ebook.values().flag("is_stock_item")
		with pytest.raises(Unsupported, match="gift cards"):
			mapped(Kind.PRODUCT, GIFT)

	def test_product_edges(self) -> None:
		board = nodes(Kind.PRODUCT)[BOARD]
		variant = board.children("variants")[0].raw()
		stone: dict[str, JsonValue] = {
			**variant,
			"shippingMeasurements": {"weight": {"unit": "STONE", "value": 1}},
		}
		weighed = mapper_of(Kind.PRODUCT).map(board.with_fields(variants=[stone]))
		assert weighed is not None and any("STONE" in w for w in weighed.warnings)
		with pytest.raises(SourceError, match="no variants"):
			mapper_of(Kind.PRODUCT).map(board.with_fields(variants=[]))
		with pytest.raises(SourceError, match="no base price"):
			mapper_of(Kind.PRODUCT).map(board.with_fields(variants=[{**variant, "pricing": None}]))
		with pytest.raises(SourceError, match="object of strings"):
			mapper_of(Kind.PRODUCT).map(board.with_fields(variants=[{**variant, "attributes": {"Size": 1}}]))

	def test_profiles(self) -> None:
		marin = values(Kind.CUSTOMER, MARIN)
		assert (marin.string("name"), marin.optional_string("email")) == (
			"Marin Boudreau",
			"marin@example.com",
		)
		assert marin.records("addresses")[0].optional_string("postcode") == "B0J 2C0"
		assert values(Kind.CUSTOMER, NADIA).records("addresses") == [], "an account holder without an address"
		assert mapped(Kind.CUSTOMER, LEE) is None, "a mailing-list subscriber"

	def test_discounts_a_coupon_applies_exactly(self) -> None:
		five_off = values(Kind.DISCOUNT, FIVE_OFF)
		assert (five_off.string("discount"), five_off.decimal("value"), five_off.decimal("min_amount")) == (
			"amount",
			Decimal(5),
			Decimal(50),
		)
		assert (five_off.optional_integer("max_uses"), five_off.optional_string("valid_from")) == (
			100,
			"2026-09-01",
		)
		scarf = values(Kind.DISCOUNT, SCARF_15)
		assert (scarf.string("applies"), scarf.strings("items")) == ("items", [f"{GID}/{SCARF}"])

		assert held(mapped(Kind.DISCOUNT, FIVE_OFF)) == []

	@pytest.mark.parametrize(
		("key", "kinds", "cards"),
		[
			# a code's once-per-customer limit: the Webshop fork enforces it (card 405)
			(WELCOME, ["once_per_customer"], []),
			# free shipping on one of the source's fulfilment options: none is mirrored as a Shipping Rule, held
			(SHIP_FREE, ["free_shipping"], ["card 405"]),
			(AUTO, [], []),
		],
	)
	def test_what_pricing_cant_enforce_is_held(self, key: str, kinds: list[str], cards: list[str]) -> None:
		desired = mapped(Kind.DISCOUNT, key)
		assert condition_kinds(desired) == kinds
		assert held(desired) == cards

	def test_automatic_and_buy_x_get_y_discounts(self) -> None:
		auto = values(Kind.DISCOUNT, AUTO)
		assert (auto.flag("automatic"), auto.strings("codes"), auto.decimal("value")) == (
			True,
			[],
			Decimal(5),
		)
		third = values(Kind.DISCOUNT, THIRD_FREE)
		assert (third.strings("items"), third.decimal("min_qty")) == ([f"{GID}/{SCARF}"], Decimal(2))
		assert third.required_record("free").all() == {
			"qty": Decimal(1),
			"recursive": True,
			"recurse_for": Decimal(2),
		}
		assert held(mapped(Kind.DISCOUNT, THIRD_FREE)) == []
		half = mapped(Kind.DISCOUNT, MUG_HALF)
		mug = values(Kind.DISCOUNT, MUG_HALF)
		assert (mug.strings("items"), mug.required_record("other").all(), mug.decimal("value")) == (
			[f"{GID}/{SCARF}"],
			{"applies": "items", "target": f"{GID}/{BOARD}"},
			Decimal(50),
		)
		assert values(Kind.DISCOUNT, MUG_HALF, "v2").decimal("value") == Decimal(40)
		assert condition_kinds(half) == ["item_limit", "item_choice"]

	def test_discount_edges(self) -> None:
		five_off = nodes(Kind.DISCOUNT)[FIVE_OFF]
		mapper = mapper_of(Kind.DISCOUNT)
		on_product = mapper.map(five_off.with_fields(criteria={"type": "PRODUCT", "productId": "x"}))
		assert condition_kinds(on_product) == ["spread_amount"]
		limited = mapper.map(nodes(Kind.DISCOUNT)[AUTO].with_fields(isLimitedUses=True, maxUsesAllowed=5))
		assert terms(limited, "shared_use_limit") == {"uses": 5}
		recurring = mapper.map(five_off.with_fields(subscriptionOptions={"type": "ALL_PAYMENTS"}))
		assert condition_kinds(recurring) == ["item_matcher"]
		assert mapper.map(five_off.with_fields(status="EXPIRED")) is None
		bxgy = nodes(Kind.DISCOUNT)[MUG_HALF]
		criteria = bxgy.required_child("criteria").raw()
		several = mapper.map(bxgy.with_fields(criteria={**criteria, "rewardProductIds": ["a", "b"]}))
		assert condition_kinds(several) == ["item_limit", "item_choice", "item_matcher"]
		fixed = mapper.map(
			bxgy.with_fields(template={"type": "BUY_X_GET_Y_FIXED_AMOUNT", "discountAmount": {"value": 3}})
		)
		assert fixed is not None and fixed.values().string("discount") == "amount"
		with pytest.raises(SourceError, match="no discount amount"):
			mapper.map(bxgy.with_fields(template={"type": "BUY_X_GET_Y_FIXED_AMOUNT"}))
		with pytest.raises(SourceError, match="without its criteria"):
			mapper.map(bxgy.with_fields(criteria={"type": "ANY_ORDER"}))
		with pytest.raises(SourceError, match="template"):
			mapper.map(five_off.with_fields(template={"type": "MYSTERY"}))
		with pytest.raises(SourceError, match="criteria"):
			mapper.map(five_off.with_fields(criteria={"type": "MYSTERY"}))
		with pytest.raises(SourceError, match="no discount amount"):
			mapper_of(Kind.DISCOUNT).map(five_off.with_fields(template={"type": "FIXED_AMOUNT"}))

	def test_a_paid_order_balances_to_squarespaces_total(self) -> None:
		order = values(Kind.ORDER, ORDER_1001)
		assert totals(order) == order.decimal("total") == Decimal("165.9")
		assert order.optional_string("customer") == f"{GID}/{MARIN}"
		payment = order.required_record("payment")
		assert (payment.string("method"), payment.optional_string("reference")) == (
			"STRIPE (VISA)",
			"ch_000501",
		)
		assert len(order.records("notes")) == 1, "the internal note"
		shipment = order.records("shipments")[0]
		assert (shipment.optional_string("carrier"), shipment.optional_string("tracking_number")) == (
			"Canada Post",
			"SQS1234567890CA",
		)
		assert order.records("lines")[0].string("name") == "Cedar cutting board (Engraving: M.B.)"
		taxes = [c for c in order.records("charges") if c.string("kind") == "tax"]
		assert [(c.string("description"), c.decimal("amount")) for c in taxes] == [("GST", Decimal("7.90"))]

	def test_an_unpaid_guest_order_and_a_refund(self) -> None:
		guest = values(Kind.ORDER, ORDER_1002)
		assert not guest.flag("submitted") and guest.optional_string("customer") is None
		assert guest.required_record("guest").optional_string("email") == "guest@example.com"
		refund = values(Kind.ORDER, ORDER_1001, "v2").records("refunds")[0]
		assert refund.decimal("amount") == Decimal(10)

	def test_orders_without_a_sale(self) -> None:
		order = nodes(Kind.ORDER)[ORDER_1001]
		mapper = mapper_of(Kind.ORDER)
		assert mapper.map(order.with_fields(testmode=True)) is None
		assert mapper.map(order.with_fields(fulfillmentStatus="CANCELED")) is None
		assert mapper.map(order.with_fields(paymentState="FAILED")) is None
		gift: list[JsonValue] = [{**order.children("lineItems")[0].raw(), "lineItemType": "GIFT_CARD"}]
		with pytest.raises(Unsupported, match="gift card"):
			mapper.map(order.with_fields(lineItems=gift))

	def test_a_tax_inclusive_order_includes_its_taxes(self) -> None:
		"""Order 1001 as if its prices included GST: GST at 5% on every line, and shipping at its price less
		its GST, then that GST at its rate."""
		order = nodes(Kind.ORDER)[ORDER_1001].with_fields(priceTaxInterpretation="INCLUSIVE")
		desired = mapper_of(Kind.ORDER).map(order)
		assert desired is not None
		found = desired.values()
		assert found.flag("taxes_included")
		assert [c.all() for c in found.records("charges")] == [
			{"kind": "included_tax", "description": "GST", "amount": Decimal("7.30"), "rate": Decimal("5.0")},
			{"kind": "shipping", "description": "Canada Post Expedited", "amount": Decimal("11.4")},
			{
				"kind": "shipping_tax",
				"description": "GST (shipping)",
				"amount": Decimal("0.6"),
				"rate": Decimal("5.0"),
			},
		]
		assert all(not line.has("taxes") for line in found.records("lines"))

	def test_tax_inclusive_lines_taxed_differently(self) -> None:
		"""A tax-exempt scarf beside the taxed board: each line carries its own sale item's rates (paired
		by amount, whatever their order)."""
		order = nodes(Kind.ORDER)[ORDER_1001]
		document = order.children("transactions")[0].raw()
		sales = document["salesLineItems"]
		assert isinstance(sales, list) and isinstance(sales[1], dict)
		sales[1]["taxes"] = []
		document["salesLineItems"] = [sales[1], sales[0]]
		desired = mapper_of(Kind.ORDER).map(
			order.with_fields(priceTaxInterpretation="INCLUSIVE", transactions=[document])
		)
		assert desired is not None
		found = desired.values()
		assert [line.all().get("taxes") for line in found.records("lines")] == [
			[{"title": "GST", "rate": Decimal("5.0")}],
			[],
		]
		assert found.records("charges")[0].all() == {
			"kind": "included_tax",
			"description": "GST",
			"amount": Decimal("4.64"),
		}

	def test_an_unpaid_tax_inclusive_order_includes_its_tax_total(self) -> None:
		order = nodes(Kind.ORDER)[ORDER_1001].with_fields(
			priceTaxInterpretation="INCLUSIVE", transactions=[], shippingLines=[]
		)
		desired = mapper_of(Kind.ORDER).map(order)
		assert desired is not None
		included = desired.values().records("charges")[0]
		assert (included.string("kind"), included.decimal("amount")) == ("included_tax", Decimal("7.9"))
		assert included.decimal("rate") == Decimal("5.720492")

	def test_an_order_without_transactions_or_shipping_lines(self) -> None:
		order = nodes(Kind.ORDER)[ORDER_1001].with_fields(transactions=[], shippingLines=[])
		desired = mapper_of(Kind.ORDER).map(order)
		assert desired is not None
		charges = [
			(c.string("description"), c.decimal("amount")) for c in desired.values().records("charges")
		]
		assert charges == [("Tax", Decimal("7.9")), ("Shipping", Decimal(12))]
		assert desired.values().required_record("payment").string("method") == "Squarespace"

	def test_currency_from_a_code_or_an_object(self) -> None:
		assert currency_of(Node({"currency": "CAD", "value": 1}, "x")) == "CAD"
		assert currency_of(Node({"currency": {"currencyCode": "USD"}, "value": 1}, "x")) == "USD"
		order = nodes(Kind.ORDER)[ORDER_1001].with_fields(grandTotal={"value": 1})
		with pytest.raises(SourceError, match="no currency"):
			mapper_of(Kind.ORDER).map(order)


def test_the_adapter() -> None:
	assert {m.kind for m in ADAPTER.mappers(CONFIG)} == set(ADAPTER.kinds())
	assert ADAPTER.filter_hint() is None
	assert isinstance(
		ADAPTER.reader(CONFIG, rest_source("squarespace", "v1"), RecordingSleeper()), SquarespaceReader
	)
