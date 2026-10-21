# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads a store from BigCommerce's REST Management API (page-numbered paging everywhere; v3 lists say how
many pages there are, v2 lists end at a short page), ported from wp-woo-idempotent-importer's
BigCommerceReader. Every node carries its identity as ``id`` (BigCommerce's own id is kept as
``source_id``). Products arrive with their variants, brand name and the store's weight unit; orders with
their lines, shipping addresses, taxes, coupons, shipments and refunds; promotions with their codes; blog
posts with the store's URL (their thumbnails are store paths). A discount run reads legacy coupons, then
promotions, then each customer group's discount rules (one node per rule), then the products that have bulk
pricing rules (one node per product); the cursor is ``<phase>:<page>``. Products, customers and orders are incremental (by
modification time); the other kinds are read in full.

BigCommerce's quota is per plan and reported on every response: the requests left in the window and the
milliseconds until it resets (docs.bigcommerce.com/docs/start/best-practices/api-rate-limits); the client
waits for the reset when the window runs out."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit, ResetFormat
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import LIST_KEY, QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page

SOURCE_ID = "bigcommerce"
API_BASE = "https://api.bigcommerce.com"
# v2 and v3 lists take up to 250 per page; each product is followed by a read of its variants.
PAGE_SIZE = 250
PRODUCT_PAGE_SIZE = 50
PRODUCT_INCLUDES = "images,custom_fields"
CUSTOMER_INCLUDES = "addresses,storecredit,attributes,formfields"
ORDER_PARTS = ("products", "shipping_addresses", "taxes", "coupons", "shipments")
BLOG_PAGE_TYPE = "blog"
COUPON_PROMOTION = "COUPON"
PHASE_COUPONS, PHASE_PROMOTIONS, PHASE_GROUPS, PHASE_BULK = "coupons", "promotions", "groups", "bulk"
PHASE_CERTIFICATES, PHASE_STORE_CREDIT = "certificates", "credit"
DISCOUNT_PHASES = (PHASE_COUPONS, PHASE_PROMOTIONS, PHASE_GROUPS, PHASE_BULK)
BULK_PRICING = "bulk_pricing_rules"
# A customer group discount rule's own identity: its group, its type and what it targets.
GROUP_RULE_TARGETS = {"category": "category_id", "product": "product_id", "price_list": "price_list_id"}
GROUP_RULE_SEPARATOR = "-"
CURSOR_SEPARATOR = ":"
FIRST_PAGE = 1
STORE_HASH = re.compile(r"^[a-z0-9]+$")
INCREMENTAL = frozenset({Kind.PRODUCT, Kind.CUSTOMER, Kind.ORDER})
RATE_LIMIT = RateLimit(
	remaining_header="X-Rate-Limit-Requests-Left",
	reset_header="X-Rate-Limit-Time-Reset-Ms",
	reset_format=ResetFormat.MILLISECONDS_FROM_NOW,
)

type Query = dict[str, QueryValue]


class BigCommerceAuthorizer:
	"""A store-level API account's access token in ``X-Auth-Token``; it lives until the account is deleted,
	so there is nothing to renew."""

	def __init__(self, access_token: str) -> None:
		if not access_token:
			raise SourceError("BigCommerce is not configured: enter the API account access token.")
		self._token = access_token

	def headers(self) -> dict[str, str]:
		return {"X-Auth-Token": self._token}

	def renew(self) -> bool:
		return False

	def __repr__(self) -> str:
		return "BigCommerceAuthorizer([redacted])"


def iso(moment: datetime) -> str:
	return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def page_number(cursor: str | None) -> int:
	return max(FIRST_PAGE, int(cursor)) if cursor else FIRST_PAGE


class BigCommerceReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		self._config = config
		self._client = client
		self._store: Node | None = None

	@classmethod
	def store_base(cls, config: ConnectionConfig) -> str:
		store_hash = config.account.strip()
		if not STORE_HASH.match(store_hash):
			raise SourceError(
				"BigCommerce is not configured: enter the store hash (the code after /stores/ in the API path) as "
				"the account."
			)
		return (config.endpoint_override or API_BASE).rstrip("/") + f"/stores/{store_hash}"

	def is_incremental(self, kind: Kind) -> bool:
		return kind in INCREMENTAL

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		match kind:
			case Kind.COLLECTION:
				items, following = self._v3_page("/v3/catalog/trees/categories", {}, cursor, PAGE_SIZE)
				return paged(
					[self._identified("Category", c.integer("category_id"), c.raw()) for c in items],
					following,
				)
			case Kind.PRODUCT:
				query: Query = {"include": PRODUCT_INCLUDES, "sort": "id", "direction": "asc"}
				if since is not None:
					query["date_modified:min"] = iso(since)
				items, following = self._v3_page("/v3/catalog/products", query, cursor, PRODUCT_PAGE_SIZE)
				return paged(self._products(items), following)
			case Kind.CUSTOMER_GROUP:
				items, following = self._v2_page("/v2/customer_groups", {}, cursor)
				return paged(
					[self._identified("CustomerGroup", g.integer("id"), g.raw()) for g in items], following
				)
			case Kind.CUSTOMER:
				query = {"include": CUSTOMER_INCLUDES, "sort": "date_created:asc"}
				if since is not None:
					query["date_modified:min"] = iso(since)
				items, following = self._v3_page("/v3/customers", query, cursor, PAGE_SIZE)
				return paged(
					[self._identified("Customer", c.integer("id"), c.raw()) for c in items], following
				)
			case Kind.DISCOUNT:
				return self._discounts(cursor)
			case Kind.ORDER:
				query = {"include": "fees", "sort": "id:asc"}
				if since is not None:
					query["min_date_modified"] = iso(since)
				items, following = self._v2_page("/v2/orders", query, cursor)
				return paged(self._orders(items), following)
			case Kind.PAGE:
				items, following = self._v3_page("/v3/content/pages", {"include": "body"}, cursor, PAGE_SIZE)
				pages = [p for p in items if p.string("type") != BLOG_PAGE_TYPE]
				return paged([self._identified("Page", p.integer("id"), p.raw()) for p in pages], following)
			case Kind.BLOG:
				return Page([self._blog()], False, None)
			case Kind.ARTICLE:
				items, following = self._v2_page("/v2/blog/posts", {}, cursor)
				return paged([self._post(post) for post in items], following)
			case Kind.URL_REDIRECT:
				items, following = self._v3_page(
					"/v3/storefront/redirects", {"include": "to_url"}, cursor, PAGE_SIZE
				)
				return paged(
					[self._identified("Redirect", r.integer("id"), r.raw()) for r in items], following
				)
			case Kind.REVIEW:
				return self._reviews(cursor)
			case Kind.GIFT_CARD:
				return self._gift_cards(cursor)
			case _:
				raise SourceError(f"BigCommerce has no {kind.value}.")

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		if kind == Kind.DISCOUNT and identity.type == "CustomerGroupRule":
			return self._group_rule(identity.id)
		if not identity.id.isdigit():
			raise SourceError(f"Not a BigCommerce identity: {gid}")
		source_id = identity.id
		match kind:
			case Kind.COLLECTION:
				return self._first(
					"/v3/catalog/trees/categories", {"category_id:in": source_id}, "Category", "category_id"
				)
			case Kind.PRODUCT:
				result = self._client.get(f"/v3/catalog/products/{source_id}", {"include": PRODUCT_INCLUDES})
				product = result.node.child("data") if result else None
				return None if product is None else self._products([product])[0]
			case Kind.CUSTOMER:
				return self._first(
					"/v3/customers", {"id:in": source_id, "include": CUSTOMER_INCLUDES}, "Customer", "id"
				)
			case Kind.CUSTOMER_GROUP:
				result = self._client.get(f"/v2/customer_groups/{source_id}")
				return (
					None
					if result is None
					else self._identified("CustomerGroup", source_id, result.node.raw())
				)
			case Kind.DISCOUNT if identity.type == "BulkPricing":
				result = self._client.get(f"/v3/catalog/products/{source_id}", {"include": BULK_PRICING})
				product = result.node.child("data") if result else None
				return None if product is None else self._bulk_pricing(product)
			case Kind.DISCOUNT if identity.type == "Promotion":
				result = self._client.get(f"/v3/promotions/{source_id}")
				promotion = result.node.child("data") if result else None
				return None if promotion is None else self._with_codes(promotion)
			case Kind.DISCOUNT:
				result = self._client.get("/v2/coupons", {"id": source_id})
				coupons = result.node.children(LIST_KEY) if result else []
				return (
					self._identified("Coupon", coupons[0].integer("id"), coupons[0].raw())
					if coupons
					else None
				)
			case Kind.ORDER:
				result = self._client.get(f"/v2/orders/{source_id}", {"include": "fees"})
				return None if result is None else self._orders([result.node])[0]
			case Kind.PAGE:
				result = self._client.get(f"/v3/content/pages/{source_id}", {"include": "body"})
				page = result.node.child("data") if result else None
				if page is None or page.string("type") == BLOG_PAGE_TYPE:
					return None
				return self._identified("Page", page.integer("id"), page.raw())
			case Kind.ARTICLE:
				result = self._client.get(f"/v2/blog/posts/{source_id}")
				return None if result is None else self._post(result.node)
			case Kind.URL_REDIRECT:
				return self._first(
					"/v3/storefront/redirects", {"id:in": source_id, "include": "to_url"}, "Redirect", "id"
				)
			case Kind.GIFT_CARD if identity.type == "StoreCredit":
				customer = self._first(
					"/v3/customers", {"id:in": source_id, "include": CUSTOMER_INCLUDES}, "Customer", "id"
				)
				return (
					self._store_credit(customer)
					if customer and customer.get("store_credit_amounts")
					else None
				)
			case Kind.GIFT_CARD:
				result = self._client.get(f"/v2/gift_certificates/{source_id}")
				return (
					None
					if result is None
					else self._identified("GiftCertificate", source_id, result.node.raw())
				)
			case _:
				raise SourceError(f"BigCommerce can't read a single {kind.value}.")

	def _products(self, products: list[Node]) -> list[Node]:
		"""Products with their variants (every page of them), brand name and the store's weight unit."""
		brand_ids = sorted({p.optional_integer("brand_id") or 0 for p in products} - {0})
		brands: dict[int, str] = {}
		if brand_ids:
			for brand in self._all_v3("/v3/catalog/brands", {"id:in": ",".join(str(b) for b in brand_ids)}):
				brands[brand.integer("id")] = brand.string("name")
		weight_units = self._store_node().optional_string("weight_units")
		assembled = []
		for product in products:
			product_id = product.integer("id")
			variants: list[JsonValue] = [
				v.raw() for v in self._all_v3(f"/v3/catalog/products/{product_id}/variants", {})
			]
			assembled.append(
				self._identified(
					"Product",
					product_id,
					{
						**product.raw(),
						"variants": variants,
						"brand": brands.get(product.optional_integer("brand_id") or 0),
						"weight_units": weight_units,
					},
				)
			)
		return assembled

	def _discounts(self, cursor: str | None) -> Page:
		"""Legacy coupons, then promotions, then customer groups' discount rules, then bulk pricing."""
		phase, _, page = (cursor or PHASE_COUPONS).partition(CURSOR_SEPARATOR)
		nodes: list[Node]
		match phase:
			case "coupons":
				coupons, following = self._v2_page("/v2/coupons", {"exclude_type": "promotion"}, page or None)
				nodes = [self._identified("Coupon", c.integer("id"), c.raw()) for c in coupons]
			case "promotions":
				promotions, following = self._v3_page("/v3/promotions", {}, page or None, PAGE_SIZE)
				nodes = [self._with_codes(p) for p in promotions]
			case "groups":
				groups, following = self._v2_page("/v2/customer_groups", {}, page or None)
				nodes = [rule for group in groups for rule in self._group_rules(group)]
			case "bulk":
				query: Query = {"include": BULK_PRICING, "include_fields": BULK_PRICING}
				products, following = self._v3_page("/v3/catalog/products", query, page or None, PAGE_SIZE)
				nodes = [self._bulk_pricing(p) for p in products if p.children(BULK_PRICING)]
			case _:
				raise SourceError(f"Not a BigCommerce discount cursor: {cursor}")
		if following:
			return Page(nodes, True, f"{phase}{CURSOR_SEPARATOR}{following}")
		index = DISCOUNT_PHASES.index(phase)
		if index + 1 < len(DISCOUNT_PHASES):
			return Page(nodes, True, f"{DISCOUNT_PHASES[index + 1]}{CURSOR_SEPARATOR}")
		return Page(nodes, False, None)

	def _group_rules(self, group: Node) -> list[Node]:
		"""A customer group's discount rules, each its own discount: identified by the group, the rule's type
		and its target (a group has at most one rule per target)."""
		group_id = group.integer("id")
		nodes = []
		for rule in group.children("discount_rules"):
			rule_type = rule.string("type")
			target = GROUP_RULE_TARGETS.get(rule_type)
			key = GROUP_RULE_SEPARATOR.join(
				(str(group_id), rule_type, *((str(rule.integer(target)),) if target else ()))
			)
			raw = {**rule.raw(), "group_id": group_id, "group_name": group.string("name")}
			nodes.append(self._identified("CustomerGroupRule", key, raw))
		return nodes

	def _group_rule(self, key: str) -> Node | None:
		group_id = key.split(GROUP_RULE_SEPARATOR, 1)[0]
		if not group_id.isdigit():
			raise SourceError(f"Not a BigCommerce customer group rule: {key}")
		result = self._client.get(f"/v2/customer_groups/{group_id}")
		rules = self._group_rules(result.node) if result else []
		return next((rule for rule in rules if rule.string("source_id") == key), None)

	def _bulk_pricing(self, product: Node) -> Node:
		product_id = product.integer("id")
		raw: dict[str, JsonValue] = {
			"product_id": product_id,
			BULK_PRICING: [r.raw() for r in product.children(BULK_PRICING)],
		}
		return self._identified("BulkPricing", product_id, raw)

	def _with_codes(self, promotion: Node) -> Node:
		"""A promotion, with every one of its codes when customers redeem it with a code."""
		promotion_id = promotion.integer("id")
		codes: list[JsonValue] = []
		if promotion.string("redemption_type") == COUPON_PROMOTION:
			after: str | None = None
			while True:
				query: Query = {"limit": PAGE_SIZE, **({"after": after} if after else {})}
				response = self._required(f"/v3/promotions/{promotion_id}/codes", query)
				codes += [code.raw() for code in response.children("data")]
				meta = response.child("meta")
				paging = meta.child("cursor_pagination") if meta else None
				links = paging.child("links") if paging else None
				after = (
					paging.optional_string("end_cursor")
					if paging and links and links.optional_string("next")
					else None
				)
				if after is None:
					break
		return self._identified("Promotion", promotion_id, {**promotion.raw(), "codes": codes})

	def _orders(self, orders: list[Node]) -> list[Node]:
		"""Orders with their v2 sub-resources (every page of each) and their refunds (read for the page)."""
		ids = [order.integer("id") for order in orders]
		refunds: dict[int, list[JsonValue]] = {}
		if ids:
			query: Query = {"order_id:in": ",".join(str(i) for i in ids)}
			for refund in self._all_v3("/v3/orders/payment_actions/refunds", query):
				refunds.setdefault(refund.integer("order_id"), []).append(refund.raw())
		assembled = []
		for order in orders:
			order_id = order.integer("id")
			parts: dict[str, JsonValue] = {}
			for part in ORDER_PARTS:
				query = {"details": "true"} if part == "taxes" else {}
				parts[part] = [item.raw() for item in self._all_v2(f"/v2/orders/{order_id}/{part}", query)]
			assembled.append(
				self._identified(
					"Order", order_id, {**order.raw(), **parts, "refunds": refunds.get(order_id, [])}
				)
			)
		return assembled

	def _blog(self) -> Node:
		"""The store's one blog, named by its blog page (BigCommerce keeps one per storefront)."""
		blog_page = next(
			(p for p in self._all_v3("/v3/content/pages", {}) if p.string("type") == BLOG_PAGE_TYPE), None
		)
		store_hash = self._config.account.strip()
		return self._identified("Blog", store_hash, {"page": blog_page.raw() if blog_page else None})

	def _post(self, post: Node) -> Node:
		"""A blog post with its blog's identity and the store's URL (thumbnails are paths on the store)."""
		return self._identified(
			"Post",
			post.integer("id"),
			{
				**post.raw(),
				"blog_id": self._config.account.strip(),
				"store_url": self._store_node().optional_string("secure_url"),
			},
		)

	def _reviews(self, cursor: str | None) -> Page:
		"""One page of products (only their review counts), as the reviews of those that have any."""
		query: Query = {"include_fields": "reviews_count", "sort": "id", "direction": "asc"}
		products, following = self._v3_page("/v3/catalog/products", query, cursor, PAGE_SIZE)
		nodes = []
		for product in products:
			if (product.optional_integer("reviews_count") or 0) < 1:
				continue
			product_id = product.integer("id")
			for review in self._all_v3(f"/v3/catalog/products/{product_id}/reviews", {}):
				nodes.append(
					self._identified(
						"Review", review.integer("id"), {**review.raw(), "product_id": product_id}
					)
				)
		return paged(nodes, following)

	def _gift_cards(self, cursor: str | None) -> Page:
		"""Gift certificates (v2), then each customer's store credit (as a StoreCredit node, in the store's
		currency); the cursor is "<phase>:<page>"."""
		phase, _, page = (cursor or PHASE_CERTIFICATES).partition(CURSOR_SEPARATOR)
		if phase == PHASE_CERTIFICATES:
			items, following = self._v2_page("/v2/gift_certificates", {}, page or None)
			nodes = [self._identified("GiftCertificate", c.integer("id"), c.raw()) for c in items]
			return Page(
				nodes,
				True,
				f"{phase}{CURSOR_SEPARATOR}{following}"
				if following
				else f"{PHASE_STORE_CREDIT}{CURSOR_SEPARATOR}",
			)
		customers, following = self._v3_page(
			"/v3/customers",
			{"include": CUSTOMER_INCLUDES, "sort": "date_created:asc"},
			page or None,
			PAGE_SIZE,
		)
		credits = [self._store_credit(c) for c in customers if c.get("store_credit_amounts")]
		return Page(
			credits,
			following is not None,
			f"{PHASE_STORE_CREDIT}{CURSOR_SEPARATOR}{following}" if following else None,
		)

	def _store_credit(self, customer: Node) -> Node:
		"""A customer's store credit, which BigCommerce keeps in the store's currency."""
		raw = {**customer.raw(), "currency": self._store_node().string("currency")}
		return self._identified(
			"StoreCredit",
			customer.string("source_id") if customer.has("source_id") else customer.integer("id"),
			raw,
		)

	def _store_node(self) -> Node:
		if self._store is None:
			self._store = self._required("/v2/store", {})
		return self._store

	def _v3_page(
		self, path: str, query: Query, cursor: str | None, limit: int
	) -> tuple[list[Node], str | None]:
		"""One page of a v3 list (``data``) and the next page number, if ``meta.pagination`` has more."""
		page = page_number(cursor)
		response = self._required(path, {**query, "page": page, "limit": limit})
		meta = response.child("meta")
		pagination = meta.child("pagination") if meta else None
		total = (pagination.optional_integer("total_pages") if pagination else None) or page
		return response.children("data"), str(page + 1) if page < total else None

	def _v2_page(self, path: str, query: Query, cursor: str | None) -> tuple[list[Node], str | None]:
		"""One page of a v2 list (a bare array, or 204 when empty) and the next page number, if it was full."""
		page = page_number(cursor)
		result = self._client.get(path, {**query, "page": page, "limit": PAGE_SIZE})
		items = result.node.children(LIST_KEY) if result else []
		return items, str(page + 1) if len(items) >= PAGE_SIZE else None

	def _all_v3(self, path: str, query: Query) -> list[Node]:
		"""Every item of a v3 list; a list with no paging information (refunds) ends at a short page."""
		items: list[Node] = []
		page = FIRST_PAGE
		while True:
			response = self._required(path, {**query, "page": page, "limit": PAGE_SIZE})
			data = response.children("data")
			items += data
			meta = response.child("meta")
			pagination = meta.child("pagination") if meta else None
			total = pagination.optional_integer("total_pages") if pagination else None
			if (len(data) < PAGE_SIZE) if total is None else page >= total:
				return items
			page += 1

	def _all_v2(self, path: str, query: Query) -> list[Node]:
		items: list[Node] = []
		cursor: str | None = None
		while True:
			page, cursor = self._v2_page(path, query, cursor)
			items += page
			if cursor is None:
				return items

	def _first(self, path: str, query: Query, type_: str, id_field: str) -> Node | None:
		result = self._client.get(path, query)
		found = result.node.children("data") if result else []
		return self._identified(type_, found[0].integer(id_field), found[0].raw()) if found else None

	def _required(self, path: str, query: Query) -> Node:
		result = self._client.get(path, query)
		if result is None:
			raise SourceError(f"BigCommerce has no {path} for this store.")
		return result.node

	def _identified(self, type_: str, id_: int | str, raw: Mapping[str, JsonValue]) -> Node:
		gid = self._config.identity(type_, id_)
		return Node({**raw, "source_id": str(id_), "id": gid}, gid)


def paged(nodes: list[Node], following: str | None) -> Page:
	return Page(nodes, following is not None, following)
