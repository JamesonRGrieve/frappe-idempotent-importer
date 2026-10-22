# SPDX-License-Identifier: AGPL-3.0-or-later
"""Webflow, through the Data API v2: one site per connection. The operator registers a Data Client app in a
Webflow workspace (Apps & Integrations → Develop) with the read scopes below and this site's
``/api/method/idempotent_importer.api.oauth_callback`` as its redirect URI, saves the app's client ID and
secret and the site's address as the Store URL, and connects with Connect to Webflow, authorising the site:
the site the token opens becomes the connection's account (with several, the one already entered as the
account).

Webflow's OAuth 2 (developers.webflow.com/data/reference/oauth-app): the authorization code grant at
webflow.com, the client's ID and secret in the token request at api.webflow.com/oauth/access_token, and an
access token that lasts until the app is uninstalled or the token revoked: Webflow issues no refresh token, so
the access token is the credential the connection keeps."""

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
from idempotent_importer.sources.webflow import mapping
from idempotent_importer.sources.webflow.reader import (
	API_HOST,
	API_PATH,
	KINDS,
	RATE_LIMIT,
	SOURCE_ID,
	WebflowReader,
)

LABEL = "Webflow"
GRANT_PATH = "/oauth/access_token"
# The read scopes of what the adapter reads (the v2 OpenAPI document's operations' scopes).
SCOPES = ("sites:read", "cms:read", "pages:read", "assets:read", "ecommerce:read")
PROVIDER = OAuthProvider(
	label=LABEL,
	authorize_url="https://webflow.com/oauth/authorize",
	token_url=API_HOST + GRANT_PATH,
	scopes=SCOPES,
	client_auth=ClientAuth.FORM,
	reconnect="connect the Webflow site again",
	rotates_refresh_token=False,
	lasting_access_token=True,
)


def oauth(config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
	if not config.api_user or not config.api_secret:
		raise SourceError("Webflow is not configured: enter the app's client ID and secret.")
	if config.save_refresh_token is None:
		raise SourceError("Webflow needs somewhere to keep the site's access token.")
	return OAuthClient(
		PROVIDER,
		OAuthApp(config.api_user, config.api_secret),
		transport,
		config.refresh_token,
		config.save_refresh_token,
		config.endpoint_override + GRANT_PATH if config.endpoint_override else None,
	)


class WebflowAdapter:
	id = SOURCE_ID
	label = LABEL
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return tuple(kind for kind in SYNC_ORDER if kind in KINDS)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		client = RestClient(
			LABEL,
			config.endpoint_override or API_HOST,
			BearerAuthorizer(oauth(config, transport)),
			transport,
			sleeper,
			RATE_LIMIT,
		)
		return WebflowReader(config, client)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return oauth(config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		"""The site the token opens (GET /v2/sites): the account, unless it opens several and the connection
		doesn't already name one of them."""
		response = transport.get(
			(config.endpoint_override or API_HOST) + API_PATH + "/sites",
			{"Authorization": f"Bearer {grant.access_token}", "Accept": "application/json"},
			REQUEST_TIMEOUT_SECONDS,
		)
		if response.status != HTTP_OK:
			raise CredentialsRejected(f"Webflow didn't list the sites (HTTP {response.status}).")
		try:
			listed = json.loads(response.body)
		except ValueError:
			raise SourceError("Webflow returned an unreadable list of sites.") from None
		sites = {
			str(site["id"]): str(site.get("displayName") or site["id"])
			for site in (listed.get("sites") or [] if isinstance(listed, dict) else [])
			if isinstance(site, dict) and site.get("id")
		}
		if config.account in sites:
			return {}
		if len(sites) == 1:
			return {"api_account": next(iter(sites))}
		names = ", ".join(f"{name} ({id_})" for id_, name in sites.items()) or "none"
		raise SourceError(
			f"Webflow opened several sites or none ({names}): enter the one to mirror as the account."
		)
