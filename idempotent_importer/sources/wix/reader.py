# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads a site from Wix's REST APIs (JSON POST queries; cursor paging where Wix has it, offset paging for the
V1 catalog, coupons and blog categories), ported from wp-woo-idempotent-importer's WixReader. Every node
carries its identity as ``id`` (Wix's own id is kept as ``source_id``). The site's Stores catalog version is
asked once: a V3 catalog's products arrive with their variants and inventory items (``variants``,
``inventory_items``), a V1 catalog's with their variants inline; both, and coupons, carry the catalog they
came from (``catalog``). Orders arrive with their payments, refunds and fulfillments; blog posts with the
site id (``blog_id``). The Blog kind is the site's blog, then its categories. Contacts and orders are
incremental (by update time), as are a V3 catalog's categories and products; an incremental product run
then also re-reads the products whose inventory changed (the cursor is ``<phase>:<cursor>``).

Wix documents no request rate or rate-limit headers; a 429 carries no timing hint and its docs say to wait a
minute (dev.wix.com/docs/api-reference/articles/work-with-wix-apis/troubleshooting/troubleshooting.md), so
the client sits out a minute on a 429 (which pauses the run)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, Page

SOURCE_ID = "wix"
API_BASE = "https://www.wixapis.com"
PAGE_SIZE = 100
RATE_LIMIT = RateLimit(cooldown_seconds=60.0)
SITE_HEADER = "wix-site-id"
STORES_NAMESPACE = "@wix/stores"
REVIEWS_NAMESPACE = "stores"
PRODUCT_FIELDS: list[JsonValue] = [
	"URL",
	"PLAIN_DESCRIPTION",
	"MEDIA_ITEMS_INFO",
	"DIRECT_CATEGORIES_INFO",
	"WEIGHT_MEASUREMENT_UNIT_INFO",
	"MERCHANT_DATA",
	"INFO_SECTION",
	"INFO_SECTION_PLAIN_DESCRIPTION",
]
VARIANT_FIELDS: list[JsonValue] = ["MERCHANT_DATA"]
CATEGORY_FIELDS: list[JsonValue] = ["DESCRIPTION"]
POST_FIELDSETS: list[JsonValue] = ["URL", "RICH_CONTENT", "SEO"]
BLOG_CATEGORY_FIELDSETS: list[JsonValue] = ["URL"]
V1_PRODUCT_OPTIONS: dict[str, JsonValue] = {
	"includeVariants": True,
	"includeHiddenProducts": True,
	"includeMerchantSpecificData": True,
}
PHASE_PRODUCTS, PHASE_STOCK = "products", "stock"
CURSOR_SEPARATOR = ":"
SINGLY_READABLE = frozenset({Kind.PRODUCT, Kind.CUSTOMER, Kind.ORDER})

type Body = dict[str, JsonValue]


class Catalog(Enum):
	"""Which Wix Stores catalog a site runs; the values are Wix's own."""

	V1 = "V1_CATALOG"
	V3 = "V3_CATALOG"
	NONE = "STORES_NOT_INSTALLED"


class WixAuthorizer:
	"""An account API key in ``Authorization`` and the site it reads in ``wix-site-id`` (a key isn't bound to
	one site). The key lives until it is revoked: nothing to renew."""

	def __init__(self, api_key: str, site_id: str) -> None:
		if not api_key:
			raise SourceError("Wix is not configured: enter the API key.")
		if not site_id:
			raise SourceError("Wix is not configured: enter the site ID as the account.")
		self._key = api_key
		self._site = site_id

	def headers(self) -> dict[str, str]:
		return {"Authorization": self._key, SITE_HEADER: self._site}

	def renew(self) -> bool:
		return False

	def __repr__(self) -> str:
		return "WixAuthorizer([redacted])"


def wix_time(moment: datetime) -> str:
	return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def oldest_first(field: str) -> list[JsonValue]:
	return [{"fieldName": field, "order": "ASC"}]


def list_query(sort_field: str, since: datetime | None) -> Body:
	"""A list's first-page query: oldest first, and only what was updated since ``since``."""
	query: Body = {"sort": oldest_first(sort_field)}
	if since is not None:
		query["filter"] = {"updatedDate": {"$gt": wix_time(since)}}
	return query


def phase_of(cursor: str | None) -> tuple[str, str | None]:
	if cursor is None:
		return PHASE_PRODUCTS, None
	phase, _, wix_cursor = cursor.partition(CURSOR_SEPARATOR)
	return (PHASE_STOCK if phase == PHASE_STOCK else PHASE_PRODUCTS), wix_cursor or None


def paged(nodes: list[Node], following: str | None) -> Page:
	return Page(nodes, following is not None, following)


class WixReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		self._config = config
		self._client = client
		self._site = config.account.strip()
		self._catalog: Catalog | None = None

	def is_incremental(self, kind: Kind) -> bool:
		if kind in (Kind.CUSTOMER, Kind.ORDER):
			return True
		return kind in (Kind.COLLECTION, Kind.PRODUCT) and self.catalog() is Catalog.V3

	def catalog(self) -> Catalog:
		"""The site's Stores catalog version, asked once."""
		if self._catalog is None:
			result = self._client.get("/stores/v3/provision/version")
			if result is None:
				raise SourceError("Wix could not say which Stores catalog this site has.")
			version = result.node.string("catalogVersion")
			try:
				self._catalog = Catalog(version)
			except ValueError:
				raise SourceError(f"Wix has an unknown Stores catalog version: {version}.") from None
		return self._catalog

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		match kind:
			case Kind.COLLECTION | Kind.PRODUCT if self.catalog() is Catalog.NONE:
				return Page([], False, None)
			case Kind.COLLECTION if self.catalog() is Catalog.V3:
				return self._categories(cursor, since)
			case Kind.COLLECTION:
				collections, following = self._offset_page(
					"/stores-reader/v1/collections/query",
					{},
					{"includeDescription": True},
					"collections",
					cursor,
				)
				return paged([self._identified("Collection", c.raw()) for c in collections], following)
			case Kind.PRODUCT if self.catalog() is Catalog.V3:
				return self._v3_products(cursor, since)
			case Kind.PRODUCT:
				products, following = self._offset_page(
					"/stores-reader/v1/products/query", {}, V1_PRODUCT_OPTIONS, "products", cursor
				)
				return paged([self._v1_product(p) for p in products], following)
			case Kind.CUSTOMER:
				contacts, following = self._cursor_page(
					"/contacts/v5/contacts/query",
					"query",
					list_query("createdDate", since),
					{},
					"contacts",
					"pagingMetadata",
					cursor,
				)
				return paged([self._identified("Contact", c.raw()) for c in contacts], following)
			case Kind.DISCOUNT:
				coupons, following = self._offset_page("/stores/v2/coupons/query", {}, {}, "coupons", cursor)
				catalog = self.catalog().value
				return paged(
					[self._identified("Coupon", {**c.raw(), "catalog": catalog}) for c in coupons], following
				)
			case Kind.ORDER:
				orders, following = self._cursor_page(
					"/ecom/v1/orders/search",
					"search",
					list_query("createdDate", since),
					{},
					"orders",
					"metadata",
					cursor,
				)
				return paged(self._orders(orders), following)
			case Kind.BLOG:
				return self._blogs(cursor)
			case Kind.ARTICLE:
				posts, following = self._cursor_page(
					"/blog/v3/posts/query",
					"query",
					{"sort": oldest_first("firstPublishedDate")},
					{"fieldsets": POST_FIELDSETS},
					"posts",
					"pagingMetadata",
					cursor,
				)
				return paged(
					[self._identified("Post", {**p.raw(), "blog_id": self._site}) for p in posts], following
				)
			case Kind.URL_REDIRECT:
				result = self._client.get("/seo-redirects-service/v1/redirects")
				if result is None:
					raise SourceError("Wix has no redirects list for this site.")
				return Page(
					[self._identified("Redirect", r.raw()) for r in result.node.children("redirects")],
					False,
					None,
				)
			case Kind.REVIEW:
				reviews, following = self._cursor_page(
					"/reviews/v1/reviews/query",
					"query",
					{"filter": {"namespace": REVIEWS_NAMESPACE}, "sort": oldest_first("createdDate")},
					{"returnPrivateReviews": True},
					"reviews",
					"metadata",
					cursor,
				)
				return paged([self._identified("Review", r.raw()) for r in reviews], following)
			case _:
				raise SourceError(f"Wix has no {kind.value}.")

	def node(self, kind: Kind, gid: str) -> Node | None:
		if kind not in SINGLY_READABLE:
			raise SourceError(f"Wix can't read a single {kind.value}.")
		source_id = Identity.parse(gid).id
		match kind:
			case Kind.PRODUCT if self.catalog() is Catalog.V1:
				query: Body = {"filter": json.dumps({"id": source_id}, separators=(",", ":"))}
				found = self._required(
					"/stores-reader/v1/products/query", {"query": query, **V1_PRODUCT_OPTIONS}
				)
				products = found.children("products")
				return self._v1_product(products[0]) if products else None
			case Kind.PRODUCT:
				filtered: Body = {"filter": {"id": {"$in": [source_id]}}}
				products = self._all_cursor(
					"/stores/v3/products/query",
					"query",
					filtered,
					{"fields": PRODUCT_FIELDS},
					"products",
					"pagingMetadata",
				)
				assembled = self._assemble_v3(products)
				return assembled[0] if assembled else None
			case Kind.CUSTOMER:
				filtered = {"filter": {"id": {"$in": [source_id]}}}
				contacts, _ = self._cursor_page(
					"/contacts/v5/contacts/query", "query", filtered, {}, "contacts", "pagingMetadata", None
				)
				return self._identified("Contact", contacts[0].raw()) if contacts else None
			case _:
				search: Body = {"filter": {"id": {"$eq": source_id}}, "cursorPaging": {"limit": PAGE_SIZE}}
				orders = self._required("/ecom/v1/orders/search", {"search": search}).children("orders")
				return self._orders(orders)[0] if orders else None

	def _categories(self, cursor: str | None, since: datetime | None) -> Page:
		"""A V3 catalog's categories of the Stores tree, hidden ones included."""
		options: Body = {
			"treeReference": {"appNamespace": STORES_NAMESPACE},
			"returnNonVisibleCategories": True,
			"fields": CATEGORY_FIELDS,
		}
		categories, following = self._cursor_page(
			"/categories/v1/categories/query",
			"query",
			list_query("createdDate", since),
			options,
			"categories",
			"pagingMetadata",
			cursor,
		)
		return paged([self._identified("Category", c.raw()) for c in categories], following)

	def _v3_products(self, cursor: str | None, since: datetime | None) -> Page:
		"""A V3 catalog's products (updated since ``since`` on an incremental run), then on an incremental run
		the products whose inventory changed since then: an inventory change doesn't update its product."""
		phase, wix_cursor = phase_of(cursor)
		if phase == PHASE_PRODUCTS:
			products, following = self._cursor_page(
				"/stores/v3/products/query",
				"query",
				list_query("createdDate", since),
				{"fields": PRODUCT_FIELDS},
				"products",
				"pagingMetadata",
				wix_cursor,
			)
			nodes = self._assemble_v3(products)
			if following is not None:
				return Page(nodes, True, f"{PHASE_PRODUCTS}{CURSOR_SEPARATOR}{following}")
			return Page(
				nodes, since is not None, f"{PHASE_STOCK}{CURSOR_SEPARATOR}" if since is not None else None
			)
		if since is None:
			return Page([], False, None)
		items, following = self._cursor_page(
			"/stores/v3/inventory-items/query",
			"query",
			list_query("updatedDate", since),
			{},
			"inventoryItems",
			"pagingMetadata",
			wix_cursor,
		)
		ids: list[JsonValue] = list(dict.fromkeys(item.string("productId") for item in items))
		restocked: list[Node] = []
		if ids:
			restocked = self._all_cursor(
				"/stores/v3/products/query",
				"query",
				{"filter": {"id": {"$in": ids}}},
				{"fields": PRODUCT_FIELDS},
				"products",
				"pagingMetadata",
			)
		return paged(
			self._assemble_v3(restocked), f"{PHASE_STOCK}{CURSOR_SEPARATOR}{following}" if following else None
		)

	def _assemble_v3(self, products: list[Node]) -> list[Node]:
		"""V3 products with their variants and inventory items (every page of both, read for the page)."""
		ids: list[JsonValue] = [product.string("id") for product in products]
		if not ids:
			return []
		variants: dict[str, list[JsonValue]] = {}
		for variant in self._all_cursor(
			"/stores/v3/products/query-variants",
			"query",
			{"filter": {"productData.productId": {"$in": ids}}},
			{"fields": VARIANT_FIELDS},
			"variants",
			"pagingMetadata",
		):
			variants.setdefault(variant.required_child("productData").string("productId"), []).append(
				variant.raw()
			)
		inventory: dict[str, list[JsonValue]] = {}
		for item in self._all_cursor(
			"/stores/v3/inventory-items/query",
			"query",
			{"filter": {"productId": {"$in": ids}}},
			{},
			"inventoryItems",
			"pagingMetadata",
		):
			inventory.setdefault(item.string("productId"), []).append(item.raw())
		return [
			self._identified(
				"Product",
				{
					**product.raw(),
					"catalog": Catalog.V3.value,
					"variants": variants.get(product.string("id"), []),
					"inventory_items": inventory.get(product.string("id"), []),
				},
			)
			for product in products
		]

	def _v1_product(self, product: Node) -> Node:
		return self._identified("Product", {**product.raw(), "catalog": Catalog.V1.value})

	def _orders(self, orders: list[Node]) -> list[Node]:
		"""Orders with their payments, refunds and fulfillments, each read for the whole page."""
		ids: list[JsonValue] = [order.string("id") for order in orders]
		if not ids:
			return []
		transactions = {
			entry.string("orderId"): entry
			for entry in self._required("/ecom/v1/payments/list-by-ids", {"orderIds": ids}).children(
				"orderTransactions"
			)
		}
		fulfillments = {
			entry.string("orderId"): [f.raw() for f in entry.children("fulfillments")]
			for entry in self._required("/ecom/v1/fulfillments/list-by-ids", {"orderIds": ids}).children(
				"ordersWithFulfillments"
			)
		}
		assembled = []
		for order in orders:
			entry = transactions.get(order.string("id"))
			raw: dict[str, JsonValue] = {
				**order.raw(),
				"payments": [p.raw() for p in entry.children("payments")] if entry else [],
				"refunds": [r.raw() for r in entry.children("refunds")] if entry else [],
				"fulfillments": list[JsonValue](fulfillments.get(order.string("id"), [])),
			}
			assembled.append(self._identified("Order", raw))
		return assembled

	def _blogs(self, cursor: str | None) -> Page:
		"""The site's blog first (on the first page), then the blog's categories."""
		categories, following = self._offset_page(
			"/blog/v3/categories/query", {}, {"fieldsets": BLOG_CATEGORY_FIELDSETS}, "categories", cursor
		)
		nodes = [self._identified("BlogCategory", c.raw()) for c in categories]
		if cursor is None:
			nodes.insert(0, self._identified("Blog", {"id": self._site}))
		return paged(nodes, following)

	def _cursor_page(
		self,
		path: str,
		envelope: str,
		query: Body,
		options: Body,
		list_key: str,
		metadata_key: str,
		cursor: str | None,
	) -> tuple[list[Node], str | None]:
		"""One page of a cursor-paged query and the next cursor. The first page sends ``query`` (its filter and
		sort) with the page size; later pages send only the cursor, as Wix requires."""
		paging: Body = {"limit": PAGE_SIZE, **({"cursor": cursor} if cursor else {})}
		envelope_body: Body = {"cursorPaging": paging} if cursor else {**query, "cursorPaging": paging}
		response = self._required(path, {envelope: envelope_body, **options})
		metadata = response.child(metadata_key)
		cursors = metadata.child("cursors") if metadata else None
		following = cursors.optional_string("next") if cursors else None
		if metadata is not None and metadata.get("hasNext") is False:
			following = None
		return response.children(list_key), following

	def _all_cursor(
		self, path: str, envelope: str, query: Body, options: Body, list_key: str, metadata_key: str
	) -> list[Node]:
		items: list[Node] = []
		cursor: str | None = None
		while True:
			page, cursor = self._cursor_page(path, envelope, query, options, list_key, metadata_key, cursor)
			items += page
			if cursor is None:
				return items

	def _offset_page(
		self, path: str, query: Body, options: Body, list_key: str, cursor: str | None
	) -> tuple[list[Node], str | None]:
		"""One page of an offset-paged query (``query.paging``) and the next offset, if this page was full."""
		offset = int(cursor) if cursor and cursor.isdigit() else 0
		body: Body = {"query": {**query, "paging": {"limit": PAGE_SIZE, "offset": offset}}, **options}
		items = self._required(path, body).children(list_key)
		return items, str(offset + len(items)) if len(items) >= PAGE_SIZE else None

	def _required(self, path: str, body: Body) -> Node:
		result = self._client.query(path, body)
		if result is None:
			raise SourceError(f"Wix has no {path} for this site.")
		return result.node

	def _identified(self, type_: str, raw: Mapping[str, JsonValue]) -> Node:
		source_id = raw.get("id")
		if not isinstance(source_id, str) or not source_id:
			raise SourceError(f"A Wix {type_} has no id.")
		gid = self._config.identity(type_, source_id)
		return Node({**raw, "source_id": source_id, "id": gid}, gid)
