# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads complete Shopify objects. A page's nodes have every nested connection declared on their EntityQuery
followed to the end; any other connection that still has more pages is an error, never a silent
truncation. Shopify's own ids (``gid://shopify/<Type>/<id>``) become this connection's identities
(``gid://shopify/<connection>/<Type>/<id>``), wherever they appear in the object."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonObject, JsonValue, Node
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.shopify.client import GraphQlClient
from idempotent_importer.sources.shopify.queries import NESTED_PAGE_SIZE, EntityQuery, entity_query

SOURCE_ID = "shopify"
PAGE_SIZE = 50
SHOPIFY_GID = re.compile(r"^gid://shopify/([A-Za-z]+)/([A-Za-z0-9-]+)(\?.*)?$")


def search_query(updated_since: datetime | None, filter_: str) -> str | None:
	"""A Shopify search ``query:`` from the incremental window and the operator's filter."""
	terms: list[str] = []
	if updated_since is not None:
		terms.append(f"updated_at:>='{updated_since.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}'")
	if filter_.strip():
		terms.append(f"({filter_.strip()})")
	return " AND ".join(terms) or None


class ShopifyReader:
	def __init__(self, config: ConnectionConfig, client: GraphQlClient) -> None:
		self._config = config
		self._client = client

	def is_incremental(self, kind: Kind) -> bool:
		return entity_query(kind).is_incremental()

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		query = entity_query(kind)
		variables: dict[str, JsonValue] = {"first": PAGE_SIZE, "after": cursor}
		if query.searchable:
			variables["query"] = search_query(since, filter_)
		connection = self._client.query(query.list_operation(), variables).required_child(query.connection)
		page_info = connection.required_child("pageInfo")
		nodes = [self._complete(query, node) for node in connection.children("nodes")]
		return Page(nodes, page_info.flag("hasNextPage"), page_info.optional_string("endCursor"))

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		query = entity_query(kind)
		data = self._client.query(
			query.node_operation(), {"id": f"gid://shopify/{identity.type}/{identity.id}"}
		)
		node = data.child(query.node_field)
		return None if node is None else self._complete(query, node)

	def _complete(self, query: EntityQuery, node: Node) -> Node:
		data = node.raw()
		shopify_id = node.id()
		for nested in query.nested:
			connection = data.get(nested.field)
			if not isinstance(connection, dict):
				continue
			while has_next_page(connection):
				page_info = connection.get("pageInfo")
				after = page_info.get("endCursor") if isinstance(page_info, dict) else None
				more = (
					self._client.query(query.page_operation(nested), {"id": shopify_id, "after": after})
					.required_child("node")
					.required_child(nested.field)
					.raw()
				)
				connection = {
					"pageInfo": more.get("pageInfo", {}),
					"nodes": [*nodes(connection), *nodes(more)],
				}
			data[nested.field] = connection
		truncated = first_truncated_path(data, shopify_id)
		if truncated is not None:
			raise SourceError(
				f"{truncated} has more than {NESTED_PAGE_SIZE} entries, which the importer cannot page through."
			)
		identified = self._identities(data)
		if not isinstance(identified, dict):
			raise SourceError(f"{shopify_id} is not an object.")
		gid = str(identified["id"])
		return Node(identified, gid)

	def _identities(self, value: JsonValue) -> JsonValue:
		"""Every Shopify global id in ``value`` as this connection's identity."""
		if isinstance(value, dict):
			return {key: self._identities(item) for key, item in value.items()}
		if isinstance(value, list):
			return [self._identities(item) for item in value]
		if isinstance(value, str) and (match := SHOPIFY_GID.match(value)):
			return self._config.identity(match.group(1), match.group(2))
		return value


def has_next_page(connection: JsonObject) -> bool:
	page_info = connection.get("pageInfo")
	return isinstance(page_info, dict) and page_info.get("hasNextPage") is True


def nodes(connection: JsonObject) -> list[JsonValue]:
	items = connection.get("nodes")
	return list(items) if isinstance(items, list) else []


def first_truncated_path(data: JsonValue, path: str) -> str | None:
	if isinstance(data, dict):
		if has_next_page(data):
			return path
		for key, value in data.items():
			found = first_truncated_path(value, f"{path}.{key}")
			if found is not None:
				return found
	if isinstance(data, list):
		for index, value in enumerate(data):
			found = first_truncated_path(value, f"{path}[{index}]")
			if found is not None:
				return found
	return None
