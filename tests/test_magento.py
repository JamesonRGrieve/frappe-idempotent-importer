# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Magento adapter over the recorded store (tests/fixtures/magento, copied from
wp-woo-idempotent-importer): the reader's searchCriteria paging, store context and assembly, and the
mappers."""

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
from idempotent_importer.sources.magento.adapter import MagentoAdapter
from idempotent_importer.sources.magento.mapping import (
	base_fields,
	legacy_path,
	rule_date,
	whole,
)
from idempotent_importer.sources.magento.reader import (
	MagentoAuthorizer,
	MagentoReader,
	api_base,
	search_query,
)
from tests.support.discounts import condition_kinds, held, terms
from tests.support.transport import FakeTransport, RecordingSleeper, rest_source

ADAPTER = MagentoAdapter()
CONFIG = ConnectionConfig("magento", "magento-m", "http://magento-fake:8080", api_secret="e2e-magento-token")
GID = "gid://magento/magento-m"


def reader(state: str = "v1") -> tuple[MagentoReader, FakeTransport]:
	transport = rest_source("magento", state)
	built = ADAPTER.reader(CONFIG, transport, RecordingSleeper())
	assert isinstance(built, MagentoReader)
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
	def test_requests_go_to_the_store_views_rest_path_with_the_token(self) -> None:
		source, transport = reader()
		source.page(Kind.COLLECTION, None, None, "")
		assert transport.requests[0].url.startswith(
			"http://magento-fake:8080/rest/default/V1/categories/list?"
		)
		assert transport.requests[0].headers["Authorization"] == "Bearer e2e-magento-token"
		with pytest.raises(SourceError, match="access token"):
			MagentoAuthorizer("")
		assert "e2e-magento-token" not in repr(MagentoAuthorizer("e2e-magento-token"))

	def test_the_store_url_and_view_code_are_checked(self) -> None:
		assert api_base(ConnectionConfig("magento", "m", "https://shop.example/", account="fr_ca")).endswith(
			"/rest/fr_ca"
		)
		with pytest.raises(SourceError, match="store URL"):
			api_base(ConnectionConfig("magento", "m", "shop.example"))
		with pytest.raises(SourceError, match="store view code"):
			api_base(ConnectionConfig("magento", "m", "https://shop.example", account="Bad-Code"))

	def test_search_criteria(self) -> None:
		query = search_query([("level", "1", "gt")], "entity_id", 2)
		assert query["searchCriteria[filterGroups][0][filters][0][conditionType]"] == "gt"
		assert (query["searchCriteria[sortOrders][0][field]"], query["searchCriteria[currentPage]"]) == (
			"entity_id",
			2,
		)

	def test_lists_end_at_their_total_count(self) -> None:
		source, _ = reader("v2")
		first = source.page(Kind.CUSTOMER, None, None, "")
		assert sorted(nodes(Kind.CUSTOMER, "v2")) == ["Customer/41", "Customer/42", "Customer/43"]
		assert first.has_next_page == (len(first.nodes) < 3)

	def test_products_arrive_with_the_store_and_a_configurable_products_children(self) -> None:
		products = nodes(Kind.PRODUCT)
		jacket = products["Product/12"]
		assert [child.integer("id") for child in jacket.children("children")] == [121, 122]
		assert jacket.children("attributes")[0].string("attribute_code") == "color"
		store = jacket.required_child("store")
		assert (store.string("media_url"), store.integers("root_category_ids")) == (
			"http://magento-fake:8080/media/",
			[1, 0, 2],
		)

	def test_orders_arrive_with_credit_memos_and_shipments(self) -> None:
		order = nodes(Kind.ORDER, "v2")["Order/501"]
		assert order.children("credit_memos") and order.children("shipments")

	def test_rules_arrive_with_their_coupons_and_every_customer_group(self) -> None:
		fall = nodes(Kind.DISCOUNT)["SalesRule/62"]
		assert [c.string("code") for c in fall.children("coupons")] == ["MAG-FALL15"]
		assert fall.integers("all_customer_group_ids")

	def test_a_discount_run_reads_rules_then_tier_prices_by_customer_group(self) -> None:
		source, _ = reader()
		rules = source.page(Kind.DISCOUNT, None, None, "")
		assert rules.end_cursor == "tiers:"
		tiers = source.page(Kind.DISCOUNT, rules.end_cursor, None, "")
		assert [n.id().removeprefix(f"{GID}/") for n in tiers.nodes] == [
			"TierPrice/11-2",
			"TierPrice/11-32000",
			"TierPrice/122-3",
		]
		assert not tiers.has_next_page
		kettle = next(n for n in tiers.nodes if n.id().endswith("/11-2"))
		assert len(kettle.children("tier_prices")) == 1, "the other website's tier price isn't this store's"
		assert source.page(Kind.DISCOUNT, "1", None, "").end_cursor == "tiers:", "a cursor saved before tiers"
		with pytest.raises(SourceError, match="discount cursor"):
			source.page(Kind.DISCOUNT, "elsewhere:1", None, "")
		assert source.node(Kind.DISCOUNT, f"{GID}/TierPrice/11-9") is None
		with pytest.raises(SourceError, match="tier price"):
			source.node(Kind.DISCOUNT, f"{GID}/TierPrice/11")
		assert set(nodes(Kind.CUSTOMER_GROUP)) == {f"CustomerGroup/{i}" for i in range(4)}
		group = source.node(Kind.CUSTOMER_GROUP, f"{GID}/CustomerGroup/2")
		assert group is not None and group.string("code") == "Wholesale"

	def test_incremental_kinds(self) -> None:
		source, transport = reader()
		assert source.is_incremental(Kind.ORDER) and not source.is_incremental(Kind.PRODUCT)
		source.page(Kind.CUSTOMER, None, datetime(2026, 9, 1, tzinfo=UTC), "")
		assert "updated_at" in transport.requests[-1].url

	def test_single_objects(self) -> None:
		source, _ = reader()
		kettle = source.node(Kind.PRODUCT, f"{GID}/Product/11")
		assert kettle is not None and kettle.string("source_id") == "11"
		assert source.node(Kind.PRODUCT, f"{GID}/Product/999") is None
		for kind, key in (
			(Kind.COLLECTION, "Category/3"),
			(Kind.CUSTOMER, "Customer/41"),
			(Kind.DISCOUNT, "SalesRule/62"),
			(Kind.ORDER, "Order/501"),
			(Kind.PAGE, "CmsPage/2"),
		):
			found = source.node(kind, f"{GID}/{key}")
			assert found is None or found.id() == f"{GID}/{key}"
		with pytest.raises(SourceError, match="Not a Magento identity"):
			source.node(Kind.ORDER, f"{GID}/Order/x")
		with pytest.raises(SourceError):
			source.node(Kind.REVIEW, f"{GID}/Review/1")
		with pytest.raises(SourceError):
			source.page(Kind.REVIEW, None, None, "")


class TestMappers:
	def test_categories_below_the_store_root(self) -> None:
		galley = mapped(Kind.COLLECTION, "Category/3")
		assert galley is not None and galley.legacy_paths == ("/galley.html",)
		fields = galley.values()
		assert (fields.optional_string("parent"), fields.optional_string("image")) == (
			None,
			"http://magento-fake:8080/media/catalog/category/galley.png",
		)
		assert values(Kind.COLLECTION, "Category/5").optional_string("parent") == f"{GID}/Category/4"

	def test_a_simple_product_with_a_special_price(self) -> None:
		kettle = mapped(Kind.PRODUCT, "Product/11")
		assert kettle is not None and kettle.legacy_paths == ("/copper-kettle.html",)
		fields = kettle.values()
		assert (fields.decimal("price"), fields.required_record("sale").decimal("price")) == (
			Decimal("89.99"),
			Decimal("79.99"),
		)
		assert (fields.optional_string("sku"), fields.decimal("stock_qty")) == ("MAG-KETTLE-1", Decimal(7))
		assert fields.required_record("weight").all() == {"value": Decimal("1.4"), "unit": "kg"}
		assert [image.string("url").rsplit("/", 1)[-1] for image in fields.records("images")] == [
			"copper-kettle.png",
			"copper-kettle-side.png",
		]
		assert fields.strings("collections") == [f"{GID}/Category/3"]

	def test_a_configurable_product_is_a_template_of_its_children(self) -> None:
		jacket = values(Kind.PRODUCT, "Product/12")
		red, navy = jacket.records("variants")
		assert (red.string("gid"), red.decimal("price"), red.decimal("stock_qty")) == (
			f"{GID}/Variant/121",
			Decimal(149),
			Decimal(4),
		)
		assert navy.required_record("attributes").all() == {"Colour": "Navy"}
		assert jacket.records("attributes")[0].all() == {"name": "Colour", "values": ["Red", "Navy"]}

	def test_disabled_downloadable_and_bundle_products(self) -> None:
		assert not values(Kind.PRODUCT, "Product/13").flag("published")
		tide = values(Kind.PRODUCT, "Product/14")
		assert not tide.flag("is_stock_item") and tide.optional_decimal("stock_qty") is None
		with pytest.raises(Unsupported, match="bundle"):
			mapped(Kind.PRODUCT, "Product/15")

	def test_product_edges(self) -> None:
		kettle = nodes(Kind.PRODUCT)["Product/11"]
		store = kettle.required_child("store").raw()
		assert mapper_of(Kind.PRODUCT).map(kettle.with_fields(store={**store, "website_id": 9})) is None
		with pytest.raises(SourceError, match="no price"):
			mapper_of(Kind.PRODUCT).map(kettle.with_fields(price=None))
		jacket = nodes(Kind.PRODUCT)["Product/12"]
		with pytest.raises(Unsupported, match="no enabled child"):
			mapper_of(Kind.PRODUCT).map(jacket.with_fields(children=[]))
		odd: dict[str, JsonValue] = {
			**jacket.children("children")[0].raw(),
			"custom_attributes": [{"attribute_code": "color", "value": "1"}],
		}
		with pytest.raises(SourceError, match="color option"):
			mapper_of(Kind.PRODUCT).map(jacket.with_fields(children=[odd]))
		with pytest.raises(SourceError, match="wasn't read"):
			mapper_of(Kind.PRODUCT).map(jacket.with_fields(attributes=[]))
		pounds = mapper_of(Kind.PRODUCT).map(
			kettle.with_fields(
				store={**store, "weight_unit": "stone"},
				options=[{"title": "Engraving"}],
				tier_prices=[{"qty": 2}],
			)
		)
		assert pounds is not None and len(pounds.warnings) == 3

	def test_customers(self) -> None:
		maeve = values(Kind.CUSTOMER, "Customer/41")
		assert (maeve.optional_string("email"), maeve.optional_string("phone")) == (
			"maeve@example.com",
			"+1 250-555-0141",
		)
		address = maeve.records("addresses")[0]
		assert (
			address.string("address_type"),
			address.optional_string("state"),
			address.optional_string("line2"),
		) == ("Billing", "BC", "Unit 3")

	def test_rules(self) -> None:
		fall = values(Kind.DISCOUNT, "SalesRule/62")
		assert (fall.string("discount"), fall.decimal("value"), fall.optional_integer("max_uses")) == (
			"percentage",
			Decimal(15),
			100,
		)
		assert (fall.optional_string("valid_from"), fall.optional_string("valid_upto")) == (
			"2026-09-01",
			"2026-10-30",
		)
		assert mapped(Kind.DISCOUNT, "SalesRule/66") is None, "an inactive rule"
		assert held(mapped(Kind.DISCOUNT, "SalesRule/62")) == []
		welcome = mapped(Kind.DISCOUNT, "SalesRule/61")
		assert (condition_kinds(welcome), held(welcome)) == (["per_customer_limit"], ["card 405"])
		assert values(Kind.DISCOUNT, "SalesRule/61").required_record("code_limits").required_record(
			"MAG-WELCOME5"
		).strings("held") == ["card 405"]
		assert terms(mapped(Kind.DISCOUNT, "SalesRule/63"), "free_shipping") == {
			"source": "for the shipment with matching items"
		}
		automatic = values(Kind.DISCOUNT, "SalesRule/64")
		assert (automatic.flag("automatic"), automatic.strings("codes"), automatic.string("applies")) == (
			True,
			[],
			"transaction",
		)
		jackets = values(Kind.DISCOUNT, "SalesRule/65")
		assert (jackets.string("discount"), jackets.string("applies"), jackets.strings("groups")) == (
			"amount",
			"groups",
			[f"{GID}/Category/4"],
		)
		assert held(mapped(Kind.DISCOUNT, "SalesRule/65")) == []

	def test_tier_prices_and_customer_groups(self) -> None:
		every = values(Kind.DISCOUNT, "TierPrice/11-32000")
		assert (every.strings("items"), every.strings("customer_groups"), every.flag("automatic")) == (
			[f"{GID}/Product/11"],
			[],
			True,
		)
		assert [t.all() for t in every.records("tiers")] == [
			{"discount": "rate", "value": Decimal("84.99"), "min_qty": Decimal(3), "max_qty": Decimal(9)},
			{"discount": "rate", "value": Decimal("79.99"), "min_qty": Decimal(10)},
		]
		assert len(values(Kind.DISCOUNT, "TierPrice/11-32000", "v2").records("tiers")) == 1
		wholesale = values(Kind.DISCOUNT, "TierPrice/11-2")
		assert wholesale.strings("customer_groups") == [f"{GID}/CustomerGroup/2"]
		assert [t.all() for t in wholesale.records("tiers")] == [
			{"discount": "percentage", "value": Decimal(15), "min_qty": Decimal(1)}
		]
		navy = values(Kind.DISCOUNT, "TierPrice/122-3")
		assert (navy.strings("items"), navy.records("variants")[0].string("variant")) == (
			[],
			f"{GID}/Variant/122",
		)
		assert (
			mapper_of(Kind.DISCOUNT).map(nodes(Kind.DISCOUNT)["TierPrice/122-3"].with_fields(tier_prices=[]))
			is None
		)
		assert values(Kind.CUSTOMER_GROUP, "CustomerGroup/2").all() == {"name": "Wholesale"}
		assert values(Kind.CUSTOMER, "Customer/41").optional_string("group") == f"{GID}/CustomerGroup/1"

	@pytest.mark.parametrize(
		("changes", "kinds"),
		[
			({"discount_qty": 2}, ["item_limit"]),
			({"discount_step": 3}, ["item_choice"]),
			({"apply_to_shipping": True}, ["shipping_discount"]),
			({"uses_per_customer": 2}, ["per_customer_limit"]),
			({"customer_group_ids": [1, 2]}, ["several_customer_groups"]),
			(
				{
					"action_condition": {
						"aggregator_type": "all",
						"value": "1",
						"conditions": [{"attribute_name": "sku", "operator": "==", "value": "X"}],
					}
				},
				["item_matcher"],
			),
			(
				{
					"action_condition": {
						"aggregator_type": "all",
						"value": "1",
						"conditions": [{"attribute_name": "category_ids", "operator": "==", "value": "x"}],
					}
				},
				["item_matcher"],
			),
			(
				{
					"condition": {
						"aggregator_type": "any",
						"value": "1",
						"conditions": [{"attribute_name": "base_subtotal", "operator": ">=", "value": "5"}],
					}
				},
				["rule_condition"],
			),
			(
				{
					"simple_action": "cart_fixed",
					"action_condition": {
						"aggregator_type": "all",
						"value": "1",
						"conditions": [{"attribute_name": "category_ids", "operator": "()", "value": "4, 5"}],
					},
				},
				["spread_amount"],
			),
		],
	)
	def test_rule_conditions_are_recorded(self, changes: dict[str, JsonValue], kinds: list[str]) -> None:
		desired = mapper_of(Kind.DISCOUNT).map(nodes(Kind.DISCOUNT)["SalesRule/62"].with_fields(**changes))
		assert condition_kinds(desired) == kinds
		assert held(desired)

	def test_rule_edges(self) -> None:
		fall = nodes(Kind.DISCOUNT)["SalesRule/62"]
		mapper = mapper_of(Kind.DISCOUNT)
		minimums: JsonValue = {
			"aggregator_type": "all",
			"value": "1",
			"conditions": [
				{"attribute_name": "base_subtotal", "operator": ">=", "value": "50", "conditions": []},
				{"attribute_name": "total_qty", "operator": ">=", "value": "3", "conditions": []},
			],
		}
		cart = mapper.map(fall.with_fields(simple_action="cart_fixed", discount_amount=5, condition=minimums))
		assert cart is not None
		assert (cart.values().decimal("min_amount"), cart.values().decimal("min_qty")) == (
			Decimal(50),
			Decimal(3),
		)
		one_group = mapper.map(fall.with_fields(customer_group_ids=[2]))
		assert one_group is not None and one_group.values().strings("customer_groups") == [
			f"{GID}/CustomerGroup/2"
		]
		own = mapper.map(
			fall.with_fields(
				coupons=[{"code": "A", "usage_limit": 5, "expiration_date": "2026-12-01"}, {"code": "B"}]
			)
		)
		assert own is not None
		assert own.values().required_record("code_limits").all() == {
			"A": {"max_uses": 5, "valid_upto": "2026-12-01"}
		}
		bogo = mapper.map(fall.with_fields(simple_action="buy_x_get_y", discount_step=2, discount_amount=1))
		assert bogo is not None
		assert bogo.values().required_record("free").all() == {
			"qty": Decimal(1),
			"recursive": True,
			"recurse_for": Decimal(2),
		}
		assert bogo.values().decimal("min_qty") == Decimal(2)
		with pytest.raises(SourceError, match="buys none"):
			mapper.map(fall.with_fields(simple_action="buy_x_get_y", discount_step=0))
		with pytest.raises(SourceError, match="action"):
			mapper.map(fall.with_fields(simple_action="by_magic"))
		assert rule_date("2026-09-01 00:00:00") == "2026-09-01" and rule_date(None) is None
		with pytest.raises(SourceError, match="not a date"):
			rule_date("soon")

	def test_a_paid_order_balances_to_magentos_total(self) -> None:
		order = values(Kind.ORDER, "Order/501")
		assert totals(order) == order.decimal("total") == Decimal("258.82")
		lines = order.records("lines")
		assert [line.optional_string("variant") for line in lines] == [
			f"{GID}/Variant/11",
			f"{GID}/Variant/122",
		]
		assert order.required_record("payment").string("method") == "Check / Money order"
		assert len(order.records("notes")) == 1, "the comment"
		shipment = order.records("shipments")[0]
		assert (shipment.optional_string("carrier"), shipment.optional_string("tracking_number")) == (
			"Canada Post",
			"LM987654321CA",
		)
		assert order.optional_string("customer") == f"{GID}/Customer/41"

	def test_a_guest_order_and_a_credit_memo(self) -> None:
		guest = values(Kind.ORDER, "Order/502")
		assert not guest.flag("submitted") and guest.optional_string("customer") is None
		taxes = [c for c in guest.records("charges") if c.string("kind") == "tax"]
		assert [(t.string("description"), t.decimal("amount")) for t in taxes] == [("GST", Decimal("8.5"))]
		memo = values(Kind.ORDER, "Order/501", "v2").records("refunds")[0]
		assert (memo.decimal("amount"), memo.optional_string("reason")) == (
			Decimal(10),
			"Dented lid: partial refund.",
		)

	def test_order_edges(self) -> None:
		order = nodes(Kind.ORDER, "v2")["Order/501"]
		mapper = mapper_of(Kind.ORDER)
		assert mapper.map(order.with_fields(state="canceled")) is None
		gift: list[JsonValue] = [{**order.children("items")[0].raw(), "product_type": "giftcard"}]
		with pytest.raises(Unsupported, match="gift card"):
			mapper.map(order.with_fields(items=gift))
		memo = order.children("credit_memos")[0].raw()
		returned: JsonValue = [{"order_item_id": 1001, "qty": 1, "row_total": 10}]
		with_line = mapper.map(order.with_fields(credit_memos=[{**memo, "items": returned}]))
		assert with_line is not None
		refund_lines = with_line.values().records("refunds")[0].records("lines")
		assert [(line.decimal("qty"), line.decimal("rate")) for line in refund_lines] == [
			(Decimal(-1), Decimal(10))
		]
		usd = mapper.map(
			order.with_fields(
				order_currency_code="USD",
				base_currency_code="CAD",
				grand_total=190.31,
				base_grand_total=258.82,
			)
		)
		assert usd is not None
		assert (usd.values().decimal("exchange_rate"), usd.values().string("base_currency")) == (
			Decimal("1.359991593"),
			"CAD",
		)
		rate = usd.values().decimal("exchange_rate")
		assert (Decimal("190.31") * rate).quantize(Decimal("0.01")) == Decimal("258.82"), (
			"rounds to Magento's base total"
		)
		assert whole(Node({"q": "2.0000"}, "x"), "q") == Decimal(2)
		with pytest.raises(Unsupported, match="isn't whole"):
			whole(Node({"q": 1.5}, "x"), "q")
		assert base_fields(Node({"grand_total": 1, "base_to_order_rate": "0.8"}, "x"), "CAD") == {
			"exchange_rate": Decimal("1.250000000"),
			"base_currency": "CAD",
		}
		with pytest.raises(SourceError, match="no base total or exchange rate"):
			base_fields(Node({"grand_total": 1, "source_id": "9"}, "x"), "CAD")

	def test_cms_pages(self) -> None:
		home = mapped(Kind.PAGE, "CmsPage/1")
		assert home is not None and home.legacy_paths == ()
		story = mapped(Kind.PAGE, "CmsPage/2")
		assert story is not None and story.legacy_paths == ("/company/our-story",)
		assert story.values().string("slug") == "our-story"
		assert not values(Kind.PAGE, "CmsPage/3").flag("published")
		assert legacy_path(None) == ()


def test_the_adapter() -> None:
	assert {m.kind for m in ADAPTER.mappers(CONFIG)} == set(ADAPTER.kinds())
	assert ADAPTER.filter_hint() is None
