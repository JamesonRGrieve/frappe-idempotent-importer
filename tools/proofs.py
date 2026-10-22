# SPDX-License-Identifier: AGPL-3.0-or-later
"""Proves the adapters' queries and test data against the real APIs' published descriptions, without a
live store:

1. every Shopify GraphQL operation the adapter sends validates against Shopify's Admin schema, and every
   recorded Shopify response executes back through its operation unchanged (so each fixture has exactly
   the shape — field names, enums, non-nulls — Shopify returns for it);
2. every recorded REST response validates against its source's OpenAPI document (WooCommerce's is
   transcribed from its official documentation, tests/schemas/woocommerce.json);
3. every Shopify operation's static cost estimate (tools/query_cost.py) matches the cost recorded from a
   live store, and every operation fits under Shopify's single-query limit at page size 1;
4. every Wave GraphQL operation validates against Wave's schema (tests/schemas/wave.json), and every
   recorded Wave answer executes back through its operation unchanged.

Each check returns its failures (empty when everything holds)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from graphql import GraphQLSchema, graphql_sync, parse, validate
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from idempotent_importer.sources.shopify.page_sizes import MAX_QUERY_COST, SMALLEST_SIZE, TARGET_SHARE
from idempotent_importer.sources.shopify.queries import QueryLibrary
from idempotent_importer.sources.wave import operations as wave_operations
from tests.support.fixtures import graphql_fixtures, rest_fixtures, states
from tools.openapi import OpenApiValidator
from tools.query_cost import QueryCost, recorded_costs
from tools.schemas import openapi
from tools.specs import DtdDocument, for_source

OPENAPI_ID = "urn:openapi"
PLACEHOLDER = re.compile(r"\{[^/]+\}")


def invalid_operations(schema: GraphQLSchema, library: QueryLibrary) -> list[str]:
	failures = []
	for operation in library.operations():
		errors = validate(schema, parse(library.document(operation)))
		failures += [f"{operation}: {error.message}" for error in errors]
	return failures


def mispriced_operations(schema: GraphQLSchema, library: QueryLibrary) -> tuple[list[str], list[str]]:
	"""The static cost estimate must match every cost recorded from a live store, and every operation must
	fit under Shopify's single-query limit (with PageSizes' margin) once its page sizes are shrunk to 1: one
	that can't is unfixable at run time. Returns the failures, and the operations over the limit at their
	default sizes (the client shrinks those)."""
	cost = QueryCost(schema)
	recorded = recorded_costs()
	limit = MAX_QUERY_COST * TARGET_SHARE
	failures: list[str] = []
	shrinking: list[str] = []
	for operation in library.operations():
		document = library.document(operation)
		call = recorded.get(operation)
		if call is None:
			failures.append(f"{operation}: no cost recorded from a live store")
			continue
		estimate = cost.of(document, call.variables)
		if estimate != call.cost:
			failures.append(f"{operation}: estimated cost {estimate}, but Shopify charged {call.cost}")
		smallest = cost.of(document, dict.fromkeys(library.page_sizes(operation), SMALLEST_SIZE))
		if smallest > limit:
			failures.append(f"{operation}: costs {smallest} even with every page size at 1, over {limit}")
		if estimate > MAX_QUERY_COST:
			shrinking.append(f"{operation} ({estimate})")
	return failures, shrinking


def field_resolver(source: Any, info: Any, **_arguments: Any) -> Any:
	"""Every field resolves from the recorded response by its name (the operations use no aliases)."""
	return source.get(info.field_name) if isinstance(source, dict) else None


def served_differently(
	schema: GraphQLSchema, document: str, data: dict[str, Any], variables: dict[str, Any], name: str
) -> list[str]:
	"""A recorded response executed back through its operation: the errors, or where what the schema
	serialises from it differs from it (so the fixture has exactly the shape the source returns)."""
	result = graphql_sync(
		schema, document, root_value=data, variable_values=variables, field_resolver=field_resolver
	)
	if result.errors:
		return [f"{name}: {error.message}" for error in result.errors]
	difference = first_difference(data, result.data)
	return [] if difference is None else [f"{name}: {difference}"]


def mismatched_shopify_fixtures(schema: GraphQLSchema, library: QueryLibrary) -> list[str]:
	failures = []
	for state in states("shopify"):
		for fixture in graphql_fixtures(state):
			failures += served_differently(
				schema,
				library.document(fixture.operation),
				fixture.data,
				{**library.page_sizes(fixture.operation), **fixture.variables},
				f"{state}/{fixture.file.name}",
			)
	return failures


def invalid_wave_operations(schema: GraphQLSchema) -> list[str]:
	"""Every Wave operation against Wave's schema (tests/schemas/wave.json)."""
	return [
		f"{operation.name}: {error.message}"
		for operation in wave_operations.OPERATIONS
		for error in validate(schema, parse(operation.document))
	]


def mismatched_wave_fixtures(schema: GraphQLSchema) -> list[str]:
	"""Every recorded Wave answer executed back through the operation its request names, with the variables
	it was sent (a fixture records a GraphQL request as a REST one: a POST to the endpoint whose body names
	the operation)."""
	failures: list[str] = []
	for state in states("wave"):
		for fixture in rest_fixtures("wave", state):
			name = f"wave/{state}/{fixture.file.name}"
			operation = next(
				(o for o in wave_operations.OPERATIONS if o.name == fixture.body.get("operationName")), None
			)
			if operation is None or not isinstance(fixture.response, dict):
				failures.append(f"{name}: names no Wave operation, or answers no object")
				continue
			failures += served_differently(
				schema,
				operation.document,
				fixture.response.get("data") or {},
				dict(fixture.body.get("variables") or {}),
				name,
			)
	return failures


def first_difference(expected: Any, actual: Any, path: str = "$") -> str | None:
	"""The first path where a fixture and what the schema serialises from it differ, or None."""
	if isinstance(expected, dict) and isinstance(actual, dict):
		for key in dict.fromkeys([*expected, *actual]):
			if key not in actual:
				return f"{path}.{key} is in the fixture but not selected by the operation"
			if key not in expected:
				return f"{path}.{key} is selected by the operation but missing from the fixture"
			found = first_difference(expected[key], actual[key], f"{path}.{key}")
			if found is not None:
				return found
		return None
	if isinstance(expected, list) and isinstance(actual, list):
		if len(expected) != len(actual):
			return f"{path}: {len(expected)} items in the fixture, {len(actual)} serialised"
		for index, (item, other) in enumerate(zip(expected, actual, strict=True)):
			found = first_difference(item, other, f"{path}[{index}]")
			if found is not None:
				return found
		return None
	numbers = (int, float)
	if expected == actual or (
		isinstance(expected, numbers) and isinstance(actual, numbers) and float(expected) == float(actual)
	):
		return None
	return f"{path}: the fixture has {expected!r}, the schema serialises {actual!r}"


# Systems the importer writes to: their fixtures record whole request bodies, each validated too.
WRITE_DESTINATIONS = frozenset({"square_stored_value"})


def invalid_source_fixtures(source: str) -> Iterator[str]:
	"""Every recorded response of a source in tools/specs.py's catalog against its pinned documents: the
	request must be an operation one of them defines, and the response must match its 200 schema strictly
	(tools/openapi.py); for a system the importer writes to, the whole request body against the operation's
	request schema too."""
	documents = [(document.load(), document.complete_objects) for document in for_source(source)]
	for state in states(source):
		for fixture in rest_fixtures(source, state):
			name = f"{source}/{state}/{fixture.file.name}"
			for spec, complete in documents:
				schema = spec.response_schema(fixture.method, fixture.path)
				if schema is None:
					continue
				validator = OpenApiValidator(spec.schemas, spec.ref_prefix, complete)
				yield from (f"{name}: {error}" for error in validator.validate(fixture.response, schema))
				if source in WRITE_DESTINATIONS and fixture.body:
					request = spec.request_schema(fixture.method, fixture.path)
					if request is None:
						yield f"{name}: {fixture.method} {fixture.path} takes no request body"
					else:
						yield from (
							f"{name} request: {error}" for error in validator.validate(fixture.body, request)
						)
				break
			else:
				yield f"{name}: {fixture.method} {fixture.path} is not an operation in the {source} spec"


def response_schema_pointer(document: dict[str, Any], method: str, path: str) -> str | None:
	"""The JSON pointer to the 200 response schema of the operation answering ``method path`` (a literal
	path wins over a templated one), or None when the document has no such operation."""
	candidates = []
	for template, operations in document.get("paths", {}).items():
		pattern = (
			"^" + PLACEHOLDER.sub("[^/]+", re.escape(template).replace(r"\{", "{").replace(r"\}", "}")) + "$"
		)
		if re.match(pattern, path) and method.lower() in operations:
			candidates.append(template)
	if not candidates:
		return None
	template = min(candidates, key=lambda t: len(PLACEHOLDER.findall(t)))
	escaped = template.replace("~", "~0").replace("/", "~1")
	return f"/paths/{escaped}/{method.lower()}/responses/200/content/application~1json/schema"


def invalid_rest_fixtures(source: str) -> Iterator[str]:
	document = openapi(source)
	resource = Resource.from_contents({"$id": OPENAPI_ID, **document}, default_specification=DRAFT202012)
	registry: Registry[Any] = Registry().with_resource(OPENAPI_ID, resource)
	for state in states(source):
		for fixture in rest_fixtures(source, state):
			name = f"{source}/{state}/{fixture.file.name}"
			pointer = response_schema_pointer(document, fixture.method, fixture.path)
			if pointer is None:
				yield f"{name}: {fixture.method} {fixture.path} is not in the {source} OpenAPI document"
				continue
			validator = Draft202012Validator({"$ref": f"{OPENAPI_ID}#{pointer}"}, registry=registry)
			for error in validator.iter_errors(fixture.response):
				location = "".join(f"[{part!r}]" for part in error.absolute_path)
				yield f"{name}: response{location}: {error.message}"


def invalid_xml_fixtures(document: DtdDocument) -> Iterator[str]:
	"""Every recorded XML response of a source against its pinned DTD (tools/dtd.py)."""
	dtd = document.dtd()
	for state in states(document.source):
		for fixture in rest_fixtures(document.source, state):
			if fixture.text is None:
				yield f"{document.source}/{state}/{fixture.file.name}: no XML response"
				continue
			yield from (
				f"{document.source}/{state}/{fixture.file.name}: {e}" for e in dtd.errors(fixture.text)
			)
