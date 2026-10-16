# SPDX-License-Identifier: AGPL-3.0-or-later
"""The one path from a source object to ERPNext: map, hash, compare with the link, and write only when the
hash differs (or the ERPNext record is gone). Used by runs, single-object syncs and the CLI alike. The
ERPNext side (links, writers, the quiet write scope, redirects) is behind the protocols below."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import UnresolvedDependency, Unsupported
from idempotent_importer.core.hasher import content_hash
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node

logger = logging.getLogger(__name__)


class Outcome(StrEnum):
	"""What happened to one source object in a run."""

	CREATED = "created"
	UPDATED = "updated"
	SKIPPED = "skipped"
	RETIRED = "retired"
	UNSUPPORTED = "unsupported"
	DEFERRED = "deferred"
	FAILED = "failed"

	def is_incomplete(self) -> bool:
		"""Outcomes that leave the kind incomplete for this run (its incremental cursor must not advance)."""
		return self in (Outcome.DEFERRED, Outcome.FAILED)

	def is_write(self) -> bool:
		return self in (Outcome.CREATED, Outcome.UPDATED, Outcome.RETIRED)


@dataclass(frozen=True)
class Target:
	"""The ERPNext record an object was written to, and its public route when it has one."""

	doctype: str
	name: str
	route: str | None = None


@dataclass(frozen=True)
class Link:
	"""One source object ↔ the ERPNext record it was written to, and the hash of what was written. A
	retired link keeps its record (disabled, cancelled or marked), so a returning object revives it."""

	kind: Kind
	gid: str
	target: Target
	hash: str
	mapper_version: int
	retired: bool = False


class LinkStore(Protocol):
	"""The identity map: (connection, kind, identity) → ERPNext record + content hash."""

	def find(self, kind: Kind, gid: str) -> Link | None: ...

	def save(self, kind: Kind, gid: str, target: Target, hash_: str, mapper_version: int) -> None: ...

	def retire(self, kind: Kind, gid: str) -> None: ...

	def unseen(self, kind: Kind, run_id: str) -> list[Link]:
		"""This connection's live links of ``kind`` that run ``run_id`` did not see: the retirement
		candidates of a full pass. Other connections' links are never candidates."""
		...


class Writer(Protocol):
	"""Writes one kind's Desired into ERPNext: only mapped fields, never a source's vocabulary."""

	def exists(self, link: Link) -> bool:
		"""Whether the linked record still exists (a record deleted by hand is written again)."""
		...

	def write(self, desired: Desired, link: Link | None) -> Target:
		"""Creates or updates the record. Raises UnresolvedDependency when something it refers to isn't
		mirrored yet."""
		...

	def retire(self, link: Link) -> None:
		"""Disables, cancels or marks the record; never deletes it."""
		...


class WriteScope(Protocol):
	"""One object's writes: no notifications or emails, and all-or-nothing (rolled back on any error)."""

	def __call__(self) -> AbstractContextManager[None]: ...


class Redirects(Protocol):
	"""301s from an object's old storefront paths to its new route."""

	def sync(self, kind: Kind, gid: str, legacy_paths: tuple[str, ...], route: str | None) -> None: ...


@dataclass(frozen=True)
class Handler:
	mapper: Mapper
	writer: Writer


class ObjectSync:
	def __init__(
		self,
		handlers: Mapping[Kind, Handler],
		links: LinkStore,
		scope: WriteScope,
		redirects: Redirects,
	) -> None:
		self._handlers = dict(handlers)
		self._links = links
		self._scope = scope
		self._redirects = redirects

	def handles(self, kind: Kind) -> bool:
		return kind in self._handlers

	def sync(self, kind: Kind, node: Node, dry_run: bool) -> Outcome:
		handler = self._handler(kind)
		gid = node.id()
		try:
			desired = handler.mapper.map(node)
		except Unsupported as e:
			logger.info("%s not mirrored: %s", gid, e)
			return Outcome.UNSUPPORTED
		if desired is None:
			return self.retire(kind, gid, dry_run)
		hash_ = content_hash(desired, handler.mapper.version)
		link = self._links.find(kind, gid)
		if link is not None and not link.retired and link.hash == hash_ and handler.writer.exists(link):
			return Outcome.SKIPPED
		outcome = Outcome.CREATED if link is None else Outcome.UPDATED
		if dry_run:
			return outcome
		try:
			with self._scope():
				target = handler.writer.write(desired, link)
				self._links.save(kind, gid, target, hash_, handler.mapper.version)
				self._redirects.sync(kind, gid, desired.legacy_paths, target.route)
		except UnresolvedDependency as e:
			logger.info("%s deferred: %s", gid, e)
			return Outcome.DEFERRED
		except Unsupported as e:
			logger.info("%s not mirrored: %s", gid, e)
			return Outcome.UNSUPPORTED
		except Exception:
			# Deliberate catch-all: one object's failure is rolled back, recorded, and the run carries on.
			logger.exception("%s failed", gid)
			return Outcome.FAILED
		for warning in desired.warnings:
			logger.warning("%s: %s", gid, warning)
		return outcome

	def retire(self, kind: Kind, gid: str, dry_run: bool) -> Outcome:
		"""The source object is gone (deleted, or no longer belongs in ERPNext)."""
		link = self._links.find(kind, gid)
		if link is None or link.retired:
			return Outcome.SKIPPED
		if dry_run:
			return Outcome.RETIRED
		try:
			with self._scope():
				self._handler(kind).writer.retire(link)
				self._links.retire(kind, gid)
				self._redirects.sync(kind, gid, (), None)
		except Exception:
			# Deliberate catch-all: rolled back, recorded, and retried by the next full run.
			logger.exception("%s could not be retired", gid)
			return Outcome.FAILED
		return Outcome.RETIRED

	def _handler(self, kind: Kind) -> Handler:
		handler = self._handlers.get(kind)
		if handler is None:
			raise LookupError(f"No handler for {kind}")
		return handler
