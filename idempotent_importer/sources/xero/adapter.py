# SPDX-License-Identifier: AGPL-3.0-or-later
"""Xero, through the Accounting and Projects APIs: the chart of accounts, tracking categories, items,
contacts as customers and suppliers, projects, quotes, purchase orders, invoices and credit notes (sales
and purchase), payments, prepayments and overpayments, manual journals, bank transactions and bank
transfers. The operator creates a Web app at developer.xero.com with this site's
``/api/method/idempotent_importer.api.oauth_callback`` as its redirect URI, saves its client ID and secret
on the connection, and connects with Connect to Xero, which keeps the approved organisation (tenant) as
the account. Xero's OAuth 2 (developer.xero.com/documentation/guides/oauth2/auth-flow): HTTP Basic client
authentication, an access token for 30 minutes, and a new refresh token with every refresh. The granular
read-only scopes (developer.xero.com/documentation/guides/oauth2/scopes) that apps made since 2 March 2026
must use."""

from __future__ import annotations

import json
from collections.abc import Mapping

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.errors import CredentialsRejected, SourceError
from idempotent_importer.core.http import HTTP_OK, HttpTransport, Sleeper
from idempotent_importer.core.kind import SYNC_ORDER, Kind
from idempotent_importer.core.oauth import (
	BearerAuthorizer,
	ClientAuth,
	OAuthApp,
	OAuthClient,
	OAuthProvider,
	TokenGrant,
)
from idempotent_importer.core.rest import REQUEST_TIMEOUT_SECONDS, RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.xero import mapping
from idempotent_importer.sources.xero.reader import (
	API_HOST,
	COLLECTIONS,
	RATE_LIMIT,
	SOURCE_ID,
	TENANT_HEADER,
	XeroReader,
)

GRANT_PATH = "/connect/token"
CONNECTIONS_PATH = "/Connections"
ORGANISATION = "ORGANISATION"
PROVIDER = OAuthProvider(
	label="Xero",
	authorize_url="https://login.xero.com/identity/connect/authorize",
	token_url="https://identity.xero.com" + GRANT_PATH,
	scopes=(
		"offline_access",
		"accounting.settings.read",
		"accounting.contacts.read",
		"accounting.invoices.read",
		"accounting.payments.read",
		"accounting.banktransactions.read",
		"accounting.manualjournals.read",
		"projects.read",
	),
	client_auth=ClientAuth.BASIC,
	reconnect="connect the organisation again",
)


def oauth(config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
	if not config.api_user or not config.api_secret:
		raise SourceError("Xero is not configured: enter the app's client ID and secret.")
	if config.save_refresh_token is None:
		raise SourceError("Xero needs somewhere to keep the refresh token it replaces on every refresh.")
	return OAuthClient(
		PROVIDER,
		OAuthApp(config.api_user, config.api_secret),
		transport,
		config.refresh_token,
		config.save_refresh_token,
		config.endpoint_override + GRANT_PATH if config.endpoint_override else None,
	)


def api_host(config: ConnectionConfig) -> str:
	return config.endpoint_override or API_HOST


class XeroAdapter:
	id = SOURCE_ID
	label = "Xero"
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return tuple(kind for kind in SYNC_ORDER if kind in COLLECTIONS)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		authorizer = BearerAuthorizer(oauth(config, transport), headers={TENANT_HEADER: config.account})
		client = RestClient("Xero", api_host(config), authorizer, transport, sleeper, RATE_LIMIT)
		return XeroReader(config, client)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return oauth(config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		"""The organisation the approval connected (GET /connections): the account, unless the approval
		connected several and the connection doesn't already name one of them."""
		response = transport.get(
			api_host(config) + CONNECTIONS_PATH,
			{"Authorization": f"Bearer {grant.access_token}", "Accept": "application/json"},
			REQUEST_TIMEOUT_SECONDS,
		)
		if response.status != HTTP_OK:
			raise CredentialsRejected(
				f"Xero didn't list the connected organisations (HTTP {response.status})."
			)
		try:
			listed = json.loads(response.body)
		except ValueError:
			raise SourceError("Xero returned an unreadable list of connections.") from None
		tenants = {
			str(entry["tenantId"]): str(entry.get("tenantName") or entry["tenantId"])
			for entry in (listed if isinstance(listed, list) else [])
			if isinstance(entry, dict) and entry.get("tenantType") == ORGANISATION and entry.get("tenantId")
		}
		if config.account in tenants:
			return {}
		if len(tenants) == 1:
			return {"api_account": next(iter(tenants))}
		names = ", ".join(f"{name} ({tenant})" for tenant, name in tenants.items()) or "none"
		raise SourceError(
			f"Xero connected several organisations or none ({names}): enter the one to mirror as the account."
		)
