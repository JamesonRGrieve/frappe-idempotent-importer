# SPDX-License-Identifier: AGPL-3.0-or-later
"""Acumatica, through the contract-based REST API's Default endpoint (24.200.001): GL accounts,
subaccounts, warehouses, item classes, stock and non-stock items, customers and vendors with their
contacts, the employees' departments and job titles, employees, projects, leads, opportunities, sales
quotations and orders, purchase orders, A/R invoices, memos and cash sales, A/P bills and adjustments,
payments and checks, and general ledger batches.

The operator registers a connected application in the instance (System → Integration → Connected
Applications, SM303010) with the authorization code flow, this site's
``/api/method/idempotent_importer.api.oauth_callback`` as a redirect URI, a secret and refresh tokens
allowed; saves its client ID (``<guid>@<tenant>``) and secret and the instance's address as the Store URL;
and connects with **Connect to Acumatica**, approving as a user who can read these records. OAuth 2 through
the instance's identity server (``/identity/connect/authorize`` and ``/token``; the flow as the Integration
Development Guide documents it, published for MYOB Acumatica at
https://help.myob.com.au/advanced/Docs/Published/IntegrationDevelopmentGuide/OAuthOIDC_AuthorizationCode_AuthorizationCode.html):
the ``api`` and ``offline_access`` scopes, the secret in the form, a refresh token chain of 30 days by
default (each refresh's newest token is kept)."""

from __future__ import annotations

from collections.abc import Mapping

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.oauth import (
	BearerAuthorizer,
	ClientAuth,
	OAuthApp,
	OAuthClient,
	OAuthProvider,
	TokenGrant,
)
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.acumatica import mapping
from idempotent_importer.sources.acumatica.reader import (
	KINDS,
	RATE_LIMIT,
	SOURCE_ID,
	AcumaticaReader,
	instance_url,
)

LABEL = "Acumatica"
AUTHORIZE_PATH = "/identity/connect/authorize"
GRANT_PATH = "/identity/connect/token"
SCOPES = ("api", "offline_access")


def provider(config: ConnectionConfig) -> OAuthProvider:
	"""The instance's own identity server (the approval goes to the instance the operator named, also when
	the API is reached at a test endpoint)."""
	instance = config.store_url.strip().rstrip("/") or instance_url(config)
	return OAuthProvider(
		label=LABEL,
		authorize_url=instance + AUTHORIZE_PATH,
		token_url=instance + GRANT_PATH,
		scopes=SCOPES,
		client_auth=ClientAuth.FORM,
		reconnect="connect the tenant again",
	)


def oauth(config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
	if not config.api_user or not config.api_secret:
		raise SourceError(
			"Acumatica is not configured: enter the connected application's client ID and secret."
		)
	if config.save_refresh_token is None:
		raise SourceError("Acumatica needs somewhere to keep the refresh token it replaces.")
	instance_url(config)
	return OAuthClient(
		provider(config),
		OAuthApp(config.api_user.strip(), config.api_secret),
		transport,
		config.refresh_token,
		config.save_refresh_token,
		config.endpoint_override + GRANT_PATH if config.endpoint_override else None,
	)


class AcumaticaAdapter:
	id = SOURCE_ID
	label = LABEL
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return KINDS

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		client = RestClient(
			LABEL,
			instance_url(config),
			BearerAuthorizer(oauth(config, transport)),
			transport,
			sleeper,
			RATE_LIMIT,
		)
		return AcumaticaReader(config, client)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return oauth(config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		"""Nothing to settle: the client ID names the tenant."""
		return {}
