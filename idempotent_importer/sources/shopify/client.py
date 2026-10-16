# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sends the library's operations to Shopify's Admin GraphQL API. Shopify meters a query-cost bucket and
reports it on every response (shopify.dev/docs/api/usage/limits#graphql-admin-api-rate-limits); before each
query the client waits until the bucket can take one as costly as the last, so a long import stays under
the throttle instead of running into it. 429/5xx are retried with backoff; when Shopify keeps pushing back
it is a RetryLater (the run pauses), and a rejected token is CredentialsRejected (the run stops). Any
GraphQL error, partial data included, is a SourceError, so a response is complete or rejected. Errors
never include the access token."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

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
from idempotent_importer.sources.shopify.queries import API_VERSION, QueryLibrary

REQUEST_TIMEOUT_SECONDS = 60.0
MIN_THROTTLE_WAIT_SECONDS = 1.0
THROTTLED = "THROTTLED"
DOMAIN = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")
ACCESS_HEADER = "X-Shopify-Access-Token"


@dataclass(frozen=True)
class Connection:
	"""Where and how to reach one store's Admin GraphQL API."""

	endpoint: str
	access_token: str = field(repr=False)

	@classmethod
	def for_store(cls, store: str, access_token: str) -> Connection:
		domain = store.strip().lower().removeprefix("https://").rstrip("/")
		if not DOMAIN.match(domain):
			raise SourceError("Shopify is not configured: the store must be its <name>.myshopify.com domain.")
		if not access_token:
			raise SourceError("Shopify is not configured: enter the Admin API access token.")
		return cls(f"https://{domain}/admin/api/{API_VERSION}/graphql.json", access_token)


class GraphQlClient:
	def __init__(
		self, connection: Connection, library: QueryLibrary, transport: HttpTransport, sleeper: Sleeper
	) -> None:
		self._connection = connection
		self._library = library
		self._transport = transport
		self._sleeper = sleeper
		self._next_query_at = 0.0

	def query(self, operation: str, variables: dict[str, JsonValue] | None = None) -> Node:
		body = json.dumps(
			{
				"query": self._library.document(operation),
				"variables": variables or {},
				"operationName": operation,
			}
		).encode()
		headers = {
			"Content-Type": "application/json",
			"Accept": "application/json",
			ACCESS_HEADER: self._connection.access_token,
		}
		attempt = 1
		while True:
			self._wait_for_bucket(operation)
			response = self._transport.post(self._connection.endpoint, headers, body, REQUEST_TIMEOUT_SECONDS)
			wait = self._retry_wait(response, attempt)
			if wait is None:
				node = self._data(response, operation)
				self._note_bucket(response)
				return node
			if not RetryPolicy.waits_inline(wait, attempt):
				raise RetryLater(
					wait,
					f"{operation}: Shopify is throttling or failing (HTTP {response.status}, attempt {attempt}).",
				)
			self._sleeper.sleep(wait)
			attempt += 1

	def _wait_for_bucket(self, operation: str) -> None:
		wait = self._next_query_at - self._sleeper.now()
		if wait > RetryPolicy.MAX_INLINE_WAIT_SECONDS:
			raise RetryLater(wait, f"{operation}: Shopify's query cost budget is spent until it refills.")
		if wait > 0:
			self._sleeper.sleep(wait)

	def _note_bucket(self, response: HttpResponse) -> None:
		self._next_query_at = self._sleeper.now() + (bucket_shortfall(decode(response.body)) or 0.0)

	def _retry_wait(self, response: HttpResponse, attempt: int) -> float | None:
		wait = RetryPolicy.wait(response, attempt)
		if wait is not None:
			return wait
		if response.status != HTTP_OK:
			return None
		decoded = decode(response.body)
		if not is_throttled(decoded):
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
			raise SourceError(f"{operation}: {error_summary(decoded['errors'])}")
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


def is_throttled(decoded: JsonObject) -> bool:
	errors = decoded.get("errors")
	if not isinstance(errors, list):
		return False
	for error in errors:
		extensions = error.get("extensions") if isinstance(error, dict) else None
		if isinstance(extensions, dict) and extensions.get("code") == THROTTLED:
			return True
	return False


def bucket_shortfall(decoded: JsonObject) -> float | None:
	"""How long until the bucket holds the query's cost again: (requested - available) / restore rate, 0
	when it already does; None when the response doesn't report the bucket."""
	extensions = decoded.get("extensions")
	cost = extensions.get("cost") if isinstance(extensions, dict) else None
	if not isinstance(cost, dict):
		return None
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
