# SPDX-License-Identifier: AGPL-3.0-or-later
"""The desk's Sync Now button: a run started by hand (the credential hold is released for it), worked
through in the background."""

from __future__ import annotations

from typing import Any

import frappe
from frappe import _

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.run import Mode
from idempotent_importer.erpnext import engine, jobs
from idempotent_importer.erpnext.stores import CONNECTION, FrappeCredentialHold


@frappe.whitelist(methods=["POST"])
def sync_now(connection: str, full: int = 0, dry_run: int = 0) -> dict[str, Any]:
	frappe.only_for("System Manager")
	if not frappe.db.exists(CONNECTION, connection):
		raise frappe.DoesNotExistError
	FrappeCredentialHold(connection).release()
	try:
		state = engine.start(
			connection, Mode.FULL if int(full) else Mode.INCREMENTAL, bool(int(dry_run)), None
		)
	except SourceError as e:
		frappe.throw(_("The run can't start: {0}").format(str(e)))
	jobs.enqueue_step(connection)
	result = engine.status(connection)
	result["started"] = state is not None
	return result
