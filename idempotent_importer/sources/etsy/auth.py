# SPDX-License-Identifier: AGPL-3.0-or-later
"""Etsy's OAuth 2 authorization-code flow with PKCE (S256), ported from wp-woo-idempotent-importer's
Etsy\\OAuth: the connect URL, the code exchange and the refresh. Every token response carries a new
refresh token, which is handed to the connection's store (``save``) before the access token is used, since
the old one stops working. Requests are made as the shop owner: the app's API key (``keystring:shared
secret``, as the spec's api_key scheme requires) on every request and a bearer token, fetched on first use
and again whenever Etsy rejects it. Tokens and secrets never appear in errors or reprs."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError
from idempotent_importer.core.http import (
	HTTP_OK,
	HTTP_SERVER_ERROR,
	HTTP_TOO_MANY_REQUESTS,
	HttpTransport,
	RetryPolicy,
)

API_BASE = "https://openapi.etsy.com"
GRANT_PATH = "/v3/public/oauth/token"
AUTHORIZE_URL = "https://www.etsy.com/oauth/connect"
# Read-only scopes: listings, receipts and payments, private shop data, buyer emails.
SCOPES = ("listings_r", "transactions_r", "shops_r", "email_r")
TOKEN_REQUEST_TIMEOUT_SECONDS = 30.0
PKCE_METHOD = "S256"
FIRST_ATTEMPT = 1
# Etsy access tokens are "<user id>.<token>".
USER_ID_SEPARATOR = "."


def pkce_challenge(verifier: str) -> str:
	"""The S256 code challenge of a PKCE verifier (RFC 7636): base64url(sha256(verifier)), unpadded."""
	digest = hashlib.sha256(verifier.encode()).digest()
	return base64.urlsafe_b64encode(digest).decode().rstrip("=")


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


class EtsyOAuth:
	def __init__(
		self, app: EtsyApp, transport: HttpTransport, refresh_token: str, save: Callable[[str], None]
	) -> None:
		self._app = app
		self._transport = transport
		self._refresh_token = refresh_token
		self._save = save

	def authorization_url(self, redirect_uri: str, state: str, verifier: str) -> str:
		query = {
			"response_type": "code",
			"client_id": self._app.keystring,
			"redirect_uri": redirect_uri,
			"scope": " ".join(SCOPES),
			"state": state,
			"code_challenge": pkce_challenge(verifier),
			"code_challenge_method": PKCE_METHOD,
		}
		return f"{AUTHORIZE_URL}?{urlencode(query)}"

	def exchange(self, code: str, verifier: str, redirect_uri: str) -> str:
		"""Completes the connect flow: trades the authorization code for tokens, keeping the refresh token."""
		return self._token(
			{
				"grant_type": "authorization_code",
				"client_id": self._app.keystring,
				"redirect_uri": redirect_uri,
				"code": code,
				"code_verifier": verifier,
			}
		)

	def refresh(self) -> str:
		"""A new access token from the stored refresh token (which the response replaces)."""
		if not self._refresh_token:
			raise CredentialsRejected("Etsy is not connected: use Connect to Etsy on the connection.")
		return self._token(
			{
				"grant_type": "refresh_token",
				"client_id": self._app.keystring,
				"refresh_token": self._refresh_token,
			}
		)

	def _token(self, form: dict[str, str]) -> str:
		response = self._transport.post(
			self._app.token_url(),
			{"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
			urlencode(form).encode(),
			TOKEN_REQUEST_TIMEOUT_SECONDS,
		)
		if response.status == HTTP_TOO_MANY_REQUESTS or response.status >= HTTP_SERVER_ERROR:
			raise RetryLater(
				RetryPolicy.wait(response, FIRST_ATTEMPT) or RetryPolicy.backoff(FIRST_ATTEMPT),
				f"Etsy could not issue an access token (HTTP {response.status}).",
			)
		if response.status != HTTP_OK:
			raise CredentialsRejected(
				f"Etsy refused the token request (HTTP {response.status}); connect the shop again."
			)
		try:
			decoded = json.loads(response.body)
		except ValueError:
			raise SourceError("Etsy returned an unreadable token response.") from None
		document = decoded if isinstance(decoded, dict) else {}
		access, refresh = document.get("access_token"), document.get("refresh_token")
		if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
			raise SourceError("Etsy returned a token response without tokens.")
		self._save(refresh)
		self._refresh_token = refresh
		return access

	def __repr__(self) -> str:
		return "EtsyOAuth([redacted])"


class EtsyAuthorizer:
	"""``core.rest.RequestAuthorizer`` for Etsy: the API key, and the shop owner's bearer token."""

	def __init__(self, app: EtsyApp, oauth: EtsyOAuth) -> None:
		self._app = app
		self._oauth = oauth
		self._access_token: str | None = None

	def headers(self) -> dict[str, str]:
		return {"x-api-key": self._app.api_key(), "Authorization": f"Bearer {self._token()}"}

	def renew(self) -> bool:
		self._access_token = None
		return True

	def user_id(self) -> int:
		"""The connected shop owner's Etsy user id, from the access token."""
		prefix = self._token().split(USER_ID_SEPARATOR, 1)[0]
		if USER_ID_SEPARATOR not in self._token() or not prefix.isdigit():
			raise SourceError("Etsy returned an access token without a user id.")
		return int(prefix)

	def _token(self) -> str:
		if self._access_token is None:
			self._access_token = self._oauth.refresh()
		return self._access_token

	def __repr__(self) -> str:
		return "EtsyAuthorizer([redacted])"
