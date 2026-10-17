# SPDX-License-Identifier: AGPL-3.0-or-later
"""The source boundary: one Adapter per platform, reading complete objects page by page. Everything after
mapping (identity, hashing, writers, retirement, redirects) is shared by every adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node


@dataclass(frozen=True)
class Page:
	"""One page of a kind, every node complete. ``end_cursor`` is where the next page starts."""

	nodes: list[Node]
	has_next_page: bool
	end_cursor: str | None


@dataclass(frozen=True)
class ConnectionConfig:
	"""How to reach one connection's source. Secrets are never rendered (see ``__repr__``)."""

	source: str
	connection: str
	store_url: str
	api_user: str = ""
	api_secret: str = field(default="", repr=False)
	auth_method: str = ""
	# The account the credentials open, where a source needs one (a store hash, a site or store id).
	account: str = ""
	# An OAuth refresh token the source replaces on every refresh (Etsy): each new one is handed to
	# ``save_refresh_token``, which keeps it before the access token is used.
	refresh_token: str = field(default="", repr=False)
	save_refresh_token: Callable[[str], None] | None = field(default=None, repr=False, compare=False)
	# Test harness only (site config, never the desk): where a fake store answers for this connection.
	endpoint_override: str | None = None

	def identity(self, type_: str, id_: int | str) -> str:
		return Identity.of(self.source, self.connection, type_, id_)

	def prefix(self) -> str:
		return Identity.prefix(self.source, self.connection)


class SourceReader(Protocol):
	"""Reads complete objects from one source. Every node it returns carries its identity as ``id``
	(``gid://<source>/<connection>/<Type>/<id>``)."""

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		"""One page of a kind. ``cursor`` is the previous page's end cursor (None first). ``since`` asks
		only for objects changed since then, and is only passed for kinds where ``is_incremental``."""
		...

	def node(self, kind: Kind, gid: str) -> Node | None:
		"""One object by its identity, or None when the source no longer has it."""
		...

	def is_incremental(self, kind: Kind) -> bool: ...


class Adapter(Protocol):
	"""One source platform. ``id`` is persisted in every identity it mints: never rename one."""

	@property
	def id(self) -> str: ...

	@property
	def label(self) -> str: ...

	@property
	def built(self) -> bool:
		"""False for a listed source whose adapter is not built yet: it refuses to run."""
		...

	def kinds(self) -> tuple[Kind, ...]:
		"""The kinds this source provides, in SYNC_ORDER."""
		...

	def mappers(self, config: ConnectionConfig) -> list[Mapper]: ...

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		"""Raises SourceError when the connection isn't configured (or the adapter isn't built)."""
		...

	def filter_hint(self) -> str | None:
		"""An example of the per-kind filter syntax runs pass to the reader, or None without filters."""
		...
