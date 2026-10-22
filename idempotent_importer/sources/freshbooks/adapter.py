# SPDX-License-Identifier: AGPL-3.0-or-later
"""FreshBooks, through its API: one business per connection. The operator creates an app in FreshBooks'
developer portal (my.freshbooks.com/#/developer) with the read scopes below and this site's
``/api/method/idempotent_importer.api.oauth_callback`` as its redirect URI, saves its client ID and secret on
the connection, and connects with Connect to FreshBooks, approving as a member of the business: the business
``users/me`` names (the one already entered as the account, when the user belongs to several) becomes the
connection's account.

FreshBooks' OAuth 2 (www.freshbooks.com/api/authentication): the authorization code grant at
auth.freshbooks.com, a JSON token request at api.freshbooks.com/auth/oauth/token with the client's ID and
secret and the redirect URI (on every refresh too), short-lived access tokens, and one-time refresh tokens:
each refresh issues a new one, which is kept before the access token is used."""

from __future__ import annotations

import json
from collections.abc import Mapping

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.errors import CredentialsRejected, SourceError
from idempotent_importer.core.http import HTTP_OK, HttpTransport, Sleeper
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
from idempotent_importer.core.rest import REQUEST_TIMEOUT_SECONDS, RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.freshbooks import mapping
from idempotent_importer.sources.freshbooks.reader import (
	ACCOUNT_SEPARATOR,
	API_HOST,
	KINDS,
	RATE_LIMIT,
	SOURCE_ID,
	Business,
	FreshBooksReader,
)

LABEL = "FreshBooks"
GRANT_PATH = "/auth/oauth/token"
IDENTITY_PATH = "/auth/api/v1/users/me"
# The read scopes of what the adapter reads (www.freshbooks.com/api/scopes).
SCOPES = (
	"user:profile:read",
	"user:clients:read",
	"user:invoices:read",
	"user:estimates:read",
	"user:payments:read",
	"user:expenses:read",
	"user:bills:read",
	"user:bill_vendors:read",
	"user:bill_payments:read",
	"user:billable_items:read",
	"user:projects:read",
	"user:time_entries:read",
	"user:taxes:read",
	"user:journal_entries:read",
	"user:other_income:read",
	"user:reports:read",
)
PROVIDER = OAuthProvider(
	label=LABEL,
	authorize_url="https://auth.freshbooks.com/oauth/authorize",
	token_url=API_HOST + GRANT_PATH,
	scopes=SCOPES,
	client_auth=ClientAuth.FORM,
	reconnect="connect the FreshBooks business again",
	json_token_requests=True,
	redirect_on_refresh=True,
)


def oauth(config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
	if not config.api_user or not config.api_secret:
		raise SourceError("FreshBooks is not configured: enter the app's client ID and secret.")
	if config.save_refresh_token is None:
		raise SourceError("FreshBooks needs somewhere to keep the refresh token it replaces.")
	return OAuthClient(
		PROVIDER,
		OAuthApp(config.api_user, config.api_secret),
		transport,
		config.refresh_token,
		config.save_refresh_token,
		config.endpoint_override + GRANT_PATH if config.endpoint_override else None,
		config.site_url + CALLBACK_PATH + PROVIDER.callback if config.site_url else None,
	)


class FreshBooksAdapter:
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
		return FreshBooksReader(config, client, sleeper)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return oauth(config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		"""The business the approving user is a member of (users/me): the one the connection already names,
		else their only one."""
		response = transport.get(
			(config.endpoint_override or API_HOST) + IDENTITY_PATH,
			{"Authorization": f"Bearer {grant.access_token}", "Accept": "application/json"},
			REQUEST_TIMEOUT_SECONDS,
		)
		if response.status != HTTP_OK:
			raise CredentialsRejected(f"FreshBooks didn't say who approved (HTTP {response.status}).")
		try:
			identity = json.loads(response.body)
		except ValueError:
			raise SourceError("FreshBooks returned an unreadable identity.") from None
		found = identity.get("response") if isinstance(identity, dict) else None
		memberships = found.get("business_memberships") if isinstance(found, dict) else None
		businesses = [
			Business(str(b["account_id"]), str(b["id"]), str(b["business_uuid"]))
			for m in (memberships if isinstance(memberships, list) else [])
			if isinstance(m, dict) and isinstance(b := m.get("business"), dict) and b.get("account_id")
		]
		named = config.account.split(ACCOUNT_SEPARATOR, 1)[0]
		chosen = next((b for b in businesses if b.account_id == named), None)
		if chosen is None and len(businesses) == 1:
			chosen = businesses[0]
		if chosen is None:
			names = ", ".join(b.account_id for b in businesses) or "none"
			raise SourceError(
				f"FreshBooks opened several businesses or none ({names}): enter the account id to mirror as the account."
			)
		return {} if chosen.account() == config.account else {"api_account": chosen.account()}
