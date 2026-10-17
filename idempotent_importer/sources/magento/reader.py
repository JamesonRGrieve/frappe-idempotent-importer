# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one store view of a Magento 2.4 / Adobe Commerce store through its REST API
(``<store URL>/rest/<store code>/V1/…``, so names and descriptions are the store view's), ported from
wp-woo-idempotent-importer's MagentoReader. Lists are searches paged by searchCriteria (page number and
size); Magento answers a page past the end with the last page again, so a list ends when the items read reach
its ``total_count``. Every node carries its identity as ``id`` (Magento's id as ``source_id``). Products are
the catalog-visible ones (a product "not visible individually" is a configurable product's child) and arrive
with the store's context (media URL, website, root categories, weight unit), and a configurable product with
its child products and the attributes it is configured by; cart price rules with their coupons and every
customer group; orders (of the store's website's store views) with their credit memos and shipments.
Customers and orders are incremental (by ``updated_at``). Products aren't: Magento doesn't move a product's
``updated_at`` when its stock or a configurable product's child changes.

Magento Open Source and Adobe Commerce document no request-rate limit for these REST endpoints (2.4.7's
optional rate limiting, off by default, covers only cart payment and order placement:
developer.adobe.com/commerce/webapi/get-started/rate-limiting), so requests aren't paced; a 429 from a store
behind a CDN is waited out for its Retry-After, else backed off, and a long wait pauses the run. Searches ask
for 100 items, under the default "Maximum Page Size" of 300."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import LIST_KEY, QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page

SOURCE_ID = "magento"
DEFAULT_STORE_CODE = "default"
PAGE_SIZE = 100
NOT_VISIBLE_INDIVIDUALLY = 1
CONFIGURABLE = "configurable"
# Categories at levels 0 and 1 are the root catalog and each store's root category, never shown.
ROOT_LEVEL = 1
FIRST_PAGE = 1
STORE_CODE = re.compile(r"^[a-z][a-z0-9_]*$")
STORE_URL = re.compile(r"^https?://[^/]+", re.IGNORECASE)
INCREMENTAL = frozenset({Kind.CUSTOMER, Kind.ORDER})
SINGLY_READABLE = frozenset(
	{Kind.COLLECTION, Kind.PRODUCT, Kind.CUSTOMER, Kind.DISCOUNT, Kind.ORDER, Kind.PAGE}
)

type Filter = tuple[str, str, str]


class MagentoAuthorizer:
	"""An integration's access token as a bearer token (since 2.4.4 the store must allow integration tokens as
	standalone bearer tokens). It lives until the integration is reauthorized: nothing to renew."""

	def __init__(self, access_token: str) -> None:
		if not access_token:
			raise SourceError("Magento is not configured: enter the integration access token.")
		self._token = access_token

	def headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self._token}"}

	def renew(self) -> bool:
		return False

	def __repr__(self) -> str:
		return "MagentoAuthorizer([redacted])"


def store_code_of(config: ConnectionConfig) -> str:
	code = config.account.strip() or DEFAULT_STORE_CODE
	if not STORE_CODE.match(code):
		raise SourceError(
			'Magento is not configured: the store view code is letters, digits and underscores (e.g. "default").'
		)
	return code


def api_base(config: ConnectionConfig) -> str:
	store_url = (config.endpoint_override or config.store_url).strip().rstrip("/")
	if not STORE_URL.match(store_url):
		raise SourceError("Magento is not configured: enter the store URL (https://shop.example).")
	return f"{store_url}/rest/{store_code_of(config)}"


def search_query(filters: list[Filter], sort_field: str | None, page: int) -> dict[str, QueryValue]:
	"""A search, each filter in its own filter group (so they must all match)."""
	query: dict[str, QueryValue] = {}
	for group, (field, value, condition) in enumerate(filters):
		prefix = f"searchCriteria[filterGroups][{group}][filters][0]"
		query[f"{prefix}[field]"] = field
		query[f"{prefix}[value]"] = value
		query[f"{prefix}[conditionType]"] = condition
	if sort_field is not None:
		query["searchCriteria[sortOrders][0][field]"] = sort_field
		query["searchCriteria[sortOrders][0][direction]"] = "ASC"
	query["searchCriteria[pageSize]"] = PAGE_SIZE
	query["searchCriteria[currentPage]"] = page
	return query


def changed_since(since: datetime | None) -> list[Filter]:
	if since is None:
		return []
	return [("updated_at", since.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"), "gt")]


def paged(nodes: list[Node], following: str | None) -> Page:
	return Page(nodes, following is not None, following)


class MagentoReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		self._config = config
		self._client = client
		self._code = store_code_of(config)
		self._store_config: Node | None = None
		self._views: list[Node] | None = None
		self._groups: list[Node] | None = None
		self._customer_groups: list[JsonValue] | None = None
		self._attributes: dict[str, JsonValue] = {}

	def is_incremental(self, kind: Kind) -> bool:
		return kind in INCREMENTAL

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		match kind:
			case Kind.COLLECTION:
				categories, following = self._search_page(
					"/V1/categories/list", [("level", str(ROOT_LEVEL), "gt")], "entity_id", cursor
				)
				return paged([self._with_store("Category", c) for c in categories], following)
			case Kind.PRODUCT:
				products, following = self._search_page(
					"/V1/products",
					[("visibility", str(NOT_VISIBLE_INDIVIDUALLY), "neq")],
					"entity_id",
					cursor,
				)
				return paged([self._product(p) for p in products], following)
			case Kind.CUSTOMER:
				customers, following = self._search_page(
					"/V1/customers/search", changed_since(since), "entity_id", cursor
				)
				return paged(
					[self._identified("Customer", c.integer("id"), c.raw()) for c in customers], following
				)
			case Kind.DISCOUNT:
				rules, following = self._search_page("/V1/salesRules/search", [], "rule_id", cursor)
				return paged(self._rules(rules), following)
			case Kind.ORDER:
				views = ",".join(str(i) for i in self._website_store_ids())
				filters: list[Filter] = [("store_id", views, "in"), *changed_since(since)]
				orders, following = self._search_page("/V1/orders", filters, "entity_id", cursor)
				return paged(self._orders(orders), following)
			case Kind.PAGE:
				pages, following = self._search_page("/V1/cmsPage/search", [], "page_id", cursor)
				return paged(
					[self._identified("CmsPage", p.integer("id"), p.raw()) for p in pages], following
				)
			case _:
				raise SourceError(f"Magento has no {kind.value}.")

	def node(self, kind: Kind, gid: str) -> Node | None:
		if kind not in SINGLY_READABLE:
			raise SourceError(f"Magento can't read a single {kind.value}.")
		source_id = Identity.parse(gid).id
		if not source_id.isdigit():
			raise SourceError(f"Not a Magento identity: {gid}")
		match kind:
			case Kind.COLLECTION:
				result = self._client.get(f"/V1/categories/{source_id}")
				if result is None or (result.node.optional_integer("level") or 0) <= ROOT_LEVEL:
					return None
				return self._with_store("Category", result.node)
			case Kind.PRODUCT:
				found = self._search(
					"/V1/products", [("entity_id", source_id, "eq")], None, FIRST_PAGE
				).children("items")
				if not found or found[0].optional_integer("visibility") == NOT_VISIBLE_INDIVIDUALLY:
					return None
				return self._product(found[0])
			case Kind.CUSTOMER:
				return self._one(f"/V1/customers/{source_id}", "Customer")
			case Kind.DISCOUNT:
				result = self._client.get(f"/V1/salesRules/{source_id}")
				return None if result is None else self._rules([result.node])[0]
			case Kind.ORDER:
				result = self._client.get(f"/V1/orders/{source_id}")
				return None if result is None else self._orders([result.node])[0]
			case _:
				return self._one(f"/V1/cmsPage/{source_id}", "CmsPage")

	def _with_store(self, type_: str, category: Node) -> Node:
		return self._identified(
			type_, category.integer("id"), {**category.raw(), "store": self._store_context()}
		)

	def _product(self, product: Node) -> Node:
		"""A product with the store's context and, when configurable, its child products and attributes."""
		children: list[JsonValue] = []
		attributes: list[JsonValue] = []
		extension = product.child("extension_attributes")
		if product.optional_string("type_id") == CONFIGURABLE and extension is not None:
			links = extension.integers("configurable_product_links")
			if links:
				ids = ",".join(str(link) for link in links)
				children = [child.raw() for child in self._all("/V1/products", [("entity_id", ids, "in")])]
			attributes = [
				self._attribute(o.string("attribute_id"))
				for o in extension.children("configurable_product_options")
			]
		raw = {
			**product.raw(),
			"store": self._store_context(),
			"children": children,
			"attributes": attributes,
		}
		return self._identified("Product", product.integer("id"), raw)

	def _attribute(self, attribute_id: str) -> JsonValue:
		"""A product attribute with its options, read once per run."""
		if attribute_id not in self._attributes:
			self._attributes[attribute_id] = self._required(
				f"/V1/products/attributes/{int(attribute_id)}", {}
			).raw()
		return self._attributes[attribute_id]

	def _rules(self, rules: list[Node]) -> list[Node]:
		"""Cart price rules with their coupons (read for the page), the store's context and every customer
		group."""
		ids = ",".join(str(rule.integer("rule_id")) for rule in rules)
		coupons: dict[int, list[JsonValue]] = {}
		for coupon in self._all("/V1/coupons/search", [("rule_id", ids, "in")]) if ids else []:
			coupons.setdefault(coupon.integer("rule_id"), []).append(coupon.raw())
		if self._customer_groups is None:
			self._customer_groups = [
				group.integer("id") for group in self._all("/V1/customerGroups/search", [])
			]
		return [
			self._identified(
				"SalesRule",
				rule.integer("rule_id"),
				{
					**rule.raw(),
					"coupons": coupons.get(rule.integer("rule_id"), []),
					"store": self._store_context(),
					"all_customer_group_ids": list(self._customer_groups),
				},
			)
			for rule in rules
		]

	def _orders(self, orders: list[Node]) -> list[Node]:
		"""Orders with their credit memos and shipments, each read once for the page."""
		ids = ",".join(str(order.integer("entity_id")) for order in orders)
		memos = self._by_order(ids, "/V1/creditmemos")
		shipments = self._by_order(ids, "/V1/shipments")
		return [
			self._identified(
				"Order",
				order.integer("entity_id"),
				{
					**order.raw(),
					"credit_memos": memos.get(order.integer("entity_id"), []),
					"shipments": shipments.get(order.integer("entity_id"), []),
				},
			)
			for order in orders
		]

	def _by_order(self, order_ids: str, path: str) -> dict[int, list[JsonValue]]:
		documents: dict[int, list[JsonValue]] = {}
		for document in self._all(path, [("order_id", order_ids, "in")]) if order_ids else []:
			documents.setdefault(document.integer("order_id"), []).append(document.raw())
		return documents

	def _store_context(self) -> dict[str, JsonValue]:
		"""What the mappers need of the store: its base and media URLs, website, root categories, weight unit
		and time zone."""
		config = self._config_node()
		roots: list[JsonValue] = list(
			dict.fromkeys([ROOT_LEVEL, *(g.integer("root_category_id") for g in self._store_groups())])
		)
		return {
			"base_url": config.optional_string("secure_base_url") or config.string("base_url"),
			"media_url": config.optional_string("secure_base_media_url") or config.string("base_media_url"),
			"website_id": self._view().integer("website_id"),
			"root_category_ids": roots,
			"weight_unit": config.optional_string("weight_unit"),
			"timezone": config.optional_string("timezone"),
		}

	def _config_node(self) -> Node:
		if self._store_config is None:
			self._store_config = self._with_code(
				self._required("/V1/store/storeConfigs", {}).children(LIST_KEY), "store configuration"
			)
		return self._store_config

	def _views_list(self) -> list[Node]:
		if self._views is None:
			self._views = self._required("/V1/store/storeViews", {}).children(LIST_KEY)
		return self._views

	def _view(self) -> Node:
		return self._with_code(self._views_list(), "store view")

	def _store_groups(self) -> list[Node]:
		if self._groups is None:
			self._groups = self._required("/V1/store/storeGroups", {}).children(LIST_KEY)
		return self._groups

	def _website_store_ids(self) -> list[int]:
		website = self._view().integer("website_id")
		return [view.integer("id") for view in self._views_list() if view.integer("website_id") == website]

	def _with_code(self, items: list[Node], what: str) -> Node:
		for item in items:
			if item.string("code") == self._code:
				return item
		raise SourceError(f'Magento has no {what} for the store view code "{self._code}".')

	def _search_page(
		self, path: str, filters: list[Filter], sort_field: str | None, cursor: str | None
	) -> tuple[list[Node], str | None]:
		"""One page of a search, and the next page number while the items read so far are fewer than its
		total."""
		page = max(FIRST_PAGE, int(cursor)) if cursor else FIRST_PAGE
		response = self._search(path, filters, sort_field, page)
		items = response.children("items")
		read = (page - 1) * PAGE_SIZE + len(items)
		return items, str(page + 1) if items and read < response.integer("total_count") else None

	def _all(self, path: str, filters: list[Filter]) -> list[Node]:
		items: list[Node] = []
		cursor: str | None = None
		while True:
			page, cursor = self._search_page(path, filters, None, cursor)
			items += page
			if cursor is None:
				return items

	def _search(self, path: str, filters: list[Filter], sort_field: str | None, page: int) -> Node:
		return self._required(path, search_query(filters, sort_field, page))

	def _one(self, path: str, type_: str) -> Node | None:
		result = self._client.get(path)
		return (
			None if result is None else self._identified(type_, result.node.integer("id"), result.node.raw())
		)

	def _required(self, path: str, query: Mapping[str, QueryValue]) -> Node:
		result = self._client.get(path, query)
		if result is None:
			raise SourceError(f"Magento has no {path} for this store.")
		return result.node

	def _identified(self, type_: str, id_: int, raw: Mapping[str, JsonValue]) -> Node:
		gid = self._config.identity(type_, id_)
		return Node({**raw, "source_id": str(id_), "id": gid}, gid)
