# SPDX-License-Identifier: AGPL-3.0-or-later
"""Values the Dynamics 365 apps send alike: OData's nulls and empty strings, Business Central's and Finance
& Operations' blank date (``0001-01-01`` / ``1900-01-01``), and a date-only source's date as a contract
time."""

from __future__ import annotations

from decimal import Decimal

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.node import Node

# The dates Dynamics sends for "no date": AL's blank date and X++'s dateNull().
BLANK_DATES = frozenset({"0001-01-01", "1900-01-01"})
DATE_LENGTH = len("YYYY-MM-DD")
# The GUID Business Central sends for a reference that names nothing.
EMPTY_GUID = "00000000-0000-0000-0000-000000000000"
# A date-only document's time: noon UTC, so it falls on the same day in every site time zone from UTC-11
# to UTC+11 (the contract's times are UTC; writers book on the site's local date).
NOON = "T12:00:00Z"


def text(node: Node, field: str) -> str | None:
	value = node.optional_string(field)
	return value.strip() or None if value else None


def reference(node: Node, field: str) -> str | None:
	"""A GUID that refers to another record; None when it names none (empty, or the empty GUID)."""
	value = text(node, field)
	return None if value is None or value == EMPTY_GUID else value


def date_of(node: Node, field: str) -> str | None:
	"""A date (or a datetime's date); None for a blank one."""
	value = text(node, field)
	if value is None:
		return None
	day = value[:DATE_LENGTH]
	return None if day in BLANK_DATES else day


def required_date(node: Node, field: str) -> str:
	day = date_of(node, field)
	if day is None:
		raise SourceError(f"{node.path}.{field} is missing")
	return day


def noon_of(day: str) -> str:
	return f"{day}{NOON}"


def amount(node: Node, field: str) -> Decimal:
	return node.optional_decimal(field) or Decimal(0)
