# SPDX-License-Identifier: AGPL-3.0-or-later
"""What every writer shares: its connection's targets, the link store (for dependencies and an object's
parts), media, and the record helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import frappe

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import UnresolvedDependency
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import ConnectionContext
from idempotent_importer.erpnext.media import Media
from idempotent_importer.erpnext.stores import FrappeLinkStore


class Writer(ABC):
	"""A writer of one kind (``core.sync.Writer``)."""

	def __init__(self, context: ConnectionContext, links: FrappeLinkStore, media: Media) -> None:
		self.context = context
		self.links = links
		self.media = media

	@abstractmethod
	def write(self, desired: Desired, link: Link | None) -> Target: ...

	@abstractmethod
	def retire(self, link: Link) -> None: ...

	def exists(self, link: Link) -> bool:
		return bool(frappe.db.exists(link.target.doctype, link.target.name))

	def dependency(self, kind: Kind, gid: str) -> str:
		"""The record ``gid`` was mirrored to; UnresolvedDependency while it isn't (it's retried)."""
		target = self.links.target(kind, gid)
		if target is None:
			raise UnresolvedDependency(f"{gid} isn't mirrored yet")
		return target

	def existing(self, link: Link | None) -> Any | None:
		"""The linked record, or None when there is no link or the record was deleted by hand."""
		if link is None or not frappe.db.exists(link.target.doctype, link.target.name):
			return None
		return frappe.get_doc(link.target.doctype, link.target.name)

	@staticmethod
	def save(doc: Any, name: str | None = None) -> None:
		"""Saves ``doc``; a new one is named ``name`` when given (records Frappe would otherwise name after
		their title, which two connections can share)."""
		if doc.is_new():
			doc.insert(ignore_permissions=True, set_name=name)
		else:
			doc.save(ignore_permissions=True)
