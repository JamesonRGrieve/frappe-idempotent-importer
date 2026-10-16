# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source images as public Files, one per source URL per connection: downloaded once, then reused by every
record that shows them."""

from __future__ import annotations

from pathlib import PurePosixPath
from urllib.parse import urlsplit

import frappe

from idempotent_importer.core.http import HttpTransport
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Target
from idempotent_importer.erpnext.context import ConnectionContext
from idempotent_importer.erpnext.stores import FrappeLinkStore

DOWNLOAD_TIMEOUT_SECONDS = 60.0
MEDIA_MAX_BYTES = 25 * 1024 * 1024
MEDIA_HASH = "media"


class Media:
	def __init__(self, context: ConnectionContext, links: FrappeLinkStore, transport: HttpTransport) -> None:
		self._context = context
		self._links = links
		self._transport = transport

	def file_url(self, url: str | None, attached_to: tuple[str, str] | None = None) -> str | None:
		"""The Frappe file URL of the source image at ``url`` (downloaded the first time)."""
		if not url:
			return None
		gid = self._context.media_gid(url)
		existing = self._links.target(Kind.FILE, gid)
		if existing and frappe.db.exists("File", existing):
			return str(frappe.db.get_value("File", existing, "file_url"))
		content = self._transport.download(url, DOWNLOAD_TIMEOUT_SECONDS, MEDIA_MAX_BYTES)
		file = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name(url),
				"is_private": 0,
				"content": content,
				"attached_to_doctype": attached_to[0] if attached_to else None,
				"attached_to_name": attached_to[1] if attached_to else None,
			}
		)
		file.insert(ignore_permissions=True)
		self._links.save(Kind.FILE, gid, Target("File", file.name), MEDIA_HASH, 1)
		return str(file.file_url)


def file_name(url: str) -> str:
	"""The file name a URL ends in ("…/toque.png?v=2" → "toque.png")."""
	return PurePosixPath(urlsplit(url).path).name or "image"
