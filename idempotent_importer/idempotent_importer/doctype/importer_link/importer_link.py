# SPDX-License-Identifier: AGPL-3.0-or-later
import frappe
from frappe.model.document import Document


class ImporterLink(Document):
	pass


def on_doctype_update() -> None:
	"""One link per (connection, kind, identity): the identity map's key."""
	frappe.db.add_unique("Importer Link", ["connection", "kind", "gid"], constraint_name="unique_identity")
