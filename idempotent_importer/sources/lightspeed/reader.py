# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads a shop from Lightspeed eCom's (C-Series) REST API (developers.lightspeedhq.com/ecom/):
``<cluster>/<language>/<resource>.json``, every response wrapped in the resource's name, lists paged by page
number (``page``, ``limit`` up to 250) and ending at a short page. Ported from wp-woo-idempotent-importer's
LightspeedReader. Every node carries its identity as ``id`` (Lightspeed's id as ``source_id``). Products
arrive with their variants, images, category ids and brand title; orders with their lines, shipments, credit
notes (each with its items) and the shop's currency; articles with their blog's URL; redirects with the
shop's domain; products and orders with whether the shop is B2B (it then shows prices without tax). A
discount run reads the discount codes, then the automatic discount rules.
Products, customers and orders are incremental (``updated_at_min``): a product also counts as changed when
one of its variants changed, and an order when one of its shipments or invoices did, so those kinds then read
the changed children and re-read their parents (the cursor is ``<phase>:<page>``).

Lightspeed limits each shop's API key to 300 requests per 5 minutes, 3,000 per hour and 12,000 per day (and
20 per second) (developers.lightspeedhq.com/ecom/introduction/rate-limiting/). Requests are paced under the
hourly bucket (one every 1.2 seconds, which also keeps under 300 per 5 minutes); a shop needing more than the
day's 12,000 runs out the daily bucket, and its 429 pauses the run until the quota frees up. The docs name no
rate-limit headers and no cool-down, so pacing is by rate alone."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page

SOURCE_ID = "lightspeed"
# API base per cluster (developers.lightspeedhq.com/ecom/introduction/clusters/).
CLUSTERS = {"eu1": "https://api.webshopapp.com", "us1": "https://api.shoplightspeed.com"}
LANGUAGES = frozenset(
	{"bg", "fc", "da", "nl", "en", "us", "fr", "de", "el", "it", "no", "pl", "pt", "ru", "es", "sv", "tr"}
)
ACCOUNT_SEPARATOR = "/"
PAGE_SIZE = 250
# Each product or order is followed by reads of its parts, so their pages are kept small.
PARENT_PAGE_SIZE = 50
RATE_LIMIT = RateLimit(requests_per_second=RateLimit.per_period(3_000, 3_600.0))
SINCE_FILTER = "updated_at_min"
# Lightspeed doesn't say which zone ``updated_at_min`` is in, so runs ask from a day earlier: nothing changed
# since the last run is missed, whatever the shop's clock (re-reading an unchanged object writes nothing).
SINCE_MARGIN = timedelta(days=1)
PHASE_PRODUCTS, PHASE_VARIANTS = "products", "variants"
PHASE_ORDERS, PHASE_SHIPMENTS, PHASE_INVOICES = "orders", "shipments", "invoices"
PHASE_CODES, PHASE_RULES = "codes", "rules"
CURSOR_SEPARATOR = ":"
FIRST_PAGE = 1
INCREMENTAL = frozenset({Kind.PRODUCT, Kind.CUSTOMER, Kind.ORDER})
# Kinds read as Lightspeed sends them: resource path, single envelope, list envelope, identity type.
PLAIN = {
	Kind.COLLECTION: ("/categories", "category", "categories", "Category"),
	Kind.CUSTOMER: ("/customers", "customer", "customers", "Customer"),
	Kind.PAGE: ("/textpages", "textpage", "textpages", "Textpage"),
	Kind.BLOG: ("/blogs", "blog", "blogs", "Blog"),
	Kind.REVIEW: ("/reviews", "review", "reviews", "Review"),
}

type Query = dict[str, QueryValue]


def unset(node: Node, field: str) -> Node | None:
	"""An object field Lightspeed sends as ``false`` when it is unset (an image, a parent, a brand)."""
	return None if node.get(field) in (False, None) else node.child(field)


def linked_id(node: Node, field: str) -> int | None:
	"""The id of the object a link field names (``{"resource": {"id": 12, …}}``); None when the field is unset
	or links to a list (``"id": false``)."""
	link = unset(node, field)
	resource = unset(link, "resource") if link else None
	return None if resource is None or resource.get("id") is False else resource.integer("id")


def api_base(config: ConnectionConfig) -> str:
	"""The API base and shop language from the account, ``<cluster>/<language>`` (e.g. ``us1/en``)."""
	cluster, _, language = config.account.strip().lower().partition(ACCOUNT_SEPARATOR)
	if language not in LANGUAGES:
		raise SourceError(
			"Lightspeed eCom is not configured: enter the cluster and shop language as the account, e.g. us1/en "
			f"(languages: {', '.join(sorted(LANGUAGES))})."
		)
	base = config.endpoint_override or CLUSTERS.get(cluster)
	if base is None:
		raise SourceError(
			f"Lightspeed eCom is not configured: the cluster is one of {' or '.join(CLUSTERS)}."
		)
	return f"{base.rstrip('/')}/{language}"


class LightspeedReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		self._config = config
		self._client = client
		self._shop: Node | None = None
		self._brands: dict[int, str | None] = {}
		self._blog_urls: dict[int, str] | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in INCREMENTAL

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		match kind:
			case Kind.PRODUCT:
				return self._products(cursor, since)
			case Kind.ORDER:
				return self._orders(cursor, since)
			case Kind.DISCOUNT:
				return self._discounts(cursor)
			case Kind.ARTICLE:
				articles, following = self._list_page(
					"/blogs/articles.json", "blogArticles", {}, page_of(cursor), PAGE_SIZE
				)
				return paged([self._with_blog(a) for a in articles], following)
			case Kind.URL_REDIRECT:
				redirects, following = self._list_page(
					"/redirects.json", "redirects", {}, page_of(cursor), PAGE_SIZE
				)
				return paged([self._with_domain(r) for r in redirects], following)
			case _ if kind in PLAIN:
				path, _, list_key, type_ = PLAIN[kind]
				query = changed_since(since) if kind in INCREMENTAL else {}
				items, following = self._list_page(
					f"{path}.json", list_key, query, page_of(cursor), PAGE_SIZE
				)
				return paged([self._identified(type_, i.integer("id"), i.raw()) for i in items], following)
			case _:
				raise SourceError(f"Lightspeed eCom has no {kind.value}.")

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		if not identity.id.isdigit():
			raise SourceError(f"Not a Lightspeed eCom identity: {gid}")
		object_id = int(identity.id)
		match kind:
			case Kind.DISCOUNT if identity.type == "DiscountRule":
				rule = self._one(f"/discount_rules/{object_id}.json", "discount_rule")
				return (
					None if rule is None else self._identified("DiscountRule", rule.integer("id"), rule.raw())
				)
			case Kind.DISCOUNT:
				code = self._one(f"/discounts/{object_id}.json", "discount")
				return None if code is None else self._identified("Discount", code.integer("id"), code.raw())
			case Kind.PRODUCT:
				return self._product(object_id)
			case Kind.ORDER:
				return self._order(object_id)
			case Kind.ARTICLE:
				article = self._one(f"/blogs/articles/{object_id}.json", "blogArticle")
				return None if article is None else self._with_blog(article)
			case Kind.URL_REDIRECT:
				redirect = self._one(f"/redirects/{object_id}.json", "redirect")
				return None if redirect is None else self._with_domain(redirect)
			case _ if kind in PLAIN:
				path, key, _, type_ = PLAIN[kind]
				item = self._one(f"{path}/{object_id}.json", key)
				return None if item is None else self._identified(type_, item.integer("id"), item.raw())
			case _:
				raise SourceError(f"Lightspeed eCom has no {kind.value}.")

	def _discounts(self, cursor: str | None) -> Page:
		"""Discount codes, then discount rules (applied without a code)."""
		phase, page = phase_of(cursor, PHASE_CODES)
		if phase == PHASE_CODES:
			codes, following = self._list_page("/discounts.json", "discounts", {}, page, PAGE_SIZE)
			nodes = [self._identified("Discount", c.integer("id"), c.raw()) for c in codes]
			return phased(nodes, PHASE_CODES, following, PHASE_RULES)
		if phase != PHASE_RULES:
			raise SourceError(f"Not a Lightspeed eCom discount cursor: {cursor}")
		rules, following = self._list_page("/discount_rules.json", "discount_rules", {}, page, PAGE_SIZE)
		nodes = [self._identified("DiscountRule", r.integer("id"), r.raw()) for r in rules]
		return phased(nodes, PHASE_RULES, following, None)

	def _products(self, cursor: str | None, since: datetime | None) -> Page:
		"""Products (changed since ``since``), then on an incremental run the products of changed variants."""
		phase, page = phase_of(cursor, PHASE_PRODUCTS)
		if phase == PHASE_PRODUCTS:
			products, following = self._list_page(
				"/products.json", "products", changed_since(since), page, PARENT_PAGE_SIZE
			)
			return phased(
				self._assemble_products(products),
				PHASE_PRODUCTS,
				following,
				PHASE_VARIANTS if since else None,
			)
		return self._changed_parents(
			"/variants.json", "variants", "product", since, PHASE_VARIANTS, page, None, self._product
		)

	def _product(self, product_id: int) -> Node | None:
		product = self._one(f"/products/{product_id}.json", "product")
		return None if product is None else self._assemble_products([product])[0]

	def _assemble_products(self, products: list[Node]) -> list[Node]:
		"""Products with every variant, their images, their categories' ids and their brand's title."""
		b2b = self._is_b2b()
		assembled = []
		for product in products:
			product_id = product.integer("id")
			categories: list[JsonValue] = []
			for link in self._required("/categories/products.json", {"product": product_id}).children(
				"categoriesProducts"
			):
				category = linked_id(link, "category")
				if category is None:
					raise SourceError(
						f"A category link of Lightspeed product {product_id} names no category."
					)
				categories.append(category)
			raw: dict[str, JsonValue] = {
				**product.raw(),
				"variants": [
					v.raw() for v in self._all("/variants.json", "variants", {"product": product_id})
				],
				"images": [
					i.raw()
					for i in self._required(f"/products/{product_id}/images.json", {}).children(
						"productImages"
					)
				],
				"category_ids": categories,
				"brand": self._brand_title(linked_id(product, "brand")),
				"shop_is_b2b": b2b,
			}
			assembled.append(self._identified("Product", product_id, raw))
		return assembled

	def _brand_title(self, brand_id: int | None) -> str | None:
		if brand_id is None:
			return None
		if brand_id not in self._brands:
			brand = self._one(f"/brands/{brand_id}.json", "brand")
			self._brands[brand_id] = brand.optional_string("title") if brand else None
		return self._brands[brand_id]

	def _orders(self, cursor: str | None, since: datetime | None) -> Page:
		"""Orders (changed since ``since``), then on an incremental run the orders of changed shipments and
		invoices."""
		phase, page = phase_of(cursor, PHASE_ORDERS)
		if phase == PHASE_ORDERS:
			orders, following = self._list_page(
				"/orders.json", "orders", changed_since(since), page, PARENT_PAGE_SIZE
			)
			return phased(
				self._assemble_orders(orders), PHASE_ORDERS, following, PHASE_SHIPMENTS if since else None
			)
		if phase == PHASE_SHIPMENTS:
			return self._changed_parents(
				"/shipments.json",
				"shipments",
				"order",
				since,
				PHASE_SHIPMENTS,
				page,
				PHASE_INVOICES,
				self._order,
			)
		return self._changed_parents(
			"/invoices.json", "invoices", "order", since, PHASE_INVOICES, page, None, self._order
		)

	def _order(self, order_id: int) -> Node | None:
		order = self._one(f"/orders/{order_id}.json", "order")
		return None if order is None else self._assemble_orders([order])[0]

	def _assemble_orders(self, orders: list[Node]) -> list[Node]:
		"""Orders with every line, their shipments, their credit notes (with their items), the shop's currency
		(orders don't name one) and whether the shop is B2B."""
		currency = self._store().required_child("currency").string("shortcode")
		b2b = self._is_b2b()
		assembled = []
		for order in orders:
			order_id = order.integer("id")
			credit_notes: list[JsonValue] = []
			for invoice in self._all("/invoices.json", "invoices", {"order": order_id}):
				if invoice.get("isCreditNote") is True:
					items = self._required(f"/invoices/{invoice.integer('id')}/items.json", {}).children(
						"invoiceItems"
					)
					credit_notes.append({**invoice.raw(), "items": [item.raw() for item in items]})
			raw: dict[str, JsonValue] = {
				**order.raw(),
				"products": [
					p.raw() for p in self._all(f"/orders/{order_id}/products.json", "orderProducts", {})
				],
				"shipments": [
					s.raw() for s in self._all("/shipments.json", "shipments", {"order": order_id})
				],
				"credit_notes": credit_notes,
				"currency": currency,
				"shop_is_b2b": b2b,
			}
			assembled.append(self._identified("Order", order_id, raw))
		return assembled

	def _with_blog(self, article: Node) -> Node:
		"""An article with its blog's URL (its storefront path is under the blog's)."""
		if self._blog_urls is None:
			self._blog_urls = {
				b.integer("id"): b.string("url") for b in self._all("/blogs.json", "blogs", {})
			}
		blog = linked_id(article, "blog")
		raw = {**article.raw(), "blog_url": self._blog_urls.get(blog) if blog is not None else None}
		return self._identified("Article", article.integer("id"), raw)

	def _with_domain(self, redirect: Node) -> Node:
		"""A redirect with the shop's domain (a target on it is an old storefront URL)."""
		raw = {**redirect.raw(), "shop_domain": self._store().optional_string("mainDomain")}
		return self._identified("Redirect", redirect.integer("id"), raw)

	def _changed_parents(
		self,
		path: str,
		key: str,
		parent_field: str,
		since: datetime | None,
		phase: str,
		page: int,
		next_phase: str | None,
		read: Callable[[int], Node | None],
	) -> Page:
		"""One page of the children changed since ``since`` (variants, shipments, invoices), as their parents
		read afresh (each once per page), then the next phase."""
		children, following = self._list_page(path, key, changed_since(since), page, PAGE_SIZE)
		parents = dict.fromkeys(p for child in children if (p := linked_id(child, parent_field)) is not None)
		nodes = [node for parent in parents if (node := read(parent)) is not None]
		return phased(nodes, phase, following, next_phase)

	def _store(self) -> Node:
		if self._shop is None:
			self._shop = self._required("/shop.json", {}).required_child("shop")
		return self._shop

	def _is_b2b(self) -> bool:
		return self._store().get("isB2b") is True

	def _list_page(
		self, path: str, key: str, query: Query, page: int, limit: int
	) -> tuple[list[Node], str | None]:
		"""One page of a list and the next page number, if this page was full."""
		items = self._required(path, {**query, "page": page, "limit": limit}).children(key)
		return items, str(page + 1) if len(items) >= limit else None

	def _all(self, path: str, key: str, query: Query) -> list[Node]:
		items: list[Node] = []
		page = FIRST_PAGE
		while True:
			batch, following = self._list_page(path, key, query, page, PAGE_SIZE)
			items += batch
			if following is None:
				return items
			page += 1

	def _one(self, path: str, key: str) -> Node | None:
		result = self._client.get(path)
		return None if result is None else result.node.required_child(key)

	def _required(self, path: str, query: Mapping[str, QueryValue]) -> Node:
		result = self._client.get(path, query)
		if result is None:
			raise SourceError(f"Lightspeed eCom has no {path} for this shop.")
		return result.node

	def _identified(self, type_: str, id_: int, raw: Mapping[str, JsonValue]) -> Node:
		gid = self._config.identity(type_, id_)
		return Node({**raw, "source_id": str(id_), "id": gid}, gid)


def changed_since(since: datetime | None) -> Query:
	if since is None:
		return {}
	return {SINCE_FILTER: (since - SINCE_MARGIN).astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")}


def page_of(cursor: str | None) -> int:
	return max(FIRST_PAGE, int(cursor)) if cursor else FIRST_PAGE


def phase_of(cursor: str | None, first: str) -> tuple[str, int]:
	if not cursor:
		return first, FIRST_PAGE
	phase, _, page = cursor.partition(CURSOR_SEPARATOR)
	return phase, page_of(page or None)


def phased(nodes: list[Node], phase: str, following: str | None, next_phase: str | None) -> Page:
	if following is not None:
		return Page(nodes, True, f"{phase}{CURSOR_SEPARATOR}{following}")
	return Page(nodes, next_phase is not None, f"{next_phase}{CURSOR_SEPARATOR}" if next_phase else None)


def paged(nodes: list[Node], following: str | None) -> Page:
	return Page(nodes, following is not None, following)
