# SPDX-License-Identifier: AGPL-3.0-or-later
"""The desk's Sync Now button (a run started by hand, the credential hold released for it, worked through
in the background) and Connect to Etsy (OAuth with PKCE: the shop owner approves read access on Etsy,
which sends them back here with a code that is exchanged for the refresh token the connection keeps)."""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

import frappe
from frappe import _

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.run import Mode
from idempotent_importer.core.transport import RequestsTransport
from idempotent_importer.erpnext import engine, jobs
from idempotent_importer.erpnext.stores import CONNECTION, FrappeCredentialHold
from idempotent_importer.sources.etsy.adapter import oauth
from idempotent_importer.sources.registry import by_label

ETSY_CALLBACK = "/api/method/idempotent_importer.api.etsy_callback"
# How long an Etsy approval may take before its state expires.
ETSY_STATE_TTL_SECONDS = 15 * 60
ETSY_STATE_CACHE = "idempotent_importer_etsy_state:"
VERIFIER_BYTES = 48


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


def etsy_redirect_uri() -> str:
	return frappe.utils.get_url(ETSY_CALLBACK)


@frappe.whitelist(methods=["POST"])
def etsy_connect(connection: str) -> str:
	"""The Etsy URL where the shop owner approves read access; Etsy returns them to ``etsy_callback``."""
	frappe.only_for("System Manager")
	doc = frappe.get_doc(CONNECTION, connection)
	state, verifier = secrets.token_urlsafe(), secrets.token_urlsafe(VERIFIER_BYTES)
	frappe.cache.set_value(
		ETSY_STATE_CACHE + state,
		{"connection": connection, "verifier": verifier},
		expires_in_sec=ETSY_STATE_TTL_SECONDS,
	)
	config = engine.config_of(doc, by_label(doc.source))
	return oauth(config, RequestsTransport()).authorization_url(etsy_redirect_uri(), state, verifier)


@frappe.whitelist(methods=["GET"])
def etsy_callback(code: str | None = None, state: str | None = None, error: str | None = None) -> None:
	"""Etsy's redirect after the shop owner approved (or refused): the code becomes the connection's refresh
	token, and the operator lands back on the connection."""
	frappe.only_for("System Manager")
	pending = frappe.cache.get_value(ETSY_STATE_CACHE + (state or "")) if state else None
	if not isinstance(pending, dict):
		raise frappe.ValidationError(
			_("This Etsy approval has expired or was not started here; connect again.")
		)
	frappe.cache.delete_value(ETSY_STATE_CACHE + str(state))
	connection = str(pending["connection"])
	if error or not code:
		raise frappe.ValidationError(
			_("Etsy did not approve the connection: {0}").format(error or _("no code"))
		)
	doc = frappe.get_doc(CONNECTION, connection)
	config = engine.config_of(doc, by_label(doc.source))
	oauth(config, RequestsTransport()).exchange(code, str(pending["verifier"]), etsy_redirect_uri())
	FrappeCredentialHold(connection).release()
	frappe.db.commit()
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = (
		"/app/importer-connection/" + connection + "?" + urlencode({"etsy": "connected"})
	)
