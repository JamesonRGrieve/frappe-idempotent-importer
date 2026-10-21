# SPDX-License-Identifier: AGPL-3.0-or-later
"""HighLevel (GoHighLevel), through API v2: one sub-account (location) per connection. The operator creates
an app in HighLevel's Marketplace (marketplace.gohighlevel.com, a developer account), of the Sub-Account
type, with the read scopes below and this site's ``/api/method/idempotent_importer.api.oauth_callback`` as a
redirect URI, saves its client ID and secret on the connection, and connects with Connect, choosing the
sub-account: the token's ``locationId`` becomes the connection's account.

HighLevel's OAuth 2 (docs/oauth/Authorization.md and Faqs.md in GoHighLevel/highlevel-api-docs, and its
OAuth document's token request): the authorization code grant from the Marketplace's location chooser, the
client's ID and secret in the token form, an access token for a day, and a refresh token replaced on every
refresh (the newest is always kept)."""

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
from idempotent_importer.sources.highlevel import mapping
from idempotent_importer.sources.highlevel.reader import (
	API_HOST,
	KINDS,
	RATE_LIMIT,
	SOURCE_ID,
	HighLevelReader,
)

LABEL = "HighLevel"
GRANT_PATH = "/oauth/token"
# The read scopes of what the adapter reads (docs/oauth/Scopes.md).
SCOPES = (
	"users.readonly",
	"products.readonly",
	"products/prices.readonly",
	"contacts.readonly",
	"businesses.readonly",
	"opportunities.readonly",
	"conversations.readonly",
	"conversations/message.readonly",
	"invoices.readonly",
	"payments/transactions.readonly",
)
PROVIDER = OAuthProvider(
	label=LABEL,
	authorize_url="https://marketplace.gohighlevel.com/v2/oauth/chooselocation",
	token_url=API_HOST + GRANT_PATH,
	scopes=SCOPES,
	client_auth=ClientAuth.FORM,
	reconnect="connect the sub-account again",
)
LOCATION = "locationId"


def oauth(config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
	if not config.api_user or not config.api_secret:
		raise SourceError("HighLevel is not configured: enter the app's client ID and secret.")
	if config.save_refresh_token is None:
		raise SourceError("HighLevel needs somewhere to keep the refresh token it replaces.")
	return OAuthClient(
		PROVIDER,
		OAuthApp(config.api_user, config.api_secret),
		transport,
		config.refresh_token,
		config.save_refresh_token,
		config.endpoint_override + GRANT_PATH if config.endpoint_override else None,
	)


class HighLevelAdapter:
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
		return HighLevelReader(config, client, sleeper)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return oauth(config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		"""The sub-account the token was issued for becomes the connection's account."""
		location = grant.extra.get(LOCATION)
		if not isinstance(location, str) or not location:
			raise SourceError("HighLevel's approval named no sub-account: install the app on a sub-account.")
		return {"api_account": location}
