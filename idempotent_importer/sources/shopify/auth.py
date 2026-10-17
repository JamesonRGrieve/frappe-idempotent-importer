# SPDX-License-Identifier: AGPL-3.0-or-later
"""How the Shopify client authenticates (``core.rest.RequestAuthorizer``). Shopify stopped letting merchants
create custom apps in the store admin on 2026-01-01; the Dev Dashboard that replaced it gives a store's own
app a client ID and secret, exchanged for an Admin API token that lasts 24 hours. A legacy custom app's
permanent token still works. Ported from wp-woo-idempotent-importer (ClientCredentialsAuthorizer,
ShopifyTokenAuthorizer). Neither ever renders a secret."""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode

from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError
from idempotent_importer.core.http import (
	HTTP_OK,
	HTTP_SERVER_ERROR,
	HTTP_TOO_MANY_REQUESTS,
	HttpTransport,
	RetryPolicy,
)

ACCESS_HEADER = "X-Shopify-Access-Token"
TOKEN_REQUEST_TIMEOUT_SECONDS = 30.0
GRANT_TYPE = "client_credentials"
FIRST_ATTEMPT = 1
# Shopify's OAuth error codes are snake_case words (e.g. app_not_installed); anything else isn't echoed.
ERROR_CODE = re.compile(r"^[a-z_]{1,64}$")


class ShopifyTokenAuthorizer:
	"""A legacy custom app's permanent Admin API access token. Nothing to renew: a rejected token stops the
	run."""

	def __init__(self, access_token: str) -> None:
		if not access_token:
			raise SourceError("Shopify is not configured: enter the Admin API access token.")
		self._token = access_token

	def headers(self) -> dict[str, str]:
		return {ACCESS_HEADER: self._token}

	def renew(self) -> bool:
		return False

	def __repr__(self) -> str:
		return "ShopifyTokenAuthorizer([redacted])"


class ClientCredentialsAuthorizer:
	"""A Dev Dashboard app installed on the store: its client ID and secret are exchanged for an Admin API
	token (the client credentials grant) on first use, and again whenever Shopify rejects the token. Shopify
	grants this only to apps of the store's own organization
	(shopify.dev/docs/apps/build/dev-dashboard/create-apps-using-dev-dashboard). A refused grant (a wrong
	secret, the app not installed) is CredentialsRejected, so the run stops instead of retrying; a busy or
	failing token endpoint is RetryLater."""

	def __init__(self, token_url: str, client_id: str, client_secret: str, transport: HttpTransport) -> None:
		if not client_id or not client_secret:
			raise SourceError("Shopify is not configured: enter the app's client ID and client secret.")
		self._token_url = token_url
		self._client_id = client_id
		self._client_secret = client_secret
		self._transport = transport
		self._token: str | None = None

	def headers(self) -> dict[str, str]:
		if self._token is None:
			self._token = self._grant()
		return {ACCESS_HEADER: self._token}

	def renew(self) -> bool:
		self._token = None
		return True

	def _grant(self) -> str:
		body = urlencode(
			{"grant_type": GRANT_TYPE, "client_id": self._client_id, "client_secret": self._client_secret}
		).encode()
		response = self._transport.post(
			self._token_url,
			{"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
			body,
			TOKEN_REQUEST_TIMEOUT_SECONDS,
		)
		if response.status == HTTP_TOO_MANY_REQUESTS or response.status >= HTTP_SERVER_ERROR:
			raise RetryLater(
				RetryPolicy.wait(response, FIRST_ATTEMPT) or RetryPolicy.backoff(FIRST_ATTEMPT),
				f"Shopify could not issue an access token (HTTP {response.status}).",
			)
		try:
			decoded = json.loads(response.body)
		except ValueError:
			decoded = None
		document = decoded if isinstance(decoded, dict) else {}
		if response.status != HTTP_OK:
			error = document.get("error")
			code = f": {error}" if isinstance(error, str) and ERROR_CODE.match(error) else ""
			raise CredentialsRejected(
				f"Shopify refused to issue an access token (HTTP {response.status}{code}). Check the app's "
				"client ID and secret, and that the app is installed on the store."
			)
		token = document.get("access_token")
		if not isinstance(token, str) or not token:
			raise SourceError("Shopify issued no access token.")
		return token

	def __repr__(self) -> str:
		return "ClientCredentialsAuthorizer([redacted])"
