# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shopify's requested cost of a query, computed statically, and the live calibration it is checked
against. Shopify documents the rules only in outline (shopify.dev/docs/apps/build/apis/graphql-admin/
rate-limits: scalars and enums 0, objects 1, interfaces and unions the maximum of their possible
selections, connections sized by ``first``/``last``, plus manual costs on some fields); the exact figures
were calibrated against a live store with Shopify-GraphQL-Cost-Debug by wp-woo-idempotent-importer's
tools/calibrate-query-cost.php and copied, with its rules, from its tools/QueryCost.php:

- a connection costs 2 + m(first) x the cost of its selection, where m(n) = max(1, floor(2 ln n));
- any other object-returning field (lists included) costs 1 plus its selection, except the zero-cost types
  and fields below.

``tests/fixtures/shopify-query-cost.json`` holds each read operation's recorded cost at its default page
sizes, and the connection multiplier m(n) measured for a range of sizes."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphql import (
	FieldNode,
	FragmentDefinitionNode,
	FragmentSpreadNode,
	GraphQLInterfaceType,
	GraphQLObjectType,
	GraphQLSchema,
	GraphQLUnionType,
	InlineFragmentNode,
	IntValueNode,
	OperationDefinitionNode,
	OperationType,
	SelectionSetNode,
	VariableNode,
	get_named_type,
	is_abstract_type,
	is_leaf_type,
	parse,
)

CALIBRATION = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "shopify-query-cost.json"
# Object types Shopify charges nothing for.
ZERO_COST_TYPES = frozenset({"PageInfo", "MoneyBag", "MoneyV2", "Weight"})
# Object-returning fields Shopify charges nothing for ("Type.field").
ZERO_COST_FIELDS = frozenset(
	{
		"Collection.ruleSet",
		"CollectionRule.conditionObject",
		"CollectionRuleSet.rules",
		"Customer.defaultAddress",
		"Customer.emailMarketingConsent",
		"Customer.smsMarketingConsent",
		"InventoryItem.measurement",
		"Shop.shopAddress",
	}
)
CONNECTION_SUFFIX = "Connection"
CONNECTION_COST = 2
OBJECT_COST = 1
SIZE_ARGUMENTS = frozenset({"first", "last"})
TYPENAME = "__typename"


def size_multiplier(first: int) -> int:
	"""Shopify's multiplier for a connection page of ``first`` objects."""
	return max(1, math.floor(2 * math.log(max(1, first))))


class QueryCost:
	def __init__(self, schema: GraphQLSchema) -> None:
		self._schema = schema
		self._fragments: dict[str, FragmentDefinitionNode] = {}
		self._variables: dict[str, Any] = {}

	def of(self, document: str, variables: dict[str, Any]) -> int:
		"""The requested cost of the document's (first) operation with these variables."""
		parsed = parse(document)
		self._fragments = {
			definition.name.value: definition
			for definition in parsed.definitions
			if isinstance(definition, FragmentDefinitionNode)
		}
		self._variables = variables
		operation = next(
			(d for d in parsed.definitions if isinstance(d, OperationDefinitionNode)),
			None,
		)
		if operation is None:
			raise ValueError("The document has no operation.")
		root = (
			self._schema.query_type
			if operation.operation is OperationType.QUERY
			else self._schema.mutation_type
		)
		if root is None:
			raise ValueError("The schema has no root type for the operation.")
		return self._selections(operation.selection_set, root)

	def _selections(self, selections: SelectionSetNode, parent: Any) -> int:
		if is_abstract_type(parent):
			costs = [self._on(selections, kind) for kind in self._schema.get_possible_types(parent)]
			return max(costs, default=0)
		if not isinstance(parent, GraphQLObjectType):
			return 0
		return self._on(selections, parent)

	def _on(self, selections: SelectionSetNode, kind: GraphQLObjectType) -> int:
		cost = 0
		for selection in selections.selections:
			if isinstance(selection, FieldNode):
				if selection.name.value != TYPENAME:
					cost += self._field(kind, selection)
			elif isinstance(selection, InlineFragmentNode):
				condition = selection.type_condition.name.value if selection.type_condition else kind.name
				if self._applies(condition, kind):
					cost += self._on(selection.selection_set, kind)
			elif isinstance(selection, FragmentSpreadNode):
				fragment = self._fragments.get(selection.name.value)
				if fragment is None:
					raise ValueError(f"Unknown fragment {selection.name.value}")
				if self._applies(fragment.type_condition.name.value, kind):
					cost += self._on(fragment.selection_set, kind)
		return cost

	def _field(self, parent: GraphQLObjectType, node: FieldNode) -> int:
		definition = parent.fields[node.name.value]
		named = get_named_type(definition.type)
		if is_leaf_type(named):
			return 0
		children = 0 if node.selection_set is None else self._selections(node.selection_set, named)
		size = self._size(node)
		if size is not None and named.name.endswith(CONNECTION_SUFFIX):
			return CONNECTION_COST + size_multiplier(size) * children
		free = named.name in ZERO_COST_TYPES or f"{parent.name}.{node.name.value}" in ZERO_COST_FIELDS
		return (0 if free else OBJECT_COST) + children

	def _size(self, node: FieldNode) -> int | None:
		"""The field's ``first``/``last`` page size (a literal or a variable), or None when it takes none."""
		for argument in node.arguments:
			if argument.name.value not in SIZE_ARGUMENTS:
				continue
			value = argument.value
			if isinstance(value, IntValueNode):
				return int(value.value)
			if isinstance(value, VariableNode):
				size = self._variables.get(value.name.value)
				if not isinstance(size, int):
					raise ValueError(f"No page size for ${value.name.value}")
				return size
		return None

	def _applies(self, condition: str, kind: GraphQLObjectType) -> bool:
		if condition == kind.name:
			return True
		condition_type = self._schema.get_type(condition)
		return isinstance(
			condition_type, GraphQLInterfaceType | GraphQLUnionType
		) and self._schema.is_sub_type(condition_type, kind)


@dataclass(frozen=True)
class RecordedCost:
	variables: dict[str, Any]
	cost: int


def calibration() -> dict[str, Any]:
	document = json.loads(CALIBRATION.read_text())
	if not isinstance(document, dict):
		raise TypeError("The query-cost calibration is not an object.")
	return document


def recorded_multipliers() -> dict[int, int]:
	"""Page size → the connection multiplier Shopify charged for it."""
	recorded = calibration().get("connectionMultipliers")
	if not isinstance(recorded, dict):
		raise TypeError("The calibration has no connectionMultipliers.")
	return {int(size): int(multiplier) for size, multiplier in recorded.items()}


def recorded_costs() -> dict[str, RecordedCost]:
	"""Operation → its variables and the requested cost Shopify reported for them."""
	recorded = calibration().get("operations")
	if not isinstance(recorded, dict):
		raise TypeError("The calibration has no operations.")
	return {
		operation: RecordedCost(dict(call.get("variables") or {}), int(call["requestedQueryCost"]))
		for operation, call in recorded.items()
	}
