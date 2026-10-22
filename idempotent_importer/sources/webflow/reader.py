# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one Webflow site through the Data API v2 (api.webflow.com/v2), as Webflow's OpenAPI document
describes it (webflow/openapi-spec, openapi/v2.yml, pinned in tools/specs.py): its assets, its e-commerce
categories, products with their SKUs and each SKU's inventory, its orders and the customers they name, its
pages with their content (the page DOM), its CMS collections and their items, and its redirects.

The site's collections are e-commerce ones (Webflow's Products, SKUs and Categories, by their slugs
``product``, ``sku`` and ``category``) or the site's own CMS. A CMS item carries its collection's field
definitions, so its mapper can tell its body (rich text) and images from its other fields. A page carries
its static content nodes, page by page, and the site's assets by id (an image node names its asset); a page
that is a collection's template has no content of its own. A product carries its SKUs, each with its
inventory (read by id).

Lists page by ``offset`` and ``limit`` (100) until a page is short or the ``pagination.total`` is reached.
Incremental runs ask for the CMS items (categories and posts) changed since the last run
(``lastUpdated[gte]``); nothing else in the API filters by change, so the rest is read whole and skipped by
its hash.

Limits (developers.webflow.com/data/reference/rate-limits): 60 requests a minute per token on the Starter and
Basic plans (120 on CMS, e-commerce and Business), reported in ``X-RateLimit-Remaining``; a 429 says when to
retry in ``Retry-After``. The client paces one request a second and quiets for the minute when the window is
spent."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.common import chained

SOURCE_ID = "webflow"
API_HOST = "https://api.webflow.com"
API_PATH = "/v2"
PAGE_SIZE = 100
REQUESTS_PER_MINUTE = 60
WINDOW_SECONDS = 60.0
RATE_LIMIT = RateLimit(
	requests_per_second=RateLimit.per_period(REQUESTS_PER_MINUTE, WINDOW_SECONDS),
	remaining_header="x-ratelimit-remaining",
	window_seconds=WINDOW_SECONDS,
)
PRODUCTS, SKUS, CATEGORIES = "product", "sku", "category"
ECOMMERCE = frozenset({PRODUCTS, SKUS, CATEGORIES})
CHANGED_SINCE = "%Y-%m-%dT%H:%M:%S.000Z"
KINDS: tuple[Kind, ...] = (
	Kind.ASSET,
	Kind.COLLECTION,
	Kind.PRODUCT,
	Kind.CUSTOMER,
	Kind.ORDER,
	Kind.PAGE,
	Kind.BLOG,
	Kind.ARTICLE,
	Kind.URL_REDIRECT,
)
INCREMENTAL = frozenset({Kind.COLLECTION, Kind.ARTICLE})
CURSOR_SEPARATOR = ":"


class WebflowReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		if not config.account:
			raise SourceError("Webflow is not connected: use Connect to Webflow and choose the site first.")
		self._config = config
		self._client = client
		self._site = config.account
		self._collections: list[Node] | None = None
		self._fields: dict[str, list[JsonValue]] = {}
		self._assets: dict[str, JsonValue] | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in INCREMENTAL

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		site = f"/sites/{self._site}"
		match kind:
			case Kind.ASSET:
				return self._offset_page(
					f"{site}/assets", "assets", cursor, lambda r: [self._identified("Asset", r.raw())]
				)
			case Kind.COLLECTION:
				categories = self._ecommerce(CATEGORIES)
				if categories is None:
					return Page([], False, None)
				return self._items(categories, cursor, since, "Category")
			case Kind.PRODUCT:
				return self._offset_page(f"{site}/products", "items", cursor, self._product)
			case Kind.CUSTOMER:
				return self._offset_page(f"{site}/orders", "orders", cursor, self._customer)
			case Kind.ORDER:
				return self._offset_page(
					f"{site}/orders",
					"orders",
					cursor,
					lambda r: [self._identified("Order", r.raw(), "orderId")],
				)
			case Kind.PAGE:
				return self._offset_page(f"{site}/pages", "pages", cursor, self._page)
			case Kind.BLOG:
				collections = [
					self._identified("Collection", c.raw() | {"fields": self._fields_of(c)})
					for c in self._cms()
				]
				return Page(collections, False, None)
			case Kind.ARTICLE:
				collections = self._cms()
				return (
					chained(
						len(collections),
						cursor,
						lambda feed, at: self._items(collections[feed], at, since, "Item"),
					)
					if collections
					else Page([], False, None)
				)
			case Kind.URL_REDIRECT:
				return self._offset_page(
					f"{site}/redirects",
					"redirects",
					cursor,
					lambda r: [self._identified("Redirect", r.raw())],
				)
		raise SourceError(f"Webflow has no {kind.value}.")

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		if kind is Kind.PRODUCT and identity.type == "Product":
			result = self._client.get(f"{API_PATH}/sites/{self._site}/products/{identity.id}")
			return self._product(result.node)[0] if result else None
		raise SourceError(f"Webflow can't read a {identity.type} as a {kind.value} by itself.")

	# -- lists ----------------------------------------------------------------------------------------------

	def _offset_page(
		self,
		path: str,
		key: str,
		cursor: str | None,
		expand: Callable[[Node], list[Node]],
		query: dict[str, QueryValue] | None = None,
	) -> Page:
		offset = int(cursor) if cursor else 0
		listed = self._get(path, {**(query or {}), "offset": offset, "limit": PAGE_SIZE})
		records = listed.children(key)
		nodes = [node for record in records for node in expand(record)]
		pagination = listed.child("pagination")
		total = pagination.optional_integer("total") if pagination else None
		read = offset + len(records)
		more = len(records) == PAGE_SIZE and (total is None or read < total)
		return Page(nodes, more, str(read) if more else None)

	def _items(self, collection: Node, cursor: str | None, since: datetime | None, type_: str) -> Page:
		"""One page of a collection's items, each with its collection's fields."""
		query: dict[str, QueryValue] = {}
		if since is not None:
			query["lastUpdated[gte]"] = since.astimezone(UTC).strftime(CHANGED_SINCE)
		fields = self._fields_of(collection)
		collection_id = collection.string("id")
		return self._offset_page(
			f"/collections/{collection_id}/items",
			"items",
			cursor,
			lambda item: [
				self._identified(type_, item.raw() | {"collection": collection_id, "fields": fields})
			],
			query,
		)

	# -- e-commerce ------------------------------------------------------------------------------------------

	def _product(self, entry: Node) -> list[Node]:
		"""A product with its SKUs, each with its inventory."""
		product = entry.required_child("product")
		sku_collection = self._ecommerce(SKUS)
		skus: list[JsonValue] = []
		for sku in entry.children("skus"):
			inventory = (
				self._get(
					f"/collections/{sku_collection.string('id')}/items/{sku.string('id')}/inventory", {}
				)
				if sku_collection is not None
				else None
			)
			skus.append(sku.raw() | {"inventory": inventory.raw() if inventory else None})
		products = self._ecommerce(PRODUCTS)
		fields = self._fields_of(products) if products is not None else []
		return [self._identified("Product", product.raw() | {"skus": skus, "fields": fields})]

	def _customer(self, order: Node) -> list[Node]:
		"""The customer an order names, by their email (Webflow keeps no customer of its own)."""
		info = order.child("customerInfo")
		email = (info.optional_string("email") if info else None) or ""
		if not email or "/" in email:
			return []
		address = order.child("billingAddress")
		record: dict[str, JsonValue] = {
			"id": email.lower(),
			"fullName": info.optional_string("fullName") if info else None,
			"email": email.lower(),
			"billingAddress": address.raw() if address else None,
		}
		return [self._identified("Customer", record)]

	def _ecommerce(self, slug: str) -> Node | None:
		return next((c for c in self._site_collections() if c.optional_string("slug") == slug), None)

	# -- content --------------------------------------------------------------------------------------------

	def _page(self, page: Node) -> list[Node]:
		"""A page with its content nodes (none for a collection's template) and the site's assets."""
		if page.optional_string("collectionId"):
			return [self._identified("Page", page.raw() | {"nodes": [], "assets": {}})]
		nodes: list[JsonValue] = []
		offset = 0
		while True:
			dom = self._get(f"/pages/{page.string('id')}/dom", {"offset": offset, "limit": PAGE_SIZE})
			found = dom.children("nodes")
			nodes += [node.raw() for node in found]
			offset += len(found)
			pagination = dom.child("pagination")
			total = pagination.optional_integer("total") if pagination else None
			if len(found) < PAGE_SIZE or (total is not None and offset >= total):
				break
		return [self._identified("Page", page.raw() | {"nodes": nodes, "assets": self._site_assets()})]

	def _site_assets(self) -> dict[str, JsonValue]:
		"""The site's assets' hosted URLs, by id."""
		if self._assets is None:
			self._assets = {}
			offset = 0
			while True:
				listed = self._get(f"/sites/{self._site}/assets", {"offset": offset, "limit": PAGE_SIZE})
				assets = listed.children("assets")
				self._assets |= {asset.string("id"): asset.string("hostedUrl") for asset in assets}
				offset += len(assets)
				if len(assets) < PAGE_SIZE:
					break
		return self._assets

	def _site_collections(self) -> list[Node]:
		if self._collections is None:
			self._collections = self._get(f"/sites/{self._site}/collections", {}).children("collections")
		return self._collections

	def _cms(self) -> list[Node]:
		"""The site's own CMS collections (not e-commerce's)."""
		return [c for c in self._site_collections() if c.optional_string("slug") not in ECOMMERCE]

	def _fields_of(self, collection: Node) -> list[JsonValue]:
		"""A collection's field definitions (read once)."""
		collection_id = collection.string("id")
		if collection_id not in self._fields:
			detail = self._get(f"/collections/{collection_id}", {})
			self._fields[collection_id] = [field.raw() for field in detail.children("fields")]
		return self._fields[collection_id]

	# -- requests -------------------------------------------------------------------------------------------

	def _get(self, path: str, query: dict[str, QueryValue]) -> Node:
		result = self._client.get(API_PATH + path, query)
		if result is None:
			raise SourceError(f"Webflow has no {path}.")
		return result.node

	def _identified(self, type_: str, record: dict[str, JsonValue], key: str = "id") -> Node:
		source_id = record.get(key)
		if not isinstance(source_id, str) or not source_id:
			raise SourceError(f"A Webflow {type_} has no {key}.")
		gid = self._config.identity(type_, source_id)
		return Node({**record, "id": gid, "source_id": source_id}, gid)
