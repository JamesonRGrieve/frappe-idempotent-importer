# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared test objects: connection configurations for the recorded stores, and in-memory stand-ins for the
engine's ERPNext-side stores (the real ones are proven end to end on a bench)."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

import pytest

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.run import RunState
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.core.sync import Link, Target


@pytest.fixture
def woo_config() -> ConnectionConfig:
	return ConnectionConfig(
		source="woocommerce",
		connection="woo-a",
		store_url="https://woo-a.example.test",
		api_user="e2e",
		api_secret="e2e-application-password",
		auth_method="Application Password",
	)


@pytest.fixture
def shopify_config() -> ConnectionConfig:
	return ConnectionConfig(
		source="shopify",
		connection="shop-s",
		store_url="e2e-fixture.myshopify.com",
		api_user="e2e-client-id",
		api_secret="e2e-client-secret",
		auth_method="Client Credentials",
	)


@dataclass
class MemoryLinks:
	"""LinkStore over a dict, scoped to one connection's identities by prefix."""

	prefix: str
	links: dict[tuple[Kind, str], Link] = field(default_factory=dict)
	seen: dict[str, set[tuple[Kind, str]]] = field(default_factory=dict)

	def find(self, kind: Kind, gid: str) -> Link | None:
		return self.links.get((kind, gid))

	def save(self, kind: Kind, gid: str, target: Target, hash_: str, mapper_version: int) -> None:
		self.links[(kind, gid)] = Link(kind, gid, target, hash_, mapper_version)

	def retire(self, kind: Kind, gid: str) -> None:
		link = self.links[(kind, gid)]
		self.links[(kind, gid)] = Link(kind, gid, link.target, "", link.mapper_version, retired=True)

	def unseen(self, kind: Kind, run_id: str) -> list[Link]:
		seen = self.seen.get(run_id, set())
		return [
			link
			for (k, gid), link in self.links.items()
			if k == kind and gid.startswith(self.prefix) and not link.retired and (k, gid) not in seen
		]

	def mark(self, run_id: str, kind: Kind, gids: Sequence[str]) -> None:
		self.seen.setdefault(run_id, set()).update((kind, gid) for gid in gids)

	def clear(self, run_id: str) -> None:
		self.seen.pop(run_id, None)


@dataclass
class MemoryWriter:
	"""A writer that records what it was asked to write; ``fail``/``defer`` make the next writes raise."""

	written: dict[str, Desired] = field(default_factory=dict)
	retired: list[str] = field(default_factory=list)
	gone: set[str] = field(default_factory=set)
	error: BaseException | None = None

	def exists(self, link: Link) -> bool:
		return link.target.name not in self.gone

	def write(self, desired: Desired, link: Link | None) -> Target:
		if self.error is not None:
			raise self.error
		self.written[desired.gid] = desired
		return Target("Record", desired.gid.rsplit("/", 1)[-1])

	def retire(self, link: Link) -> None:
		self.retired.append(link.gid)


@dataclass
class MemoryScope:
	"""The write scope: counts commits and rollbacks."""

	commits: int = 0
	rollbacks: int = 0

	@contextmanager
	def __call__(self) -> Iterator[None]:
		try:
			yield
		except BaseException:
			self.rollbacks += 1
			raise
		self.commits += 1


@dataclass
class MemoryRedirects:
	synced: dict[str, tuple[tuple[str, ...], str | None]] = field(default_factory=dict)

	def sync(self, kind: Kind, gid: str, legacy_paths: tuple[str, ...], route: str | None) -> None:
		self.synced[gid] = (legacy_paths, route)


@dataclass
class MemoryRunStore:
	state: RunState | None = None
	finished: list[tuple[str, str]] = field(default_factory=list)

	def load(self) -> RunState | None:
		return self.state

	def start(self, state: RunState) -> None:
		self.state = state

	def save(self, state: RunState) -> None:
		self.state = state

	def finish(self, state: RunState, status: str) -> None:
		self.finished.append((state.id, status))
		self.state = None


@dataclass
class MemoryCursors:
	cursors: dict[Kind, datetime] = field(default_factory=dict)

	def last_clean_start(self, kind: Kind) -> datetime | None:
		return self.cursors.get(kind)

	def advance(self, kind: Kind, run_started_at: datetime) -> None:
		self.cursors[kind] = run_started_at


@dataclass
class MemoryLock:
	holder: str | None = None
	held_until: datetime | None = None

	def acquire(self, run_id: str) -> bool:
		if self.holder is not None:
			return False
		self.holder = run_id
		return True

	def hold_until(self, run_id: str, until: datetime) -> None:
		if self.holder == run_id:
			self.held_until = until

	def refresh(self, run_id: str) -> None:
		return None

	def release(self, run_id: str) -> None:
		if self.holder == run_id:
			self.holder = None


@dataclass
class MemoryHold:
	reasons: list[str] = field(default_factory=list)

	def hold(self, reason: str) -> None:
		self.reasons.append(reason)
