# SPDX-License-Identifier: AGPL-3.0-or-later
"""OAuth 2's authorization-code grant (RFC 6749 §4.1), shared by every source that connects that way
(Etsy, QuickBooks Online, Xero, Zoho): the URL where the account's owner approves read access, the code
exchange, and every refresh; and the client credentials grant (RFC 6749 §4.4) of a source whose app reads as
itself (Microsoft Entra ID's, for Dynamics 365), which has no refresh token at all. A source that rotates its refresh token (a new one in every token response)
has it handed to the connection's store (``save``) before the access token is used, since the old one stops
working; one that keeps it (Zoho) sends none back, and the stored one is kept. The client authenticates as
the provider says: HTTP Basic (RFC 6749 §2.3.1), the secret in the form, or a public client with PKCE
(RFC 7636). Tokens and secrets never appear in errors or reprs."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol
from urllib.parse import urlencode

from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError
from idempotent_importer.core.http import (
	HTTP_OK,
	HTTP_SERVER_ERROR,
	HTTP_TOO_MANY_REQUESTS,
	HttpTransport,
	RetryPolicy,
)
from idempotent_importer.core.node import JsonValue

TOKEN_REQUEST_TIMEOUT_SECONDS = 30.0
PKCE_METHOD = "S256"
FIRST_ATTEMPT = 1
BEARER = "Bearer"


def pkce_challenge(verifier: str) -> str:
	"""The S256 code challenge of a PKCE verifier (RFC 7636): base64url(sha256(verifier)), unpadded."""
	digest = hashlib.sha256(verifier.encode()).digest()
	return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class ClientAuth(Enum):
	"""How the app proves itself at the token endpoint."""

	# client_id:client_secret as HTTP Basic (Intuit, Xero).
	BASIC = "basic"
	# client_id and client_secret in the form (Zoho).
	FORM = "form"
	# client_id only, the code bound to a PKCE verifier (Etsy).
	PUBLIC = "public"


@dataclass(frozen=True)
class OAuthProvider:
	"""One source's documented OAuth 2 endpoints and the read-only scopes the importer asks for.
	``reconnect`` is what the operator is told when the source refuses a token request ("connect the shop
	again"); ``authorize_params`` are extra query parameters the approval URL needs (Zoho's offline access)."""

	label: str
	authorize_url: str
	token_url: str
	scopes: tuple[str, ...]
	client_auth: ClientAuth
	reconnect: str
	rotates_refresh_token: bool = True
	scope_separator: str = " "
	authorize_params: tuple[tuple[str, str], ...] = ()
	# The desk method the source sends the owner back to (the redirect URI registered with the app).
	callback: str = "oauth_callback"

	@property
	def pkce(self) -> bool:
		return self.client_auth is ClientAuth.PUBLIC


@dataclass(frozen=True)
class OAuthApp:
	"""The app registered with the source: its client id and secret (the secret unused by a public client)."""

	client_id: str
	client_secret: str = field(repr=False)


@dataclass(frozen=True)
class TokenGrant:
	"""What a token response granted. ``extra`` holds the response's other fields a source documents (Zoho's
	``api_domain``); never a token."""

	access_token: str = field(repr=False)
	extra: Mapping[str, JsonValue] = field(default_factory=dict)


class OAuthClient:
	def __init__(
		self,
		provider: OAuthProvider,
		app: OAuthApp,
		transport: HttpTransport,
		refresh_token: str,
		save: Callable[[str], None],
		token_url: str | None = None,
	) -> None:
		self.provider = provider
		self._app = app
		self._transport = transport
		self._refresh_token = refresh_token
		self._save = save
		self._token_url = token_url or provider.token_url

	def authorization_url(self, redirect_uri: str, state: str, verifier: str) -> str:
		"""Where the account's owner approves read access; the source sends them back to ``redirect_uri``."""
		query = {
			"response_type": "code",
			"client_id": self._app.client_id,
			"redirect_uri": redirect_uri,
			"scope": self.provider.scope_separator.join(self.provider.scopes),
			"state": state,
			**dict(self.provider.authorize_params),
		}
		if self.provider.pkce:
			query |= {"code_challenge": pkce_challenge(verifier), "code_challenge_method": PKCE_METHOD}
		return f"{self.provider.authorize_url}?{urlencode(query)}"

	def exchange(self, code: str, verifier: str, redirect_uri: str) -> TokenGrant:
		"""Completes the connect flow: trades the authorization code for tokens, keeping the refresh token."""
		form = {"grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": code}
		if self.provider.pkce:
			form["code_verifier"] = verifier
		return self.token(form, needs_refresh_token=True)

	def refresh(self) -> str:
		"""A new access token from the stored refresh token (which a rotating source replaces)."""
		return self.refresh_grant().access_token

	def refresh_grant(self) -> TokenGrant:
		if not self._refresh_token:
			raise CredentialsRejected(
				f"{self.provider.label} is not connected: use Connect on the connection."
			)
		return self.token(
			{"grant_type": "refresh_token", "refresh_token": self._refresh_token},
			needs_refresh_token=self.provider.rotates_refresh_token,
		)

	def token(self, form: dict[str, str], needs_refresh_token: bool) -> TokenGrant:
		"""One token request of ``form``'s grant, the client authenticated as the provider says."""
		headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
		match self.provider.client_auth:
			case ClientAuth.BASIC:
				pair = f"{self._app.client_id}:{self._app.client_secret}".encode()
				headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
			case ClientAuth.FORM:
				form |= {"client_id": self._app.client_id, "client_secret": self._app.client_secret}
			case ClientAuth.PUBLIC:
				form |= {"client_id": self._app.client_id}
		response = self._transport.post(
			self._token_url, headers, urlencode(form).encode(), TOKEN_REQUEST_TIMEOUT_SECONDS
		)
		label = self.provider.label
		if response.status == HTTP_TOO_MANY_REQUESTS or response.status >= HTTP_SERVER_ERROR:
			raise RetryLater(
				RetryPolicy.wait(response, FIRST_ATTEMPT) or RetryPolicy.backoff(FIRST_ATTEMPT),
				f"{label} could not issue an access token (HTTP {response.status}).",
			)
		if response.status != HTTP_OK:
			raise CredentialsRejected(
				f"{label} refused the token request (HTTP {response.status}); {self.provider.reconnect}."
			)
		try:
			decoded = json.loads(response.body)
		except ValueError:
			raise SourceError(f"{label} returned an unreadable token response.") from None
		document: dict[str, JsonValue] = decoded if isinstance(decoded, dict) else {}
		if isinstance(document.get("error"), str):
			# Zoho answers a refused grant with 200 and an error code.
			raise CredentialsRejected(
				f"{label} refused the token request ({document['error']}); {self.provider.reconnect}."
			)
		access, refresh = document.get("access_token"), document.get("refresh_token")
		if not isinstance(access, str) or not access:
			raise SourceError(f"{label} returned a token response without tokens.")
		if isinstance(refresh, str) and refresh:
			self._save(refresh)
			self._refresh_token = refresh
		elif needs_refresh_token:
			raise SourceError(f"{label} returned a token response without tokens.")
		extra = {
			key: value
			for key, value in document.items()
			if key not in ("access_token", "refresh_token", "id_token")
		}
		return TokenGrant(access, extra)

	def __repr__(self) -> str:
		return f"OAuthClient({self.provider.label}, [redacted])"


class AccessTokens(Protocol):
	"""Where a BearerAuthorizer gets a fresh access token."""

	def refresh(self) -> str: ...


@dataclass(frozen=True)
class ClientCredentialsProvider:
	"""One source's token endpoint for the client credentials grant, the scope its app asks for (e.g. a
	Dynamics 365 resource's ``<resource>/.default``), how the app authenticates, and what the operator is
	told when the grant is refused."""

	label: str
	token_url: str
	scopes: tuple[str, ...]
	reconnect: str
	client_auth: ClientAuth = ClientAuth.FORM


class ClientCredentials:
	"""The client credentials grant: the app's own access token, fetched again whenever it lapses (there is
	no refresh token, and nothing to save)."""

	def __init__(self, provider: ClientCredentialsProvider, app: OAuthApp, transport: HttpTransport) -> None:
		if not app.client_id or not app.client_secret:
			raise SourceError(
				f"{provider.label} is not configured: enter the app's client ID and client secret."
			)
		self._provider = provider
		self._app = app
		self._transport = transport

	def refresh(self) -> str:
		oauth = OAuthClient(
			OAuthProvider(
				self._provider.label,
				"",
				self._provider.token_url,
				self._provider.scopes,
				self._provider.client_auth,
				self._provider.reconnect,
				rotates_refresh_token=False,
			),
			self._app,
			self._transport,
			"",
			lambda _token: None,
		)
		form = {"grant_type": "client_credentials", "scope": " ".join(self._provider.scopes)}
		return oauth.token(form, needs_refresh_token=False).access_token

	def __repr__(self) -> str:
		return f"ClientCredentials({self._provider.label}, [redacted])"


class BearerAuthorizer:
	"""``core.rest.RequestAuthorizer`` for an OAuth source: the access token (fetched on first use, and again
	whenever the source rejects it) under the source's scheme ("Bearer", Zoho's "Zoho-oauthtoken"), and any
	headers the source needs on every request (Xero's tenant)."""

	def __init__(
		self, oauth: AccessTokens, scheme: str = BEARER, headers: Mapping[str, str] | None = None
	) -> None:
		self._oauth = oauth
		self._scheme = scheme
		self._headers = dict(headers or {})
		self._access_token: str | None = None

	def headers(self) -> dict[str, str]:
		return {**self._headers, "Authorization": f"{self._scheme} {self.token()}"}

	def renew(self) -> bool:
		self._access_token = None
		return True

	def token(self) -> str:
		if self._access_token is None:
			self._access_token = self._oauth.refresh()
		return self._access_token

	def __repr__(self) -> str:
		return "BearerAuthorizer([redacted])"
