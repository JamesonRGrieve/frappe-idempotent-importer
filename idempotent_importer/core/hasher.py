# SPDX-License-Identifier: AGPL-3.0-or-later
"""The change signal: SHA-256 of the mapper version and the desired state, over canonical JSON (object
keys sorted at every depth, list order kept, Decimals in plain notation), so equal states always hash
equally and any change to a mapped field, a legacy path or the mapping itself changes the hash."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.fields import Field
from idempotent_importer.core.money import decimal_text

type Canonical = str | int | bool | list[Canonical] | dict[str, Canonical] | None


def canonical(value: Field) -> Canonical:
	if isinstance(value, Decimal):
		return {"$decimal": decimal_text(value)}
	if isinstance(value, list):
		return [canonical(item) for item in value]
	if isinstance(value, dict):
		return {key: canonical(value[key]) for key in sorted(value)}
	return value


def content_hash(desired: Desired, mapper_version: int) -> str:
	return digest([mapper_version, canonical(desired.fields), list(desired.legacy_paths)])


def part_hash(fields: Field) -> str:
	"""The change signal of one part of an object that has its own ERPNext record (an order's refund)."""
	return digest(canonical(fields))


def digest(document: Canonical) -> str:
	text = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
	return hashlib.sha256(text.encode()).hexdigest()
