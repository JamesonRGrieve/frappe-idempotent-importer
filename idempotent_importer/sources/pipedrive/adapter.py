# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pipedrive, through its API v2 (and v1 where v2 has no endpoint yet): one company per connection. The
operator creates an app in Pipedrive's Developer Hub with the read scopes below and this site's
``/api/method/idempotent_importer.api.oauth_callback`` as its callback URL, saves its client ID and secret on
the connection, and connects with Connect, approving as a user of the company: the token's ``api_domain``
(``https://<company>.pipedrive.com``) becomes the connection's account, the host its API answers on.

Pipedrive's OAuth 2 (pipedrive.readme.io/docs/marketplace-oauth-authorization): the authorization code grant
at oauth.pipedrive.com, the client authenticated by HTTP Basic, an hour's access token and a refresh token
that stays the same (unused for 60 days, it lapses and the company must connect again)."""

from __future__ import annotations

from collections.abc import Mapping

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import SYNC_ORDER, Kind
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
from idempotent_importer.sources.pipedrive import mapping
from idempotent_importer.sources.pipedrive.reader import KINDS, RATE_LIMIT, SOURCE_ID, PipedriveReader

LABEL = "Pipedrive"
OAUTH_HOST = "https://oauth.pipedrive.com"
GRANT_PATH = "/oauth/token"
API_DOMAIN = "api_domain"
# The scopes of what the adapter reads (pipedrive.readme.io/docs/marketplace-scopes-and-permissions-explanations).
SCOPES = (
	"base",
	"users:read",
	"deals:read",
	"contacts:read",
	"leads:read",
	"activities:read",
	"products:read",
	"phone-integration",
)
PROVIDER = OAuthProvider(
	label=LABEL,
	authorize_url=OAUTH_HOST + "/oauth/authorize",
	token_url=OAUTH_HOST + GRANT_PATH,
	scopes=SCOPES,
	client_auth=ClientAuth.BASIC,
	reconnect="connect the Pipedrive company again",
	rotates_refresh_token=False,
)


def oauth(config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
	if not config.api_user or not config.api_secret:
		raise SourceError("Pipedrive is not configured: enter the app's client ID and secret.")
	if config.save_refresh_token is None:
		raise SourceError("Pipedrive needs somewhere to keep its refresh token.")
	return OAuthClient(
		PROVIDER,
		OAuthApp(config.api_user, config.api_secret),
		transport,
		config.refresh_token,
		config.save_refresh_token,
		config.endpoint_override + GRANT_PATH if config.endpoint_override else None,
	)


class PipedriveAdapter:
	id = SOURCE_ID
	label = LABEL
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return tuple(kind for kind in SYNC_ORDER if kind in KINDS)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		host = config.endpoint_override or config.account
		if not host.startswith("https://") and not config.endpoint_override:
			raise SourceError("Pipedrive is not connected: use Connect to Pipedrive first.")
		client = RestClient(
			LABEL, host, BearerAuthorizer(oauth(config, transport)), transport, sleeper, RATE_LIMIT
		)
		return PipedriveReader(config, client)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return oauth(config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		"""The company's API host the token names becomes the connection's account."""
		domain = grant.extra.get(API_DOMAIN)
		if not isinstance(domain, str) or not domain.startswith("https://"):
			raise SourceError("Pipedrive's approval named no company domain.")
		return {"api_account": domain.rstrip("/")}
