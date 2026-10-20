# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads the seller's account from the Square API (cursor paging everywhere), ported from
wp-woo-idempotent-importer's SquareOnlineReader. Every node carries its identity as ``id`` (the Square id
is kept as ``square_id``). Catalog items arrive with the objects they reference — images, categories and
item options — and their in-stock counts at the main location (the location Square Online sells from), as
``image_objects``, ``category_objects``, ``option_objects`` and ``inventory_counts``. Orders are searched
location by location (Square takes at most ten per search) and carry ``variation_items`` (each line's
variation id → its item id), since a line names only the variation. A discount run reads the pricing rules (each with its discount,
product sets, time periods and the type of every catalog object its sets name, as ``discount_object``,
``product_sets``, ``time_periods`` and ``catalog_types``), then the discounts no rule applies (the cursor is
``<phase>:<cursor>``); discounts are read in full. The other kinds are incremental: catalog
objects by ``begin_time`` (deleted ones included, so they are retired), stock by ``updated_after`` (the
items whose counts changed are re-read), customers and orders by ``updated_at``. The main location's
return policy is a policy page.

Square documents no fixed request rate; it answers 429 when an application sends too many requests
(developer.squareup.com/docs/build-basics/handling-errors#rate-limiting), which RetryPolicy retries with
backoff and pauses the run on."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from urllib.parse import quote

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.common import plain_html

SOURCE_ID = "square_online"
API_BASE = "https://connect.squareup.com"
# The Square API version this adapter reads (the pinned spec in tools/specs.py).
API_VERSION = "2026-09-16"
# SearchCatalogObjects takes up to 1000; 100 keeps one page's follow-up requests small.
CATALOG_PAGE_SIZE = 100
CUSTOMER_PAGE_SIZE = 100
ORDER_PAGE_SIZE = 100
LOCATIONS_PER_ORDER_SEARCH = 10
# Draft orders are unsubmitted carts and API drafts, never part of the store's history.
ORDER_STATES = ("OPEN", "COMPLETED", "CANCELED")
STOCK_STATE = "IN_STOCK"
ACTIVE = "ACTIVE"
PHASE_ITEMS, PHASE_STOCK = "items", "stock"
PHASE_RULES, PHASE_DISCOUNTS = "rules", "discounts"
# A product set's members: catalog items, variations, categories (or other product sets).
SET_MEMBERS = ("product_ids_any", "product_ids_all")
CURSOR_SEPARATOR = ":"
TYPES = {"ITEM": "Item", "CATEGORY": "Category"}
INCREMENTAL = frozenset({Kind.COLLECTION, Kind.PRODUCT, Kind.CUSTOMER, Kind.ORDER})

type Body = dict[str, JsonValue]


class SquareAuthorizer:
	"""A personal (or OAuth) access token as a bearer token, and the Square-Version the reader was written
	against, so Square answers in that version's shapes. Nothing to renew."""

	def __init__(self, access_token: str) -> None:
		if not access_token:
			raise SourceError("Square Online is not configured: enter the access token.")
		self._token = access_token

	def headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self._token}", "Square-Version": API_VERSION}

	def renew(self) -> bool:
		return False

	def __repr__(self) -> str:
		return "SquareAuthorizer([redacted])"


def unique(ids: Iterable[str | None]) -> list[str]:
	"""Distinct, sorted, without empty ones."""
	return sorted({identifier for identifier in ids if identifier})


def is_deleted(node: Node) -> bool:
	return node.get("is_deleted") is True


def rfc3339(moment: datetime) -> str:
	return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def with_cursor(body: Body, cursor: str | None) -> Body:
	return {**body, "cursor": cursor} if cursor else body


def references(objects: Iterable[Node]) -> list[str]:
	"""The ids of the images, categories and item options catalog objects reference, sorted."""
	ids: list[str | None] = []
	for item in objects:
		category = item.child("category_data")
		if category is not None:
			ids += category.strings("image_ids")
		data = item.child("item_data")
		if data is None:
			continue
		ids += data.strings("image_ids")
		ids += [category.string("id") for category in data.children("categories")]
		ids += [option.optional_string("item_option_id") for option in data.children("item_options")]
		for variation in data.children("variations"):
			variation_data = variation.child("item_variation_data")
			ids += variation_data.strings("image_ids") if variation_data else []
	return unique(ids)


def by_id(objects: Iterable[Node]) -> dict[str, Node]:
	return {item.string("id"): item for item in objects}


class SquareOnlineReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		self._config = config
		self._client = client
		self._merchant: Node | None = None
		self._locations: list[Node] | None = None
		self._ruled: set[str] | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in INCREMENTAL

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		match kind:
			case Kind.POLICY:
				return Page(self._policies(), False, None)
			case Kind.COLLECTION:
				return self._categories(cursor, since)
			case Kind.PRODUCT:
				return self._items(cursor, since)
			case Kind.CUSTOMER:
				return self._customers(cursor, since)
			case Kind.ORDER:
				return self._orders(cursor, since)
			case Kind.DISCOUNT:
				return self._discounts(cursor)
			case _:
				raise SourceError(f"Square Online has no {kind.value}.")

	def node(self, kind: Kind, gid: str) -> Node | None:
		square_id = quote(Identity.parse(gid).id, safe="")
		match kind:
			case Kind.PRODUCT:
				return self._catalog_object(square_id, "ITEM")
			case Kind.COLLECTION:
				return self._catalog_object(square_id, "CATEGORY")
			case Kind.CUSTOMER:
				result = self._client.get(f"/v2/customers/{square_id}")
				customer = result.node.child("customer") if result else None
				return None if customer is None else self._identified("Customer", customer.raw())
			case Kind.ORDER:
				result = self._client.get(f"/v2/orders/{square_id}")
				order = result.node.child("order") if result else None
				return None if order is None else self._with_variation_items([order])[0]
			case _:
				raise SourceError(f"Square Online can't read a single {kind.value}.")

	def main_location_id(self) -> str:
		"""The location Square Online sells from: the seller's main location, else its first active one."""
		main = self._merchant_node().optional_string("main_location_id")
		if main is not None:
			return main
		for location in self._location_nodes():
			if location.optional_string("status") == ACTIVE:
				return location.string("id")
		raise SourceError("The Square account has no active location.")

	def _policies(self) -> list[Node]:
		main = next(
			(loc for loc in self._location_nodes() if loc.string("id") == self.main_location_id()), None
		)
		policy = main.optional_string("return_policy") if main else None
		if main is None or policy is None:
			return []
		return [
			self._identified(
				"ReturnPolicy",
				{
					"id": main.string("id"),
					"title": "Return policy",
					"slug": "policies/return-policy",
					"html": plain_html(policy),
				},
			)
		]

	def _discounts(self, cursor: str | None) -> Page:
		"""Pricing rules, then the discounts no pricing rule applies (applied by staff at the point of sale)."""
		phase, _, square_cursor = (cursor or PHASE_RULES).partition(CURSOR_SEPARATOR)
		if phase == PHASE_RULES:
			search = self._catalog_search("PRICING_RULE", square_cursor or None, None)
			related = by_id(search.children("related_objects"))
			nodes = [self._pricing_rule(rule, related) for rule in search.children("objects")]
			following = search.optional_string("cursor")
			after = (
				f"{PHASE_RULES}{CURSOR_SEPARATOR}{following}"
				if following
				else f"{PHASE_DISCOUNTS}{CURSOR_SEPARATOR}"
			)
			return Page(nodes, True, after)
		if phase != PHASE_DISCOUNTS:
			raise SourceError(f"Not a Square discount cursor: {cursor}")
		search = self._catalog_search("DISCOUNT", square_cursor or None, None)
		ruled = self._ruled_discounts()
		nodes = [
			self._identified("Discount", d.raw())
			for d in search.children("objects")
			if d.string("id") not in ruled
		]
		following = search.optional_string("cursor")
		return Page(
			nodes,
			following is not None,
			f"{PHASE_DISCOUNTS}{CURSOR_SEPARATOR}{following}" if following else None,
		)

	def _ruled_discounts(self) -> set[str]:
		"""The discounts pricing rules apply, read once per run."""
		if self._ruled is None:
			ruled: set[str] = set()
			cursor: str | None = None
			while True:
				search = self._catalog_search("PRICING_RULE", cursor, None)
				for rule in search.children("objects"):
					data = rule.child("pricing_rule_data")
					if data is not None and data.optional_string("discount_id"):
						ruled.add(data.string("discount_id"))
				cursor = search.optional_string("cursor")
				if cursor is None:
					break
			self._ruled = ruled
		return self._ruled

	def _pricing_rule(self, rule: Node, related: dict[str, Node]) -> Node:
		"""A pricing rule with its discount, product sets and time periods, and the type of each catalog object
		its sets name (with a variation's item)."""
		data = rule.child("pricing_rule_data")
		if data is None or is_deleted(rule):
			return self._identified("PricingRule", rule.raw())
		set_ids = unique(
			data.optional_string(f) for f in ("match_products_id", "exclude_products_id", "apply_products_id")
		)
		wanted = unique([data.optional_string("discount_id"), *set_ids, *data.strings("time_period_ids")])
		related = self._with_missing(related, wanted)
		sets = {i: related[i] for i in set_ids if i in related}
		members = unique(
			member
			for product_set in sets.values()
			for field in SET_MEMBERS
			for member in (product_set.child("product_set_data") or Node({})).strings(field)
		)
		related = self._with_missing(related, members)
		types: dict[str, JsonValue] = {}
		for member in members:
			found = related.get(member)
			if found is None:
				continue
			variation = found.child("item_variation_data")
			types[member] = {
				"type": found.string("type"),
				**({"item_id": variation.optional_string("item_id")} if variation else {}),
			}
		discount = related.get(data.optional_string("discount_id") or "")
		raw: dict[str, JsonValue] = {
			**rule.raw(),
			"discount_object": discount.raw() if discount else None,
			"product_sets": {i: node.raw() for i, node in sets.items()},
			"time_periods": [related[t].raw() for t in data.strings("time_period_ids") if t in related],
			"catalog_types": types,
		}
		return self._identified("PricingRule", raw)

	def _categories(self, cursor: str | None, since: datetime | None) -> Page:
		search = self._catalog_search("CATEGORY", cursor, since)
		categories = search.children("objects")
		related = self._with_missing(by_id(search.children("related_objects")), references(categories))
		nodes = [self._assembled(category, related, {}) for category in categories]
		following = search.optional_string("cursor")
		return Page(nodes, following is not None, following)

	def _items(self, cursor: str | None, since: datetime | None) -> Page:
		"""A full run reads every item; an incremental run reads the items changed since ``since`` (deleted
		ones included), then the items whose stock changed since then. The cursor is ``<phase>:<cursor>``."""
		phase, _, square_cursor = (cursor or PHASE_ITEMS).partition(CURSOR_SEPARATOR)
		if phase == PHASE_STOCK and since is not None:
			return self._restocked(square_cursor or None, since)
		search = self._catalog_search("ITEM", square_cursor or None, since)
		nodes = self._assemble_items(search.children("objects"), by_id(search.children("related_objects")))
		following = search.optional_string("cursor")
		if following is not None:
			return Page(nodes, True, f"{PHASE_ITEMS}{CURSOR_SEPARATOR}{following}")
		return Page(
			nodes, since is not None, f"{PHASE_STOCK}{CURSOR_SEPARATOR}" if since is not None else None
		)

	def _restocked(self, cursor: str | None, since: datetime) -> Page:
		"""One page of stock counts changed since ``since`` at the main location, as their items."""
		body: Body = {
			"location_ids": [self.main_location_id()],
			"states": [STOCK_STATE],
			"updated_after": rfc3339(since),
		}
		counts = self._query("/v2/inventory/counts/batch-retrieve", with_cursor(body, cursor))
		variation_ids = unique(
			count.optional_string("catalog_object_id") for count in counts.children("counts")
		)
		item_ids: list[str] = []
		if variation_ids:
			item_ids = unique(
				variation.required_child("item_variation_data").optional_string("item_id")
				for variation in self._batch(variation_ids, False).children("objects")
			)
		nodes: list[Node] = []
		if item_ids:
			items = self._batch(item_ids, True)
			nodes = self._assemble_items(items.children("objects"), by_id(items.children("related_objects")))
		following = counts.optional_string("cursor")
		return Page(
			nodes, following is not None, f"{PHASE_STOCK}{CURSOR_SEPARATOR}{following}" if following else None
		)

	def _assemble_items(self, items: list[Node], related: dict[str, Node]) -> list[Node]:
		live = [item for item in items if not is_deleted(item)]
		related = self._with_missing(related, references(live))
		counts = self._stock(live)
		return [self._assembled(item, related, counts) for item in items]

	def _assembled(
		self, item: Node, related: Mapping[str, Node], counts: Mapping[str, list[JsonValue]]
	) -> Node:
		"""A catalog object with the objects it references, by kind, and (for an item) its variations' counts.
		A deleted object is passed on as Square sent it: it is retired."""
		square_type = TYPES.get(item.string("type"))
		if square_type is None:
			raise SourceError(f"Square catalog object {item.string('id')} is not an item or a category.")
		if is_deleted(item):
			return self._identified(square_type, item.raw())

		def referenced(kind: str) -> list[JsonValue]:
			found = [related.get(ref) for ref in references([item])]
			return [
				node.raw() for node in found if node and node.string("type") == kind and not is_deleted(node)
			]

		extra: dict[str, JsonValue] = {"image_objects": referenced("IMAGE")}
		if square_type == "Item":
			data = item.child("item_data")
			variation_counts = [
				count
				for variation in (data.children("variations") if data else [])
				for count in counts.get(variation.string("id"), [])
			]
			extra |= {
				"category_objects": referenced("CATEGORY"),
				"option_objects": referenced("ITEM_OPTION"),
				"inventory_counts": variation_counts,
				"main_location_id": self.main_location_id(),
			}
		return self._identified(square_type, {**item.raw(), **extra})

	def _stock(self, items: list[Node]) -> dict[str, list[JsonValue]]:
		"""In-stock counts at the main location of every variation of these items."""
		variation_ids = unique(
			variation.string("id")
			for item in items
			for variation in (
				item.required_child("item_data").children("variations") if item.child("item_data") else []
			)
		)
		counts: dict[str, list[JsonValue]] = {}
		cursor: str | None = None
		while variation_ids:
			body: Body = {
				"catalog_object_ids": list[JsonValue](variation_ids),
				"location_ids": [self.main_location_id()],
				"states": [STOCK_STATE],
			}
			page = self._query("/v2/inventory/counts/batch-retrieve", with_cursor(body, cursor))
			for count in page.children("counts"):
				counts.setdefault(count.string("catalog_object_id"), []).append(count.raw())
			cursor = page.optional_string("cursor")
			if cursor is None:
				break
		return counts

	def _customers(self, cursor: str | None, since: datetime | None) -> Page:
		if since is None:
			query: dict[str, QueryValue] = {
				"limit": CUSTOMER_PAGE_SIZE,
				"sort_field": "CREATED_AT",
				"sort_order": "ASC",
				**({"cursor": cursor} if cursor else {}),
			}
			result = self._client.get("/v2/customers", query)
			page = result.node if result else None
		else:
			body: Body = {
				"limit": CUSTOMER_PAGE_SIZE,
				"query": {
					"filter": {"updated_at": {"start_at": rfc3339(since)}},
					"sort": {"field": "CREATED_AT", "order": "ASC"},
				},
			}
			page = self._query("/v2/customers/search", with_cursor(body, cursor))
		if page is None:
			raise SourceError("Square has no customers endpoint for this account.")
		nodes = [self._identified("Customer", customer.raw()) for customer in page.children("customers")]
		following = page.optional_string("cursor")
		return Page(nodes, following is not None, following)

	def _orders(self, cursor: str | None, since: datetime | None) -> Page:
		"""Orders, ten locations at a time; the cursor is ``<location group>:<Square cursor>``."""
		locations = unique(location.string("id") for location in self._location_nodes())
		groups = [
			locations[i : i + LOCATIONS_PER_ORDER_SEARCH]
			for i in range(0, len(locations), LOCATIONS_PER_ORDER_SEARCH)
		]
		group_text, _, square_cursor = (cursor or "0").partition(CURSOR_SEPARATOR)
		group = int(group_text)
		if group >= len(groups):
			return Page([], False, None)
		filter_: Body = {"state_filter": {"states": list(ORDER_STATES)}}
		if since is not None:
			filter_["date_time_filter"] = {"updated_at": {"start_at": rfc3339(since)}}
		body: Body = {
			"location_ids": list[JsonValue](groups[group]),
			"limit": ORDER_PAGE_SIZE,
			"query": {
				"filter": filter_,
				"sort": {"sort_field": "CREATED_AT" if since is None else "UPDATED_AT", "sort_order": "ASC"},
			},
		}
		search = self._query("/v2/orders/search", with_cursor(body, square_cursor or None))
		nodes = self._with_variation_items(search.children("orders"))
		following = search.optional_string("cursor")
		if following is not None:
			return Page(nodes, True, f"{group}{CURSOR_SEPARATOR}{following}")
		more = group + 1 < len(groups)
		return Page(nodes, more, f"{group + 1}{CURSOR_SEPARATOR}" if more else None)

	def _with_variation_items(self, orders: list[Node]) -> list[Node]:
		"""Orders identified, each with the item every line's variation belongs to. A variation Square no
		longer has is left out: that line keeps its name but no product."""
		variation_ids = unique(
			line.optional_string("catalog_object_id")
			for order in orders
			for line in order.children("line_items")
		)
		items: dict[str, str] = {}
		if variation_ids:
			for variation in self._batch(variation_ids, False).children("objects"):
				data = variation.child("item_variation_data")
				item_id = data.optional_string("item_id") if data else None
				if item_id is not None and not is_deleted(variation):
					items[variation.string("id")] = item_id
		identified: list[Node] = []
		for order in orders:
			named = (line.optional_string("catalog_object_id") for line in order.children("line_items"))
			lines: dict[str, JsonValue] = {
				variation_id: items[variation_id]
				for variation_id in named
				if variation_id and variation_id in items
			}
			identified.append(self._identified("Order", {**order.raw(), "variation_items": lines}))
		return identified

	def _catalog_object(self, square_id: str, square_type: str) -> Node | None:
		result = self._client.get(f"/v2/catalog/object/{square_id}", {"include_related_objects": True})
		item = result.node.child("object") if result else None
		if result is None or item is None or item.string("type") != square_type or is_deleted(item):
			return None
		related = by_id(result.node.children("related_objects"))
		if square_type == "ITEM":
			return self._assemble_items([item], related)[0]
		return self._assembled(item, self._with_missing(related, references([item])), {})

	def _catalog_search(self, square_type: str, cursor: str | None, since: datetime | None) -> Node:
		body: Body = {
			"object_types": [square_type],
			"include_related_objects": True,
			"limit": CATALOG_PAGE_SIZE,
		}
		if since is not None:
			body |= {"begin_time": rfc3339(since), "include_deleted_objects": True}
		return self._query("/v2/catalog/search", with_cursor(body, cursor))

	def _batch(self, ids: list[str], related: bool) -> Node:
		"""Catalog objects by id (and, with ``related``, the objects they reference)."""
		return self._query(
			"/v2/catalog/batch-retrieve",
			{"object_ids": list[JsonValue](ids), "include_related_objects": related},
		)

	def _with_missing(self, related: dict[str, Node], ids: list[str]) -> dict[str, Node]:
		"""``related`` plus any of ``ids`` it lacks, read in one batch (Square's related objects don't
		include item options, and may leave out variation images)."""
		missing = [identifier for identifier in ids if identifier not in related]
		if not missing:
			return related
		return {**related, **by_id(self._batch(missing, False).children("objects"))}

	def _merchant_node(self) -> Node:
		if self._merchant is None:
			result = self._client.get("/v2/merchants/me")
			if result is None:
				raise SourceError("Square has no merchant for this account.")
			self._merchant = result.node.required_child("merchant")
		return self._merchant

	def _location_nodes(self) -> list[Node]:
		if self._locations is None:
			result = self._client.get("/v2/locations")
			if result is None:
				raise SourceError("Square has no locations for this account.")
			self._locations = result.node.children("locations")
		return self._locations

	def _query(self, path: str, body: Body) -> Node:
		result = self._client.query(path, body)
		if result is None:
			raise SourceError(f"Square has no {path} for this account.")
		return result.node

	def _identified(self, type_: str, raw: Mapping[str, JsonValue]) -> Node:
		square_id = str(raw["id"])
		gid = self._config.identity(type_, square_id)
		return Node({**raw, "square_id": square_id, "id": gid}, gid)
