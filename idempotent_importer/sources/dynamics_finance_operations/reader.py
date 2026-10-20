# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one legal entity of a Dynamics 365 finance and operations environment through its OData data
entities (``<environment>/data/<entity>``): each kind's entity from the connection's field map
(``fieldmap``), the legal entity's records only (``cross-company=true`` and ``dataAreaId``, as
learn.microsoft.com/dynamics365/fin-ops-core/dev-itpro/data-entities/odata documents), paged by the server
(``@odata.nextLink``; 200 at a time with ``Prefer: odata.maxpagesize``, under its maximum page size of
10,000). A document's lines are their own entity: read once per step and handed to each document by its
key. Data entities share no documented modification field, so every kind is read in full (unchanged
records are skipped by their hash). Every node carries its identity as ``id`` (its key, escaped, as
``source_id``) and its field map's roles as ``roles``."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from urllib.parse import quote

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.dynamics.odata import OData, quoted
from idempotent_importer.sources.dynamics_finance_operations.fieldmap import (
	EntityMap,
	Fields,
	entity_maps,
	usable,
)

SOURCE_ID = "dynamics_finance_operations"
ENVIRONMENT_URL = re.compile(r"^https://[a-z0-9-]+(\.[a-z0-9-]+)*\.dynamics\.com/?$", re.IGNORECASE)
LEGAL_ENTITY = re.compile(r"^[A-Za-z0-9]{1,4}$")
ACCOUNT_SEPARATOR = "/"
PAGE_SIZE = 200
DATA_PATH = "/data"
# The identity type each kind mints.
TYPES = {
	Kind.CUSTOMER: "Customer",
	Kind.SUPPLIER: "Vendor",
	Kind.PRODUCT: "Item",
	Kind.SALES_ORDER: "SalesOrder",
	Kind.PURCHASE_ORDER: "PurchaseOrder",
}


def environment_of(config: ConnectionConfig) -> str:
	url = config.store_url.strip().rstrip("/")
	if not ENVIRONMENT_URL.match(url):
		raise SourceError(
			"Finance & Operations is not configured: enter the environment's URL "
			"(https://<environment>.operations.dynamics.com)."
		)
	return url


def tenant_and_legal_entity(config: ConnectionConfig) -> tuple[str, str]:
	"""The tenant (id or domain) and the legal entity (its data area id): ``<tenant>/<legal entity>``."""
	tenant, _, legal_entity = config.account.strip().partition(ACCOUNT_SEPARATOR)
	if not tenant or not LEGAL_ENTITY.match(legal_entity):
		raise SourceError(
			"Finance & Operations is not configured: enter the account as <tenant>/<legal entity> (e.g. contoso.com/usmf)."
		)
	return tenant, legal_entity.lower()


def api_base(config: ConnectionConfig) -> str:
	return (config.endpoint_override or environment_of(config)).rstrip("/") + DATA_PATH


def key_of(value: JsonValue) -> str:
	"""A record's key as an identity id (escaped: keys may hold "/")."""
	if isinstance(value, bool) or not isinstance(value, str | int) or value == "":
		raise SourceError("A Finance & Operations record has no key.")
	return quote(str(value), safe="")


class FinanceOperationsReader:
	def __init__(self, config: ConnectionConfig, odata: OData) -> None:
		self._config = config
		self._odata = odata
		_, self._legal_entity = tenant_and_legal_entity(config)
		self._maps = entity_maps(config.field_map)
		self._lines: dict[Kind, dict[str, list[JsonValue]]] = {}

	def is_incremental(self, kind: Kind) -> bool:
		return False

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		entity_map = self._map(kind)
		page = self._odata.page(cursor or self._odata.first(f"/{entity_map.entity}", self._query()))
		nodes = [self._assemble(kind, entity_map, node) for node in page.nodes]
		return Page(nodes, page.next_path is not None, page.next_path)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		entity_map = self._map(kind)
		for node in self._odata.all(f"/{entity_map.entity}", self._query()):
			if key_of(Fields(node, entity_map.fields).get("key")) == identity.id:
				return self._assemble(kind, entity_map, node)
		return None

	def _map(self, kind: Kind) -> EntityMap:
		if kind not in TYPES:
			raise SourceError(f"Finance & Operations has no {kind.value}.")
		return usable(self._maps, kind)

	def _query(self) -> dict[str, QueryValue]:
		return {"cross-company": "true", "$filter": f"dataAreaId eq {quoted(self._legal_entity)}"}

	def _assemble(self, kind: Kind, entity_map: EntityMap, node: Node) -> Node:
		key = key_of(Fields(node, entity_map.fields).get("key"))
		extra: dict[str, JsonValue] = {"roles": dict(entity_map.fields)}
		if entity_map.lines:
			extra["lines"] = self._lines_of(kind, entity_map).get(key, [])
			extra["line_roles"] = dict(entity_map.line_fields)
		gid = self._config.identity(TYPES[kind], key)
		return Node({**node.raw(), **extra, "source_id": key, "id": gid}, gid)

	def _lines_of(self, kind: Kind, entity_map: EntityMap) -> Mapping[str, list[JsonValue]]:
		"""Every line of the kind's documents, by its document's key."""
		if kind not in self._lines:
			grouped: dict[str, list[JsonValue]] = {}
			for line in self._odata.all(f"/{entity_map.lines}", self._query()):
				order = Fields(line, entity_map.line_fields).get("order")
				grouped.setdefault(key_of(order), []).append(line.raw())
			self._lines[kind] = grouped
		return self._lines[kind]
