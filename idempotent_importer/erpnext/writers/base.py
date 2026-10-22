# SPDX-License-Identifier: AGPL-3.0-or-later
"""What every writer shares: its connection's targets, the link store (for dependencies and an object's
parts), media, and the record helpers."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import frappe
from frappe.desk.doctype.tag.tag import DocTags

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import UnresolvedDependency
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import ConnectionContext
from idempotent_importer.erpnext.custom_fields import CUSTOM_VALUES
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


@contextmanager
def parties_enabled(*docs: Any) -> Iterator[None]:
	"""ERPNext books nothing for a disabled party (no entry, no reversal), but a source keeps the history of
	a party it archived: each disabled party the documents post to is enabled while they are written,
	submitted or cancelled, and disabled again after (its modified time untouched)."""
	disabled = sorted(
		{
			(doctype, name)
			for doc in docs
			if doc is not None
			for doctype, name in parties_of(doc)
			if frappe.db.get_value(doctype, name, "disabled")
		}
	)
	for doctype, name in disabled:
		frappe.db.set_value(doctype, name, "disabled", 0, update_modified=False)
	try:
		yield
	finally:
		for doctype, name in disabled:
			frappe.db.set_value(doctype, name, "disabled", 1, update_modified=False)


def cancel_booked(doc: Any) -> None:
	"""Cancels a booked document, even when the source retired one of its parties first."""
	with parties_enabled(doc):
		doc.cancel()


def custom_json(values: Values) -> str | None:
	"""The values of the source's own fields (``custom``, by label) as a record's Source Fields
	(``custom_fields.CUSTOM_VALUES``), or None."""
	custom = values.record("custom")
	found = custom.all() if custom is not None else {}
	return json.dumps(found, sort_keys=True) if found else None


def set_custom(doc: Any, values: Values) -> None:
	doc.set(CUSTOM_VALUES, custom_json(values))


DRAFT, SUBMITTED, CANCELLED = 0, 1, 2
RETIRED_COMMENT = "Removed at the source."


def writable(doctype: str, doc: Any | None) -> Any:
	"""The record a submittable document is written into: a new one (none yet, or the last was cancelled),
	the linked draft, or, for a submitted one, its amendment (the submitted one is cancelled)."""
	if doc is None or doc.docstatus == CANCELLED:
		return frappe.new_doc(doctype)
	if doc.docstatus == SUBMITTED:
		cancel_booked(doc)
		amended = frappe.copy_doc(doc)
		amended.docstatus = DRAFT
		amended.amended_from = doc.name
		return amended
	return doc


def mark_retired(doctype: str, name: str) -> None:
	"""Marks a record the source removed that has no state of its own to switch off: a comment on it, once."""
	if not frappe.db.exists(
		"Comment", {"reference_doctype": doctype, "reference_name": name, "content": RETIRED_COMMENT}
	):
		frappe.get_doc(doctype, name).add_comment("Comment", RETIRED_COMMENT)


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
