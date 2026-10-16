# SPDX-License-Identifier: AGPL-3.0-or-later
"""Identities are ``gid://<source>/<connection>/<Type>/<id>``: one link table holds every source and every
connection to it without collisions, and one connection's links are found by prefix. Two WooCommerce
stores with the same product ids are two connections, so their identities never meet."""

from __future__ import annotations

import re
from dataclasses import dataclass

from idempotent_importer.core.errors import SourceError

SCHEME = "gid://"
SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
# source, connection, type, id
IDENTITY_PARTS = 4


@dataclass(frozen=True)
class Identity:
	"""One parsed identity."""

	source: str
	connection: str
	type: str
	id: str

	@classmethod
	def of(cls, source: str, connection: str, type_: str, id_: int | str) -> str:
		text = str(id_)
		if not SEGMENT.match(source) or not SEGMENT.match(connection):
			raise SourceError(f"Not an identity host: {source}/{connection}")
		if not TYPE.match(type_) or text == "" or "/" in text:
			raise SourceError(f"Not an identity path: {type_}/{text}")
		return f"{SCHEME}{source}/{connection}/{type_}/{text}"

	@classmethod
	def prefix(cls, source: str, connection: str) -> str:
		"""The prefix every identity of one connection starts with."""
		return f"{SCHEME}{source}/{connection}/"

	@classmethod
	def parse(cls, gid: str) -> Identity:
		parts = gid.removeprefix(SCHEME).split("/") if gid.startswith(SCHEME) else []
		if len(parts) != IDENTITY_PARTS or "" in parts:
			raise SourceError(f"Not a source identity: {gid}")
		return cls(parts[0], parts[1], parts[2], parts[3])

	def __str__(self) -> str:
		return self.of(self.source, self.connection, self.type, self.id)
