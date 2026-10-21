# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads a store from the WooCommerce REST API (``wc/v3``), its WordPress site's REST API (``wp/v2``) and,
when installed, the Redirection plugin's REST API. Lists are page-numbered (``X-WP-TotalPages`` says how
many there are); the cursor is the next page number. Every node carries its identity as ``id``. Variable
products arrive with their variations (``variation_nodes``), orders with their refunds and notes
(``refund_nodes``, ``note_nodes``) and posts with their author's display name (``author_name``).

Products, coupons, orders, pages and posts are incremental (``modified_after``, WooCommerce 5.8+ and
WordPress 5.7+, read as GMT); WooCommerce has no such filter for categories, customers or reviews, which
are read in full. Draft and private pages and posts need an Application Password: with a consumer key
WooCommerce authenticates only its own ``wc/`` routes, so WordPress content is read anonymously
(published only) and the Redirection plugin's redirects (admin only) are not read at all."""

from __future__ import annotations

import html
from collections.abc import Mapping
from datetime import UTC, datetime
from urllib.parse import urlsplit

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import LIST_KEY, BasicAuthorizer, QueryValue, RestClient, RestResult
from idempotent_importer.core.source import ConnectionConfig, Page

SOURCE_ID = "woocommerce"
API_PREFIX = "/wp-json"
TOTAL_PAGES_HEADER = "X-WP-TotalPages"
PAGE_SIZE = 100
# Each product is followed by a read of its variations and each order by its refunds and notes, so those
# pages are kept small.
ASSEMBLED_PAGE_SIZE = 25
# The Redirection plugin numbers pages from 0 and allows up to 200 per page (its OpenAPI document).
REDIRECT_PAGE_SIZE = 200
FIRST_PAGE = 1
WP_CONTENT_STATUSES = "publish,future,draft,pending,private"
WORDPRESS_LOGIN = "Application Password"
CONSUMER_KEY = "Consumer Key"
VARIABLE = "variable"
WEIGHT_UNIT_SETTING = "/wc/v3/settings/products/woocommerce_weight_unit"
CURRENCY_SETTING = "/wc/v3/settings/general/woocommerce_currency"
TAGS_PATH = "/wp/v2/tags"
INCREMENTAL = frozenset({Kind.PRODUCT, Kind.DISCOUNT, Kind.ORDER, Kind.PAGE, Kind.ARTICLE})
TYPES: Mapping[Kind, str] = {
	Kind.COLLECTION: "ProductCategory",
	Kind.PRODUCT: "Product",
	Kind.CUSTOMER: "Customer",
	Kind.DISCOUNT: "Coupon",
	Kind.ORDER: "Order",
	Kind.PAGE: "Page",
	Kind.BLOG: "Category",
	Kind.ARTICLE: "Post",
	Kind.REVIEW: "ProductReview",
	Kind.URL_REDIRECT: "Redirect",
	Kind.GIFT_CARD: "GiftCard",
}
PATHS: Mapping[Kind, str] = {
	Kind.COLLECTION: "/wc/v3/products/categories",
	Kind.PRODUCT: "/wc/v3/products",
	Kind.CUSTOMER: "/wc/v3/customers",
	Kind.DISCOUNT: "/wc/v3/coupons",
	Kind.ORDER: "/wc/v3/orders",
	Kind.PAGE: "/wp/v2/pages",
	Kind.BLOG: "/wp/v2/categories",
	Kind.ARTICLE: "/wp/v2/posts",
	Kind.REVIEW: "/wc/v3/products/reviews",
	Kind.URL_REDIRECT: "/redirection/v1/redirect",
	# The WooCommerce Gift Cards extension's REST API (https://woocommerce.com/document/gift-cards/rest-api-reference/).
	Kind.GIFT_CARD: "/wc/v3/gift-cards",
}


class WooCommerceReader:
	def __init__(self, config: ConnectionConfig, client: RestClient, anonymous: RestClient) -> None:
		self._config = config
		self._client = client
		# WordPress content when the credentials are a WooCommerce consumer key (see the module docstring).
		self._wordpress = client if config.auth_method == WORDPRESS_LOGIN else anonymous
		self._authors: dict[int, str] = {}
		self._tags: dict[int, str] = {}
		self._store_weight_unit: str | None = None
		self._store_currency: str | None = None

	@classmethod
	def connect(
		cls, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper
	) -> WooCommerceReader:
		url = urlsplit(config.store_url.strip())
		if url.scheme != "https" or not url.hostname:
			raise SourceError("WooCommerce is not configured: the store URL must be an https:// address.")
		if config.auth_method not in (WORDPRESS_LOGIN, CONSUMER_KEY):
			raise SourceError("WooCommerce is not configured: choose Application Password or Consumer Key.")
		base = (config.endpoint_override or config.store_url).rstrip("/") + API_PREFIX
		authorizer = BasicAuthorizer(
			config.api_user,
			config.api_secret,
			"WooCommerce is not configured: enter the user (or consumer key) and its secret.",
		)
		# WooCommerce documents no request limit for wc/v3 (its rate limiting covers only the Store API,
		# developer.woocommerce.com/docs/apis/store-api/rate-limiting), so requests aren't paced; 429s and
		# 5xx from a host's own limits are retried and paused on through RetryPolicy.
		client = RestClient("WooCommerce", base, authorizer, transport, sleeper)
		anonymous = RestClient("WordPress", base, NoAuthorizer(), transport, sleeper)
		return cls(config, client, anonymous)

	def is_incremental(self, kind: Kind) -> bool:
		return kind in INCREMENTAL

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		number = int(cursor) if cursor else FIRST_PAGE
		match kind:
			case Kind.URL_REDIRECT:
				return self._redirects(int(cursor) if cursor else 0)
			case Kind.PRODUCT:
				items, more = self._list(kind, number, since, ASSEMBLED_PAGE_SIZE, {"status": "any"})
				nodes = [self._product(item) for item in items]
			case Kind.ORDER:
				items, more = self._list(kind, number, since, ASSEMBLED_PAGE_SIZE, {"status": "any"})
				nodes = [self._order(item) for item in items]
			case Kind.CUSTOMER:
				items, more = self._list(kind, number, since, PAGE_SIZE, {"role": "all"})
				nodes = [self._identified(kind, item) for item in items]
			case Kind.REVIEW:
				items, more = self._list(kind, number, since, PAGE_SIZE, {"status": "all"})
				nodes = [self._identified(kind, item) for item in items]
			case Kind.PAGE | Kind.ARTICLE:
				items, more = self._list(kind, number, since, PAGE_SIZE, self._content_statuses())
				nodes = [self._content(kind, item) for item in items]
			case Kind.GIFT_CARD:
				items, more = self._list(kind, number, since, PAGE_SIZE, {})
				nodes = [self._gift_card(item) for item in items]
			case Kind.COLLECTION | Kind.DISCOUNT | Kind.BLOG:
				items, more = self._list(kind, number, since, PAGE_SIZE, {})
				nodes = [self._identified(kind, item) for item in items]
			case _:
				raise SourceError(f"WooCommerce has no {kind.value}.")
		return Page(nodes, more, str(number + 1) if more else None)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		if kind not in PATHS or kind is Kind.URL_REDIRECT or identity.type != TYPES[kind]:
			raise SourceError(f"WooCommerce can't read a single {kind.value} {gid}.")
		path = f"{PATHS[kind]}/{int(identity.id)}"
		client = self._wordpress if kind in (Kind.PAGE, Kind.BLOG, Kind.ARTICLE) else self._client
		result = client.get(path, {"context": "view"})
		if result is None:
			return None
		match kind:
			case Kind.PRODUCT:
				return self._product(result.node)
			case Kind.ORDER:
				return self._order(result.node)
			case Kind.PAGE | Kind.ARTICLE:
				return self._content(kind, result.node)
			case _:
				return self._identified(kind, result.node)

	def _list(
		self, kind: Kind, page: int, since: datetime | None, size: int, extra: Mapping[str, QueryValue]
	) -> tuple[list[Node], bool]:
		query: dict[str, QueryValue] = {
			"page": page,
			"per_page": size,
			"orderby": "id",
			"order": "asc",
			**extra,
		}
		if since is not None:
			query["modified_after"] = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
			query["dates_are_gmt"] = True
		client = self._wordpress if kind in (Kind.PAGE, Kind.BLOG, Kind.ARTICLE) else self._client
		result = self._required(client, PATHS[kind], query)
		return result.node.children(LIST_KEY), page < self._total_pages(result, page)

	def _all(self, path: str, query: Mapping[str, QueryValue]) -> list[Node]:
		"""Every item of a sub-list (a product's variations, an order's refunds)."""
		items: list[Node] = []
		page = FIRST_PAGE
		while True:
			result = self._required(self._client, path, {"page": page, "per_page": PAGE_SIZE, **query})
			items.extend(result.node.children(LIST_KEY))
			if page >= self._total_pages(result, page):
				return items
			page += 1

	def _product(self, product: Node) -> Node:
		variations: list[JsonValue] = []
		if product.string("type") == VARIABLE:
			variations = [
				variation.raw()
				for variation in self._all(f"/wc/v3/products/{product.integer('id')}/variations", {})
			]
		return self._identified(
			Kind.PRODUCT, product.with_fields(variation_nodes=variations, weight_unit=self._weight_unit())
		)

	def _gift_card(self, card: Node) -> Node:
		"""A gift card with the store's currency (cards are in it; the extension doesn't repeat it)."""
		if self._store_currency is None:
			setting = self._client.get(CURRENCY_SETTING)
			value = setting.node.get("value") if setting else None
			if not isinstance(value, str) or not value:
				raise SourceError("WooCommerce didn't say the store's currency.")
			self._store_currency = value
		return self._identified(Kind.GIFT_CARD, card.with_fields(currency=self._store_currency))

	def _weight_unit(self) -> str | None:
		"""The store's one weight unit (a WooCommerce setting, not part of each product)."""
		if self._store_weight_unit is None:
			setting = self._client.get(WEIGHT_UNIT_SETTING)
			value = setting.node.get("value") if setting else None
			self._store_weight_unit = value if isinstance(value, str) else ""
		return self._store_weight_unit or None

	def _order(self, order: Node) -> Node:
		order_id = order.integer("id")
		refunds: list[JsonValue] = [
			refund.raw() for refund in self._all(f"/wc/v3/orders/{order_id}/refunds", {})
		]
		result = self._required(self._client, f"/wc/v3/orders/{order_id}/notes", {"type": "any"})
		notes: list[JsonValue] = [note.raw() for note in result.node.children(LIST_KEY)]
		return self._identified(Kind.ORDER, order.with_fields(refund_nodes=refunds, note_nodes=notes))

	def _content(self, kind: Kind, item: Node) -> Node:
		if kind is Kind.ARTICLE:
			tags: list[JsonValue] = list(self._tag_names(item.integers("tags")))
			item = item.with_fields(author_name=self._author(item.integer("author")), tag_names=tags)
		return self._identified(kind, item)

	def _tag_names(self, ids: list[int]) -> list[str]:
		"""A post's tags by name (a post lists only their ids), in the post's order."""
		missing = [tag for tag in ids if tag not in self._tags]
		if missing:
			include = [str(tag) for tag in missing]
			result = self._wordpress.get(TAGS_PATH, {"include": include, "per_page": len(include)})
			for tag in result.node.children(LIST_KEY) if result else []:
				self._tags[tag.integer("id")] = html.unescape(tag.string("name"))
		return [self._tags[tag] for tag in ids if tag in self._tags]

	def _author(self, user_id: int) -> str | None:
		if user_id not in self._authors:
			result = self._wordpress.get(f"/wp/v2/users/{user_id}", {"context": "view"})
			self._authors[user_id] = result.node.text("name") if result else ""
		return self._authors[user_id] or None

	def _redirects(self, page: int) -> Page:
		if self._config.auth_method != WORDPRESS_LOGIN:
			return Page([], False, None)
		result = self._client.get(PATHS[Kind.URL_REDIRECT], {"page": page, "per_page": REDIRECT_PAGE_SIZE})
		if result is None:
			# The Redirection plugin isn't installed: the store has no redirects to read.
			return Page([], False, None)
		items = result.node.children("items")
		total = result.node.integer("total")
		more = (page + 1) * REDIRECT_PAGE_SIZE < total
		return Page(
			[self._identified(Kind.URL_REDIRECT, item) for item in items],
			more,
			str(page + 1) if more else None,
		)

	def _content_statuses(self) -> dict[str, QueryValue]:
		return {"status": WP_CONTENT_STATUSES} if self._config.auth_method == WORDPRESS_LOGIN else {}

	def _identified(self, kind: Kind, item: Node) -> Node:
		gid = self._config.identity(TYPES[kind], item.integer("id"))
		return Node({**item.raw(), "source_id": item.integer("id"), "id": gid}, gid)

	@staticmethod
	def _total_pages(result: RestResult, page: int) -> int:
		header = result.header(TOTAL_PAGES_HEADER)
		return int(header) if header and header.isdigit() else page

	@staticmethod
	def _required(client: RestClient, path: str, query: Mapping[str, QueryValue]) -> RestResult:
		result = client.get(path, query)
		if result is None:
			raise SourceError(f"{client.label} has no {path} for this store.")
		return result


class NoAuthorizer:
	"""Anonymous requests (published WordPress content)."""

	def headers(self) -> dict[str, str]:
		return {}

	def renew(self) -> bool:
		return False
