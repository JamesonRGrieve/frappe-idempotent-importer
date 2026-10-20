# SPDX-License-Identifier: AGPL-3.0-or-later
"""Odoo's external API, two ways, one interface (``OdooApi.call``: a model's method on some record ids
with named parameters):

- the **JSON-2 API** (Odoo 19 and later): ``POST /json/2/<model>/<method>`` with the method's named
  parameters, ``ids`` and ``context`` as a JSON object, an API key as a bearer token and, where one host
  serves several databases, the database in ``X-Odoo-Database``; a 4xx/5xx answer carries the exception's
  fully qualified ``name``
  (https://www.odoo.com/documentation/19.0/developer/reference/external_api.html);
- the **JSON-RPC API** (Odoo 17 and 18, and 19 until it is removed in Odoo 22): ``POST /jsonrpc`` calling
  the ``object`` service's ``execute_kw(db, uid, key, model, method, args, kwargs)`` after ``common``'s
  ``authenticate(db, login, key, {})``; errors come back as HTTP 200 with an ``error`` object
  (https://www.odoo.com/documentation/18.0/developer/reference/external_api.html). The XML-RPC API is the
  same object service in another encoding, so it adds nothing to read.

Odoo documents no request-rate limit for either API, so requests aren't paced; a 429 or 5xx (a proxy, Odoo
Online's front end) is retried through the shared RetryPolicy and a long wait pauses the run. Rejected
credentials (an invalid API key, ``AccessDenied``) stop the run; a model the user may not read
(``AccessError``: e.g. HR without the Employees officer right) is a KindUnavailable, so the rest of the run
completes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from idempotent_importer.core.errors import CredentialsRejected, KindUnavailable, SourceError
from idempotent_importer.core.http import HTTP_FORBIDDEN, HttpResponse
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import LIST_KEY, RestClient, RestResult

JSON2_PREFIX = "/json/2"
JSONRPC_PATH = "/jsonrpc"
DATABASE_HEADER = "X-Odoo-Database"
ACCESS_DENIED = "odoo.exceptions.AccessDenied"
ACCESS_ERROR = "odoo.exceptions.AccessError"
# What the object service raises for a model the database doesn't have.
MISSING_MODEL = "builtins.KeyError"
JSON_RPC_VERSION = "2.0"
RESULT_KEY = "result"


class OdooApi(Protocol):
	"""Odoo's ORM over HTTP."""

	def call(
		self, model: str, method: str, ids: list[int] | None, params: Mapping[str, JsonValue]
	) -> JsonValue:
		"""``model.browse(ids).method(**params)`` (no ids for an ``@api.model`` method), its return value."""
		...


class ApiKeyAuthorizer:
	"""An Odoo API key as the JSON-2 API's bearer token, and the database when one is named. A key lives
	until it expires or is revoked: there is nothing to renew."""

	def __init__(self, api_key: str, database: str) -> None:
		if not api_key:
			raise SourceError("Odoo is not configured: enter an API key of the user the importer reads as.")
		self._key = api_key
		self._database = database

	def headers(self) -> dict[str, str]:
		headers = {"Authorization": f"bearer {self._key}"}
		if self._database:
			headers[DATABASE_HEADER] = self._database
		return headers

	def renew(self) -> bool:
		return False

	def __repr__(self) -> str:
		return "ApiKeyAuthorizer([redacted])"


class NoHeaders:
	"""JSON-RPC carries its credentials in the request body."""

	def headers(self) -> dict[str, str]:
		return {}

	def renew(self) -> bool:
		return False


class OdooRestClient(RestClient):
	"""The JSON-2 API answers a record the user may not read with 403 and an ``AccessError``: that is one
	model the credentials can't read (KindUnavailable), not rejected credentials."""

	def _result(self, response: HttpResponse, path: str) -> RestResult | None:
		if response.status == HTTP_FORBIDDEN and exception_name(response.body) == ACCESS_ERROR:
			raise KindUnavailable(f"{path}: the Odoo user may not read this ({ACCESS_ERROR}).")
		return super()._result(response, path)


def exception_name(body: bytes) -> str | None:
	try:
		decoded = json.loads(body)
	except ValueError:
		return None
	name = decoded.get("name") if isinstance(decoded, dict) else None
	return name if isinstance(name, str) else None


def unwrap(result: RestResult | None, what: str) -> JsonValue:
	"""A method's return value from the client's node (a JSON list arrives under ``items``)."""
	if result is None:
		raise SourceError(f"Odoo has no {what}.")
	raw = result.node.raw()
	return raw[LIST_KEY] if set(raw) == {LIST_KEY} else raw


@dataclass
class Json2Api:
	client: RestClient

	def call(
		self, model: str, method: str, ids: list[int] | None, params: Mapping[str, JsonValue]
	) -> JsonValue:
		body: dict[str, JsonValue] = dict(params)
		if ids is not None:
			body["ids"] = list[JsonValue](ids)
		result = self.client.query(f"{JSON2_PREFIX}/{model}/{method}", body)
		if result is None:
			raise KindUnavailable(f"Odoo has no {model} (its app isn't installed).")
		return unwrap(result, f"{model}.{method}")


class JsonRpcApi:
	"""The object service through ``/jsonrpc``: the user's id is fetched once with ``authenticate``."""

	def __init__(self, client: RestClient, database: str, login: str, api_key: str) -> None:
		if not (database and login and api_key):
			raise SourceError(
				"Odoo is not configured: JSON-RPC needs the database (the account), the user's login and an API key."
			)
		self._client = client
		self._database = database
		self._login = login
		self._key = api_key
		self._uid: int | None = None
		self._sequence = 0

	def call(
		self, model: str, method: str, ids: list[int] | None, params: Mapping[str, JsonValue]
	) -> JsonValue:
		args: list[JsonValue] = [list[JsonValue](ids)] if ids is not None else []
		return self._rpc(
			"object",
			"execute_kw",
			[self._database, self._user(), self._key, model, method, args, dict(params)],
			f"{model}.{method}",
		)

	def _user(self) -> int:
		if self._uid is None:
			uid = self._rpc("common", "authenticate", [self._database, self._login, self._key, {}], "login")
			if isinstance(uid, bool) or not isinstance(uid, int):
				raise CredentialsRejected(
					"Odoo rejected the login and API key (authenticate returned no user)."
				)
			self._uid = uid
		return self._uid

	def _rpc(self, service: str, method: str, args: list[JsonValue], what: str) -> JsonValue:
		self._sequence += 1
		body: dict[str, JsonValue] = {
			"jsonrpc": JSON_RPC_VERSION,
			"method": "call",
			"params": {"service": service, "method": method, "args": args},
			"id": self._sequence,
		}
		answer = unwrap(self._client.query(JSONRPC_PATH, body), what)
		node = Node(answer if isinstance(answer, dict) else {}, JSONRPC_PATH)
		error = node.child("error")
		if error is not None:
			data = error.child("data")
			name = data.optional_string("name") if data else None
			if name == ACCESS_DENIED:
				raise CredentialsRejected(f"{what}: Odoo rejected the credentials ({ACCESS_DENIED}).")
			if name == MISSING_MODEL:
				raise KindUnavailable(f"{what}: Odoo has no such model (its app isn't installed).")
			if name == ACCESS_ERROR:
				raise KindUnavailable(f"{what}: the Odoo user may not read this ({ACCESS_ERROR}).")
			raise SourceError(
				f"{what}: Odoo answered {name or error.optional_string('message') or 'an error'}."
			)
		if RESULT_KEY not in node.raw():
			raise SourceError(f"{what}: Odoo's JSON-RPC answer has no result.")
		return node.get(RESULT_KEY)
