# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sends the library's operations to Shopify's Admin GraphQL API, authenticated by the store's authorizer
(a token it renews once when Shopify rejects it). Every operation's page sizes come from PageSizes: a query
Shopify refuses as too costly (MAX_COST_EXCEEDED) is resent at once with smaller pages, without using up a
retry or pausing the run. Shopify meters a query-cost bucket and reports it on every response
(shopify.dev/docs/api/usage/limits#graphql-admin-api-rate-limits); before each query the client waits until
the bucket can take one as costly as the last, so a long import stays under the throttle instead of running
into it. 429/5xx are retried with backoff; when Shopify keeps pushing back it is a RetryLater (the run
pauses), and a rejected token is CredentialsRejected (the run stops). Any other GraphQL error, partial data
included, is a SourceError (AccessDenied for a missing scope), so a response is complete or rejected.
Errors never include a credential."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError
from idempotent_importer.core.http import (
	HTTP_FORBIDDEN,
	HTTP_OK,
	HTTP_UNAUTHORIZED,
	HttpResponse,
	HttpTransport,
	RetryPolicy,
	Sleeper,
)
from idempotent_importer.core.node import JsonObject, JsonValue, Node
from idempotent_importer.core.rest import RequestAuthorizer
from idempotent_importer.sources.shopify.page_sizes import PageSizes
from idempotent_importer.sources.shopify.queries import API_VERSION, QueryLibrary

REQUEST_TIMEOUT_SECONDS = 60.0
MIN_THROTTLE_WAIT_SECONDS = 1.0
THROTTLED = "THROTTLED"
MAX_COST_EXCEEDED = "MAX_COST_EXCEEDED"
ACCESS_DENIED = "ACCESS_DENIED"
# The refusal's wording, read when its extensions don't carry the numbers.
MAX_COST = re.compile(r"Query cost is (\d+), which exceeds the single query max cost limit \((\d+)\)")
DOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
# Where the store grants OAuth tokens.
GRANT_PATH = "/admin/oauth/access_token"


class AccessDenied(SourceError):
	"""Shopify answered, but the app lacks an access scope a field in the query needs (ACCESS_DENIED)."""


@dataclass(frozen=True)
class Connection:
	"""Where one store's Admin API lives: its GraphQL endpoint, derived from the myshopify domain, and its
	OAuth token endpoint on the same host. An explicit endpoint exists only for the test harness; the token
	endpoint follows it, so a fake store serves both."""

	endpoint: str

	@classmethod
	def for_store(cls, store: str) -> Connection:
		domain = store.strip().lower().removeprefix("https://").rstrip("/")
		if not DOMAIN.match(domain):
			raise SourceError("Shopify is not configured: the store must be its <name>.myshopify.com domain.")
		return cls(f"https://{domain}/admin/api/{API_VERSION}/graphql.json")

	def token_url(self) -> str:
		"""The store's OAuth token endpoint (the client credentials grant)."""
		parts = urlsplit(self.endpoint)
		if not parts.scheme or not parts.netloc:
			raise SourceError("The Shopify endpoint is not a URL.")
		return f"{parts.scheme}://{parts.netloc}{GRANT_PATH}"


class GraphQlClient:
	def __init__(
		self,
		connection: Connection,
		library: QueryLibrary,
		authorizer: RequestAuthorizer,
		transport: HttpTransport,
		sleeper: Sleeper,
	) -> None:
		self._connection = connection
		self._library = library
		self._authorizer = authorizer
		self._transport = transport
		self._sleeper = sleeper
		self._page_sizes = PageSizes(library)
		self._next_query_at = 0.0

	def query(self, operation: str, variables: dict[str, JsonValue] | None = None) -> Node:
		renewed = False
		attempt = 1
		while True:
			self._wait_for_bucket(operation)
			response = self._send(operation, variables or {})
			if response.status == HTTP_UNAUTHORIZED and not renewed and self._authorizer.renew():
				renewed = True
				continue
			wait = self._retry_wait(response, attempt)
			if wait is None:
				refusal = cost_refusal(response)
				if refusal is not None:
					self._page_sizes.refused(operation, *refusal)
					continue
				node = self._data(response, operation)
				self._note_cost(operation, response)
				return node
			if not RetryPolicy.waits_inline(wait, attempt):
				raise RetryLater(
					wait,
					f"{operation}: Shopify is throttling or failing (HTTP {response.status}, attempt {attempt}).",
				)
			self._sleeper.sleep(wait)
			attempt += 1

	def _send(self, operation: str, variables: dict[str, JsonValue]) -> HttpResponse:
		sizes: dict[str, JsonValue] = dict(self._page_sizes.sizes(operation))
		body = json.dumps(
			{
				"query": self._library.document(operation),
				"variables": {**variables, **sizes},
				"operationName": operation,
			}
		).encode()
		headers = {
			"Content-Type": "application/json",
			"Accept": "application/json",
			**self._authorizer.headers(),
		}
		return self._transport.post(self._connection.endpoint, headers, body, REQUEST_TIMEOUT_SECONDS)

	def _wait_for_bucket(self, operation: str) -> None:
		wait = self._next_query_at - self._sleeper.now()
		if wait > RetryPolicy.MAX_INLINE_WAIT_SECONDS:
			raise RetryLater(wait, f"{operation}: Shopify's query cost budget is spent until it refills.")
		if wait > 0:
			self._sleeper.sleep(wait)

	def _note_cost(self, operation: str, response: HttpResponse) -> None:
		"""Records the response's cost: when the bucket can take the next query, and room to grow the page
		sizes."""
		decoded = decode(response.body)
		self._next_query_at = self._sleeper.now() + (bucket_shortfall(decoded) or 0.0)
		requested = cost_of(decoded).get("requestedQueryCost")
		if isinstance(requested, int | float) and not isinstance(requested, bool):
			self._page_sizes.succeeded(operation, int(requested))

	def _retry_wait(self, response: HttpResponse, attempt: int) -> float | None:
		wait = RetryPolicy.wait(response, attempt)
		if wait is not None:
			return wait
		if response.status != HTTP_OK:
			return None
		decoded = decode(response.body)
		if not has_error_code(decoded, THROTTLED):
			return None
		return max(MIN_THROTTLE_WAIT_SECONDS, bucket_shortfall(decoded) or MIN_THROTTLE_WAIT_SECONDS)

	@staticmethod
	def _data(response: HttpResponse, operation: str) -> Node:
		if response.status in (HTTP_UNAUTHORIZED, HTTP_FORBIDDEN):
			raise CredentialsRejected(
				f"{operation}: Shopify rejected the access token or the app lacks a required scope (HTTP {response.status})."
			)
		if response.status != HTTP_OK:
			raise SourceError(f"{operation}: Shopify returned HTTP {response.status}.")
		decoded = decode(response.body)
		if "errors" in decoded:
			message = f"{operation}: {error_summary(decoded['errors'])}"
			raise AccessDenied(message) if only_access_denied(decoded) else SourceError(message)
		data = decoded.get("data")
		if not isinstance(data, dict):
			raise SourceError(f"{operation}: Shopify returned no data.")
		return Node(data, operation)


def decode(body: bytes) -> JsonObject:
	try:
		decoded = json.loads(body)
	except ValueError:
		raise SourceError("Shopify returned a response that is not JSON.") from None
	if not isinstance(decoded, dict):
		raise SourceError("Shopify returned a response that is not a JSON object.")
	return decoded


def errors_of(decoded: JsonObject) -> list[JsonObject]:
	errors = decoded.get("errors")
	return [error for error in errors if isinstance(error, dict)] if isinstance(errors, list) else []


def code_of(error: JsonObject) -> JsonValue:
	extensions = error.get("extensions")
	return extensions.get("code") if isinstance(extensions, dict) else None


def has_error_code(decoded: JsonObject, code: str) -> bool:
	return any(code_of(error) == code for error in errors_of(decoded))


def only_access_denied(decoded: JsonObject) -> bool:
	errors = errors_of(decoded)
	return bool(errors) and all(code_of(error) == ACCESS_DENIED for error in errors)


def cost_refusal(response: HttpResponse) -> tuple[int, int] | None:
	"""The requested cost and limit when Shopify refused the query as too costly to run at all, else None."""
	if response.status != HTTP_OK:
		return None
	for error in errors_of(decode(response.body)):
		if code_of(error) != MAX_COST_EXCEEDED:
			continue
		extensions = error.get("extensions")
		if isinstance(extensions, dict):
			cost, limit = extensions.get("cost"), extensions.get("maxCost")
			if isinstance(cost, int) and isinstance(limit, int):
				return cost, limit
		message = error.get("message")
		match = MAX_COST.search(message) if isinstance(message, str) else None
		if match is not None:
			return int(match.group(1)), int(match.group(2))
		raise SourceError("Shopify refused a query as too costly without saying its cost.")
	return None


def cost_of(decoded: JsonObject) -> JsonObject:
	"""The response's ``extensions.cost`` (requested and actual cost, throttle status), or {} when absent."""
	extensions = decoded.get("extensions")
	cost = extensions.get("cost") if isinstance(extensions, dict) else None
	return cost if isinstance(cost, dict) else {}


def bucket_shortfall(decoded: JsonObject) -> float | None:
	"""How long until the bucket holds the query's cost again: (requested - available) / restore rate, 0
	when it already does; None when the response doesn't report the bucket."""
	cost = cost_of(decoded)
	status = cost.get("throttleStatus")
	if not isinstance(status, dict):
		return None
	requested = number(cost.get("requestedQueryCost"))
	available = number(status.get("currentlyAvailable"))
	restore_rate = number(status.get("restoreRate"))
	if restore_rate <= 0:
		return None
	return max(0.0, (requested - available) / restore_rate)


def number(value: JsonValue) -> float:
	return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0


def error_summary(errors: JsonValue) -> str:
	if not isinstance(errors, list):
		return "Shopify returned an error."
	messages = [
		message
		for error in errors
		if isinstance(error, dict) and isinstance(message := error.get("message"), str)
	]
	return "; ".join(messages) if messages else "Shopify returned an error."
