# SPDX-License-Identifier: AGPL-3.0-or-later
"""HubSpot, through the CRM API v3: one account per connection. The operator creates an app in a HubSpot
developer account with the read scopes below and this site's
``/api/method/idempotent_importer.api.oauth_callback`` as its redirect URL, saves the app's client ID and
secret on the connection, and connects with Connect, choosing the account (the app is installed on it).

HubSpot's OAuth 2 (developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/oauth/
working-with-oauth; the authorization code flow and token URL the v3 OpenAPI documents declare): the
authorization code grant at app.hubspot.com, the client's ID and secret in the token form, an access token
for half an hour, and a refresh token that stays the same (kept as issued)."""

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
from idempotent_importer.sources.hubspot import mapping
from idempotent_importer.sources.hubspot.reader import (
	API_HOST,
	KINDS,
	RATE_LIMIT,
	SEARCH_RATE_LIMIT,
	SOURCE_ID,
	HubSpotReader,
)

LABEL = "HubSpot"
GRANT_PATH = "/oauth/v1/token"
# The read scopes of what the adapter reads (the v3 OpenAPI documents' oauth2 scopes).
SCOPES = (
	"oauth",
	"crm.objects.owners.read",
	"crm.objects.contacts.read",
	"crm.objects.companies.read",
	"crm.objects.deals.read",
	"crm.objects.calls.read",
	"crm.objects.emails.read",
	"crm.objects.meetings.read",
	"crm.objects.notes.read",
	"crm.objects.tasks.read",
	"crm.objects.products.read",
	"crm.objects.line_items.read",
	"crm.objects.quotes.read",
	"crm.objects.orders.read",
	"crm.objects.invoices.read",
	"crm.objects.commercepayments.read",
)
PROVIDER = OAuthProvider(
	label=LABEL,
	authorize_url="https://app.hubspot.com/oauth/authorize",
	token_url=API_HOST + GRANT_PATH,
	scopes=SCOPES,
	client_auth=ClientAuth.FORM,
	reconnect="connect the HubSpot account again",
	rotates_refresh_token=False,
)


def oauth(config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
	if not config.api_user or not config.api_secret:
		raise SourceError("HubSpot is not configured: enter the app's client ID and secret.")
	if config.save_refresh_token is None:
		raise SourceError("HubSpot needs somewhere to keep its refresh token.")
	return OAuthClient(
		PROVIDER,
		OAuthApp(config.api_user, config.api_secret),
		transport,
		config.refresh_token,
		config.save_refresh_token,
		config.endpoint_override + GRANT_PATH if config.endpoint_override else None,
	)


class HubSpotAdapter:
	id = SOURCE_ID
	label = LABEL
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return tuple(kind for kind in SYNC_ORDER if kind in KINDS)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		authorizer = BearerAuthorizer(oauth(config, transport))
		host = config.endpoint_override or API_HOST
		client = RestClient(LABEL, host, authorizer, transport, sleeper, RATE_LIMIT)
		search = RestClient(LABEL, host, authorizer, transport, sleeper, SEARCH_RATE_LIMIT)
		return HubSpotReader(config, client, search)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return oauth(config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		"""The token is for the account the owner chose; nothing more to settle."""
		return {}
