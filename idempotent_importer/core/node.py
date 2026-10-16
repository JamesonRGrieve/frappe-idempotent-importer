# SPDX-License-Identifier: AGPL-3.0-or-later
"""Typed, validated read access to one object of a source's response. Every read names the field it
expects, so a response that doesn't match the schema fails loudly at the boundary, with its path."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from decimal import Decimal

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.money import decimal_of
from idempotent_importer.core.times import iso_utc

type JsonValue = str | int | float | bool | list[JsonValue] | dict[str, JsonValue] | None
type JsonObject = dict[str, JsonValue]

INTEGER = re.compile(r"^-?\d+$")
CONNECTION_NODES = "nodes"


class Node:
	"""One JSON object, read field by field."""

	__slots__ = ("_data", "path")

	def __init__(self, data: Mapping[str, JsonValue], path: str = "$") -> None:
		self._data: JsonObject = dict(data)
		self.path = path

	def raw(self) -> JsonObject:
		return dict(self._data)

	def has(self, field: str) -> bool:
		return self._data.get(field) is not None

	def get(self, field: str) -> JsonValue:
		return self._data.get(field)

	def with_fields(self, **fields: JsonValue) -> Node:
		"""This object with extra fields (what a reader assembles around it, e.g. its variants)."""
		return Node({**self._data, **fields}, self.path)

	def string(self, field: str) -> str:
		value = self._data.get(field)
		if not isinstance(value, str):
			raise self._mismatch(field, "a string")
		return value

	def optional_string(self, field: str) -> str | None:
		"""A string, or None when absent, null or empty."""
		value = self._data.get(field)
		if value is not None and not isinstance(value, str):
			raise self._mismatch(field, "a string or null")
		return value or None

	def text(self, field: str) -> str:
		"""A string that may be absent or null (read as empty)."""
		return self.optional_string(field) or ""

	def flag(self, field: str) -> bool:
		value = self._data.get(field)
		if not isinstance(value, bool):
			raise self._mismatch(field, "a boolean")
		return value

	def optional_flag(self, field: str) -> bool | None:
		value = self._data.get(field)
		if value is not None and not isinstance(value, bool):
			raise self._mismatch(field, "a boolean or null")
		return value

	def optional_integer(self, field: str) -> int | None:
		value = self._data.get(field)
		if value is None:
			return None
		if isinstance(value, int) and not isinstance(value, bool):
			return value
		if isinstance(value, str) and INTEGER.match(value):
			return int(value)
		raise self._mismatch(field, "an integer or null")

	def integer(self, field: str) -> int:
		value = self.optional_integer(field)
		if value is None:
			raise self._mismatch(field, "an integer")
		return value

	def optional_decimal(self, field: str) -> Decimal | None:
		"""A decimal sent as a string or a JSON number, exactly as spelled; None when absent, null or ""."""
		value = self._data.get(field)
		if value is None or value == "":
			return None
		if isinstance(value, bool) or not isinstance(value, int | float | str):
			raise self._mismatch(field, "a decimal or null")
		try:
			return decimal_of(value)
		except ValueError:
			raise self._mismatch(field, "a decimal or null") from None

	def decimal(self, field: str) -> Decimal:
		value = self.optional_decimal(field)
		if value is None:
			raise self._mismatch(field, "a decimal")
		return value

	def optional_time(self, field: str) -> str | None:
		"""A timestamp as UTC ``YYYY-MM-DDTHH:MM:SSZ``; None when absent, null or empty."""
		try:
			return iso_utc(self.optional_string(field))
		except SourceError:
			raise self._mismatch(field, "a timestamp") from None

	def time(self, field: str) -> str:
		value = self.optional_time(field)
		if value is None:
			raise self._mismatch(field, "a timestamp")
		return value

	def strings(self, field: str) -> list[str]:
		value = self._data.get(field) or []
		if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
			raise self._mismatch(field, "a list of strings")
		return [item for item in value if isinstance(item, str)]

	def integers(self, field: str) -> list[int]:
		value = self._data.get(field) or []
		if not isinstance(value, list):
			raise self._mismatch(field, "a list of integers")
		ints: list[int] = []
		for item in value:
			if isinstance(item, bool) or not isinstance(item, int):
				raise self._mismatch(field, "a list of integers")
			ints.append(item)
		return ints

	def child(self, field: str) -> Node | None:
		"""A nested object, or None when absent or null. (An empty JSON list, which PHP-backed APIs send for
		an empty object, is read as an empty object.)"""
		value = self._data.get(field)
		if value is None or value == []:
			return None if value is None else Node({}, f"{self.path}.{field}")
		if not isinstance(value, dict):
			raise self._mismatch(field, "an object or null")
		return Node(value, f"{self.path}.{field}")

	def required_child(self, field: str) -> Node:
		node = self.child(field)
		if node is None:
			raise self._mismatch(field, "an object")
		return node

	def children(self, field: str) -> list[Node]:
		"""The objects of a list field, or of a GraphQL connection's ``nodes``."""
		value = self._data.get(field) or []
		if isinstance(value, dict) and CONNECTION_NODES in value:
			value = value[CONNECTION_NODES] or []
		if not isinstance(value, list):
			raise self._mismatch(field, "a list or connection")
		nodes: list[Node] = []
		for index, item in enumerate(value):
			if not isinstance(item, dict):
				raise self._mismatch(f"{field}[{index}]", "an object")
			nodes.append(Node(item, f"{self.path}.{field}[{index}]"))
		return nodes

	def items(self) -> Iterator[tuple[str, JsonValue]]:
		return iter(self._data.items())

	def id(self) -> str:
		return self.string("id")

	def typename(self) -> str:
		"""The GraphQL object type of a union/interface member (requires ``__typename`` in the selection)."""
		return self.string("__typename")

	def _mismatch(self, field: str, expected: str) -> SourceError:
		return SourceError(f"Source response {self.path}.{field} is not {expected}")
