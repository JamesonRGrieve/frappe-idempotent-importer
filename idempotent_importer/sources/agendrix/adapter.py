# SPDX-License-Identifier: AGPL-3.0-or-later
"""Agendrix, through its public API (developers.agendrix.com): the organisation's sites, positions and
members, time off types and requests, shifts, clock logs and time entries, into HRMS. The operator creates
an application in Agendrix's developer portal (a developer account of its own; the API is for
organisations on Agendrix's Plus plan), adds this site's
``/api/method/idempotent_importer.api.oauth_callback`` as a redirect URI, saves its client ID and secret on
the connection with the Store URL https://app.agendrix.com (or https://sandbox.agendrix.net for the
sandbox), and connects the organisation with Connect, signed in as one of its administrators (the API
answers within what its token's owner may see).

Agendrix's OAuth 2 (the API description's "OAuth 2.0" section): the authorization code grant with the
read scope; the token endpoint takes JSON and wants the redirect URI on every refresh; an access token
lasts two hours, and each refresh token works once (the new one is kept before the access token is
used)."""

from __future__ import annotations

from collections.abc import Mapping

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import SYNC_ORDER, Kind
from idempotent_importer.core.oauth import (
	CALLBACK_PATH,
	BearerAuthorizer,
	ClientAuth,
	OAuthApp,
	OAuthClient,
	OAuthProvider,
	TokenGrant,
)
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.agendrix import mapping
from idempotent_importer.sources.agendrix.reader import (
	KINDS,
	RATE_LIMIT,
	SOURCE_ID,
	AgendrixReader,
	api_base,
	app_host,
)

LABEL = "Agendrix"
OAUTH_PATH = "/oauth"
# Every request is JSON (the API description's "Headers").
JSON_HEADERS = {"Content-Type": "application/json"}


def provider(host: str) -> OAuthProvider:
	return OAuthProvider(
		label=LABEL,
		authorize_url=f"https://{host}{OAUTH_PATH}/authorize",
		token_url=f"https://{host}{OAUTH_PATH}/token",
		scopes=("read",),
		client_auth=ClientAuth.FORM,
		reconnect="connect the organisation again",
		json_token_requests=True,
		redirect_on_refresh=True,
	)


def oauth(config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
	if not config.api_user or not config.api_secret:
		raise SourceError("Agendrix is not configured: enter the application's client ID and secret.")
	if config.save_refresh_token is None:
		raise SourceError("Agendrix needs somewhere to keep the refresh token it replaces.")
	chosen = provider(app_host(config))
	return OAuthClient(
		chosen,
		OAuthApp(config.api_user, config.api_secret),
		transport,
		config.refresh_token,
		config.save_refresh_token,
		config.endpoint_override + OAUTH_PATH + "/token" if config.endpoint_override else None,
		config.site_url + CALLBACK_PATH + chosen.callback if config.site_url else None,
	)


class AgendrixAdapter:
	id = SOURCE_ID
	label = LABEL
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return tuple(kind for kind in SYNC_ORDER if kind in KINDS)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		authorizer = BearerAuthorizer(oauth(config, transport), headers=JSON_HEADERS)
		client = RestClient(LABEL, api_base(config), authorizer, transport, sleeper, RATE_LIMIT)
		return AgendrixReader(config, client, sleeper)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return oauth(config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		"""Nothing to settle: the token is the organisation the administrator chose."""
		return {}
