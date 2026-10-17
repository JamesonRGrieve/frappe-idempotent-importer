# SPDX-License-Identifier: AGPL-3.0-or-later
"""The static Shopify query-cost estimate (tools/query_cost.py) and its live calibration."""

from __future__ import annotations

from graphql import build_schema

from tools.query_cost import QueryCost, recorded_costs, recorded_multipliers, size_multiplier

SCHEMA = build_schema(
	"""
	type Query {
	  products(first: Int, after: String): ProductConnection!
	  node(id: ID!): Node
	  shop: Shop!
	}
	interface Node { id: ID! }
	type Shop { name: String! shopAddress: Address! billingAddress: Address }
	type Address { city: String }
	type PageInfo { hasNextPage: Boolean! }
	type MoneyV2 { amount: String! }
	type ProductConnection { pageInfo: PageInfo! nodes: [Product!]! }
	type Product implements Node {
	  id: ID!
	  title: String!
	  price: MoneyV2!
	  options: [Option!]!
	  variants(first: Int): VariantConnection!
	}
	type Option { name: String! }
	type VariantConnection { pageInfo: PageInfo! nodes: [Variant!]! }
	type Variant implements Node { id: ID! product: Product! }
	"""
)


def test_the_size_multiplier_matches_every_one_measured_on_a_live_store() -> None:
	measured = recorded_multipliers()
	assert measured
	assert {size: size_multiplier(size) for size in measured} == measured


def test_objects_cost_one_and_scalars_and_zero_cost_types_nothing() -> None:
	cost = QueryCost(SCHEMA)
	assert cost.of("{ shop { name shopAddress { city } } }", {}) == 1, "Shop.shopAddress is free"
	assert cost.of("{ shop { name billingAddress { city } } }", {}) == 2


def test_connections_are_sized_by_their_page_size_and_nested_connections_multiply() -> None:
	cost = QueryCost(SCHEMA)
	query = (
		"query P($first: Int!, $variantsFirst: Int!) { products(first: $first) { pageInfo { hasNextPage } "
		"nodes { title price { amount } options { name } variants(first: $variantsFirst) { nodes { id } } } } }"
	)
	# products: 2 + m(50)=7 x (nodes 1 + options 1 + variants (2 + m(250)=11 x nodes 1)) = 2 + 7 x 15
	assert cost.of(query, {"first": 50, "variantsFirst": 250}) == 107
	assert cost.of(query, {"first": 1, "variantsFirst": 1}) == 7


def test_interfaces_cost_their_most_costly_possible_selection() -> None:
	query = (
		'{ node(id: "x") { __typename ... on Product { options { name } } '
		"... on Variant { product { options { name } } } } }"
	)
	assert QueryCost(SCHEMA).of(query, {}) == 3, "node 1 + Variant.product 1 + options 1"


def test_the_calibration_covers_the_defaults_shopify_refused() -> None:
	costs = recorded_costs()
	assert costs["ProductList"].cost == 1941
	assert costs["OrderList"].cost == 1080
	assert costs["DeliveryProfileList"].cost == 3957
