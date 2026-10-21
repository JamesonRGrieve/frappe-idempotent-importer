# SPDX-License-Identifier: AGPL-3.0-or-later
"""The desk's Sync Now button (a run started by hand, the credential hold released for it, worked through
in the background) and Connect, for every source approved through OAuth 2 (``core.source.OAuthSource``):
the account's owner approves read access at the source, which sends them back here with a code that is
exchanged for the refresh token the connection keeps, and settles what the approval names (QuickBooks'
company, Xero's organisation, Zoho's data centre)."""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode

import frappe
from frappe import _

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.oauth import CALLBACK_PATH
from idempotent_importer.core.run import Mode
from idempotent_importer.core.source import OAuthSource
from idempotent_importer.core.transport import RequestsTransport
from idempotent_importer.erpnext import engine, jobs
from idempotent_importer.erpnext.stores import CONNECTION, FrappeCredentialHold
from idempotent_importer.sources.registry import by_label

# How long an approval may take before its state expires.
STATE_TTL_SECONDS = 15 * 60
STATE_CACHE = "idempotent_importer_oauth_state:"
VERIFIER_BYTES = 48
# The connection fields an approval may settle.
SETTLED_FIELDS = frozenset({"api_account", "store_url"})


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


def oauth_source(doc: Any) -> OAuthSource:
	adapter = by_label(doc.source)
	if not isinstance(adapter, OAuthSource):
		raise frappe.ValidationError(_("{0} connections aren't approved through OAuth.").format(doc.source))
	return adapter


def redirect_uri(source: OAuthSource, doc: Any) -> str:
	"""The callback URL registered with the source's app."""
	config = engine.config_of(doc, by_label(doc.source))
	callback = source.oauth(config, RequestsTransport()).provider.callback
	return frappe.utils.get_url(CALLBACK_PATH + callback)


@frappe.whitelist(methods=["POST"])
def connect(connection: str) -> str:
	"""The source's URL where the account's owner approves read access; it sends them back to the callback."""
	frappe.only_for("System Manager")
	doc = frappe.get_doc(CONNECTION, connection)
	source = oauth_source(doc)
	state, verifier = secrets.token_urlsafe(), secrets.token_urlsafe(VERIFIER_BYTES)
	frappe.cache.set_value(
		STATE_CACHE + state,
		{"connection": connection, "verifier": verifier},
		expires_in_sec=STATE_TTL_SECONDS,
	)
	config = engine.config_of(doc, by_label(doc.source))
	return source.oauth(config, RequestsTransport()).authorization_url(
		redirect_uri(source, doc), state, verifier
	)


@frappe.whitelist(methods=["GET"])
def oauth_callback(
	code: str | None = None, state: str | None = None, error: str | None = None, **params: str
) -> None:
	"""The source's redirect after the owner approved (or refused): the code becomes the connection's refresh
	token, what the approval names is kept on the connection, and the operator lands back on it."""
	frappe.only_for("System Manager")
	pending = frappe.cache.get_value(STATE_CACHE + (state or "")) if state else None
	if not isinstance(pending, dict):
		raise frappe.ValidationError(_("This approval has expired or was not started here; connect again."))
	frappe.cache.delete_value(STATE_CACHE + str(state))
	connection = str(pending["connection"])
	if error or not code:
		raise frappe.ValidationError(
			_("The source did not approve the connection: {0}").format(error or _("no code"))
		)
	doc = frappe.get_doc(CONNECTION, connection)
	source = oauth_source(doc)
	transport = RequestsTransport()
	config = engine.config_of(doc, by_label(doc.source))
	flow = source.oauth(config, transport)
	grant = flow.exchange(code, str(pending["verifier"]), redirect_uri(source, doc))
	settled = source.connected(config, transport, grant, {k: str(v) for k, v in params.items()})
	unknown = set(settled) - SETTLED_FIELDS
	if unknown:
		raise SourceError(f"An approval can't settle {', '.join(sorted(unknown))}.")
	if settled:
		frappe.db.set_value(CONNECTION, connection, settled)
	FrappeCredentialHold(connection).release()
	frappe.db.commit()
	frappe.local.response["type"] = "redirect"
	frappe.local.response["location"] = (
		"/app/importer-connection/" + connection + "?" + urlencode({"oauth": "connected"})
	)


# Etsy apps registered before the other OAuth sources shared a callback send the owner here.
# (The same whitelisted function, so it is allowed at this path too.)
etsy_callback = oauth_callback
