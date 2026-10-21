# SPDX-License-Identifier: AGPL-3.0-or-later
"""Salesforce (Sales Cloud), through the REST API: one org per connection. The operator creates an External
Client App (or a connected app) in the org with OAuth enabled, the ``api`` and ``refresh_token`` scopes, this
site's ``/api/method/idempotent_importer.api.oauth_callback`` as its callback URL and a secret required for
the web server flow; saves its consumer key and secret and the org's My Domain URL
(``https://<domain>.my.salesforce.com``) as the Store URL; and connects with **Connect to Salesforce**,
approving as a user who can read these records.

OAuth 2 through the org's My Domain (the web server flow,
help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oauth_web_server_flow.htm): ``/services/oauth2/
authorize`` and ``/services/oauth2/token``, the consumer key and secret in the token form, an access token
for the org's session timeout, and a refresh token kept until revoked (or replaced on each refresh when the
app rotates them: the newest is always kept)."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

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
from idempotent_importer.sources.salesforce import mapping
from idempotent_importer.sources.salesforce.reader import (
	KINDS,
	RATE_LIMIT,
	SOURCE_ID,
	LimitGuard,
	SalesforceReader,
)

LABEL = "Salesforce"
AUTHORIZE_PATH = "/services/oauth2/authorize"
GRANT_PATH = "/services/oauth2/token"
SCOPES = ("api", "refresh_token")


def org_url(config: ConnectionConfig) -> str:
	"""The org's My Domain address (``https://<domain>.my.salesforce.com``)."""
	url = config.store_url.strip().rstrip("/")
	parts = urlsplit(url)
	if parts.scheme != "https" or not parts.hostname:
		raise SourceError(
			"Salesforce is not configured: enter the org's My Domain URL (https://<domain>.my.salesforce.com)."
		)
	return f"https://{parts.hostname}"


def provider(config: ConnectionConfig) -> OAuthProvider:
	org = org_url(config)
	return OAuthProvider(
		label=LABEL,
		authorize_url=org + AUTHORIZE_PATH,
		token_url=org + GRANT_PATH,
		scopes=SCOPES,
		client_auth=ClientAuth.FORM,
		reconnect="connect the org again",
		rotates_refresh_token=False,
	)


def oauth(config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
	if not config.api_user or not config.api_secret:
		raise SourceError("Salesforce is not configured: enter the app's consumer key and secret.")
	if config.save_refresh_token is None:
		raise SourceError("Salesforce needs somewhere to keep its refresh token.")
	return OAuthClient(
		provider(config),
		OAuthApp(config.api_user.strip(), config.api_secret),
		transport,
		config.refresh_token,
		config.save_refresh_token,
		config.endpoint_override + GRANT_PATH if config.endpoint_override else None,
	)


class SalesforceAdapter:
	id = SOURCE_ID
	label = LABEL
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return tuple(kind for kind in SYNC_ORDER if kind in KINDS)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		guarded = LimitGuard(transport)
		client = RestClient(
			LABEL,
			config.endpoint_override or org_url(config),
			BearerAuthorizer(oauth(config, guarded)),
			guarded,
			sleeper,
			RATE_LIMIT,
		)
		return SalesforceReader(config, client)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return oauth(config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		"""The token is for the org at the connection's My Domain; nothing more to settle."""
		return {}
