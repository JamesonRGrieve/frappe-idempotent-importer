# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Shopify adapter over the recorded store (tests/fixtures/shopify, ported from
wp-woo-idempotent-importer): its query library, page sizing, client authentication and throttling, reader
and mappers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs

import pytest

from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import (
	CredentialsRejected,
	KindUnavailable,
	RetryLater,
	SourceError,
	Unsupported,
)
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonObject, JsonValue, Node
from idempotent_importer.core.rest import RequestAuthorizer
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.shopify.adapter import ShopifyAdapter, authorizer
from idempotent_importer.sources.shopify.auth import ClientCredentialsAuthorizer, ShopifyTokenAuthorizer
from idempotent_importer.sources.shopify.client import (
	AccessDenied,
	Connection,
	GraphQlClient,
	bucket_shortfall,
)
from idempotent_importer.sources.shopify.page_sizes import PageSizes
from idempotent_importer.sources.shopify.queries import (
	NESTED_PAGE_SIZE,
	PAGE_SIZE,
	QueryLibrary,
	entity_query,
)
from idempotent_importer.sources.shopify.reader import ShopifyReader, search_query
from tests.support.transport import FakeTransport, RecordingSleeper, scripted, shopify

ADAPTER = ShopifyAdapter()
LIBRARY = QueryLibrary.bundled()
ENDPOINT = "https://fixture.myshopify.com/admin/api/2026-10/graphql.json"
TOKEN_URL = "https://fixture.myshopify.com/admin/oauth/access_token"


def client(transport: FakeTransport, sleeper: RecordingSleeper | None = None) -> GraphQlClient:
	return GraphQlClient(
		Connection(ENDPOINT),
		LIBRARY,
		ShopifyTokenAuthorizer("token"),
		transport,
		sleeper or RecordingSleeper(),
	)


def reader(config: ConnectionConfig, state: str = "v1") -> ShopifyReader:
	return ShopifyReader(config, client(shopify(state)))


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
		assert "ShopPolicies" in operations, "hand-written operations are loaded too"

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

	def test_every_page_size_is_a_variable_with_a_default(self) -> None:
		assert LIBRARY.page_sizes("OrderList") == {
			"first": PAGE_SIZE,
			"lineItemsFirst": NESTED_PAGE_SIZE,
			"shippingLinesFirst": NESTED_PAGE_SIZE,
			"eventsFirst": NESTED_PAGE_SIZE,
			"metafieldsFirst": NESTED_PAGE_SIZE,
		}
		assert LIBRARY.page_sizes("OrderNode") == {
			"lineItemsFirst": NESTED_PAGE_SIZE,
			"shippingLinesFirst": NESTED_PAGE_SIZE,
			"eventsFirst": NESTED_PAGE_SIZE,
			"metafieldsFirst": NESTED_PAGE_SIZE,
		}
		assert LIBRARY.page_sizes("OrderLineItemsPage") == {"first": NESTED_PAGE_SIZE}
		assert LIBRARY.page_sizes("ShopPolicies") == {}
		document = LIBRARY.document("OrderList")
		assert "$lineItemsFirst: Int!" in document
		assert "lineItems(first: $lineItemsFirst)" in document
		assert "refunds(first: 250)" in document, "a connection nothing pages through keeps its literal size"
		assert "variants(first: $first, after: $after)" in LIBRARY.document("ProductVariantsPage")

	def test_list_queries_sort_and_search_where_they_can(self) -> None:
		assert "sortKey: UPDATED_AT" in LIBRARY.document("ProductList")
		assert "query: $query" in LIBRARY.document("CommentList")
		assert entity_query(Kind.PRODUCT).is_incremental()
		assert not entity_query(Kind.BLOG).is_incremental()

	def test_unknown_operations(self) -> None:
		with pytest.raises(SourceError):
			LIBRARY.document("Nope")
		with pytest.raises(SourceError):
			LIBRARY.page_sizes("Nope")
		with pytest.raises(SourceError):
			entity_query(Kind.REVIEW)

	def test_search_query(self) -> None:
		since = datetime(2026, 9, 1, 12, tzinfo=UTC)
		assert search_query(since, "") == "updated_at:>='2026-09-01T12:00:00Z'"
		assert (
			search_query(since, "status:active") == "updated_at:>='2026-09-01T12:00:00Z' AND (status:active)"
		)
		assert search_query(None, " ") is None


class TestPageSizes:
	def test_starts_from_the_librarys_defaults(self) -> None:
		sizes = PageSizes(LIBRARY)
		assert sizes.sizes("OrderList") == LIBRARY.page_sizes("OrderList")
		assert sizes.sizes("ShopPolicies") == {}

	def test_a_refusal_shrinks_every_size_in_proportion_and_only_that_operation(self) -> None:
		sizes = PageSizes(LIBRARY)
		sizes.refused("OrderList", 1800, 1000)
		assert sizes.sizes("OrderList") == {
			"first": 25,
			"lineItemsFirst": 125,
			"shippingLinesFirst": 125,
			"eventsFirst": 125,
			"metafieldsFirst": 125,
		}, "scaled by 900 / 1800"
		assert sizes.sizes("ProductList") == LIBRARY.page_sizes("ProductList")

	def test_a_refusal_barely_over_the_limit_still_shrinks_below_the_margin(self) -> None:
		sizes = PageSizes(LIBRARY)
		sizes.refused("CommentList", 1001, 1000)
		assert sizes.sizes("CommentList") == {"first": 44}, "50 x 900 / 1001"

	def test_sizes_never_go_below_one(self) -> None:
		sizes = PageSizes(LIBRARY)
		sizes.refused("ProductList", 1_000_000, 1000)
		assert set(sizes.sizes("ProductList").values()) == {1}
		with pytest.raises(SourceError, match="ProductList: one object costs 1500 points even one at a time"):
			sizes.refused("ProductList", 1500, 1000)

	def test_an_operation_without_page_sizes_cannot_shrink(self) -> None:
		with pytest.raises(SourceError):
			PageSizes(LIBRARY).refused("ShopPolicies", 1200, 1000)

	def test_grows_back_toward_the_defaults_but_never_to_a_refused_size(self) -> None:
		sizes = PageSizes(LIBRARY)
		sizes.refused("CommentList", 1800, 1000)
		assert sizes.sizes("CommentList") == {"first": 25}
		sizes.succeeded("CommentList", 300)
		assert sizes.sizes("CommentList") == {"first": 49}, "capped just under the refused 50"
		sizes.succeeded("CommentList", 300)
		assert sizes.sizes("CommentList") == {"first": 49}, "growing further would ask for the refused size"

	def test_does_not_grow_without_room_or_when_at_the_defaults(self) -> None:
		sizes = PageSizes(LIBRARY)
		sizes.succeeded("CommentList", 10)
		assert sizes.sizes("CommentList") == {"first": 50}, "never above the default"
		sizes.refused("CommentList", 1800, 1000)
		sizes.succeeded("CommentList", 950)
		assert sizes.sizes("CommentList") == {"first": 25}, "no room under the 900 target"

	def test_the_same_responses_give_the_same_sizes(self) -> None:
		def run() -> dict[str, int]:
			sizes = PageSizes(LIBRARY)
			sizes.refused("ProductList", 1941, 1000)
			sizes.succeeded("ProductList", 700)
			sizes.refused("ProductList", 1027, 1000)
			sizes.succeeded("ProductList", 822)
			return sizes.sizes("ProductList")

		assert run() == run()

	def test_a_store_with_a_lower_limit_is_targeted_below_it(self) -> None:
		sizes = PageSizes(LIBRARY)
		sizes.refused("CommentList", 1000, 500)
		assert sizes.sizes("CommentList") == {"first": 22}, "50 x 450 / 1000"
		sizes.succeeded("CommentList", 449)
		assert sizes.sizes("CommentList") == {"first": 22}, "growth aims under the reported limit"


def graphql(body: Mapping[str, object]) -> HttpResponse:
	return HttpResponse(200, json.dumps(body).encode())


def sent(transport: FakeTransport, index: int) -> JsonObject:
	decoded = json.loads(transport.requests[index].body)
	assert isinstance(decoded, dict)
	return decoded


def too_costly(cost: int, *, with_numbers: bool = True) -> HttpResponse:
	extensions: JsonObject = {"code": "MAX_COST_EXCEEDED"}
	if with_numbers:
		extensions |= {"cost": cost, "maxCost": 1000}
	message = f"Query cost is {cost}, which exceeds the single query max cost limit (1000)."
	return graphql({"errors": [{"message": message, "extensions": extensions}]})


class TestClient:
	def test_waits_for_the_cost_bucket_after_a_costly_query(self) -> None:
		sleeper = RecordingSleeper()
		bucket = {
			"cost": {
				"requestedQueryCost": 500,
				"throttleStatus": {"currentlyAvailable": 100, "restoreRate": 100},
			}
		}
		graph = client(scripted(graphql({"data": {"ok": True}, "extensions": bucket})), sleeper)
		graph.query("ProductList")
		graph.query("ProductList")
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
		graph = client(scripted(graphql(throttled), graphql({"data": {"ok": True}})), sleeper)
		assert graph.query("ProductList").flag("ok")
		assert sleeper.waits == [4.0]

	def test_page_sizes_are_sent_with_every_query(self) -> None:
		transport = scripted(graphql({"data": {}}))
		client(transport).query("OrderList", {"after": "c1"})
		assert sent(transport, 0)["variables"] == {"after": "c1", **LIBRARY.page_sizes("OrderList")}

	@pytest.mark.parametrize("with_numbers", [True, False])
	def test_a_query_refused_as_too_costly_is_resent_at_once_with_smaller_pages(
		self, with_numbers: bool
	) -> None:
		sleeper = RecordingSleeper()
		transport = scripted(too_costly(1941, with_numbers=with_numbers), graphql({"data": {"ok": True}}))
		assert client(transport, sleeper).query("ProductList").flag("ok")
		first, second = sent(transport, 0)["variables"], sent(transport, 1)["variables"]
		assert isinstance(first, dict) and isinstance(second, dict)
		assert (first["first"], first["variantsFirst"]) == (50, 250)
		assert (second["first"], second["variantsFirst"]) == (23, 115), "x 900 / 1941"
		assert sleeper.waits == [], "no retry wait, no pause"

	def test_sizes_that_fit_are_kept_for_the_rest_of_the_run(self) -> None:
		cost: JsonObject = {"cost": {"requestedQueryCost": 950}}
		transport = scripted(too_costly(1941), graphql({"data": {}, "extensions": cost}))
		graph = client(transport)
		graph.query("ProductList")
		graph.query("ProductList")
		assert sent(transport, 2)["variables"] == sent(transport, 1)["variables"]

	def test_a_refusal_without_its_cost_is_an_error(self) -> None:
		refusal = graphql(
			{"errors": [{"message": "Too costly", "extensions": {"code": "MAX_COST_EXCEEDED"}}]}
		)
		with pytest.raises(SourceError, match="without saying its cost"):
			client(scripted(refusal)).query("ProductList")

	def test_a_missing_scope_is_access_denied(self) -> None:
		denied = graphql(
			{
				"errors": [
					{
						"message": "Access denied for shopPolicies field.",
						"extensions": {"code": "ACCESS_DENIED"},
					}
				]
			}
		)
		with pytest.raises(AccessDenied, match="ShopPolicies: Access denied"):
			client(scripted(denied)).query("ShopPolicies")
		mixed = graphql(
			{"errors": [{"message": "denied", "extensions": {"code": "ACCESS_DENIED"}}, {"message": "bad"}]}
		)
		with pytest.raises(SourceError) as error:
			client(scripted(mixed)).query("ShopPolicies")
		assert not isinstance(error.value, AccessDenied)

	def test_rejected_tokens_stop_and_errors_fail(self) -> None:
		with pytest.raises(CredentialsRejected):
			client(scripted(HttpResponse(401, b"{}"))).query("ProductList")
		with pytest.raises(SourceError, match="bad field"):
			client(scripted(graphql({"errors": [{"message": "bad field"}]}))).query("ProductList")

	def test_a_struggling_shop_pauses_the_run(self) -> None:
		with pytest.raises(RetryLater):
			client(scripted(HttpResponse(503, b"", {"retry-after": "90"}))).query("ProductList")

	def test_the_token_is_sent_and_never_shown(self) -> None:
		transport = scripted(graphql({"data": {}}))
		token = ShopifyTokenAuthorizer("shpat_secret")
		GraphQlClient(Connection(ENDPOINT), LIBRARY, token, transport, RecordingSleeper()).query(
			"ProductList"
		)
		assert transport.requests[0].headers["X-Shopify-Access-Token"] == "shpat_secret"
		assert "shpat_secret" not in repr(token)

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
			Connection.for_store(store)
		assert Connection.for_store("Fixture.myshopify.com").endpoint == ENDPOINT

	def test_the_token_endpoint_is_on_the_stores_host_and_follows_a_test_endpoint(self) -> None:
		assert Connection(ENDPOINT).token_url() == TOKEN_URL
		fake = Connection("http://shopify-fake:8080/admin/api/2026-10/graphql.json")
		assert fake.token_url() == "http://shopify-fake:8080/admin/oauth/access_token"
		with pytest.raises(SourceError):
			Connection("not a url").token_url()


def granted(token: str) -> HttpResponse:
	return HttpResponse(200, json.dumps({"access_token": token, "scope": "read_products"}).encode())


class TestAuthorizers:
	def credentials(self, transport: FakeTransport) -> ClientCredentialsAuthorizer:
		return ClientCredentialsAuthorizer(TOKEN_URL, "client-id", "client-secret", transport)

	def test_client_credentials_are_exchanged_once_and_renewed_when_shopify_rejects_the_token(self) -> None:
		token_endpoint = scripted(granted("first"), granted("second"))
		credentials = self.credentials(token_endpoint)
		api = scripted(HttpResponse(401, b"{}"), graphql({"data": {"ok": True}}))
		graph = GraphQlClient(Connection(ENDPOINT), LIBRARY, credentials, api, RecordingSleeper())
		assert graph.query("ProductList").flag("ok")
		assert [r.headers["X-Shopify-Access-Token"] for r in api.requests] == ["first", "second"]
		grant = parse_qs(token_endpoint.requests[0].body.decode())
		assert grant == {
			"grant_type": ["client_credentials"],
			"client_id": ["client-id"],
			"client_secret": ["client-secret"],
		}
		assert token_endpoint.requests[0].url == TOKEN_URL
		credentials.headers()
		assert len(token_endpoint.requests) == 2, "a token is reused until Shopify rejects it"

	def test_a_token_rejected_twice_stops_the_run(self) -> None:
		credentials = self.credentials(scripted(granted("t")))
		api = scripted(HttpResponse(401, b"{}"))
		graph = GraphQlClient(Connection(ENDPOINT), LIBRARY, credentials, api, RecordingSleeper())
		with pytest.raises(CredentialsRejected):
			graph.query("ProductList")
		assert len(api.requests) == 2

	def test_a_refused_grant_stops_the_run_with_shopifys_reason_but_no_secret(self) -> None:
		refused = HttpResponse(400, b'{"error":"invalid_client","error_description":"client-secret is bad"}')
		with pytest.raises(CredentialsRejected) as error:
			self.credentials(scripted(refused)).headers()
		assert "invalid_client" in str(error.value)
		assert "client-secret" not in str(error.value)
		odd = HttpResponse(401, b'{"error":"Not A Code <script>"}')
		with pytest.raises(CredentialsRejected) as odd_error:
			self.credentials(scripted(odd)).headers()
		assert "script" not in str(odd_error.value)

	def test_a_busy_token_endpoint_pauses_the_run(self) -> None:
		with pytest.raises(RetryLater):
			self.credentials(scripted(HttpResponse(503, b"", {"retry-after": "120"}))).headers()

	def test_a_grant_without_a_token_is_an_error(self) -> None:
		with pytest.raises(SourceError, match="no access token"):
			self.credentials(scripted(HttpResponse(200, b'{"scope":"x"}'))).headers()
		with pytest.raises(SourceError, match="no access token"):
			self.credentials(scripted(HttpResponse(200, b"not json"))).headers()

	def test_missing_credentials_are_a_configuration_error(self) -> None:
		with pytest.raises(SourceError, match="client ID and client secret"):
			ClientCredentialsAuthorizer(TOKEN_URL, "", "secret", scripted())
		with pytest.raises(SourceError, match="access token"):
			ShopifyTokenAuthorizer("")

	def test_a_legacy_token_has_nothing_to_renew(self) -> None:
		assert not ShopifyTokenAuthorizer("token").renew()

	def test_secrets_are_never_rendered(self) -> None:
		assert "client-secret" not in repr(self.credentials(scripted()))

	def test_the_connection_chooses_its_authorizer(self, shopify_config: ConnectionConfig) -> None:
		connection = Connection(ENDPOINT)
		chosen: RequestAuthorizer = authorizer(shopify_config, connection, scripted())
		assert isinstance(chosen, ClientCredentialsAuthorizer)
		legacy = replace(shopify_config, auth_method="Access Token", api_user="")
		assert isinstance(authorizer(legacy, connection, scripted()), ShopifyTokenAuthorizer)
		with pytest.raises(SourceError, match="Client Credentials"):
			authorizer(replace(shopify_config, auth_method=""), connection, scripted())


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
		page = ShopifyReader(shopify_config, client(transport)).page(Kind.ORDER, None, None, "")
		assert [line.id() for line in page.nodes[0].children("lineItems")] == [
			"gid://shopify/shop-s/LineItem/1",
			"gid://shopify/shop-s/LineItem/2",
		]
		second = sent(transport, 1)
		assert (second["operationName"], second["variables"]) == (
			"OrderLineItemsPage",
			{"id": "gid://shopify/Order/9", "after": "c1", "first": NESTED_PAGE_SIZE},
		)

	def test_a_connection_it_cannot_page_is_an_error(self, shopify_config: ConnectionConfig) -> None:
		truncated = {
			"id": "gid://shopify/Product/9",
			"options": {"pageInfo": {"hasNextPage": True}, "nodes": []},
		}
		listing = {"products": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [truncated]}}
		graph = client(scripted(graphql({"data": listing})))
		with pytest.raises(SourceError, match="more entries than one query returns"):
			ShopifyReader(shopify_config, graph).page(Kind.PRODUCT, None, None, "")

	def test_single_objects(self, shopify_config: ConnectionConfig) -> None:
		transport: FakeTransport = scripted(graphql({"data": {"node": None}}))
		assert (
			ShopifyReader(shopify_config, client(transport)).node(
				Kind.PRODUCT, "gid://shopify/shop-s/Product/1"
			)
			is None
		)
		assert sent(transport, 0)["variables"] == {
			"id": "gid://shopify/Product/1",
			**LIBRARY.page_sizes("ProductNode"),
		}

	def test_policies_are_read_on_their_own(self, shopify_config: ConnectionConfig) -> None:
		policies = reader(shopify_config).page(Kind.POLICY, None, None, "")
		assert [p.id() for p in policies.nodes] == [
			"gid://shopify/shop-s/ShopPolicy/11",
			"gid://shopify/shop-s/ShopPolicy/12",
		]
		assert not policies.has_next_page
		assert not reader(shopify_config).is_incremental(Kind.POLICY)
		one = reader(shopify_config).node(Kind.POLICY, "gid://shopify/shop-s/ShopPolicy/12")
		assert one is not None and one.string("type") == "PRIVACY_POLICY"
		assert reader(shopify_config).node(Kind.POLICY, "gid://shopify/shop-s/ShopPolicy/99") is None

	def test_without_the_policies_scope_the_kind_is_unavailable(
		self, shopify_config: ConnectionConfig
	) -> None:
		denied = graphql(
			{
				"errors": [
					{
						"message": "Access denied for shopPolicies field.",
						"extensions": {"code": "ACCESS_DENIED"},
					}
				]
			}
		)
		with pytest.raises(KindUnavailable, match="read_legal_policies"):
			ShopifyReader(shopify_config, client(scripted(denied))).page(Kind.POLICY, None, None, "")


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

	def test_a_fully_refunded_order_gets_exactly_shopifys_refunds(
		self, shopify_config: ConnectionConfig
	) -> None:
		"""The live store's fully refunded order (anonymised): one refund of the whole total, shipping and
		the discounted line's tax included, and nothing derived from its REFUNDED status."""
		order = values(shopify_config, Kind.ORDER, "804")
		assert order.flag("submitted")
		lines = order.records("lines")
		goods = sum((line.decimal("qty") * line.decimal("rate") for line in lines), Decimal(0))
		charges = sum((c.decimal("amount") for c in order.records("charges")), Decimal(0))
		assert goods - order.decimal("discount") + charges == order.decimal("total") == Decimal("118.59")
		refunds = order.records("refunds")
		assert [refund.decimal("amount") for refund in refunds] == [Decimal("118.59")]
		credit = refunds[0]
		returned = sum(
			(line.decimal("qty") * line.decimal("rate") for line in credit.records("lines")), Decimal(0)
		)
		refunded = sum((c.decimal("amount") for c in credit.records("charges")), Decimal(0))
		assert returned + refunded == Decimal("-118.59")
		assert [line.string("name") for line in credit.records("lines")] == [
			"Silk Scarf - 10",
			"Refund adjustment",
		]

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

	def test_policies_are_pages_at_shopifys_paths(self, shopify_config: ConnectionConfig) -> None:
		refund = mapped(shopify_config, Kind.POLICY, "11")
		assert refund is not None
		assert refund.values().all() == {
			"title": "Refund policy",
			"slug": "policies/refund-policy",
			"parent": None,
			"published": True,
			"html": "<p>Returns within 30 days.</p>",
		}
		assert refund.legacy_paths == ("/policies/refund-policy",)


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
	fake = replace(
		shopify_config, endpoint_override="http://shopify-fake:8080/admin/api/2026-10/graphql.json"
	)
	assert isinstance(ADAPTER.reader(fake, scripted(), RecordingSleeper()), ShopifyReader)
