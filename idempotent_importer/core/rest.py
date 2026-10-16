# SPDX-License-Identifier: AGPL-3.0-or-later
"""A source's JSON REST API, read-only: GETs (and the POST queries some APIs use for searches) with the
source's authentication, requests paced under its RateLimit, and rate limits and server errors retried
through RetryPolicy. When the source keeps pushing back it is a RetryLater (the run pauses); rejected
credentials are CredentialsRejected (the run stops). A JSON list response is returned under ``items``;
204 is an empty list; 404 is "no such resource". Errors name the source and path, never credentials."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError
from idempotent_importer.core.http import (
	HTTP_FORBIDDEN,
	HTTP_NO_CONTENT,
	HTTP_NOT_FOUND,
	HTTP_OK,
	HTTP_UNAUTHORIZED,
	HttpResponse,
	HttpTransport,
	RateLimit,
	RequestPacer,
	RetryPolicy,
	Sleeper,
)
from idempotent_importer.core.node import JsonValue, Node

REQUEST_TIMEOUT_SECONDS = 60.0
LIST_KEY = "items"

type QueryValue = str | int | bool | Sequence[str]


class RequestAuthorizer(Protocol):
	"""How one source authenticates its API requests."""

	def headers(self) -> dict[str, str]:
		"""The headers that authenticate a request (fetching or refreshing a token first if needed)."""
		...

	def renew(self) -> bool:
		"""Forget any cached token so the next ``headers`` fetches a fresh one; False when there is none."""
		...


class BasicAuthorizer:
	"""HTTP Basic credentials (a WordPress Application Password, a WooCommerce consumer key/secret)."""

	def __init__(self, username: str, password: str, missing: str) -> None:
		if not username or not password:
			raise SourceError(missing)
		token = base64.b64encode(f"{username}:{password}".encode()).decode()
		self._header = f"Basic {token}"

	def headers(self) -> dict[str, str]:
		return {"Authorization": self._header}

	def renew(self) -> bool:
		return False

	def __repr__(self) -> str:
		return "BasicAuthorizer([redacted])"


@dataclass(frozen=True)
class RestResult:
	node: Node
	headers: dict[str, str]

	def header(self, name: str) -> str | None:
		return self.headers.get(name.lower()) or None


class RestClient:
	def __init__(
		self,
		label: str,
		base_url: str,
		authorizer: RequestAuthorizer,
		transport: HttpTransport,
		sleeper: Sleeper,
		rate_limit: RateLimit | None = None,
	) -> None:
		self.label = label
		self._base_url = base_url.rstrip("/")
		self._authorizer = authorizer
		self._transport = transport
		self._sleeper = sleeper
		self._rate_limit = rate_limit or RateLimit()
		self._pacer = RequestPacer(self._rate_limit, sleeper)

	def get(self, path: str, query: Mapping[str, QueryValue] | None = None) -> RestResult | None:
		"""None when the source has no such resource (404). List values are sent as repeated keys."""
		url = self._base_url + path + query_string(query or {})
		return self._send(path, lambda headers: self._transport.get(url, headers, REQUEST_TIMEOUT_SECONDS))

	def query(self, path: str, body: Mapping[str, JsonValue]) -> RestResult | None:
		"""A read-only query sent as a JSON POST (search endpoints that take their filter in the body)."""
		payload = json.dumps(dict(body), separators=(",", ":")).encode()
		url = self._base_url + path
		return self._send(
			path,
			lambda headers: self._transport.post(
				url, {"Content-Type": "application/json", **headers}, payload, REQUEST_TIMEOUT_SECONDS
			),
		)

	def _send(self, path: str, request: Callable[[dict[str, str]], HttpResponse]) -> RestResult | None:
		renewed = False
		attempt = 1
		while True:
			self._pacer.before_request(self.label)
			response = request({"Accept": "application/json", **self._authorizer.headers()})
			self._pacer.after_response(response)
			if response.status == HTTP_UNAUTHORIZED and not renewed and self._authorizer.renew():
				renewed = True
				continue
			wait = RetryPolicy.wait(response, attempt, self._rate_limit.cooldown_seconds)
			if wait is None:
				return self._result(response, path)
			if not RetryPolicy.waits_inline(wait, attempt):
				raise RetryLater(
					wait,
					f"{path}: {self.label} is rate limiting or failing (HTTP {response.status}, attempt {attempt}).",
				)
			self._sleeper.sleep(wait)
			attempt += 1

	def _result(self, response: HttpResponse, path: str) -> RestResult | None:
		if response.status == HTTP_NOT_FOUND:
			return None
		if response.status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
			raise CredentialsRejected(
				f"{path}: {self.label} rejected the credentials or they lack a required scope (HTTP {response.status})."
			)
		if response.status == HTTP_NO_CONTENT:
			return RestResult(Node({LIST_KEY: []}, path), response.headers)
		if response.status != HTTP_OK:
			raise SourceError(f"{path}: {self.label} returned HTTP {response.status}.")
		try:
			decoded = json.loads(response.body)
		except ValueError:
			raise SourceError(f"{path}: {self.label} returned a response that isn't JSON.") from None
		if isinstance(decoded, list):
			return RestResult(Node({LIST_KEY: decoded}, path), response.headers)
		if not isinstance(decoded, dict):
			raise SourceError(f"{path}: {self.label} returned no object.")
		return RestResult(Node(decoded, path), response.headers)


def query_string(query: Mapping[str, QueryValue]) -> str:
	pairs: list[str] = []
	for name, value in query.items():
		values = [value] if isinstance(value, str | int | bool) else list(value)
		for item in values:
			text = ("true" if item else "false") if isinstance(item, bool) else str(item)
			pairs.append(f"{quote(name, safe='')}={quote(text, safe='')}")
	return "?" + "&".join(pairs) if pairs else ""
