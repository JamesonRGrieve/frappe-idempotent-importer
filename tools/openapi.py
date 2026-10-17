# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checks a JSON value against an OpenAPI 3 / Swagger 2 schema, as wp-woo-idempotent-importer's
tools/OpenApiValidator.php does (ported). No undeclared property may appear (a fixture can't invent a
field) unless the schema allows extra keys (``additionalProperties``). With ``complete_objects`` every
declared property must be present (sources that return complete objects); otherwise only the schema's
``required`` ones, and an optional property may be null (Swagger 2 has no ``nullable``). Supports type (a
name, or OpenAPI 3.1's list of names, ``null`` included), nullable / x-nullable, enum, minimum, maximum,
items, properties, required, additionalProperties, allOf, $ref, and oneOf/anyOf (a value must match at
least one member; overlapping members aren't rejected)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

NULL_TYPE = "null"
OPENAPI_REF_PREFIX = "#/components/schemas/"
SWAGGER_REF_PREFIX = "#/definitions/"
TEMPLATE_PARAMETER = re.compile(r"\\\{[A-Za-z_]+\\\}")

type Schema = dict[str, Any]


class OpenApiValidator:
	def __init__(self, schemas: dict[str, Schema], ref_prefix: str, complete_objects: bool) -> None:
		self._schemas = schemas
		self._ref_prefix = ref_prefix
		self._complete = complete_objects

	def validate(self, value: Any, schema: Schema, path: str = "$", optional: bool = False) -> list[str]:
		"""Every violation, each with its JSON path."""
		schema = self._resolve(schema)
		members = self._members(schema)
		if value is None:
			nullable = self._is_nullable(schema) or (optional and not self._complete)
			return [] if nullable else [f"{path} is null but not nullable"]
		if members is not None:
			return self._any_member(value, members, path)
		types = types_of(schema)
		if types and not any(has_type(value, kind) for kind in types):
			return [f"{path} should be {' or '.join(types)}, got {type(value).__name__}"]
		errors: list[str] = []
		enum = schema.get("enum")
		if isinstance(enum, list) and not any(same(value, allowed) for allowed in enum):
			errors.append(f"{path} is not one of {', '.join(json.dumps(item) for item in enum)}")
		number = isinstance(value, int | float) and not isinstance(value, bool)
		minimum, maximum = schema.get("minimum"), schema.get("maximum")
		if number and isinstance(minimum, int | float) and value < minimum:
			errors.append(f"{path} is below {minimum}")
		if number and isinstance(maximum, int | float) and value > maximum:
			errors.append(f"{path} is above {maximum}")
		if isinstance(value, list) and isinstance(schema.get("items"), dict):
			for index, item in enumerate(value):
				errors += self.validate(item, schema["items"], f"{path}[{index}]")
		if ("properties" in schema or "additionalProperties" in schema) and (
			isinstance(value, dict) or value == []
		):
			# PHP-backed APIs send an empty object as [].
			errors += self._object(value if isinstance(value, dict) else {}, schema, path)
		return errors

	def _object(self, value: dict[str, Any], schema: Schema, path: str) -> list[str]:
		properties: dict[str, Schema] = schema.get("properties") or {}
		listed = schema.get("required")
		required: list[str] = (
			[name for name in listed if isinstance(name, str)] if isinstance(listed, list) else []
		)
		errors: list[str] = []
		for name, property_schema in properties.items():
			is_required = self._complete or name in required
			if name not in value:
				if is_required:
					errors.append(f"{path}.{name} is missing")
				continue
			errors += self.validate(value[name], property_schema, f"{path}.{name}", not is_required)
		extra = schema.get("additionalProperties", False)
		for name, item in value.items():
			if name in properties:
				continue
			if isinstance(extra, dict):
				errors += self.validate(item, extra, f"{path}.{name}")
			elif extra is not True:
				errors.append(f"{path}.{name} is not in the spec")
		return errors

	def _resolve(self, schema: Schema) -> Schema:
		"""Follows $ref and single-member oneOf/anyOf, merges allOf members, keeping the wrapper's nullable."""
		nullable = declares_null(schema)
		while True:
			one = schema.get("oneOf") if isinstance(schema.get("oneOf"), list) else schema.get("anyOf")
			ref = schema.get("$ref")
			if isinstance(ref, str) and ref.startswith(self._ref_prefix):
				name = ref.removeprefix(self._ref_prefix)
				if name not in self._schemas:
					raise LookupError(f"Unknown schema {ref}")
				schema = self._schemas[name]
			elif isinstance(one, list) and len(one) == 1:
				schema = one[0]
			elif isinstance(schema.get("allOf"), list):
				schema = self._merge(schema)
			else:
				return {**schema, "nullable": True} if nullable else schema
			nullable = nullable or declares_null(schema)

	def _merge(self, schema: Schema) -> Schema:
		"""One schema from an allOf: the members' properties and required lists combined."""
		members = schema["allOf"]
		merged: Schema = {key: value for key, value in schema.items() if key != "allOf"}
		properties: dict[str, Schema] = dict(merged.get("properties") or {})
		required: list[str] = list(merged.get("required") or [])
		for member in members:
			resolved = self._resolve(member)
			properties |= resolved.get("properties") or {}
			required += [name for name in resolved.get("required") or [] if name not in required]
			if merged.get("type") is None and resolved.get("type") is not None:
				merged["type"] = resolved["type"]
			if "additionalProperties" in resolved and "additionalProperties" not in merged:
				merged["additionalProperties"] = resolved["additionalProperties"]
		return {**merged, "properties": properties, "required": required}

	def _any_member(self, value: Any, members: list[Schema], path: str) -> list[str]:
		rejections: list[str] = []
		for member in members:
			errors = self.validate(value, member, path)
			if not errors:
				return []
			rejections.append(errors[0])
		return [f"{path} matches no oneOf/anyOf member ({' | '.join(rejections)})"]

	@staticmethod
	def _members(schema: Schema) -> list[Schema] | None:
		members = schema.get("oneOf") if isinstance(schema.get("oneOf"), list) else schema.get("anyOf")
		if not isinstance(members, list) or len(members) < 2:  # noqa: PLR2004 - one member is a $ref in disguise
			return None
		return members

	def _is_nullable(self, schema: Schema) -> bool:
		if declares_null(schema):
			return True
		return any(self._is_nullable(self._resolve(member)) for member in self._members(schema) or [])


def types_of(schema: Schema) -> list[str]:
	kind = schema.get("type")
	if isinstance(kind, str):
		return [kind]
	return [item for item in kind if isinstance(item, str)] if isinstance(kind, list) else []


def declares_null(schema: Schema) -> bool:
	return schema.get("nullable") is True or schema.get("x-nullable") is True or NULL_TYPE in types_of(schema)


def has_type(value: Any, kind: str) -> bool:
	match kind:
		case "null":
			return value is None
		case "object":
			return isinstance(value, dict) or value == []
		case "array":
			return isinstance(value, list)
		case "string":
			return isinstance(value, str)
		case "integer":
			return isinstance(value, int) and not isinstance(value, bool)
		case "number":
			return isinstance(value, int | float) and not isinstance(value, bool)
		case "boolean":
			return isinstance(value, bool)
		case _:
			return True


def same(value: Any, allowed: Any) -> bool:
	"""Equality as PHP's strict ``in_array`` sees it (a boolean is not a number)."""
	return type(value) is type(allowed) and value == allowed


@dataclass(frozen=True)
class ApiSpec:
	"""One published API document (OpenAPI 3 or Swagger 2): its schemas, and the 200 response schema of an
	operation on a concrete request path. A request path is matched after stripping the document's server
	prefix (e.g. ``/rest/[^/]+``), given as a regular expression."""

	schemas: dict[str, Schema]
	ref_prefix: str
	paths: dict[str, dict[str, Any]]
	prefix_pattern: str

	@classmethod
	def of(cls, document: dict[str, Any], prefix_pattern: str) -> ApiSpec:
		swagger = "swagger" in document
		schemas = (
			document.get("definitions") if swagger else (document.get("components") or {}).get("schemas")
		)
		return cls(
			dict(schemas or {}),
			SWAGGER_REF_PREFIX if swagger else OPENAPI_REF_PREFIX,
			dict(document.get("paths") or {}),
			prefix_pattern,
		)

	def response_schema(self, method: str, path: str) -> Schema | None:
		relative = self._relative(path)
		if relative is None:
			return None
		method = method.lower()
		for template, operations in self.paths.items():
			pattern = "^" + TEMPLATE_PARAMETER.sub("[^/]+", re.escape(template)) + "$"
			if (
				not re.match(pattern, relative)
				or not isinstance(operations, dict)
				or method not in operations
			):
				continue
			return at(
				operations, (method, "responses", "200", "content", "application/json", "schema")
			) or at(operations, (method, "responses", "200", "schema"))
		return None

	def _relative(self, path: str) -> str | None:
		if not self.prefix_pattern:
			return path
		relative, count = re.subn("^" + self.prefix_pattern, "", path, count=1)
		return relative if count == 1 else None


def at(value: Any, keys: tuple[str, ...]) -> Schema | None:
	for key in keys:
		if not isinstance(value, dict) or key not in value:
			return None
		value = value[key]
	return value if isinstance(value, dict) else None
