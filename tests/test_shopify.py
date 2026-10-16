# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Shopify adapter over the recorded store (tests/fixtures/shopify, ported from
wp-woo-idempotent-importer): its query library, client throttling, reader and mappers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonObject, JsonValue, Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.shopify.adapter import ShopifyAdapter
from idempotent_importer.sources.shopify.client import Connection, GraphQlClient, bucket_shortfall
from idempotent_importer.sources.shopify.queries import NESTED_PAGE_SIZE, QueryLibrary, entity_query
from idempotent_importer.sources.shopify.reader import ShopifyReader, search_query
from tests.support.transport import FakeTransport, RecordingSleeper, scripted, shopify

ADAPTER = ShopifyAdapter()
LIBRARY = QueryLibrary.bundled()
ENDPOINT = "https://fixture.myshopify.com/admin/api/2026-10/graphql.json"


def reader(config: ConnectionConfig, state: str = "v1") -> ShopifyReader:
	client = GraphQlClient(Connection(ENDPOINT, "token"), LIBRARY, shopify(state), RecordingSleeper())
	return ShopifyReader(config, client)


def nodes(config: ConnectionConfig, kind: Kind, state: str = "v1") -> dict[str, Node]:
	return {
		node.id().rsplit("/", 1)[-1]: node for node in reader(config, state).page(kind, None, None, "").nodes
	}


def mapped(config: ConnectionConfig, kind: Kind, source_id: str, state: str = "v1") -> Desired | None:
	mapper = {m.kind: m for m in ADAPTER.mappers(config)}[kind]
	return mapper.map(nodes(config, kind, state)[source_id])


def values(config: ConnectionConfig, kind: Kind, source_id: str, state: str = "v1") -> Values:
	desired = mapped(config, kind, source_id, state)
	assert desired is not None
	return desired.values()


class TestQueryLibrary:
	def test_every_kind_has_list_node_and_nested_page_operations(self) -> None:
		operations = set(LIBRARY.operations())
		assert {"ProductList", "ProductNode", "ProductVariantsPage", "OrderLineItemsPage"} <= operations

	def test_documents_carry_every_fragment_they_spread(self) -> None:
		document = LIBRARY.document("OrderList")
		for fragment in (
			"OrderFields",
			"LineItemConnectionFields",
			"LineItemFields",
			"MoneyBagFields",
			"PageInfoFields",
		):
			assert f"fragment {fragment} on" in document
		assert document.count("fragment MoneyBagFields on") == 1

	def test_list_queries_sort_and_search_where_they_can(self) -> None:
		assert "sortKey: UPDATED_AT" in LIBRARY.document("ProductList")
		assert "query: $query" in LIBRARY.document("CommentList")
		assert entity_query(Kind.PRODUCT).is_incremental()
		assert not entity_query(Kind.BLOG).is_incremental()

	def test_unknown_operations(self) -> None:
		with pytest.raises(SourceError):
			LIBRARY.document("Nope")
		with pytest.raises(SourceError):
			entity_query(Kind.REVIEW)

	def test_search_query(self) -> None:
		since = datetime(2026, 9, 1, 12, tzinfo=UTC)
		assert search_query(since, "") == "updated_at:>='2026-09-01T12:00:00Z'"
		assert (
			search_query(since, "status:active") == "updated_at:>='2026-09-01T12:00:00Z' AND (status:active)"
		)
		assert search_query(None, " ") is None


def graphql(body: Mapping[str, object]) -> HttpResponse:
	return HttpResponse(200, json.dumps(body).encode())


class TestClient:
	def test_waits_for_the_cost_bucket_after_a_costly_query(self) -> None:
		sleeper = RecordingSleeper()
		bucket = {
			"cost": {
				"requestedQueryCost": 500,
				"throttleStatus": {"currentlyAvailable": 100, "restoreRate": 100},
			}
		}
		transport = scripted(graphql({"data": {"ok": True}, "extensions": bucket}))
		client = GraphQlClient(Connection(ENDPOINT, "token"), LIBRARY, transport, sleeper)
		client.query("ProductList", {"first": 1})
		client.query("ProductList", {"first": 1})
		assert sleeper.waits == [4.0]

	def test_a_throttled_query_is_retried_once_the_bucket_refills(self) -> None:
		sleeper = RecordingSleeper()
		throttled = {
			"errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}],
			"extensions": {
				"cost": {
					"requestedQueryCost": 300,
					"throttleStatus": {"currentlyAvailable": 100, "restoreRate": 50},
				}
			},
		}
		transport = scripted(graphql(throttled), graphql({"data": {"ok": True}}))
		client = GraphQlClient(Connection(ENDPOINT, "token"), LIBRARY, transport, sleeper)
		assert client.query("ProductList").flag("ok")
		assert sleeper.waits == [4.0]

	def test_rejected_tokens_stop_and_errors_fail(self) -> None:
		client = GraphQlClient(
			Connection(ENDPOINT, "token"), LIBRARY, scripted(HttpResponse(401, b"{}")), RecordingSleeper()
		)
		with pytest.raises(CredentialsRejected):
			client.query("ProductList")
		failing = GraphQlClient(
			Connection(ENDPOINT, "token"),
			LIBRARY,
			scripted(graphql({"errors": [{"message": "bad field"}]})),
			RecordingSleeper(),
		)
		with pytest.raises(SourceError, match="bad field"):
			failing.query("ProductList")

	def test_a_struggling_shop_pauses_the_run(self) -> None:
		client = GraphQlClient(
			Connection(ENDPOINT, "token"),
			LIBRARY,
			scripted(HttpResponse(503, b"", {"retry-after": "90"})),
			RecordingSleeper(),
		)
		with pytest.raises(RetryLater):
			client.query("ProductList")

	def test_the_token_is_sent_and_never_shown(self) -> None:
		transport = scripted(graphql({"data": {}}))
		connection = Connection(ENDPOINT, "shpat_secret")
		GraphQlClient(connection, LIBRARY, transport, RecordingSleeper()).query("ProductList")
		assert transport.requests[0].headers["X-Shopify-Access-Token"] == "shpat_secret"
		assert "shpat_secret" not in repr(connection)

	def test_bucket_shortfall(self) -> None:
		assert bucket_shortfall({}) is None
		status: JsonObject = {"currentlyAvailable": 1000, "restoreRate": 50}
		assert (
			bucket_shortfall({"extensions": {"cost": {"requestedQueryCost": 10, "throttleStatus": status}}})
			== 0
		)

	@pytest.mark.parametrize("store", ["shop.example.com", "https://x.myshopify.com.evil", ""])
	def test_the_store_must_be_a_myshopify_domain(self, store: str) -> None:
		with pytest.raises(SourceError, match="myshopify"):
			Connection.for_store(store, "token")
		assert Connection.for_store("Fixture.myshopify.com", "t").endpoint == ENDPOINT


class TestReader:
	def test_identities_are_scoped_to_the_connection(self, shopify_config: ConnectionConfig) -> None:
		scarf = nodes(shopify_config, Kind.PRODUCT)["301"]
		assert scarf.id() == "gid://shopify/shop-s/Product/301"
		assert scarf.children("collections")[0].id().startswith("gid://shopify/shop-s/Collection/")
		other = nodes(replace(shopify_config, connection="shop-t"), Kind.PRODUCT)["301"]
		assert other.id() == "gid://shopify/shop-t/Product/301"

	def test_nested_connections_are_followed_to_the_end(self, shopify_config: ConnectionConfig) -> None:
		first = {
			"pageInfo": {"hasNextPage": True, "endCursor": "c1"},
			"nodes": [{"id": "gid://shopify/LineItem/1"}],
		}
		more = {
			"pageInfo": {"hasNextPage": False, "endCursor": None},
			"nodes": [{"id": "gid://shopify/LineItem/2"}],
		}
		order = {"id": "gid://shopify/Order/9", "lineItems": first}
		listing = {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [order]}}
		transport = scripted(graphql({"data": listing}), graphql({"data": {"node": {"lineItems": more}}}))
		client = GraphQlClient(Connection(ENDPOINT, "token"), LIBRARY, transport, RecordingSleeper())
		page = ShopifyReader(shopify_config, client).page(Kind.ORDER, None, None, "")
		assert [line.id() for line in page.nodes[0].children("lineItems")] == [
			"gid://shopify/shop-s/LineItem/1",
			"gid://shopify/shop-s/LineItem/2",
		]
		second = json.loads(transport.requests[1].body)
		assert (second["operationName"], second["variables"]) == (
			"OrderLineItemsPage",
			{"id": "gid://shopify/Order/9", "after": "c1"},
		)

	def test_a_connection_it_cannot_page_is_an_error(self, shopify_config: ConnectionConfig) -> None:
		truncated = {
			"id": "gid://shopify/Product/9",
			"options": {"pageInfo": {"hasNextPage": True}, "nodes": []},
		}
		listing = {"products": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [truncated]}}
		client = GraphQlClient(
			Connection(ENDPOINT, "token"), LIBRARY, scripted(graphql({"data": listing})), RecordingSleeper()
		)
		with pytest.raises(SourceError, match=f"more than {NESTED_PAGE_SIZE}"):
			ShopifyReader(shopify_config, client).page(Kind.PRODUCT, None, None, "")

	def test_single_objects(self, shopify_config: ConnectionConfig) -> None:
		transport: FakeTransport = scripted(graphql({"data": {"node": None}}))
		client = GraphQlClient(Connection(ENDPOINT, "token"), LIBRARY, transport, RecordingSleeper())
		assert (
			ShopifyReader(shopify_config, client).node(Kind.PRODUCT, "gid://shopify/shop-s/Product/1") is None
		)
		assert json.loads(transport.requests[0].body)["variables"] == {"id": "gid://shopify/Product/1"}


class TestMappers:
	def test_a_one_variant_product_is_one_item(self, shopify_config: ConnectionConfig) -> None:
		scarf = values(shopify_config, Kind.PRODUCT, "301")
		assert scarf.string("type") == "simple"
		assert scarf.optional_string("sku") == "SCARF-1"
		assert scarf.decimal("price") == Decimal("80.00"), "the compare-at price is the regular price"
		assert scarf.required_record("sale").decimal("price") == Decimal("60.00")
		assert scarf.decimal("stock_qty") == Decimal(7)
		assert scarf.required_record("weight").all() == {"value": Decimal("120.0"), "unit": "g"}

	def test_a_product_with_options(self, shopify_config: ConnectionConfig) -> None:
		bra = mapped(shopify_config, Kind.PRODUCT, "302")
		assert bra is not None
		fields = bra.values()
		assert [a.string("name") for a in fields.records("attributes")] == ["Size", "Color"]
		assert len(fields.records("variants")) == 2
		assert any("Video" in warning for warning in bra.warnings)

	def test_gift_cards_have_no_home(self, shopify_config: ConnectionConfig) -> None:
		with pytest.raises(Unsupported, match="gift card"):
			mapped(shopify_config, Kind.PRODUCT, "303")

	def test_an_order_with_a_refund_balances(self, shopify_config: ConnectionConfig) -> None:
		order = values(shopify_config, Kind.ORDER, "801")
		lines = order.records("lines")
		goods = sum((line.decimal("qty") * line.decimal("rate") for line in lines), Decimal(0))
		charges = sum((c.decimal("amount") for c in order.records("charges")), Decimal(0))
		assert goods - order.decimal("discount") + charges == order.decimal("total") == Decimal("137.76")
		refund = order.records("refunds")[0]
		returned = sum(
			(line.decimal("qty") * line.decimal("rate") for line in refund.records("lines")), Decimal(0)
		)
		refunded = sum((c.decimal("amount") for c in refund.records("charges")), Decimal(0))
		assert returned + refunded == -refund.decimal("amount") == Decimal("-30.0")
		assert order.records("notes")[0].string("text") == "Called customer about size."

	def test_a_voided_order_has_no_invoice(self, shopify_config: ConnectionConfig) -> None:
		assert mapped(shopify_config, Kind.ORDER, "802") is None

	def test_discounts_erpnext_cannot_enforce(self, shopify_config: ConnectionConfig) -> None:
		for discount, reason in (("701", "once-per-customer"), ("702", "FreeShipping"), ("703", "Bxgy")):
			with pytest.raises(Unsupported, match=reason):
				mapped(shopify_config, Kind.DISCOUNT, discount)

	def test_customers(self, shopify_config: ConnectionConfig) -> None:
		avery = values(shopify_config, Kind.CUSTOMER, "401")
		assert avery.string("name") == "Avery Lin"
		assert [a.string("address_type") for a in avery.records("addresses")] == ["Billing", "Shipping"]

	def test_content(self, shopify_config: ConnectionConfig) -> None:
		assert (
			values(shopify_config, Kind.ARTICLE, "1201").string("category")
			== "gid://shopify/shop-s/Blog/1101"
		)
		assert values(shopify_config, Kind.COMMENT, "1301").string("author_name") == "Rae"
		assert values(shopify_config, Kind.PAGE, "1001").flag("published")
		assert values(shopify_config, Kind.URL_REDIRECT, "1501").all() == {
			"source": "/old-scarf",
			"target": "/products/silk-scarf",
			"status": 301,
		}


def basic_discount(**discount: JsonValue) -> Node:
	base: JsonObject = {
		"__typename": "DiscountCodeBasic",
		"title": "Ten",
		"status": "ACTIVE",
		"startsAt": "2026-09-01T00:00:00Z",
		"endsAt": None,
		"usageLimit": None,
		"appliesOncePerCustomer": False,
		"codes": {"nodes": [{"code": "TEN"}]},
		"context": {"__typename": "DiscountBuyerSelectionAll"},
		"minimumRequirement": None,
		"customerGets": {
			"value": {"__typename": "DiscountPercentage", "percentage": 0.1},
			"items": {"__typename": "AllDiscountItems"},
		},
	}
	return Node({"id": "gid://shopify/shop-s/DiscountCodeNode/9", "discount": {**base, **discount}})


class TestDiscounts:
	MAPPER = {m.kind: m for m in ADAPTER.mappers(ConnectionConfig("shopify", "shop-s", "x.myshopify.com"))}[
		Kind.DISCOUNT
	]

	def test_a_percentage_off_everything(self) -> None:
		desired = self.MAPPER.map(basic_discount())
		assert desired is not None
		ten = desired.values()
		assert (ten.string("discount"), ten.string("applies"), ten.decimal("value")) == (
			"percentage",
			"transaction",
			Decimal("10.0"),
		)
		assert (ten.strings("codes"), ten.optional_string("valid_from")) == (["TEN"], "2026-09-01")

	def test_an_amount_off_each_item_of_some_products(self) -> None:
		gets: JsonObject = {
			"value": {"__typename": "DiscountAmount", "amount": {"amount": "5.0"}, "appliesOnEachItem": True},
			"items": {
				"__typename": "DiscountProducts",
				"products": {"nodes": [{"id": "gid://shopify/shop-s/Product/1"}]},
				"productVariants": {"nodes": []},
			},
		}
		desired = self.MAPPER.map(basic_discount(customerGets=gets))
		assert desired is not None
		assert desired.values().strings("items") == ["gid://shopify/shop-s/Product/1"]

	@pytest.mark.parametrize(
		("change", "reason"),
		[
			({"status": "EXPIRED"}, None),
			({"context": {"__typename": "DiscountCustomers"}}, "selected customers"),
			({"minimumRequirement": {"__typename": "DiscountMinimumQuantity"}}, "minimum quantity"),
			(
				{
					"customerGets": {
						"value": {
							"__typename": "DiscountAmount",
							"amount": {"amount": "5"},
							"appliesOnEachItem": False,
						},
						"items": {"__typename": "AllDiscountItems"},
					}
				},
				"spread across the order",
			),
		],
	)
	def test_what_coupons_cannot_hold(self, change: JsonObject, reason: str | None) -> None:
		if reason is None:
			assert self.MAPPER.map(basic_discount(**change)) is None
			return
		with pytest.raises(Unsupported, match=reason):
			self.MAPPER.map(basic_discount(**change))


class TestOrderStates:
	def order(self, shopify_config: ConnectionConfig, **change: JsonValue) -> Node:
		return nodes(shopify_config, Kind.ORDER)["801"].with_fields(**change)

	def mapper(self, shopify_config: ConnectionConfig) -> Mapper:
		return {m.kind: m for m in ADAPTER.mappers(shopify_config)}[Kind.ORDER]

	def test_awaiting_payment_is_a_draft(self, shopify_config: ConnectionConfig) -> None:
		desired = self.mapper(shopify_config).map(
			self.order(shopify_config, displayFinancialStatus="PENDING")
		)
		assert desired is not None
		assert not desired.values().flag("submitted")
		assert desired.values().records("refunds") == []

	def test_tax_inclusive_prices_and_duties_are_unsupported(self, shopify_config: ConnectionConfig) -> None:
		with pytest.raises(Unsupported, match="tax-inclusive"):
			self.mapper(shopify_config).map(self.order(shopify_config, taxesIncluded=True))
		duties: JsonObject = {"shopMoney": {"amount": "3.0", "currencyCode": "CAD"}}
		with pytest.raises(Unsupported, match="duties"):
			self.mapper(shopify_config).map(self.order(shopify_config, originalTotalDutiesSet=duties))

	def test_tips_are_a_fee(self, shopify_config: ConnectionConfig) -> None:
		tip: JsonObject = {"shopMoney": {"amount": "2.0", "currencyCode": "CAD"}}
		desired = self.mapper(shopify_config).map(self.order(shopify_config, totalTipReceivedSet=tip))
		assert desired is not None
		assert desired.values().records("charges")[-1].all() == {
			"kind": "fee",
			"description": "Tip",
			"amount": Decimal("2.0"),
		}


def test_the_adapter(shopify_config: ConnectionConfig) -> None:
	assert {mapper.kind for mapper in ADAPTER.mappers(shopify_config)} == set(ADAPTER.kinds())
	assert ADAPTER.filter_hint()
	assert isinstance(ADAPTER.reader(shopify_config, scripted(), RecordingSleeper()), ShopifyReader)
