# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-process stand-ins for the network and the clock: a transport that answers from the recorded
fixtures (or a script of responses) and a sleeper that records waits instead of sleeping."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs

from idempotent_importer.core.http import HttpResponse
from tests.support.dynamics import ENTRA_CLIENT_SECRET, ENTRA_TOKEN
from tests.support.fixtures import Answer, RestResponder, ShopifyResponder

ETSY_TOKEN_PATH = "/v3/public/oauth/token"
ENTRA_GRANT_PATH = "/oauth2/v2.0/token"
# The fixtures' shop owner (user 7001), as Etsy's access tokens name their user.
ETSY_ACCESS_TOKEN = "7001.fixture-access-token"
ETSY_ROTATED_REFRESH_TOKEN = "fixture-refresh-token"

type Route = Callable[[str, str, bytes, dict[str, str]], HttpResponse]


@dataclass
class Request:
	method: str
	url: str
	headers: dict[str, str]
	body: bytes = b""


@dataclass
class FakeTransport:
	"""Answers every request through ``route``, and records it."""

	route: Route
	requests: list[Request] = field(default_factory=list)

	def get(self, url: str, headers: dict[str, str], timeout_seconds: float) -> HttpResponse:
		assert timeout_seconds > 0, "every request carries a timeout"
		self.requests.append(Request("GET", url, headers))
		return self.route("GET", url, b"", headers)

	def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> HttpResponse:
		assert timeout_seconds > 0, "every request carries a timeout"
		self.requests.append(Request("POST", url, headers, body))
		return self.route("POST", url, body, headers)

	def download(self, url: str, timeout_seconds: float, max_bytes: int) -> bytes:
		self.requests.append(Request("DOWNLOAD", url, {}))
		return b"image"


def response(answer: Answer) -> HttpResponse:
	return HttpResponse(answer.status, answer.body, {k.lower(): v for k, v in answer.headers.items()})


def woocommerce(state: str) -> FakeTransport:
	responder = RestResponder("woocommerce", state)
	return FakeTransport(lambda method, url, _body, _headers: response(responder.answer(method, url)))


def rest_source(source: str, state: str) -> FakeTransport:
	"""A REST source answering from its fixtures (requests' bodies matched too)."""
	responder = RestResponder(source, state)
	return FakeTransport(lambda method, url, body, _headers: response(responder.answer(method, url, body)))


def etsy(state: str) -> FakeTransport:
	"""Etsy as the tests see it: the token endpoint grants the shop owner's tokens (a new refresh token
	each time), everything else comes from the Etsy fixtures."""
	responder = RestResponder("etsy", state)

	def route(method: str, url: str, body: bytes, _headers: dict[str, str]) -> HttpResponse:
		if method == "POST" and url.endswith(ETSY_TOKEN_PATH):
			grant = {
				"access_token": ETSY_ACCESS_TOKEN,
				"token_type": "Bearer",
				"expires_in": 3600,
				"refresh_token": ETSY_ROTATED_REFRESH_TOKEN,
			}
			return HttpResponse(200, json.dumps(grant).encode())
		return response(responder.answer(method, url, body))

	return FakeTransport(route)


def entra(source: str, state: str) -> FakeTransport:
	"""A Dynamics 365 source as the tests see it: Microsoft Entra ID's token endpoint grants the e2e app's
	client credentials a token, and the source answers from its fixtures to that token only."""
	responder = RestResponder(source, state)

	def route(method: str, url: str, body: bytes, headers: dict[str, str]) -> HttpResponse:
		if method == "POST" and url.endswith(ENTRA_GRANT_PATH):
			form = parse_qs(body.decode())
			if form.get("client_secret") != [ENTRA_CLIENT_SECRET] or form.get("grant_type") != [
				"client_credentials"
			]:
				return HttpResponse(401, b'{"error":"invalid_client"}')
			grant = {"token_type": "Bearer", "expires_in": 3599, "access_token": ENTRA_TOKEN}
			return HttpResponse(200, json.dumps(grant).encode())
		if headers.get("Authorization") != f"Bearer {ENTRA_TOKEN}":
			return HttpResponse(401, b'{"error":{"code":"Authentication_InvalidCredentials"}}')
		return response(responder.answer(method, url, body))

	return FakeTransport(route)


# The tokens the fake OAuth sources (QuickBooks Online, Xero, Zoho) grant: the access token the fixtures'
# requests carry, and the refresh token a rotating source issues in its place.
OAUTH_ACCESS_TOKEN = "fixture-oauth-access-token"
OAUTH_ROTATED_REFRESH_TOKEN = "fixture-oauth-refresh-token"


def oauth_source(source: str, state: str, grant_path: str, rotates: bool = True) -> FakeTransport:
	"""A REST source approved through OAuth 2: its token endpoint (any POST to ``grant_path``) grants the
	fixture tokens (a new refresh token each time when it ``rotates``); everything else comes from the
	source's fixtures."""
	responder = RestResponder(source, state)

	def route(method: str, url: str, body: bytes, _headers: dict[str, str]) -> HttpResponse:
		if method == "POST" and url.split("?", 1)[0].endswith(grant_path):
			grant: dict[str, object] = {
				"access_token": OAUTH_ACCESS_TOKEN,
				"token_type": "Bearer",
				"expires_in": 3600,
			}
			if rotates:
				grant["refresh_token"] = OAUTH_ROTATED_REFRESH_TOKEN
			return HttpResponse(200, json.dumps(grant).encode())
		return response(responder.answer(method, url, body))

	return FakeTransport(route)


def shopify(state: str) -> FakeTransport:
	responder = ShopifyResponder(state)
	return FakeTransport(lambda _method, _url, body, _headers: response(responder.answer(body)))


def scripted(*responses: HttpResponse) -> FakeTransport:
	"""Answers requests with ``responses`` in order (the last one repeats)."""
	queue = list(responses)

	def route(_method: str, _url: str, _body: bytes, _headers: dict[str, str]) -> HttpResponse:
		return queue.pop(0) if len(queue) > 1 else queue[0]

	return FakeTransport(route)


@dataclass
class RecordingSleeper:
	"""A clock that only moves when something sleeps, recording every wait."""

	clock: float = 1_000_000.0
	waits: list[float] = field(default_factory=list)

	def sleep(self, seconds: float) -> None:
		self.waits.append(seconds)
		self.clock += seconds

	def now(self) -> float:
		return self.clock
