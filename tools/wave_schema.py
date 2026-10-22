# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes tests/schemas/wave.json: Wave's public GraphQL schema, introspected from its public endpoint
(https://gql.waveapps.com/graphql/public answers introspection without a token), pruned to what the importer's
operations (idempotent_importer/sources/wave/operations.py) select: each type they reach, with only the fields
they select (every argument of those fields kept, and every enum, scalar and input type those arguments
take). Descriptions are Wave's. The file (JSON, its SDL a line each) records the date and the SHA-256 of the
whole schema as Wave printed it, so a change upstream is visible in the diff when the file is rewritten.

``python -m tools.wave_schema`` rewrites it (it needs the network); tools/prove.py proves every Wave operation
and fixture against the committed file."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import requests
from graphql import (
	DefinitionNode,
	DocumentNode,
	FieldNode,
	GraphQLInputObjectType,
	GraphQLNamedType,
	GraphQLObjectType,
	GraphQLSchema,
	ObjectTypeDefinitionNode,
	TypeInfo,
	TypeInfoVisitor,
	Visitor,
	build_client_schema,
	build_schema,
	get_introspection_query,
	get_named_type,
	parse,
	print_ast,
	print_schema,
	visit,
)
from graphql.type import specified_scalar_types

from idempotent_importer.sources.wave.operations import OPERATIONS

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests" / "schemas" / "wave.json"
ENDPOINT = "https://gql.waveapps.com/graphql/public"
FETCH_TIMEOUT_SECONDS = 120


def fetch() -> str:
	"""Wave's whole schema, as SDL."""
	response = requests.post(
		ENDPOINT, json={"query": get_introspection_query()}, timeout=FETCH_TIMEOUT_SECONDS
	)
	response.raise_for_status()
	payload = response.json()
	if (
		not isinstance(payload, dict)
		or not isinstance(payload.get("data"), dict)
		or "__schema" not in payload["data"]
	):
		raise RuntimeError("Wave's endpoint returned no introspection.")
	return print_schema(build_client_schema(payload["data"]))


class Selected(Visitor):
	"""The fields the operations select, by their type, and the types those fields return."""

	def __init__(self, info: TypeInfo) -> None:
		super().__init__()
		self.info = info
		self.fields: dict[str, set[str]] = {}

	def enter_field(self, node: FieldNode, *_: object) -> None:
		parent = self.info.get_parent_type()
		if parent is not None and not node.name.value.startswith("__"):
			self.fields.setdefault(parent.name, set()).add(node.name.value)


def selected_fields(schema: GraphQLSchema) -> dict[str, set[str]]:
	info = TypeInfo(schema)
	selected = Selected(info)
	for operation in OPERATIONS:
		visit(parse(operation.document), TypeInfoVisitor(info, selected))
	return selected.fields


def reached_types(schema: GraphQLSchema, fields: dict[str, set[str]]) -> set[str]:
	"""Every type a kept field returns or takes as an argument, with the input types those take in turn."""
	reached = set(fields)
	pending: list[GraphQLNamedType] = []
	for type_name, names in fields.items():
		owner = schema.get_type(type_name)
		if not isinstance(owner, GraphQLObjectType):
			raise TypeError(f"{type_name} is selected from but is not an object type")
		for name in names:
			field = owner.fields[name]
			pending.append(get_named_type(field.type))
			pending += [get_named_type(argument.type) for argument in field.args.values()]
	while pending:
		found = pending.pop()
		if found.name in reached and found.name not in fields:
			continue
		reached.add(found.name)
		if isinstance(found, GraphQLInputObjectType):
			pending += [
				get_named_type(field.type)
				for field in found.fields.values()
				if get_named_type(field.type).name not in reached
			]
	return reached


def pruned(sdl: str) -> str:
	schema = build_schema(sdl)
	fields = selected_fields(schema)
	kept = reached_types(schema, fields) - set(specified_scalar_types)
	document = parse(sdl)
	definitions: list[DefinitionNode] = []
	for definition in document.definitions:
		name = getattr(getattr(definition, "name", None), "value", None)
		if name not in kept:
			continue
		if isinstance(definition, ObjectTypeDefinitionNode):
			definitions.append(
				ObjectTypeDefinitionNode(
					name=definition.name,
					description=definition.description,
					directives=definition.directives,
					interfaces=(),
					fields=tuple(
						field_definition
						for field_definition in definition.fields or ()
						if field_definition.name.value in fields.get(name, set())
					),
				)
			)
		else:
			definitions.append(definition)
	text = print_ast(DocumentNode(definitions=tuple(definitions)))
	build_schema(text)
	return text + "\n"


def main() -> int:
	sdl = fetch()
	digest = hashlib.sha256(sdl.encode()).hexdigest()
	today = datetime.now(UTC).strftime("%Y-%m-%d")
	document = {
		"source": ENDPOINT,
		"introspected": today,
		"sha256": digest,
		"description": "Wave's public GraphQL schema, pruned by tools/wave_schema.py to what the importer's "
		"operations select; sha256 is the whole schema's as Wave printed it. sdl is the pruned schema, a line each.",
		"sdl": pruned(sdl).splitlines(),
	}
	TARGET.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
