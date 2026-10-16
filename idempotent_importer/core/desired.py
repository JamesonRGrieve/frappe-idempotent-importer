# SPDX-License-Identifier: AGPL-3.0-or-later
"""What one source object should look like in ERPNext, and the pure translation that produces it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from idempotent_importer.core.fields import Fields, Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node


@dataclass(frozen=True)
class Desired:
	"""The mapped fields of one source object (and only those), in the shared field contract every
	writer reads whatever the source; the old storefront paths that should redirect to its new home; and
	anything about it ERPNext can't represent, reported as warnings."""

	kind: Kind
	gid: str
	fields: Fields
	legacy_paths: tuple[str, ...] = ()
	warnings: tuple[str, ...] = field(default=(), compare=False)

	def values(self) -> Values:
		return Values(self.fields)


class Mapper(Protocol):
	"""Pure translation of one source object into its desired ERPNext state. Bump ``version`` whenever
	the mapping's output changes, so every object of the kind is rewritten on the next run."""

	@property
	def kind(self) -> Kind: ...

	@property
	def version(self) -> int: ...

	def map(self, node: Node) -> Desired | None:
		"""None when the object should not exist in ERPNext (so an earlier copy is retired).

		Raises Unsupported when ERPNext has no home for the object."""
		...
