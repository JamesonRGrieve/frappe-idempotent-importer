# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Wix adapter over the recorded site (tests/fixtures/wix, copied from wp-woo-idempotent-importer): a V3
catalog site in two states and a V1 catalog site; the reader's cursor and offset paging, assembly and
incremental phases, the rich content renderer and the mappers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.wix.adapter import WixAdapter
from idempotent_importer.sources.wix.mapping import epoch_time, state_of, text_html
from idempotent_importer.sources.wix.reader import Catalog, WixAuthorizer, WixReader, phase_of
from idempotent_importer.sources.wix.rich_content import rich_html
from tests.support.discounts import condition_kinds, held
from tests.support.transport import FakeTransport, RecordingSleeper, rest_source

ADAPTER = WixAdapter()
SITE = "5f3c3d55-wix-site"
CONFIG = ConnectionConfig("wix", "wix-w", "http://wix-fake:8080", api_secret="e2e-wix-key", account=SITE)
GID = "gid://wix/wix-w"
MUG, TOWEL, BOWL = (f"Product/1a7e5c10-000{n}-4c2e-9b41-6f0d2a1e000{n}" for n in range(1, 4))
MAYA, OWEN, LENA = (f"Contact/2c4e6a80-000{n}-4b3d-9e1f-5a6b7c8d000{n}" for n in range(1, 4))
ORDER_1, ORDER_2, ORDER_3 = (f"Order/8f1e2d30-000{n}-4c5b-a69d-7e8f9a0b000{n}" for n in range(1, 4))
COUPON = "Coupon/c0000000-0000-4000-8000-00000000000{}"
SAGE = f"{GID}/Variant/1a7e5c10-0002-4c2e-9b41-6f0d2a1e0002:7c1d3e20-0001-4f8a-a3b2-1d9c4e5f0001"


def reader(state: str = "v1") -> tuple[WixReader, FakeTransport]:
	transport = rest_source("wix", state)
	built = ADAPTER.reader(CONFIG, transport, RecordingSleeper())
	assert isinstance(built, WixReader)
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


def bodies(transport: FakeTransport) -> list[dict[str, JsonValue]]:
	return [json.loads(r.body) for r in transport.requests if r.body]


class TestReader:
	def test_requests_carry_the_key_and_the_site(self) -> None:
		source, transport = reader()
		source.page(Kind.COLLECTION, None, None, "")
		headers = transport.requests[0].headers
		assert (headers["Authorization"], headers["wix-site-id"]) == ("e2e-wix-key", SITE)
		with pytest.raises(SourceError, match="API key"):
			WixAuthorizer("", SITE)
		with pytest.raises(SourceError, match="site ID"):
			WixAuthorizer("key", "")
		assert "e2e-wix-key" not in repr(WixAuthorizer("e2e-wix-key", SITE))

	def test_the_catalog_version_is_asked_once(self) -> None:
		source, transport = reader()
		assert source.catalog() is Catalog.V3 and source.catalog() is Catalog.V3
		assert sum(1 for r in transport.requests if r.url.endswith("/provision/version")) == 1
		v1, _ = reader("catalog-v1")
		assert v1.catalog() is Catalog.V1
		assert not v1.is_incremental(Kind.PRODUCT) and source.is_incremental(Kind.PRODUCT)

	def test_v3_products_arrive_with_variants_and_inventory(self) -> None:
		towel = nodes(Kind.PRODUCT)[TOWEL]
		assert towel.string("catalog") == "V3_CATALOG"
		assert len(towel.children("variants")) == 2 and towel.children("inventory_items")

	def test_an_incremental_product_run_then_reads_restocked_products(self) -> None:
		since = datetime(2026, 9, 1, tzinfo=UTC)
		source, transport = reader()
		first = source.page(Kind.PRODUCT, None, since, "")
		assert first.has_next_page and first.end_cursor == "stock:"
		restocked = source.page(Kind.PRODUCT, first.end_cursor, since, "")
		assert not restocked.has_next_page
		sent = bodies(transport)
		query = sent[0]["query"]
		assert isinstance(query, dict) and query["filter"] == {
			"updatedDate": {"$gt": "2026-09-01T00:00:00.000Z"}
		}
		assert phase_of("stock:abc") == ("stock", "abc") and phase_of(None) == ("products", None)
		full, _ = reader()
		assert full.page(Kind.PRODUCT, "stock:", None, "").nodes == []

	def test_v1_products_and_collections(self) -> None:
		products = nodes(Kind.PRODUCT, "catalog-v1")
		assert all(p.string("catalog") == "V1_CATALOG" for p in products.values())
		assert "Collection/c01e0000-1001-4a5b-8c9d-0e1f2a3b1001" in nodes(Kind.COLLECTION, "catalog-v1")

	def test_contacts_page_by_cursor(self) -> None:
		assert sorted(nodes(Kind.CUSTOMER, "v2")) == sorted([MAYA, OWEN, LENA])

	def test_orders_arrive_with_payments_refunds_and_fulfillments(self) -> None:
		order = nodes(Kind.ORDER, "v2")[ORDER_1]
		assert order.children("payments") and len(order.children("refunds")) == 2
		assert order.children("fulfillments")

	def test_the_blog_comes_first_then_its_categories(self) -> None:
		blogs = list(nodes(Kind.BLOG))
		assert blogs[0] == f"Blog/{SITE}" and blogs[1].startswith("BlogCategory/")
		post = nodes(Kind.ARTICLE)["Post/0e1f2a30-0001-4b5c-9d6e-7f8a9b0c0001"]
		assert post.string("blog_id") == SITE

	def test_single_objects(self) -> None:
		source, _ = reader()
		mug = source.node(Kind.PRODUCT, f"{GID}/{MUG}")
		assert mug is not None and mug.children("variants")
		assert source.node(Kind.PRODUCT, f"{GID}/Product/9f9f9f9f-0000-4000-8000-00000000dead") is None
		maya = source.node(Kind.CUSTOMER, f"{GID}/{MAYA}")
		assert maya is not None and maya.id() == f"{GID}/{MAYA}"
		order = source.node(Kind.ORDER, f"{GID}/{ORDER_1}")
		assert order is not None and order.children("payments")
		planter, _ = reader("catalog-v1")
		found = planter.node(Kind.PRODUCT, f"{GID}/Product/6b5a4c30-1001-4d2e-8f1a-9b0c1d2e1001")
		assert found is not None and found.string("catalog") == "V1_CATALOG"
		with pytest.raises(SourceError):
			source.node(Kind.REVIEW, f"{GID}/Review/1")
		with pytest.raises(SourceError):
			source.page(Kind.PAGE, None, None, "")


class TestRichContent:
	def test_the_recorded_post(self) -> None:
		post = nodes(Kind.ARTICLE)["Post/0e1f2a30-0001-4b5c-9d6e-7f8a9b0c0001"]
		html = rich_html(post.child("richContent"))
		assert html.startswith("<h2>Three days of fire</h2><p>We stoke <strong>every five minutes</strong>")
		assert '<a href="https://www.prairiepottery.example/kiln">our kiln</a>.' in html
		assert "<ul><li><p>Ash glaze</p></li>" in html
		assert (
			'<figure><img src="http://wix-fake:8080/media/kiln.png" alt="The kiln at night"></figure>' in html
		)

	def test_other_blocks_and_marks(self) -> None:
		def render(*children: JsonValue) -> str:
			return rich_html(Node({"nodes": list(children)}, "doc"))

		def text(value: str, *marks: JsonValue) -> JsonValue:
			return {"type": "TEXT", "textData": {"text": value, "decorations": list(marks)}}

		assert render({"type": "DIVIDER"}) == "<hr>"
		assert render({"type": "CODE_BLOCK", "nodes": [text("x < y")]}) == "<pre><code>x &lt; y</code></pre>"
		assert render({"type": "HEADING", "nodes": [text("H")], "headingData": {"level": 9}}) == "<h6>H</h6>"
		assert render(text("i", {"type": "ITALIC"}, {"type": "FONT_SIZE"})) == "<em>i</em>"
		linked: JsonValue = {
			"type": "IMAGE",
			"imageData": {"image": {"src": {"id": "a.png"}}, "link": {"url": "https://x.example"}},
		}
		assert render(linked) == (
			'<figure><a href="https://x.example"><img src="https://static.wixstatic.com/media/a.png" alt=""></a></figure>'
		)
		with pytest.raises(Unsupported, match="GALLERY"):
			render({"type": "GALLERY"})
		with pytest.raises(Unsupported, match="SPOILER"):
			render(text("s", {"type": "SPOILER"}))
		with pytest.raises(Unsupported, match="anchor"):
			render(text("a", {"type": "LINK", "linkData": {"link": {"anchor": "top"}}}))
		with pytest.raises(Unsupported, match="no source"):
			render({"type": "IMAGE", "imageData": {"image": {"src": {}}}})
		assert rich_html(None) == ""


class TestMappers:
	def test_categories(self) -> None:
		mugs = values(Kind.COLLECTION, "Category/3b9f2a40-0001-4d6c-8e1f-2a3b4c5d0001")
		assert (mugs.string("slug"), mugs.optional_string("description")) == (
			"mugs",
			"Everyday mugs.<br>\nDishwasher safe.",
		)
		linens = values(Kind.COLLECTION, "Category/3b9f2a40-0002-4d6c-8e1f-2a3b4c5d0002")
		assert linens.optional_string("parent") == f"{GID}/Category/3b9f2a40-0001-4d6c-8e1f-2a3b4c5d0001"
		planters = values(Kind.COLLECTION, "Collection/c01e0000-1001-4a5b-8c9d-0e1f2a3b1001", "catalog-v1")
		assert planters.optional_string("parent") is None

	def test_a_v3_product_without_options_is_simple(self) -> None:
		mug = mapped(Kind.PRODUCT, MUG)
		assert mug is not None and mug.legacy_paths == ("/product-page/speckled-clay-mug",)
		fields = mug.values()
		assert (fields.string("type"), fields.optional_string("sku"), fields.optional_string("barcode")) == (
			"simple",
			"WIX-MUG-1",
			"0845511200019",
		)
		assert (fields.decimal("price"), fields.decimal("stock_qty")) == (Decimal(28), Decimal(14))
		assert len(fields.records("images")) == 2

	def test_v3_variants_with_a_compare_at_price_and_untracked_stock(self) -> None:
		towel = values(Kind.PRODUCT, TOWEL)
		sage, rust = towel.records("variants")
		assert sage.string("gid") == SAGE
		assert (sage.decimal("price"), sage.required_record("sale").decimal("price")) == (
			Decimal(22),
			Decimal(18),
		)
		assert rust.optional_decimal("stock_qty") is None, "an untracked variant"
		assert towel.records("attributes")[0].all() == {"name": "Colour", "values": ["Sage", "Rust"]}

	def test_a_hidden_product_is_unpublished(self) -> None:
		assert not values(Kind.PRODUCT, BOWL).flag("published")

	def test_v1_products(self) -> None:
		planter = mapped(Kind.PRODUCT, "Product/6b5a4c30-1001-4d2e-8f1a-9b0c1d2e1001", "catalog-v1")
		assert planter is not None and planter.warnings
		assert planter.values().decimal("stock_qty") == Decimal(4)
		tumbler = values(Kind.PRODUCT, "Product/6b5a4c30-1002-4d2e-8f1a-9b0c1d2e1002", "catalog-v1")
		assert tumbler.string("type") == "variable" and tumbler.records("variants")

	def test_product_edges(self) -> None:
		mug = nodes(Kind.PRODUCT)[MUG]
		with pytest.raises(SourceError, match="no variants"):
			mapper_of(Kind.PRODUCT).map(mug.with_fields(variants=[]))
		variant = mug.children("variants")[0].raw()
		priceless: JsonValue = [{**variant, "price": {"compareAtPrice": {"amount": "3"}}}]
		with pytest.raises(SourceError, match="no price"):
			mapper_of(Kind.PRODUCT).map(mug.with_fields(variants=priceless))
		modified = mapper_of(Kind.PRODUCT).map(mug.with_fields(modifiers=[{"name": "Gift note"}]))
		assert modified is not None and any("modifiers" in w for w in modified.warnings)

	def test_contacts(self) -> None:
		maya = values(Kind.CUSTOMER, MAYA)
		assert (maya.optional_string("email"), maya.optional_string("company")) == (
			"maya.thunderchild@example.com",
			"Thunderchild Design",
		)
		billing, shipping = maya.records("addresses")
		assert (
			billing.string("address_type"),
			billing.optional_string("city"),
			billing.optional_string("state"),
		) == (
			"Billing",
			"Regina",
			"SK",
		)
		assert (shipping.optional_string("line1"), shipping.optional_string("state")) == (
			"88 Coteau Street West",
			"SK",
		)

	def test_coupons(self) -> None:
		towel = values(Kind.DISCOUNT, COUPON.format(4))
		assert (towel.string("discount"), towel.strings("items"), towel.optional_string("valid_from")) == (
			"percentage",
			[f"{GID}/{TOWEL}"],
			"2026-08-01",
		)
		assert mapped(Kind.DISCOUNT, COUPON.format(5)) is None, "a deactivated coupon"
		for number, kinds, cards in (
			(1, ["per_customer_limit"], []),  # the Webshop fork enforces these (card 405)
			(2, ["free_shipping"], []),
			(3, ["spread_amount"], ["card 407"]),
		):
			desired = mapped(Kind.DISCOUNT, COUPON.format(number))
			assert (condition_kinds(desired), held(desired)) == (kinds, cards)
		assert held(mapped(Kind.DISCOUNT, COUPON.format(4))) == []

	def test_discount_rules(self) -> None:
		source, _ = reader()
		coupons = source.page(Kind.DISCOUNT, None, None, "")
		assert coupons.end_cursor == "rules:"
		assert source.page(Kind.DISCOUNT, "0", None, "").end_cursor == "rules:", (
			"an offset saved before rules"
		)
		with pytest.raises(SourceError, match="discount cursor"):
			source.page(Kind.DISCOUNT, "elsewhere:0", None, "")
		mugs = values(Kind.DISCOUNT, "DiscountRule/d0000000-0000-4000-8000-000000000001")
		assert (
			mugs.flag("automatic"),
			mugs.string("applies"),
			mugs.decimal("value"),
			mugs.decimal("min_qty"),
		) == (
			True,
			"items",
			Decimal(10),
			Decimal(2),
		)
		assert mugs.strings("items") == [f"{GID}/Product/1a7e5c10-0001-4c2e-9b41-6f0d2a1e0001"]
		assert held(mapped(Kind.DISCOUNT, "DiscountRule/d0000000-0000-4000-8000-000000000001")) == []
		assert (
			values(Kind.DISCOUNT, "DiscountRule/d0000000-0000-4000-8000-000000000001", "v2").decimal("value")
			== 15
		)
		three = mapped(Kind.DISCOUNT, "DiscountRule/d0000000-0000-4000-8000-000000000002")
		assert condition_kinds(three) == ["fixed_price_set"]
		assert three is not None and three.values().decimal("min_amount") == Decimal(100)
		assert mapped(Kind.DISCOUNT, "DiscountRule/d0000000-0000-4000-8000-000000000003") is None

	def test_discount_rule_shapes(self) -> None:
		rule = nodes(Kind.DISCOUNT)["DiscountRule/d0000000-0000-4000-8000-000000000001"]
		value = rule.required_child("discounts").children("values")[0].raw()
		mapper = mapper_of(Kind.DISCOUNT)

		def with_value(**changes: JsonValue) -> Desired | None:
			return mapper.map(rule.with_fields(discounts={"values": [{**value, **changes}]}))

		amount = with_value(discountType="FIXED_AMOUNT", fixedAmount="2.5")
		assert amount is not None and amount.values().decimal("value") == Decimal("2.5")
		price = with_value(discountType="FIXED_PRICE", fixedPrice="9")
		assert price is not None and price.values().string("discount") == "rate"
		custom = with_value(
			specificItemsInfo={
				"scopes": [{"customFilter": {"params": {}}, "id": "x", "type": "CUSTOM_FILTER"}]
			}
		)
		assert condition_kinds(custom) == ["item_matcher", "rule_condition"], "and its trigger's other items"
		ranged = mapper.map(
			rule.with_fields(
				trigger={
					"and": {
						"triggers": [
							{"subtotalRange": {"from": "20", "to": "200"}},
							{"customerEligibility": {"individualMembersInfo": {"memberIds": ["m"]}}},
						]
					}
				}
			)
		)
		assert ranged is not None
		assert (ranged.values().decimal("min_amount"), ranged.values().decimal("max_amount")) == (
			Decimal(20),
			Decimal(200),
		)
		assert condition_kinds(ranged) == ["rule_condition"]
		other = mapper.map(
			rule.with_fields(trigger={"itemQuantityRange": {"from": 2, "scopes": [{"id": "other"}]}})
		)
		assert condition_kinds(other) == ["rule_condition"]
		always = mapper.map(rule.with_fields(trigger=None))
		assert always is not None and "min_qty" not in always.fields
		with pytest.raises(SourceError, match="type"):
			with_value(discountType="BOGUS")
		with pytest.raises(SourceError, match="discounts"):
			mapper.map(rule.with_fields(discounts={"values": []}))

	def test_coupon_edges(self) -> None:
		coupon = nodes(Kind.DISCOUNT)[COUPON.format(4)]
		spec = coupon.required_child("specification").raw()

		def with_spec(**changes: JsonValue) -> Desired | None:
			return mapper_of(Kind.DISCOUNT).map(coupon.with_fields(specification={**spec, **changes}))

		on_collection = with_spec(
			scope={"namespace": "stores", "group": {"name": "collection", "entityId": "c1"}}
		)
		assert on_collection is not None and on_collection.values().strings("groups") == [
			f"{GID}/Category/c1"
		]
		store_wide = with_spec(scope={"namespace": "stores"}, minimumSubtotal=40, usageLimit=10)
		assert store_wide is not None and store_wide.values().string("applies") == "transaction"
		amount = with_spec(percentOffRate=None, moneyOffAmount=5, scope=None)
		assert amount is not None and amount.values().string("discount") == "amount"
		bogo = with_spec(percentOffRate=None, buyXGetY={"x": 2, "y": 1}, scope=None)
		assert bogo is not None and bogo.values().required_record("free").all() == {
			"qty": Decimal(1),
			"recursive": True,
			"recurse_for": Decimal(2),
		}
		assert bogo.values().string("applies") == "all_items"
		fixed = with_spec(percentOffRate=None, fixedPriceAmount=12)
		assert fixed is not None and fixed.values().string("discount") == "rate"
		for changes, kinds in (
			({"limitedToOneItem": True}, ["item_limit"]),
			({"scope": {"namespace": "bookings"}}, ["item_matcher"]),
			({"scope": {"namespace": "stores", "group": {"name": "subscription"}}}, ["item_matcher"]),
			({"appliesToSubscriptions": True, "discountedCycleCount": 3}, ["item_matcher"]),
		):
			assert condition_kinds(with_spec(**changes)) == kinds
		with pytest.raises(SourceError, match="no discount"):
			with_spec(percentOffRate=None)

	def test_a_paid_order_balances_to_wixs_total(self) -> None:
		order = values(Kind.ORDER, ORDER_1)
		assert totals(order) == order.decimal("total") == Decimal("69.93")
		assert order.optional_string("customer") == f"{GID}/{MAYA}"
		taxes = [c for c in order.records("charges") if c.string("kind") == "tax"]
		assert [(t.string("description"), t.decimal("amount")) for t in taxes] == [("GST", Decimal("3.33"))]
		payment = order.required_record("payment")
		assert (payment.string("method"), payment.optional_string("reference")) == (
			"Credit/Debit Cards",
			"pi_wix_0001",
		)
		assert order.records("lines")[0].string("name") == "Linen tea towel (Colour: Sage)"
		shipment = order.records("shipments")[0]
		assert (shipment.optional_string("carrier"), shipment.optional_string("contents")) == (
			"Canada Post",
			"2 x Linen tea towel, 1 x Speckled clay mug",
		)

	def test_refunds_that_returned_money(self) -> None:
		refunds = values(Kind.ORDER, ORDER_1, "v2").records("refunds")
		assert len(refunds) == 1, "the failed refund returned nothing"
		lines = [(line.decimal("qty"), line.decimal("rate")) for line in refunds[0].records("lines")]
		assert lines == [(Decimal(-1), Decimal("16.20")), (Decimal(-1), Decimal("0.81"))]

	def test_unpaid_and_unsold_orders(self) -> None:
		assert not values(Kind.ORDER, ORDER_2).flag("submitted")
		order = nodes(Kind.ORDER)[ORDER_1]
		mapper = mapper_of(Kind.ORDER)
		assert mapper.map(order.with_fields(status="CANCELED")) is None
		assert mapper.map(order.with_fields(paymentStatus="DECLINED")) is None
		gift: JsonValue = [{**order.children("lineItems")[0].raw(), "itemType": {"preset": "GIFT_CARD"}}]
		with pytest.raises(Unsupported, match="gift card"):
			mapper.map(order.with_fields(lineItems=gift))

	def test_a_tax_inclusive_order_includes_its_taxes(self) -> None:
		order = nodes(Kind.ORDER)[ORDER_1].with_fields(taxIncludedInPrices=True)
		desired = mapper_of(Kind.ORDER).map(order)
		assert desired is not None
		found = desired.values()
		assert found.flag("taxes_included")
		assert [line.decimal("rate") for line in found.records("lines")] == [
			Decimal("18.81"),
			Decimal("29.26"),
		]
		assert [c.all() for c in found.records("charges")] == [
			{
				"kind": "included_tax",
				"description": "GST",
				"amount": Decimal("2.88"),
				"rate": Decimal("5.00"),
			},
			{"kind": "shipping", "description": "Canada Post Regular Parcel", "amount": Decimal("8.55")},
			{
				"kind": "shipping_tax",
				"description": "GST (Canada Post Regular Parcel)",
				"amount": Decimal("0.45"),
				"rate": Decimal("5.00"),
			},
		]

	def test_tax_inclusive_lines_taxed_differently_and_a_taxed_fee(self) -> None:
		order = nodes(Kind.ORDER)[ORDER_1]
		exempt = {**order.children("lineItems")[1].raw(), "taxInfo": None}
		fee: JsonValue = {
			"name": "Rush",
			"price": {"amount": "2.10"},
			"priceBeforeTax": {"amount": "2.00"},
			"taxInfo": {"taxAmount": {"amount": "0.10"}, "taxRate": "0.05"},
		}
		desired = mapper_of(Kind.ORDER).map(
			order.with_fields(
				taxIncludedInPrices=True,
				lineItems=[order.children("lineItems")[0].raw(), exempt],
				shippingInfo=None,
				additionalFees=[fee],
			)
		)
		assert desired is not None
		found = desired.values()
		assert [line.all().get("taxes") for line in found.records("lines")] == [
			[{"title": "GST", "rate": Decimal("5.00")}],
			[],
		]
		assert [c.all() for c in found.records("charges")] == [
			{"kind": "included_tax", "description": "GST", "amount": Decimal("1.62")},
			{"kind": "fee", "description": "Rush", "amount": Decimal("2.00")},
			{
				"kind": "shipping_tax",
				"description": "Tax (Rush)",
				"amount": Decimal("0.10"),
				"rate": Decimal("5.00"),
			},
		]

	def test_order_edges(self) -> None:
		order = nodes(Kind.ORDER, "v2")[ORDER_1]
		bare = mapper_of(Kind.ORDER).map(
			order.with_fields(
				lineItems=[{**line.raw(), "taxInfo": None} for line in order.children("lineItems")],
				shippingInfo=None,
				additionalFees=[{"name": "Rush", "price": {"amount": "2.00"}}],
				buyerInfo=None,
				payments=[{"status": "APPROVED", "giftcardPaymentDetails": {}}],
			)
		)
		assert bare is not None
		charges = [(c.string("description"), c.decimal("amount")) for c in bare.values().records("charges")]
		assert charges == [("Tax", Decimal("3.33")), ("Rush", Decimal(2))]
		assert bare.values().required_record("guest").optional_string("name") == "Maya Thunderchild"
		assert bare.values().required_record("payment").string("method") == "Gift card"
		stray: dict[str, JsonValue] = {
			**order.children("refunds")[0].raw(),
			"details": {"lineItems": [{"lineItemId": "x", "quantity": 1}]},
		}
		with pytest.raises(Unsupported, match="line the order doesn't have"):
			mapper_of(Kind.ORDER).map(order.with_fields(refunds=[stray]))

	def test_blog_posts_reviews_and_redirects(self) -> None:
		blog = mapped(Kind.BLOG, f"Blog/{SITE}")
		assert blog is not None and blog.legacy_paths == ("/blog",)
		studio = values(Kind.BLOG, "BlogCategory/4d3c2b10-0001-4e5f-8a9b-0c1d2e3f0001")
		assert studio.string("title") == "Studio notes"
		post = values(Kind.ARTICLE, "Post/0e1f2a30-0001-4b5c-9d6e-7f8a9b0c0001")
		assert post.string("category") == f"{GID}/BlogCategory/4d3c2b10-0001-4e5f-8a9b-0c1d2e3f0001"
		assert post.strings("tags") == ["woodfire", "studio"]
		assert post.strings("images") == ["http://wix-fake:8080/media/kiln.png"]
		glaze = values(Kind.ARTICLE, "Post/0e1f2a30-0002-4b5c-9d6e-7f8a9b0c0002", "v2")
		assert glaze.string("category") == f"{GID}/Blog/{SITE}"
		review = values(Kind.REVIEW, "Review/aa000000-0000-4000-8000-000000000001")
		assert (review.string("product"), review.decimal("rating")) == (f"{GID}/{MUG}", Decimal(1))
		assert mapped(Kind.REVIEW, "Review/aa000000-0000-4000-8000-000000000002") is None
		redirect = values(Kind.URL_REDIRECT, "Redirect/e1000000-0000-4000-8000-000000000001")
		assert redirect.all() == {
			"source": "/old-mug",
			"target": "/product-page/speckled-clay-mug",
			"status": 301,
		}
		with pytest.raises(Unsupported, match="every path"):
			mapped(Kind.URL_REDIRECT, "Redirect/e1000000-0000-4000-8000-000000000003")

	def test_field_helpers(self) -> None:
		assert epoch_time(Node({"t": "1785585600000"}, "x"), "t") == "2026-08-01T12:00:00Z"
		assert epoch_time(Node({}, "x"), "t") is None
		assert (state_of("CA-SK", "CA"), state_of("SK", "CA"), state_of(None, "CA")) == ("SK", "SK", None)
		assert text_html("a & b") == "a &amp; b" and text_html(None) is None


def test_the_adapter() -> None:
	assert {m.kind for m in ADAPTER.mappers(CONFIG)} == set(ADAPTER.kinds())
	assert ADAPTER.filter_hint() is None
