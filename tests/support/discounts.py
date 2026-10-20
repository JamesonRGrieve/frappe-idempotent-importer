# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads of a mapped discount's recorded conditions, shared by the sources' tests."""

from __future__ import annotations

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.fields import Fields


def condition_kinds(desired: Desired | None) -> list[str]:
	"""The kinds of a discount's recorded conditions, in order (none when it has none)."""
	assert desired is not None
	return [c.string("kind") for c in desired.values().records("conditions")]


def terms(desired: Desired | None, kind: str) -> Fields:
	"""The terms of a discount's one condition of ``kind``."""
	assert desired is not None
	found = [c for c in desired.values().records("conditions") if c.string("kind") == kind]
	assert len(found) == 1, f"one {kind} condition"
	return found[0].required_record("terms").all()


def held(desired: Desired | None) -> list[str]:
	"""The cards a discount is held for (none when it isn't)."""
	assert desired is not None
	return desired.values().strings("held")
