# SPDX-License-Identifier: AGPL-3.0-or-later
"""What every writer shares: its connection's targets, the link store (for dependencies and an object's
parts), media, and the record helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import frappe
from frappe.desk.doctype.tag.tag import DocTags

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import UnresolvedDependency
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import ConnectionContext
from idempotent_importer.erpnext.media import Media
from idempotent_importer.erpnext.stores import FrappeLinkStore

# The hash of a part whose content is its identity (a tag, a coupon code): it is there, or it isn't.
PART_HASH = "part"


PARTY_FIELDS = (("Customer", "customer"), ("Supplier", "supplier"))


def parties_of(doc: Any) -> set[tuple[str, str]]:
	"""The parties a booked document posts to: its customer or supplier, its party, its lines' parties."""
	found = {(doctype, str(doc.get(field))) for doctype, field in PARTY_FIELDS if doc.get(field)}
	if doc.get("party_type") and doc.get("party"):
		found.add((str(doc.party_type), str(doc.party)))
	for row in doc.get("accounts") or []:
		if row.get("party_type") and row.get("party"):
			found.add((str(row.party_type), str(row.party)))
	return found


def cancel_booked(doc: Any) -> None:
	"""Cancels a booked document, even when the source retired one of its parties first (ERPNext posts no
	reversing entry for a disabled party): each disabled party is enabled for the cancellation only."""
	disabled = [
		(doctype, name) for doctype, name in parties_of(doc) if frappe.db.get_value(doctype, name, "disabled")
	]
	for doctype, name in disabled:
		frappe.db.set_value(doctype, name, "disabled", 0, update_modified=False)
	try:
		doc.cancel()
	finally:
		for doctype, name in disabled:
			frappe.db.set_value(doctype, name, "disabled", 1, update_modified=False)


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

	def tags(self, gid: str, doctype: str, name: str, tags: list[str]) -> None:
		"""The source's tags on a record as Frappe document tags. Each mirrored tag is its own part, so a tag
		the source drops is removed while tags a person added are kept."""
		doc_tags = DocTags(doctype)
		for tag in tags:
			key = f"{gid}#tag:{tag}"
			if self.links.target(Kind.TAG, key) is None:
				doc_tags.add(name, tag)
				self.links.save(Kind.TAG, key, Target("Tag", tag), PART_HASH, 1)
		for link in self.links.children(Kind.TAG, f"{gid}#tag:"):
			if link.target.name not in tags:
				doc_tags.remove(name, link.target.name)
				self.links.retire(Kind.TAG, link.gid)

	@staticmethod
	def save(doc: Any, name: str | None = None) -> None:
		"""Saves ``doc``; a new one is named ``name`` when given (records Frappe would otherwise name after
		their title, which two connections can share)."""
		if doc.is_new():
			doc.insert(ignore_permissions=True, set_name=name)
		else:
			doc.save(ignore_permissions=True)
