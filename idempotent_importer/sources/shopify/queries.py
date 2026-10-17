# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every GraphQL document the Shopify adapter sends. Hand-written operations live in graphql/operations; the
list, by-id and nested-page operations of each EntityQuery are generated from one template each; every
fragment an operation spreads (transitively) is appended from graphql/fragments/<Name>.graphql. Ported from
wp-woo-idempotent-importer's QueryLibrary.

Every page size a generated operation requests is an ``Int!`` variable: ``$first`` for the operation's own
connection and ``NestedConnection.size_variable`` for each nested connection its node fragment pages
through. ``page_sizes`` gives their defaults; the client shrinks them when Shopify refuses a query as too
costly (``PageSizes``). Connections nothing pages through keep a literal ``first: 250`` (NESTED_PAGE_SIZE):
shrinking those would truncate them."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.kind import Kind

# The Shopify Admin API version every query is written and validated against.
API_VERSION = "2026-10"
GRAPHQL_DIR = Path(__file__).parent / "graphql"
# The default number of objects a list operation asks for.
PAGE_SIZE = 50
# Shopify's largest page: the default for nested connections, and the literal size of the unpaged ones.
NESTED_PAGE_SIZE = 250
FIRST_VARIABLE = "first"
SIZE_VARIABLE_SUFFIX = "First"
INCREMENTAL_SORT_KEY = "UPDATED_AT"
SPREAD = re.compile(r"\.\.\.\s*([A-Z][A-Za-z0-9_]*)")
METAFIELDS = "MetafieldConnectionFields"
SPDX_PREFIX = "# SPDX"


@dataclass(frozen=True)
class NestedConnection:
	"""A connection on a top-level node that can hold more than one page (e.g. Product.variants). Its
	selection is a connection-level fragment, shared by the main query and the follow-up page query. In the
	node's fragment its page size is the variable ``size_variable`` (e.g. ``variants(first:
	$variantsFirst)``), so the client can shrink it when Shopify refuses a query as too costly."""

	field: str
	fragment: str
	extra_arguments: str = ""

	@property
	def size_variable(self) -> str:
		return f"{self.field}{SIZE_VARIABLE_SUFFIX}"


@dataclass(frozen=True)
class EntityQuery:
	"""How one synced kind is read: its root connection, node fragment and paging abilities."""

	kind: Kind
	graphql_type: str
	connection: str
	fragment: str
	sort_key: str | None
	searchable: bool
	nested: tuple[NestedConnection, ...] = field(default=())
	node_field: str = "node"

	def is_incremental(self) -> bool:
		"""Whether runs can fetch only what changed (`updated_at:>=` and UPDATED_AT order)."""
		return self.searchable and self.sort_key == INCREMENTAL_SORT_KEY

	def list_operation(self) -> str:
		return f"{self.graphql_type}List"

	def node_operation(self) -> str:
		return f"{self.graphql_type}Node"

	def page_operation(self, nested: NestedConnection) -> str:
		return f"{self.graphql_type}{nested.field[0].upper()}{nested.field[1:]}Page"


def entity_queries() -> dict[Kind, EntityQuery]:
	"""The read definition of every kind the adapter lists, in SYNC_ORDER."""
	updated = INCREMENTAL_SORT_KEY
	metafields = NestedConnection("metafields", METAFIELDS)
	queries = (
		EntityQuery(
			Kind.COLLECTION, "Collection", "collections", "CollectionFields", updated, True, (metafields,)
		),
		EntityQuery(
			Kind.PRODUCT,
			"Product",
			"products",
			"ProductFields",
			updated,
			True,
			(
				NestedConnection("variants", "ProductVariantConnectionFields"),
				NestedConnection("media", "MediaConnectionFields"),
				NestedConnection("collections", "CollectionIdConnectionFields"),
				metafields,
			),
		),
		EntityQuery(
			Kind.CUSTOMER,
			"Customer",
			"customers",
			"CustomerFields",
			updated,
			True,
			(NestedConnection("addressesV2", "MailingAddressConnectionFields"), metafields),
		),
		EntityQuery(
			Kind.DISCOUNT,
			"DiscountNode",
			"discountNodes",
			"DiscountNodeFields",
			updated,
			True,
			(),
			"discountNode",
		),
		EntityQuery(
			Kind.ORDER,
			"Order",
			"orders",
			"OrderFields",
			updated,
			True,
			(
				NestedConnection("lineItems", "LineItemConnectionFields"),
				NestedConnection("shippingLines", "ShippingLineConnectionFields", "includeRemovals: false"),
				NestedConnection("events", "EventConnectionFields"),
				metafields,
			),
		),
		EntityQuery(Kind.PAGE, "Page", "pages", "PageFields", updated, True, (metafields,)),
		EntityQuery(Kind.BLOG, "Blog", "blogs", "BlogFields", None, True, (metafields,)),
		EntityQuery(Kind.ARTICLE, "Article", "articles", "ArticleFields", updated, True, (metafields,)),
		EntityQuery(Kind.COMMENT, "Comment", "comments", "CommentFields", None, True),
		EntityQuery(Kind.URL_REDIRECT, "UrlRedirect", "urlRedirects", "UrlRedirectFields", None, True),
	)
	return {query.kind: query for query in queries}


def entity_query(kind: Kind) -> EntityQuery:
	query = entity_queries().get(kind)
	if query is None:
		raise SourceError(f"Shopify has no {kind.value} list.")
	return query


class QueryLibrary:
	def __init__(self, directory: Path, queries: dict[Kind, EntityQuery]) -> None:
		self._directory = directory
		bodies: dict[str, str] = {}
		sizes: dict[str, dict[str, int]] = {}
		for path in sorted((directory / "operations").glob("*.graphql")):
			bodies[path.stem] = self._read(f"operations/{path.name}")
		for query in queries.values():
			nested_sizes = {nested.size_variable: NESTED_PAGE_SIZE for nested in query.nested}
			for nested in query.nested:
				bodies[query.page_operation(nested)] = self._page_body(query, nested)
				sizes[query.page_operation(nested)] = {FIRST_VARIABLE: NESTED_PAGE_SIZE}
			bodies[query.list_operation()] = self._list_body(query)
			sizes[query.list_operation()] = {FIRST_VARIABLE: PAGE_SIZE, **nested_sizes}
			bodies[query.node_operation()] = self._node_body(query)
			sizes[query.node_operation()] = nested_sizes
		self._bodies = dict(sorted(bodies.items()))
		self._sizes = sizes

	@classmethod
	def bundled(cls) -> QueryLibrary:
		return cls(GRAPHQL_DIR, entity_queries())

	def operations(self) -> list[str]:
		return list(self._bodies)

	def page_sizes(self, operation: str) -> dict[str, int]:
		"""The default page size of every size variable the operation declares (none for hand-written ones)."""
		if operation not in self._bodies:
			raise SourceError(f"Unknown GraphQL operation {operation}")
		return dict(self._sizes.get(operation, {}))

	def document(self, operation: str) -> str:
		body = self._bodies.get(operation)
		if body is None:
			raise SourceError(f"Unknown GraphQL operation {operation}")
		included: set[str] = set()
		pending = self._spreads(body)
		fragments = ""
		while pending:
			name = pending.pop(0)
			if name in included:
				continue
			included.add(name)
			fragment = self._read(f"fragments/{name}.graphql")
			fragments += "\n" + fragment
			pending.extend(self._spreads(fragment))
		return body + fragments

	@staticmethod
	def _nested_size_variables(query: EntityQuery) -> str:
		"""The ``, $<field>First: Int!`` declarations of the nested connections its node fragment uses."""
		return "".join(f", ${nested.size_variable}: Int!" for nested in query.nested)

	@classmethod
	def _list_body(cls, query: EntityQuery) -> str:
		variables = (
			"$first: Int!, $after: String"
			+ (", $query: String" if query.searchable else "")
			+ cls._nested_size_variables(query)
		)
		arguments = "first: $first, after: $after" + (", query: $query" if query.searchable else "")
		if query.sort_key is not None:
			arguments += f", sortKey: {query.sort_key}"
		return (
			f"query {query.list_operation()}({variables}) {{\n"
			f"  {query.connection}({arguments}) {{\n"
			"    pageInfo {\n      ...PageInfoFields\n    }\n"
			f"    nodes {{\n      ...{query.fragment}\n    }}\n"
			"  }\n}\n"
		)

	@classmethod
	def _node_body(cls, query: EntityQuery) -> str:
		return (
			f"query {query.node_operation()}($id: ID!{cls._nested_size_variables(query)}) {{\n"
			f"  {query.node_field}(id: $id) {{\n    ...{query.fragment}\n  }}\n}}\n"
		)

	@staticmethod
	def _page_body(query: EntityQuery, nested: NestedConnection) -> str:
		extra = f", {nested.extra_arguments}" if nested.extra_arguments else ""
		return (
			f"query {query.page_operation(nested)}($id: ID!, $after: String, $first: Int!) {{\n"
			"  node(id: $id) {\n"
			f"    ... on {query.graphql_type} {{\n"
			f"      {nested.field}(first: $first, after: $after{extra}) {{\n"
			f"        ...{nested.fragment}\n      }}\n    }}\n  }}\n}}\n"
		)

	@staticmethod
	def _spreads(document: str) -> list[str]:
		return list(dict.fromkeys(SPREAD.findall(document)))

	def _read(self, relative: str) -> str:
		path = self._directory / relative
		if not path.is_file():
			raise SourceError(f"Missing GraphQL document {relative}")
		lines = [line for line in path.read_text().splitlines() if not line.startswith(SPDX_PREFIX)]
		return "\n".join(lines).strip() + "\n"
