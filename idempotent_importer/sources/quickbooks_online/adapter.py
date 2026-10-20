# SPDX-License-Identifier: AGPL-3.0-or-later
"""QuickBooks Online, through the Accounting API: the chart of accounts, classes and departments, items
and their categories, customers, projects, vendors and employees, estimates, purchase orders, invoices,
sales receipts, credit memos and refund receipts, bills and vendor credits, payments and bill payments,
journal entries, deposits, transfers and expenses. The operator creates an app on developer.intuit.com with
this site's ``/api/method/idempotent_importer.api.oauth_callback`` as a redirect URI, saves its client ID
and secret on the connection, and connects the company with Connect to QuickBooks Online, which keeps the
company (realm) id as the account. Intuit's OAuth 2 (developer.intuit.com/app/developer/qbo/docs/develop/
authentication-and-authorization/oauth-2.0): HTTP Basic client authentication, an access token for an
hour, and a refresh token that may be replaced on any refresh (the newest is always kept)."""

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
from idempotent_importer.sources.quickbooks_online import mapping
from idempotent_importer.sources.quickbooks_online.reader import (
	ENTITIES,
	RATE_LIMIT,
	SOURCE_ID,
	QuickBooksReader,
	api_base,
)

GRANT_PATH = "/oauth2/v1/tokens/bearer"
# Intuit has no read-only scope: the accounting scope is the narrowest that reads the books.
PROVIDER = OAuthProvider(
	label="QuickBooks Online",
	authorize_url="https://appcenter.intuit.com/connect/oauth2",
	token_url="https://oauth.platform.intuit.com" + GRANT_PATH,
	scopes=("com.intuit.quickbooks.accounting",),
	client_auth=ClientAuth.BASIC,
	reconnect="connect the company again",
)
# The callback's query names the company the owner approved.
REALM_PARAMETER = "realmId"


def oauth(config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
	if not config.api_user or not config.api_secret:
		raise SourceError("QuickBooks Online is not configured: enter the app's client ID and secret.")
	if config.save_refresh_token is None:
		raise SourceError("QuickBooks Online needs somewhere to keep the refresh token Intuit replaces.")
	return OAuthClient(
		PROVIDER,
		OAuthApp(config.api_user, config.api_secret),
		transport,
		config.refresh_token,
		config.save_refresh_token,
		config.endpoint_override + GRANT_PATH if config.endpoint_override else None,
	)


class QuickBooksAdapter:
	id = SOURCE_ID
	label = "QuickBooks Online"
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return tuple(kind for kind in SYNC_ORDER if kind in ENTITIES)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		authorizer = BearerAuthorizer(oauth(config, transport))
		client = RestClient("QuickBooks Online", api_base(config), authorizer, transport, sleeper, RATE_LIMIT)
		return QuickBooksReader(config, client)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return oauth(config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		"""The approved company's realm id becomes the connection's account."""
		realm = params.get(REALM_PARAMETER)
		if not realm:
			raise SourceError("QuickBooks Online's approval named no company.")
		return {"api_account": realm}
