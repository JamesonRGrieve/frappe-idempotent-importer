# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one PeopleSoft installation (HCM or Financials) through the Integration Broker's REST Query Access
Service, the delivered service operation QAS_EXECUTEQRY_REST_GET (PeopleTools PeopleBooks, Reporting Web
Services, "Executing the Query":
https://docs.oracle.com/cd/E92519_02/pt856pbr3/eng/pt/trws/task_ExecutingtheQuery-5e7fb0.html): each kind's
query (named by the connection's field map, ``fieldmap``) runs synchronously at

    <Store URL>/PSIGW/RESTListeningConnector/<node>/ExecuteQuery.v1/<owner>/<query>/JSON/NONFILE
        ?isconnectedquery=N&maxrows=<n>&json_resp=true
        &prompt_uniquepromptname=<prompt>&prompt_fieldvalue=<value> (once per prompt, in order)

and answers ``{"status": "success", "data": {"query": {"numrows", "rows": [{"attr:rownumber", <columns>}]}}}``
(PeopleBooks' JSON/NONFILE example). The service has no offset: a query is paged by its key through its
own "key after" prompt (rows ordered by key), the last key of a full page left for the next, so a document's
rows never split; a query without one is read in a single request, and one that fills ``max_rows`` is an
error rather than a silent truncation. An incremental run passes the query's "changed since" prompt the
day of the last clean run (a day's overlap; the change hash skips what didn't change); a query without one
is read whole. Deletions come only from a completed, unfiltered full pass.

Security: PeopleBooks' "Understanding OAuth 2.0" (pt859pbr5/eng/pt/tsec/UnderstandingOAuth2_0.html) says the
only authentication for provider REST services is Basic Authentication and PeopleSoft Token, so the
connection's PeopleSoft user ID and password are sent as HTTP Basic credentials; the user needs the
QAS_QRY_REST permission list's service operations and query access to the named queries. Oracle documents
no request limit for the Integration Broker (it is the customer's own web and application servers): the
client sends one request at a time, at most PACE_PER_SECOND a second, retries 429 and 5xx with backoff, and
pauses the run when the gateway keeps failing. Every node carries its identity as ``id``
(``gid://peoplesoft/<connection>/<Type>/<key>``, a composite key's columns joined by ``:``)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from urllib.parse import quote

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.common import chained
from idempotent_importer.sources.peoplesoft.fieldmap import (
	KEY_SEPARATOR,
	FieldMap,
	QueryMap,
	column_name,
	field_map,
)

SOURCE_ID = "peoplesoft"
GATEWAY = "/PSIGW/RESTListeningConnector"
SERVICE = "ExecuteQuery.v1"
OUTPUT = "JSON/NONFILE"
SUCCESS = "success"
ROW_NUMBER = "attr:rownumber"
PACE_PER_SECOND = 2.0
RATE_LIMIT = RateLimit(requests_per_second=PACE_PER_SECOND)
SINCE_FORMAT = "%Y-%m-%d"
# A key's characters an identity can't hold.
UNSAFE_KEY = "/"


def key_text(values: Sequence[JsonValue]) -> str:
	return KEY_SEPARATOR.join("" if value is None else str(value).strip() for value in values).replace(
		UNSAFE_KEY, quote(UNSAFE_KEY, safe="")
	)


class Row:
	"""One answered row, its columns found case-insensitively and without their record alias."""

	def __init__(self, row: Node) -> None:
		self._values = {column_name(name): value for name, value in row.items() if name != ROW_NUMBER}

	def get(self, column: str) -> JsonValue:
		return self._values.get(column_name(column))

	def has(self, column: str) -> bool:
		return column_name(column) in self._values

	def raw(self) -> dict[str, JsonValue]:
		return dict(self._values)


class PeopleSoftReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		self._config = config
		self._client = client
		self._map: FieldMap = field_map(config.field_map)

	@property
	def map(self) -> FieldMap:
		return self._map

	def is_incremental(self, kind: Kind) -> bool:
		maps = self._map.kinds.get(kind, ())
		return bool(maps) and all(query.since_prompt for query in maps)

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		queries = self._map.queries(kind)
		return chained(
			len(queries), cursor, lambda feed, after: self._query_page(queries[feed], after, since)
		)

	def node(self, kind: Kind, gid: str) -> Node | None:
		raise SourceError("PeopleSoft's queries are read whole: one object can't be read by itself.")

	def _query_page(self, query: QueryMap, after: str | None, since: datetime | None) -> Page:
		prompts: list[tuple[str, str]] = []
		if query.since_prompt and since is not None:
			prompts.append((query.since_prompt, since.strftime(SINCE_FORMAT)))
		if query.after_prompt:
			prompts.append((query.after_prompt, after or ""))
		rows = self._execute(query, prompts)
		groups = self._documents(query, rows)
		full = len(rows) >= self._map.max_rows
		if not full:
			return Page(self._nodes(query, groups), False, None)
		if not query.after_prompt:
			raise SourceError(
				f"PeopleSoft's {query.query} answered max_rows ({self._map.max_rows}) rows: give it an "
				"after_prompt to page by its key, or raise max_rows."
			)
		if len(groups) < 2:  # noqa: PLR2004 - the last key's rows may continue on the next page
			raise SourceError(f"PeopleSoft's {query.query} has more than max_rows rows for one key.")
		complete = dict(list(groups.items())[:-1])
		last = next(reversed(complete))
		return Page(self._nodes(query, complete), True, last)

	def _execute(self, query: QueryMap, prompts: list[tuple[str, str]]) -> list[Row]:
		path = "/".join(
			(
				GATEWAY,
				quote(self._map.node, safe=""),
				SERVICE,
				self._map.owner,
				quote(query.query, safe=""),
				OUTPUT,
			)
		)
		parameters: dict[str, QueryValue] = {
			"isconnectedquery": "N",
			"maxrows": self._map.max_rows,
			"json_resp": "true",
		}
		if prompts:
			parameters |= {
				"prompt_uniquepromptname": [name for name, _ in prompts],
				"prompt_fieldvalue": [value for _, value in prompts],
			}
		result = self._client.get(path, parameters)
		if result is None:
			raise SourceError(f"PeopleSoft has no query {query.query} for node {self._map.node} (HTTP 404).")
		answer = result.node
		if answer.optional_string("status") != SUCCESS:
			detail = answer.get("data")
			raise SourceError(
				f"PeopleSoft refused query {query.query}: {detail if isinstance(detail, str) else 'no reason given'}."
			)
		answered = answer.required_child("data").required_child("query")
		return [Row(row) for row in answered.children("rows")]

	@staticmethod
	def _documents(query: QueryMap, rows: list[Row]) -> dict[str, list[Row]]:
		"""The rows of each key, in the order the query answered them."""
		groups: dict[str, list[Row]] = {}
		for row in rows:
			key = key_text([row.get(column) for column in query.key])
			if key.strip(KEY_SEPARATOR) == "":
				raise SourceError(f"PeopleSoft's {query.query} answered a row with no {'/'.join(query.key)}.")
			groups.setdefault(key, []).append(row)
		return groups

	def _nodes(self, query: QueryMap, groups: dict[str, list[Row]]) -> list[Node]:
		nodes = []
		for key, rows in groups.items():
			gid = self._config.identity(query.type, key)
			nodes.append(
				Node(
					{
						"id": gid,
						"source_id": key,
						"query": query.query,
						"type": query.type,
						"rows": [row.raw() for row in rows],
					},
					gid,
				)
			)
		return nodes
