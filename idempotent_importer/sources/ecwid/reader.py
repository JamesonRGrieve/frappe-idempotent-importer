# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads a store from Ecwid's REST API v3 (docs.ecwid.com/api-reference/rest-api): ``<host>/api/v3/<store id>/
<resource>``, every list a ``{total, count, offset, limit, items}`` page read by ``offset`` and ``limit`` (at
most 100). Ported from wp-woo-idempotent-importer's EcwidReader. Every node carries its identity as ``id``
(Ecwid's id as ``source_id``). Products arrive with their variations (``combinations``) and images and carry
the store's weight unit; orders arrive with their items, refunds and shipments and carry the store's currency;
redirects carry the store's URL. A discount run reads the coupons, then each product's wholesale price tiers
(with its variations' own) as ``WholesalePrice`` nodes. Products, customers, discounts, orders and reviews are
incremental
(``updatedFrom``, a UNIX timestamp, so no zone margin is needed); categories and redirects are read in full.

Ecwid accepts up to 600 requests per minute per token; past that every request is refused with a 429 and a
Retry-After cool-down (docs.ecwid.com/api-reference/rest-api/rest-api-overview). Requests are paced under it
(one every 0.1 seconds) and a 429 waits out its Retry-After; the docs name no rate-limit headers."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page

SOURCE_ID = "ecwid"
API_HOST = "https://app.ecwid.com"
API_PATH = "/api/v3/"
PAGE_SIZE = 100
SINCE_FILTER = "updatedFrom"
RATE_LIMIT = RateLimit(requests_per_second=RateLimit.per_period(600, 60.0))
STORE_ID = re.compile(r"^\d+$")
FIRST_OFFSET = 0
INCREMENTAL = frozenset({Kind.PRODUCT, Kind.CUSTOMER, Kind.DISCOUNT, Kind.ORDER, Kind.REVIEW})
SINGLY_READABLE = frozenset({Kind.COLLECTION, Kind.PRODUCT, Kind.CUSTOMER, Kind.ORDER})
# Every list the reader pages: resource path, identity type, query sent with every request. Products ask for
# clean URLs (no "#!" hashbang), so their URLs are storefront paths; hidden categories are asked for too.
LISTS: dict[Kind, tuple[str, str, dict[str, QueryValue]]] = {
	Kind.COLLECTION: ("/categories", "Category", {"hidden_categories": "true"}),
	Kind.PRODUCT: ("/products", "Product", {"cleanUrls": "true"}),
	Kind.CUSTOMER: ("/customers", "Customer", {}),
	Kind.DISCOUNT: ("/discount_coupons", "DiscountCoupon", {}),
	Kind.ORDER: ("/orders", "Order", {}),
	Kind.URL_REDIRECT: ("/instant-site/redirects", "Redirect", {}),
	Kind.REVIEW: ("/reviews", "Review", {}),
}
REVIEW_FILTER = "reviewId"
# A discount run reads the coupons, then each product's wholesale prices; the cursor is ``<phase>:<offset>``.
PHASE_COUPONS, PHASE_WHOLESALE = "coupons", "wholesale"
CURSOR_SEPARATOR = ":"


class EcwidAuthorizer:
	"""A custom app's secret access token as a bearer token; it doesn't expire: nothing to renew."""

	def __init__(self, secret_token: str) -> None:
		if not secret_token:
			raise SourceError("Ecwid is not configured: enter the secret access token of the custom app.")
		self._token = secret_token

	def headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self._token}"}

	def renew(self) -> bool:
		return False

	def __repr__(self) -> str:
		return "EcwidAuthorizer([redacted])"


def api_base(config: ConnectionConfig) -> str:
	store_id = config.account.strip()
	if not STORE_ID.match(store_id):
		raise SourceError("Ecwid is not configured: enter the store ID (a number) as the account.")
	return (config.endpoint_override or API_HOST).rstrip("/") + API_PATH + store_id


class EcwidReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		self._config = config
		self._client = client
		self._profile: Node | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in INCREMENTAL

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		if kind not in LISTS:
			raise SourceError(f"Ecwid has no {kind.value}.")
		if kind is Kind.DISCOUNT:
			return self._discounts(cursor, since)
		nodes, following = self._list_page(kind, cursor, since)
		return Page(nodes, following is not None, following)

	def _list_page(
		self, kind: Kind, cursor: str | None, since: datetime | None
	) -> tuple[list[Node], str | None]:
		"""One page of a list, from the offset ``cursor``, and the next offset while there are more."""
		path, type_, query = LISTS[kind]
		if since is not None and kind in INCREMENTAL:
			query = {**query, SINCE_FILTER: int(since.timestamp())}
		offset = max(FIRST_OFFSET, int(cursor)) if cursor else FIRST_OFFSET
		response = self._required(path, {**query, "offset": offset, "limit": PAGE_SIZE})
		items = response.children("items")
		following = offset + len(items)
		more = bool(items) and following < response.integer("total")
		return [self._assemble(kind, type_, item) for item in items], str(following) if more else None

	def _discounts(self, cursor: str | None, since: datetime | None) -> Page:
		"""Discount coupons, then the wholesale prices of the products (changed since ``since``) that have any."""
		phase, separator, offset = (cursor or "").partition(CURSOR_SEPARATOR)
		if not separator:
			# An offset alone: a coupons cursor saved before the wholesale phase existed.
			phase, offset = PHASE_COUPONS, cursor or ""
		if phase == PHASE_COUPONS:
			coupons, following = self._list_page(Kind.DISCOUNT, offset or None, since)
			after = f"{PHASE_COUPONS if following else PHASE_WHOLESALE}{CURSOR_SEPARATOR}{following or ''}"
			return Page(coupons, True, after)
		if phase != PHASE_WHOLESALE:
			raise SourceError(f"Not an Ecwid discount cursor: {cursor}")
		products, following = self._list_page(Kind.PRODUCT, offset or None, since)
		nodes = [self._wholesale(p) for p in products if p.children("wholesalePrices")]
		return Page(
			nodes,
			following is not None,
			f"{PHASE_WHOLESALE}{CURSOR_SEPARATOR}{following}" if following else None,
		)

	def _wholesale(self, product: Node) -> Node:
		"""A product's wholesale price tiers, with its variations' own."""
		product_id = product.string("source_id")
		raw: dict[str, JsonValue] = {
			"product_id": product_id,
			"wholesalePrices": product.get("wholesalePrices"),
			"combinations": [
				{"id": c.get("id"), "wholesalePrices": c.get("wholesalePrices")}
				for c in product.children("combinations")
			],
		}
		return self._identified("WholesalePrice", product_id, raw)

	def node(self, kind: Kind, gid: str) -> Node | None:
		source_id = Identity.parse(gid).id
		if kind is Kind.REVIEW:
			found = self._required("/reviews", {REVIEW_FILTER: source_id}).children("items")
			review = next((r for r in found if str(r.get("id")) == source_id), None)
			return None if review is None else self._assemble(kind, "Review", review)
		if kind not in SINGLY_READABLE:
			raise SourceError(f"Ecwid can't read a single {kind.value}.")
		if kind is not Kind.ORDER and not STORE_ID.match(source_id):
			raise SourceError(f"Not an Ecwid identity: {gid}")
		path, type_, query = LISTS[kind]
		result = self._client.get(f"{path}/{source_id}", query if kind is Kind.PRODUCT else {})
		return None if result is None else self._assemble(kind, type_, result.node)

	def _assemble(self, kind: Kind, type_: str, item: Node) -> Node:
		"""An item with its identity and what its mapper needs from the store profile."""
		raw: dict[str, JsonValue] = dict(item.raw())
		match kind:
			case Kind.PRODUCT:
				raw["weight_unit"] = self._formats().optional_string("weightUnit")
			case Kind.ORDER:
				raw["currency"] = self._formats().string("currency")
			case Kind.URL_REDIRECT:
				raw["store_url"] = (
					self._store_profile().required_child("generalInfo").optional_string("storeUrl")
				)
			case _:
				pass
		source_id = item.get("id")
		if isinstance(source_id, bool) or not isinstance(source_id, int | str) or source_id == "":
			raise SourceError("An Ecwid object has no id.")
		return self._identified(type_, str(source_id), raw)

	def _formats(self) -> Node:
		return self._store_profile().required_child("formatsAndUnits")

	def _store_profile(self) -> Node:
		if self._profile is None:
			self._profile = self._required("/profile", {})
		return self._profile

	def _required(self, path: str, query: Mapping[str, QueryValue]) -> Node:
		result = self._client.get(path, query)
		if result is None:
			raise SourceError(f"Ecwid has no {path} for this store.")
		return result.node

	def _identified(self, type_: str, id_: str, raw: Mapping[str, JsonValue]) -> Node:
		gid = self._config.identity(type_, id_)
		return Node({**raw, "source_id": id_, "id": gid}, gid)
