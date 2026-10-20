# SPDX-License-Identifier: AGPL-3.0-or-later
"""The recorded source responses in tests/fixtures/<source>/<state>/*.json, answered as the source would.

A REST fixture is ``{"request": {"method"?, "path", "query"?, "query_patterns"?, "body"?}, "headers"?:
{...}, "status"?: int, "response": ...}``: the response the source gives a request to that path (GET unless
named) whose query includes those parameters (others, like page sizes, are ignored), each parameter in
``query_patterns`` matching its regular expression whole (QuickBooks' query statement, whose
modified-since condition changes with every run), and, for a JSON POST query, whose body has
those values (an object matches when every key it names matches, a list when it has as many items and each
matches, anything else when equal). A Shopify fixture is ``{"operation", "variables", "data"}``: the
``data`` Shopify returns for that operation, matched by its ``after`` and ``id`` variables. Each store
state (v1, v2, …) is a complete snapshot of a store. Shared by the unit tests' transport and the
end-to-end fake store."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
HTTP_OK = 200
HTTP_NOT_FOUND = 404
SHOPIFY_MATCHED_VARIABLES = ("after", "id")


@dataclass(frozen=True)
class RestFixture:
	file: Path
	method: str
	path: str
	query: dict[str, str]
	status: int
	headers: dict[str, str]
	response: Any
	body: dict[str, Any] = field(default_factory=dict)
	query_patterns: dict[str, re.Pattern[str]] = field(default_factory=dict)

	def matches_query(self, sent: dict[str, list[str]]) -> bool:
		return all(sent.get(k) == [v] for k, v in self.query.items()) and all(
			len(sent.get(k, [])) == 1 and pattern.fullmatch(sent[k][0]) is not None
			for k, pattern in self.query_patterns.items()
		)


@dataclass(frozen=True)
class GraphQlFixture:
	file: Path
	operation: str
	variables: dict[str, Any]
	data: dict[str, Any]


@dataclass(frozen=True)
class Answer:
	status: int
	body: bytes
	headers: dict[str, str] = field(default_factory=dict)


def states(source: str) -> list[str]:
	return sorted(path.name for path in (FIXTURES / source).iterdir() if path.is_dir())


def load_json(path: Path) -> Any:
	return json.loads(path.read_text())


def rest_fixtures(source: str, state: str) -> list[RestFixture]:
	fixtures = []
	for path in sorted((FIXTURES / source / state).glob("*.json")):
		raw = load_json(path)
		request = raw["request"]
		query = {
			str(k): str(v).lower() if isinstance(v, bool) else str(v)
			for k, v in request.get("query", {}).items()
		}
		fixtures.append(
			RestFixture(
				path,
				str(request.get("method", "GET")).upper(),
				str(request["path"]),
				query,
				int(raw.get("status", HTTP_OK)),
				{str(k): str(v) for k, v in raw.get("headers", {}).items()},
				raw["response"],
				dict(request.get("body") or {}),
				{str(k): re.compile(str(v)) for k, v in request.get("query_patterns", {}).items()},
			)
		)
	if not fixtures:
		raise LookupError(f"No {source} fixtures for state {state}.")
	return fixtures


def graphql_fixtures(state: str) -> list[GraphQlFixture]:
	fixtures = []
	for path in sorted((FIXTURES / "shopify" / state).glob("*.json")):
		raw = load_json(path)
		fixtures.append(
			GraphQlFixture(path, str(raw["operation"]), dict(raw.get("variables", {})), dict(raw["data"]))
		)
	return fixtures


def value_matches(recorded: Any, sent: Any) -> bool:
	if isinstance(recorded, dict):
		return isinstance(sent, dict) and all(
			name in sent and value_matches(value, sent[name]) for name, value in recorded.items()
		)
	if isinstance(recorded, list) and recorded:
		return (
			isinstance(sent, list)
			and len(sent) == len(recorded)
			and all(value_matches(item, other) for item, other in zip(recorded, sent, strict=True))
		)
	return bool(recorded == sent)


def without_incremental_terms(body: Any) -> Any:
	"""An Odoo JSON-2 request as its fixture records it: an incremental run's ``write_date`` term left out of
	the domain (a fixture is the store's whole answer, as a full run reads it)."""
	if isinstance(body, dict) and isinstance(body.get("domain"), list):
		domain = [
			term
			for term in body["domain"]
			if not (isinstance(term, list) and term[:1] == [ODOO_CHANGE_FIELD])
		]
		return {**body, "domain": domain}
	return body


# How a source's requests are compared with its fixtures, where the request carries more than they record.
BODY_NORMALISERS = {"odoo": without_incremental_terms}
ODOO_CHANGE_FIELD = "write_date"


class RestResponder:
	"""Answers a REST source's requests from one store state's fixtures; unknown requests get a 404."""

	def __init__(self, source: str, state: str) -> None:
		self.fixtures = rest_fixtures(source, state)
		self._normalise = BODY_NORMALISERS.get(source, lambda body: body)

	def answer(self, method: str, url: str, body: bytes = b"") -> Answer:
		parts = urlsplit(url)
		sent = parse_qs(parts.query, keep_blank_values=True)
		decoded = self._normalise(json.loads(body) if body.strip() else {})
		for fixture in self.fixtures:
			if (
				fixture.method == method
				and fixture.path == parts.path
				and fixture.matches_query(sent)
				and value_matches(fixture.body, decoded)
			):
				return Answer(fixture.status, json.dumps(fixture.response).encode(), fixture.headers)
		return Answer(HTTP_NOT_FOUND, b'{"code":"rest_no_route","message":"No route was found."}')


class ShopifyResponder:
	"""Answers GraphQL requests as Shopify would: by operation name, and for paged or by-id operations by
	the ``after`` cursor and ``id``. Page size and search filters are ignored (a fixture is the store's
	whole answer)."""

	def __init__(self, state: str) -> None:
		self.fixtures = graphql_fixtures(state)

	def answer(self, body: bytes) -> Answer:
		request = json.loads(body)
		operation = request.get("operationName")
		variables = request.get("variables") or {}
		for fixture in self.fixtures:
			if fixture.operation == operation and all(
				fixture.variables.get(name) == variables.get(name) for name in SHOPIFY_MATCHED_VARIABLES
			):
				return Answer(HTTP_OK, json.dumps({"data": fixture.data}).encode())
		message = f"No fixture for {operation} {json.dumps(variables, sort_keys=True)}"
		return Answer(HTTP_OK, json.dumps({"errors": [{"message": message}]}).encode())
