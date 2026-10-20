# SPDX-License-Identifier: AGPL-3.0-or-later
"""Etsy's OAuth 2: the authorization-code flow with PKCE (S256) through the shared ``core.oauth`` client, as
a public client (the keystring alone at the token endpoint), with a new refresh token in every token
response (developers.etsy.com/documentation/essentials/authentication). Requests are made as the shop
owner: the app's API key (``keystring:shared secret``, as the spec's api_key scheme requires) on every
request and a bearer token. Tokens and secrets never appear in errors or reprs."""

from __future__ import annotations

from dataclasses import dataclass, field

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.oauth import BearerAuthorizer, ClientAuth, OAuthApp, OAuthClient, OAuthProvider

API_BASE = "https://openapi.etsy.com"
GRANT_PATH = "/v3/public/oauth/token"
AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
# Read-only scopes: listings, receipts and payments, private shop data, buyer emails.
SCOPES = ("listings_r", "transactions_r", "shops_r", "email_r")
# Etsy access tokens are "<user id>.<token>".
USER_ID_SEPARATOR = "."
PROVIDER = OAuthProvider(
	label="Etsy",
	authorize_url=AUTHORIZE_URL,
	token_url=API_BASE + GRANT_PATH,
	scopes=SCOPES,
	client_auth=ClientAuth.PUBLIC,
	reconnect="connect the shop again",
	# The callback Etsy apps were registered with before the other OAuth sources shared one.
	callback="etsy_callback",
)


@dataclass(frozen=True)
class EtsyApp:
	"""The registered Etsy app (keystring and shared secret) and where its API answers."""

	keystring: str
	shared_secret: str = field(repr=False)
	api_base: str = API_BASE

	def __post_init__(self) -> None:
		if not self.keystring or not self.shared_secret:
			raise SourceError("Etsy is not configured: enter the app keystring and shared secret.")

	def api_key(self) -> str:
		return f"{self.keystring}:{self.shared_secret}"

	def token_url(self) -> str:
		return self.api_base.rstrip("/") + GRANT_PATH

	def oauth_app(self) -> OAuthApp:
		return OAuthApp(self.keystring, self.shared_secret)


class EtsyAuthorizer(BearerAuthorizer):
	"""The API key and the shop owner's bearer token on every request; the owner's user id from the token."""

	def __init__(self, app: EtsyApp, oauth: OAuthClient) -> None:
		super().__init__(oauth, headers={"x-api-key": app.api_key()})

	def user_id(self) -> int:
		"""The connected shop owner's Etsy user id, from the access token."""
		token = self.token()
		prefix = token.split(USER_ID_SEPARATOR, 1)[0]
		if USER_ID_SEPARATOR not in token or not prefix.isdigit():
			raise SourceError("Etsy returned an access token without a user id.")
		return int(prefix)

	def __repr__(self) -> str:
		return "EtsyAuthorizer([redacted])"
