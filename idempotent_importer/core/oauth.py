# SPDX-License-Identifier: AGPL-3.0-or-later
"""OAuth 2's authorization-code grant (RFC 6749 §4.1), shared by every source that connects that way
(Etsy, QuickBooks Online, Xero, Zoho): the URL where the account's owner approves read access, the code
exchange, and every refresh; and the client credentials grant (RFC 6749 §4.4) of a source whose app reads as
itself, which has no refresh token at all. A source that rotates its refresh token (a new one in every token
response) has it handed to the connection's store (``save``) before the access token is used, since the old
one stops working; one that keeps it (Zoho) sends none back, and the stored one is kept. The client
authenticates as the provider says: HTTP Basic (RFC 6749 §2.3.1), the secret in the form (Microsoft Entra
ID's, for Dynamics 365), a public client with PKCE (RFC 7636), or a JWT client assertion signed by the
app's certificate key (RFC 7523 §2.2, NetSuite's machine-to-machine flow). Tokens, keys and secrets never
appear in errors or reprs."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
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
CLIENT_CREDENTIALS = "client_credentials"
JWT_BEARER_ASSERTION = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
# How long a client assertion is valid: long enough for its token request, well inside providers' limits
# (NetSuite's: under an hour).
ASSERTION_LIFETIME_SECONDS = 300
CURVE_ALGORITHMS = {"secp256r1": "ES256", "secp384r1": "ES384", "secp521r1": "ES512"}
RSA_ALGORITHM = "PS256"
# A PEM block whose line breaks were lost (pasted into a single-line field): its label and its body.
PEM_ON_ONE_LINE = re.compile(r"-----BEGIN ([A-Z ]+)-----([A-Za-z0-9+/=\s]+)-----END \1-----")
PEM_LINE_LENGTH = 64
FORM_HEADERS = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
JSON_CONTENT_TYPE = "application/json"
# Where a source sends the owner back: this path, then the provider's ``callback`` method.
CALLBACK_PATH = "/api/method/idempotent_importer.api."
# Token-response fields that are credentials, never kept in a grant's ``extra``.
TOKEN_FIELDS = ("access_token", "refresh_token", "id_token")


def pkce_challenge(verifier: str) -> str:
	"""The S256 code challenge of a PKCE verifier (RFC 7636): base64url(sha256(verifier)), unpadded."""
	digest = hashlib.sha256(verifier.encode()).digest()
	return base64.urlsafe_b64encode(digest).decode().rstrip("=")


class ClientAuth(Enum):
	"""How the app proves itself at the token endpoint."""

	# client_id:client_secret as HTTP Basic (Intuit, Xero).
	BASIC = "basic"
	# client_id and client_secret in the form (Zoho, Microsoft Entra ID).
	FORM = "form"
	# client_id only, the code bound to a PKCE verifier (Etsy).
	PUBLIC = "public"
	# A short-lived JWT signed by the private key of a certificate mapped to the app (NetSuite).
	ASSERTION = "assertion"


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
	# A provider whose token endpoint takes its parameters as JSON rather than a form (Agendrix's).
	json_token_requests: bool = False
	# A provider that wants the redirect URI on every refresh too (Agendrix's).
	redirect_on_refresh: bool = False

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


@dataclass(frozen=True)
class SigningKey:
	"""A certificate's private key (PEM) and the id the provider gave the certificate (the JWT's ``kid``).
	It signs RSA-PSS or ECDSA as its type and size say: PS256 for RSA; ES256, ES384 or ES512 for P-256,
	P-384 or P-521."""

	key_id: str
	pem: str = field(repr=False)

	def text(self) -> str:
		"""The key as PEM text. A key pasted into a one-line field has lost its line breaks; they are put
		back (the body in lines of 64, RFC 7468 §2)."""
		pasted = PEM_ON_ONE_LINE.fullmatch(self.pem.strip())
		if pasted is None:
			return self.pem.strip() + "\n"
		label, body = pasted.group(1), "".join(pasted.group(2).split())
		lines = [body[start : start + PEM_LINE_LENGTH] for start in range(0, len(body), PEM_LINE_LENGTH)]
		return "\n".join((f"-----BEGIN {label}-----", *lines, f"-----END {label}-----")) + "\n"

	def algorithm(self) -> str:
		# Imported here: only a source that signs needs them, and the e2e fake store imports this module
		# under a Python that doesn't have them.
		from cryptography.hazmat.primitives.asymmetric import ec, rsa  # noqa: PLC0415
		from cryptography.hazmat.primitives.serialization import load_pem_private_key  # noqa: PLC0415

		try:
			key = load_pem_private_key(self.text().encode(), password=None)
		except ValueError, TypeError:
			raise SourceError(
				"The private key is not an unencrypted PEM key (paste the certificate's private key)."
			) from None
		if isinstance(key, rsa.RSAPrivateKey):
			return RSA_ALGORITHM
		if isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name in CURVE_ALGORITHMS:
			return CURVE_ALGORITHMS[key.curve.name]
		raise SourceError("The private key must be RSA, or EC on P-256, P-384 or P-521.")

	def sign(self, claims: Mapping[str, str | int]) -> str:
		import jwt  # noqa: PLC0415 - see algorithm

		return jwt.encode(
			dict(claims), self.text(), algorithm=self.algorithm(), headers={"typ": "JWT", "kid": self.key_id}
		)

	def __repr__(self) -> str:
		return f"SigningKey({self.key_id}, [redacted])"


def token_response(
	transport: HttpTransport,
	url: str,
	headers: Mapping[str, str],
	form: Mapping[str, str],
	label: str,
	reconnect: str,
	as_json: bool = False,
) -> dict[str, JsonValue]:
	"""A token endpoint's answer to ``form`` (sent as JSON when ``as_json``): rate limiting and server errors
	pause the run, any other refusal (an HTTP error, or a 200 carrying an ``error``) rejects the credentials."""
	if as_json:
		headers = {**headers, "Content-Type": JSON_CONTENT_TYPE}
		body = json.dumps(dict(form), separators=(",", ":")).encode()
	else:
		body = urlencode(form).encode()
	response = transport.post(url, dict(headers), body, TOKEN_REQUEST_TIMEOUT_SECONDS)
	if response.status == HTTP_TOO_MANY_REQUESTS or response.status >= HTTP_SERVER_ERROR:
		raise RetryLater(
			RetryPolicy.wait(response, FIRST_ATTEMPT) or RetryPolicy.backoff(FIRST_ATTEMPT),
			f"{label} could not issue an access token (HTTP {response.status}).",
		)
	if response.status != HTTP_OK:
		raise CredentialsRejected(f"{label} refused the token request (HTTP {response.status}); {reconnect}.")
	try:
		decoded = json.loads(response.body)
	except ValueError:
		raise SourceError(f"{label} returned an unreadable token response.") from None
	document: dict[str, JsonValue] = decoded if isinstance(decoded, dict) else {}
	if isinstance(document.get("error"), str):
		# Zoho answers a refused grant with 200 and an error code.
		raise CredentialsRejected(f"{label} refused the token request ({document['error']}); {reconnect}.")
	if not isinstance(document.get("access_token"), str) or not document["access_token"]:
		raise SourceError(f"{label} returned a token response without tokens.")
	return document


def authenticated(
	client_auth: ClientAuth, app: OAuthApp, form: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, str]]:
	"""The headers and form of a token request whose client proves itself by ``client_auth`` with a shared
	secret or none (an assertion is added by its grant, which holds the key)."""
	headers, body = dict(FORM_HEADERS), dict(form)
	match client_auth:
		case ClientAuth.BASIC:
			pair = f"{app.client_id}:{app.client_secret}".encode()
			headers["Authorization"] = "Basic " + base64.b64encode(pair).decode()
		case ClientAuth.FORM:
			body |= {"client_id": app.client_id, "client_secret": app.client_secret}
		case ClientAuth.PUBLIC:
			body |= {"client_id": app.client_id}
		case ClientAuth.ASSERTION:
			pass
	return headers, body


class OAuthClient:
	def __init__(
		self,
		provider: OAuthProvider,
		app: OAuthApp,
		transport: HttpTransport,
		refresh_token: str,
		save: Callable[[str], None],
		token_url: str | None = None,
		redirect_uri: str | None = None,
	) -> None:
		self.provider = provider
		# The callback registered with the app, for a provider that wants it on every refresh.
		self._redirect_uri = redirect_uri
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
		form = {"grant_type": "refresh_token", "refresh_token": self._refresh_token}
		if self.provider.redirect_on_refresh:
			if not self._redirect_uri:
				raise SourceError(f"{self.provider.label} needs the app's redirect URI to refresh its token.")
			form["redirect_uri"] = self._redirect_uri
		return self.token(form, needs_refresh_token=self.provider.rotates_refresh_token)

	def token(self, form: dict[str, str], needs_refresh_token: bool) -> TokenGrant:
		"""One token request of ``form``'s grant, the client authenticated as the provider says."""
		headers, body = authenticated(self.provider.client_auth, self._app, form)
		label = self.provider.label
		document = token_response(
			self._transport,
			self._token_url,
			headers,
			body,
			label,
			self.provider.reconnect,
			self.provider.json_token_requests,
		)
		refresh = document.get("refresh_token")
		if isinstance(refresh, str) and refresh:
			self._save(refresh)
			self._refresh_token = refresh
		elif needs_refresh_token:
			raise SourceError(f"{label} returned a token response without tokens.")
		extra = {key: value for key, value in document.items() if key not in TOKEN_FIELDS}
		return TokenGrant(str(document["access_token"]), extra)

	def __repr__(self) -> str:
		return f"OAuthClient({self.provider.label}, [redacted])"


class AccessTokens(Protocol):
	"""Whatever issues a source's access tokens: an authorization-code client refreshing its grant, or a
	client credentials grant."""

	def refresh(self) -> str: ...


@dataclass(frozen=True)
class ClientCredentialsProvider:
	"""One source's token endpoint for the client credentials grant, the scopes its app asks for (e.g. a
	Dynamics 365 resource's ``<resource>/.default``), how the app authenticates, and what the operator is
	told when the grant is refused. An assertion's audience is the token endpoint's canonical URL
	(``audience``, when the endpoint is reached at another address)."""

	label: str
	token_url: str
	scopes: tuple[str, ...]
	reconnect: str
	client_auth: ClientAuth = ClientAuth.FORM
	audience: str | None = None


class ClientCredentials:
	"""The client credentials grant: the app's own access token, for no user in particular, fetched again
	whenever it lapses (there is no refresh token, and nothing to save). The app proves itself with its
	secret, or (``ClientAuth.ASSERTION``) with a JWT its certificate's ``key`` signs, whose ``scope`` claim
	carries the scopes."""

	def __init__(
		self,
		provider: ClientCredentialsProvider,
		app: OAuthApp,
		transport: HttpTransport,
		key: SigningKey | None = None,
		clock: Callable[[], float] = time.time,
	) -> None:
		if provider.client_auth is ClientAuth.ASSERTION:
			if not app.client_id or key is None or not key.key_id or not key.pem:
				raise SourceError(
					f"{provider.label} is not configured: enter the client ID, the certificate ID and its private key."
				)
		elif not app.client_id or not app.client_secret:
			raise SourceError(
				f"{provider.label} is not configured: enter the app's client ID and client secret."
			)
		self._provider = provider
		self._app = app
		self._transport = transport
		self._key = key
		self._clock = clock

	def assertion(self) -> str:
		"""The signed client assertion (RFC 7523 §3): the app is its issuer, the token endpoint its audience."""
		if self._key is None:
			raise SourceError(f"{self._provider.label} has no certificate key to sign with.")
		issued = int(self._clock())
		return self._key.sign(
			{
				"iss": self._app.client_id,
				"scope": " ".join(self._provider.scopes),
				"aud": self._provider.audience or self._provider.token_url,
				"iat": issued,
				"exp": issued + ASSERTION_LIFETIME_SECONDS,
			}
		)

	def refresh(self) -> str:
		form = {"grant_type": CLIENT_CREDENTIALS}
		if self._provider.client_auth is ClientAuth.ASSERTION:
			form |= {"client_assertion_type": JWT_BEARER_ASSERTION, "client_assertion": self.assertion()}
		else:
			form["scope"] = " ".join(self._provider.scopes)
		headers, body = authenticated(self._provider.client_auth, self._app, form)
		provider = self._provider
		document = token_response(
			self._transport, provider.token_url, headers, body, provider.label, provider.reconnect
		)
		return str(document["access_token"])

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
