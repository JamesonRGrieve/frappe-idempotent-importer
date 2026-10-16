# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mapped fields of a Desired, and typed reads of them for writers. A field of the wrong type is a
programming error between a mapper and its writer, never a source problem."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

type Field = str | int | bool | Decimal | list[Field] | dict[str, Field] | None
type Fields = dict[str, Field]


class Values:
	"""Typed reads of one record of mapped fields (a Desired's, or a nested record in them)."""

	__slots__ = ("_fields",)

	def __init__(self, fields: Mapping[str, Field]) -> None:
		self._fields: Fields = dict(fields)

	def all(self) -> Fields:
		return dict(self._fields)

	def has(self, key: str) -> bool:
		return self._fields.get(key) is not None

	def string(self, key: str) -> str:
		value = self._fields.get(key)
		if not isinstance(value, str):
			raise self._wrong(key, "string")
		return value

	def optional_string(self, key: str) -> str | None:
		value = self._fields.get(key)
		if value is not None and not isinstance(value, str):
			raise self._wrong(key, "string or None")
		return value or None

	def flag(self, key: str) -> bool:
		value = self._fields.get(key)
		if not isinstance(value, bool):
			raise self._wrong(key, "bool")
		return value

	def integer(self, key: str) -> int:
		value = self.optional_integer(key)
		if value is None:
			raise self._wrong(key, "int")
		return value

	def optional_integer(self, key: str) -> int | None:
		value = self._fields.get(key)
		if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
			raise self._wrong(key, "int or None")
		return value

	def decimal(self, key: str) -> Decimal:
		value = self.optional_decimal(key)
		if value is None:
			raise self._wrong(key, "Decimal")
		return value

	def optional_decimal(self, key: str) -> Decimal | None:
		value = self._fields.get(key)
		if value is not None and not isinstance(value, Decimal):
			raise self._wrong(key, "Decimal or None")
		return value

	def strings(self, key: str) -> list[str]:
		value = self._fields.get(key) or []
		if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
			raise self._wrong(key, "list of strings")
		return [item for item in value if isinstance(item, str)]

	def record(self, key: str) -> Values | None:
		value = self._fields.get(key)
		if value is None:
			return None
		if not isinstance(value, dict):
			raise self._wrong(key, "record or None")
		return Values(value)

	def required_record(self, key: str) -> Values:
		record = self.record(key)
		if record is None:
			raise self._wrong(key, "record")
		return record

	def records(self, key: str) -> list[Values]:
		value = self._fields.get(key) or []
		if not isinstance(value, list):
			raise self._wrong(key, "list of records")
		records: list[Values] = []
		for item in value:
			if not isinstance(item, dict):
				raise self._wrong(key, "list of records")
			records.append(Values(item))
		return records

	def _wrong(self, key: str, expected: str) -> TypeError:
		return TypeError(f'Mapped field "{key}" is not a {expected}.')
