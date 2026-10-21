# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Square Online adapter over the recorded seller (tests/fixtures/square_online, copied from
wp-woo-idempotent-importer): the reader's catalog assembly, stock, paging and incremental phases, and the
mappers."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.square_online.adapter import SquareOnlineAdapter
from idempotent_importer.sources.square_online.mapping import CAP_SCOPE, whole
from idempotent_importer.sources.square_online.reader import SquareAuthorizer, SquareOnlineReader
from tests.support.discounts import condition_kinds, held, terms
from tests.support.transport import FakeTransport, RecordingSleeper, rest_source

ADAPTER = SquareOnlineAdapter()
CONFIG = ConnectionConfig(
	"square_online", "square-q", "https://clay.square.site", api_secret="e2e-square-token"
)


def reader(state: str = "v1") -> tuple[SquareOnlineReader, FakeTransport]:
	transport = rest_source("square_online", state)
	built = ADAPTER.reader(CONFIG, transport, RecordingSleeper())
	assert isinstance(built, SquareOnlineReader)
	return built, transport


def nodes(kind: Kind, state: str = "v1", since: datetime | None = None) -> dict[str, Node]:
	source, _ = reader(state)
	found: dict[str, Node] = {}
	cursor: str | None = None
	while True:
		page = source.page(kind, cursor, since, "")
		found |= {node.string("square_id"): node for node in page.nodes}
		if not page.has_next_page:
			return found
		cursor = page.end_cursor


def mapped(kind: Kind, square_id: str, state: str = "v1") -> Desired | None:
	return {m.kind: m for m in ADAPTER.mappers(CONFIG)}[kind].map(nodes(kind, state)[square_id])


def values(kind: Kind, square_id: str, state: str = "v1") -> Values:
	desired = mapped(kind, square_id, state)
	assert desired is not None
	return desired.values()


def totals(order: Values) -> Decimal:
	goods = sum((line.decimal("qty") * line.decimal("rate") for line in order.records("lines")), Decimal(0))
	charges = sum((charge.decimal("amount") for charge in order.records("charges")), Decimal(0))
	return goods - order.decimal("discount") + charges


class TestReader:
	def test_requests_carry_the_token_and_the_pinned_version(self) -> None:
		source, transport = reader()
		source.page(Kind.COLLECTION, None, None, "")
		assert transport.requests[0].headers["Authorization"] == "Bearer e2e-square-token"
		assert transport.requests[0].headers["Square-Version"] == "2026-09-16"
		with pytest.raises(SourceError, match="access token"):
			SquareAuthorizer("")
		assert "e2e-square-token" not in repr(SquareAuthorizer("e2e-square-token"))

	def test_items_arrive_with_what_they_reference_and_their_stock(self) -> None:
		mug = nodes(Kind.PRODUCT)["ITEM_MUG"]
		assert mug.id() == "gid://square_online/square-q/Item/ITEM_MUG"
		assert [image.string("id") for image in mug.children("image_objects")]
		assert mug.string("main_location_id") == "LOCMAIN1"
		assert mug.children("inventory_counts")
		assert nodes(Kind.PRODUCT)["ITEM_APRON"].children("option_objects"), "item options read in a batch"

	def test_customers_page_through(self) -> None:
		assert sorted(nodes(Kind.CUSTOMER)) == ["CUST_ADA", "CUST_BOB"]
		assert "CUST_GRACE" in nodes(Kind.CUSTOMER, "v2")

	def test_an_incremental_run_reads_changed_items_then_restocked_ones(self) -> None:
		since = datetime(2026, 9, 1, tzinfo=UTC)
		source, transport = reader("v2")
		assert source.is_incremental(Kind.PRODUCT)
		first = source.page(Kind.PRODUCT, None, since, "")
		assert first.has_next_page and first.end_cursor == "stock:"
		assert any(node.get("is_deleted") is True for node in first.nodes), "deleted items, to retire"
		restocked = source.page(Kind.PRODUCT, first.end_cursor, since, "")
		assert [node.string("square_id") for node in restocked.nodes] == ["ITEM_MUG"]
		bodies = [json.loads(r.body) for r in transport.requests if r.body]
		assert any(body.get("updated_after") == "2026-09-01T00:00:00Z" for body in bodies)

	def test_customers_and_orders_since(self) -> None:
		since = datetime(2026, 9, 1, tzinfo=UTC)
		source, transport = reader()
		source.page(Kind.CUSTOMER, None, since, "")
		source.page(Kind.ORDER, None, since, "")
		bodies = [json.loads(r.body) for r in transport.requests if r.body]
		assert bodies[0]["query"]["filter"]["updated_at"] == {"start_at": "2026-09-01T00:00:00Z"}
		assert bodies[-2]["query"]["filter"]["date_time_filter"] == {
			"updated_at": {"start_at": "2026-09-01T00:00:00Z"}
		}

	def test_orders_name_their_lines_items(self) -> None:
		order = nodes(Kind.ORDER)["ORDER_1"]
		assert order.required_child("variation_items").string("VAR_APRON_CHAR") == "ITEM_APRON"

	def test_single_objects(self) -> None:
		source, _ = reader()
		mug = source.node(Kind.PRODUCT, "gid://square_online/square-q/Item/ITEM_MUG")
		assert mug is not None and mug.string("square_id") == "ITEM_MUG"
		assert source.node(Kind.COLLECTION, "gid://square_online/square-q/Category/NOPE") is None
		assert source.node(Kind.CUSTOMER, "gid://square_online/square-q/Customer/NOPE") is None
		assert source.node(Kind.ORDER, "gid://square_online/square-q/Order/NOPE") is None
		with pytest.raises(SourceError):
			source.node(Kind.REVIEW, "gid://square_online/square-q/Review/1")
		with pytest.raises(SourceError):
			source.page(Kind.REVIEW, None, None, "")


class TestMappers:
	def test_a_one_variation_item(self) -> None:
		mug = values(Kind.PRODUCT, "ITEM_MUG")
		assert (mug.string("type"), mug.optional_string("sku"), mug.optional_string("barcode")) == (
			"simple",
			"PP-MUG-OCEAN",
			"0629104150021",
		)
		assert (mug.decimal("price"), mug.decimal("stock_qty")) == (Decimal(32), Decimal(4))
		assert mug.strings("collections") == ["gid://square_online/square-q/Category/CAT_MUGS"]
		assert len(mug.records("images")) == 2

	def test_variations_at_the_main_location(self) -> None:
		apron = values(Kind.PRODUCT, "ITEM_APRON")
		charcoal = apron.records("variants")[1]
		assert (charcoal.decimal("price"), charcoal.decimal("stock_qty")) == (Decimal(48), Decimal(0))
		assert charcoal.required_record("attributes").all() == {"Colour": "Charcoal"}

	def test_archived_and_gift_card_items(self) -> None:
		assert not values(Kind.PRODUCT, "ITEM_SAMPLE").flag("published")
		with pytest.raises(Unsupported, match="GIFT_CARD"):
			mapped(Kind.PRODUCT, "ITEM_GIFT")

	def test_a_deleted_item_is_retired(self) -> None:
		mapper = {m.kind: m for m in ADAPTER.mappers(CONFIG)}[Kind.PRODUCT]
		assert mapper.map(nodes(Kind.PRODUCT)["ITEM_MUG"].with_fields(is_deleted=True)) is None

	def test_a_completed_order_balances(self) -> None:
		order = values(Kind.ORDER, "ORDER_1")
		assert totals(order) == order.decimal("total") == Decimal("123.06")
		assert order.decimal("discount") == Decimal("4.80")
		assert order.records("lines")[1].optional_string("variant") == (
			"gid://square_online/square-q/ItemVariation/VAR_APRON_CHAR"
		)
		assert order.optional_string("customer") == "gid://square_online/square-q/Customer/CUST_ADA"
		assert order.records("notes"), "fulfillment state as a comment"

	def test_an_open_unpaid_order_is_a_draft_and_refunds_are_credit_notes(self) -> None:
		assert not values(Kind.ORDER, "ORDER_2").flag("submitted")
		refund = values(Kind.ORDER, "ORDER_1", "v2").records("refunds")[0]
		assert refund.decimal("amount") == Decimal(10)
		cancelled = nodes(Kind.ORDER)["ORDER_1"].with_fields(state="CANCELED")
		assert {m.kind: m for m in ADAPTER.mappers(CONFIG)}[Kind.ORDER].map(cancelled) is None
		returned = nodes(Kind.ORDER)["ORDER_1"].with_fields(returns=[{"uid": "r"}])
		with pytest.raises(Unsupported, match="return"):
			{m.kind: m for m in ADAPTER.mappers(CONFIG)}[Kind.ORDER].map(returned)

	def test_customers_categories_and_the_return_policy(self) -> None:
		ada = values(Kind.CUSTOMER, "CUST_ADA")
		assert (ada.string("name"), ada.records("addresses")[0].optional_string("postcode")) == (
			"Ada Lovelace",
			"T5J 1A1",
		)
		assert values(Kind.COLLECTION, "CAT_MUGS").string("name") == "Mugs"
		assert values(Kind.POLICY, "LOCMAIN1").string("slug") == "policies/return-policy"

	def test_quantities_are_whole(self) -> None:
		assert whole("2", "x") == Decimal(2)
		with pytest.raises(Unsupported, match="fractional"):
			whole("1.5", "x")


GID = "gid://square_online/square-q"


class TestDiscounts:
	MAPPER = {m.kind: m for m in ADAPTER.mappers(CONFIG)}[Kind.DISCOUNT]

	def test_a_discount_run_reads_pricing_rules_then_staff_discounts(self) -> None:
		source, _ = reader()
		rules = source.page(Kind.DISCOUNT, None, None, "")
		assert rules.end_cursor == "discounts:"
		mugs = next(n for n in rules.nodes if n.string("square_id") == "PR_MUGS")
		assert mugs.required_child("catalog_types").raw() == {"ITEM_MUG": {"type": "ITEM"}}
		assert sorted(nodes(Kind.DISCOUNT)) == ["DISC_STAFF", "PR_APRON", "PR_MUGS"], (
			"rules' discounts aren't staff's"
		)
		with pytest.raises(SourceError, match="discount cursor"):
			source.page(Kind.DISCOUNT, "elsewhere:", None, "")

	def test_pricing_rules(self) -> None:
		mugs = values(Kind.DISCOUNT, "PR_MUGS")
		assert (
			mugs.flag("automatic"),
			mugs.strings("items"),
			mugs.decimal("value"),
			mugs.decimal("min_qty"),
		) == (
			True,
			[f"{GID}/Item/ITEM_MUG"],
			Decimal(10),
			Decimal(2),
		)
		assert mugs.optional_string("valid_from") == "2026-09-01"
		assert held(mapped(Kind.DISCOUNT, "PR_MUGS")) == []
		assert values(Kind.DISCOUNT, "PR_MUGS", "v2").decimal("value") == Decimal(15)
		apron = mapped(Kind.DISCOUNT, "PR_APRON")
		assert apron is not None and apron.values().records("variants")[0].all() == {
			"product": f"{GID}/Item/ITEM_APRON",
			"variant": f"{GID}/ItemVariation/VAR_APRON_NAT",
		}
		assert (apron.values().string("discount"), apron.values().decimal("value")) == ("amount", Decimal(5))
		assert (condition_kinds(apron), held(apron)) == (["schedule"], ["card 405"])
		staff = mapped(Kind.DISCOUNT, "DISC_STAFF")
		assert (condition_kinds(staff), held(staff)) == (["applied_by_staff"], ["an operator decision"])

	def test_rule_shapes(self) -> None:
		rule = nodes(Kind.DISCOUNT)["PR_MUGS"]
		data = rule.required_child("pricing_rule_data").raw()
		sets = rule.required_child("product_sets").raw()
		mug_set = rule.required_child("product_sets").required_child("PS_MUGS")
		set_data = mug_set.required_child("product_set_data").raw()

		def with_set(**changes: JsonValue) -> Desired | None:
			product_set: JsonValue = {**mug_set.raw(), "product_set_data": {**set_data, **changes}}
			return self.MAPPER.map(rule.with_fields(product_sets={**sets, "PS_MUGS": product_set}))

		everything = with_set(product_ids_any=[], all_products=True)
		assert everything is not None and everything.values().string("applies") == "all_items"
		assert condition_kinds(with_set(quantity_exact=3)) == ["item_limit"]
		assert condition_kinds(with_set(quantity_max=4)) == ["item_limit"]
		assert condition_kinds(with_set(product_ids_all=["ITEM_MUG"])) == ["rule_condition"]
		assert condition_kinds(with_set(product_ids_any=["NESTED_SET"])) == ["item_matcher"]
		ruled = self.MAPPER.map(
			rule.with_fields(
				pricing_rule_data={
					**data,
					"exclude_products_id": "PS_MUGS",
					"exclude_strategy": "LEAST_EXPENSIVE",
					"customer_group_ids_any": ["GRP"],
					"valid_from_local_time": "09:00:00",
					"minimum_order_subtotal_money": {"amount": 2500, "currency": "CAD"},
				}
			)
		)
		assert condition_kinds(ruled) == ["item_choice", "schedule", "several_customer_groups"]
		assert ruled is not None and ruled.values().decimal("min_amount") == Decimal(25)
		capped = self.MAPPER.map(
			rule.with_fields(
				discount_object={
					"type": "DISCOUNT",
					"id": "DISC_TEN",
					"discount_data": {
						"discount_type": "FIXED_PERCENTAGE",
						"percentage": "10",
						"maximum_amount_money": {"amount": 1000, "currency": "CAD"},
						"modify_tax_basis": "DO_NOT_MODIFY_TAX_BASIS",
					},
				}
			)
		)
		assert terms(capped, "cap") == {"amount": Decimal(10), "undocumented": [CAP_SCOPE]}
		assert condition_kinds(capped) == ["cap", "rule_condition"]
		assert self.MAPPER.map(rule.with_fields(is_deleted=True)) is None
		assert condition_kinds(self.MAPPER.map(rule.with_fields(product_sets={}))) == ["item_matcher"]
		with pytest.raises(SourceError, match="names no discount"):
			self.MAPPER.map(rule.with_fields(discount_object=None))
		with pytest.raises(SourceError, match="type"):
			self.MAPPER.map(
				rule.with_fields(
					discount_object={"type": "DISCOUNT", "id": "X", "discount_data": {"discount_type": "ODD"}}
				)
			)


def test_the_adapter() -> None:
	assert {mapper.kind for mapper in ADAPTER.mappers(CONFIG)} == set(ADAPTER.kinds())
	assert ADAPTER.filter_hint() is None
