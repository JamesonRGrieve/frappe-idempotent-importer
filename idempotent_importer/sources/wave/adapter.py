# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wave, through its public GraphQL API: one business per connection. The operator creates an application in
Wave's developer portal (developer.waveapps.com, Manage Applications) with this site's
``/api/method/idempotent_importer.api.oauth_callback`` as its redirect URI, saves its client ID and secret on
the connection, and connects with Connect to Wave: the business the approval opens (the one already entered
as the account, when it opens several) becomes the connection's account.

Wave's OAuth 2 (developer.waveapps.com, OAuth Guide): the authorization code grant at
api.waveapps.com/oauth2/authorize/, the client's ID and secret and the redirect URI in the token request at
api.waveapps.com/oauth2/token/ (on every refresh too), short-lived access tokens and a refresh token; a new
refresh token, when a refresh returns one, is kept before the access token is used."""

from __future__ import annotations

import json
from collections.abc import Mapping

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.errors import CredentialsRejected, SourceError
from idempotent_importer.core.http import HTTP_OK, HttpTransport, Sleeper
from idempotent_importer.core.kind import SYNC_ORDER, Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.oauth import (
	CALLBACK_PATH,
	BearerAuthorizer,
	ClientAuth,
	OAuthApp,
	OAuthClient,
	OAuthProvider,
	TokenGrant,
)
from idempotent_importer.core.rest import REQUEST_TIMEOUT_SECONDS, RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.wave import mapping, operations
from idempotent_importer.sources.wave.reader import (
	API_HOST,
	API_PATH,
	KINDS,
	PAGE_SIZE,
	RATE_LIMIT,
	SOURCE_ID,
	WaveReader,
)

LABEL = "Wave"
OAUTH_HOST = "https://api.waveapps.com"
GRANT_PATH = "/oauth2/token/"
# The read scopes of what the adapter reads (Wave's OAuth scopes: <resource>:read).
SCOPES = (
	"business:read",
	"account:read",
	"sales_tax:read",
	"product:read",
	"customer:read",
	"vendor:read",
	"invoice:read",
	"user:read",
)
PROVIDER = OAuthProvider(
	label=LABEL,
	authorize_url=OAUTH_HOST + "/oauth2/authorize/",
	token_url=OAUTH_HOST + GRANT_PATH,
	scopes=SCOPES,
	client_auth=ClientAuth.FORM,
	reconnect="connect the Wave business again",
	rotates_refresh_token=False,
	redirect_on_refresh=True,
)


def oauth(config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
	if not config.api_user or not config.api_secret:
		raise SourceError("Wave is not configured: enter the application's client ID and secret.")
	if config.save_refresh_token is None:
		raise SourceError("Wave needs somewhere to keep the refresh token it replaces.")
	return OAuthClient(
		PROVIDER,
		OAuthApp(config.api_user, config.api_secret),
		transport,
		config.refresh_token,
		config.save_refresh_token,
		config.endpoint_override + GRANT_PATH if config.endpoint_override else None,
		config.site_url + CALLBACK_PATH + PROVIDER.callback if config.site_url else None,
	)


class WaveAdapter:
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
		return WaveReader(config, client)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return oauth(config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		"""The business the approval opens (``businesses``): the one the connection already names, else the
		only one."""
		body = {
			"operationName": operations.BUSINESSES.name,
			"query": operations.BUSINESSES.document,
			"variables": {"page": 1, "pageSize": PAGE_SIZE},
		}
		response = transport.post(
			(config.endpoint_override or API_HOST) + API_PATH,
			{
				"Authorization": f"Bearer {grant.access_token}",
				"Accept": "application/json",
				"Content-Type": "application/json",
			},
			json.dumps(body).encode(),
			REQUEST_TIMEOUT_SECONDS,
		)
		if response.status != HTTP_OK:
			raise CredentialsRejected(f"Wave didn't list the businesses (HTTP {response.status}).")
		try:
			answer = Node(json.loads(response.body), "businesses")
		except ValueError, TypeError:
			raise SourceError("Wave returned an unreadable list of businesses.") from None
		data = answer.child("data")
		listed = data.child("businesses") if data else None
		if listed is None:
			raise CredentialsRejected("Wave didn't list the businesses the approval opens.")
		businesses = {
			node.string("id"): node.string("name")
			for edge in listed.children("edges")
			if (node := edge.child("node")) is not None
		}
		if config.account in businesses:
			return {}
		if len(businesses) == 1:
			return {"api_account": next(iter(businesses))}
		names = ", ".join(f"{name} ({id_})" for id_, name in businesses.items()) or "none"
		raise SourceError(
			f"Wave opened several businesses or none ({names}): enter the one to mirror as the account."
		)
