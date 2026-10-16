# SPDX-License-Identifier: AGPL-3.0-or-later
"""Proves the adapters' queries and test data against the real APIs' published descriptions, without a
live store:

1. every Shopify GraphQL operation the adapter sends validates against Shopify's Admin schema, and every
   recorded Shopify response executes back through its operation unchanged (so each fixture has exactly
   the shape — field names, enums, non-nulls — Shopify returns for it);
2. every recorded REST response validates against its source's OpenAPI document (WooCommerce's is
   transcribed from its official documentation, tests/schemas/woocommerce.json).

Each check returns its failures (empty when everything holds)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

from graphql import GraphQLSchema, graphql_sync, parse, validate
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from idempotent_importer.sources.shopify.queries import QueryLibrary
from tests.support.fixtures import graphql_fixtures, rest_fixtures, states
from tools.schemas import openapi

OPENAPI_ID = "urn:openapi"
PLACEHOLDER = re.compile(r"\{[^/]+\}")


def invalid_operations(schema: GraphQLSchema, library: QueryLibrary) -> list[str]:
	failures = []
	for operation in library.operations():
		errors = validate(schema, parse(library.document(operation)))
		failures += [f"{operation}: {error.message}" for error in errors]
	return failures


def field_resolver(source: Any, info: Any, **_arguments: Any) -> Any:
	"""Every field resolves from the recorded response by its name (the operations use no aliases)."""
	return source.get(info.field_name) if isinstance(source, dict) else None


def mismatched_shopify_fixtures(schema: GraphQLSchema, library: QueryLibrary) -> list[str]:
	failures = []
	for state in states("shopify"):
		for fixture in graphql_fixtures(state):
			result = graphql_sync(
				schema,
				library.document(fixture.operation),
				root_value=fixture.data,
				variable_values=fixture.variables,
				field_resolver=field_resolver,
			)
			name = f"{state}/{fixture.file.name}"
			if result.errors:
				failures += [f"{name}: {error.message}" for error in result.errors]
				continue
			difference = first_difference(fixture.data, result.data)
			if difference is not None:
				failures.append(f"{name}: {difference}")
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
