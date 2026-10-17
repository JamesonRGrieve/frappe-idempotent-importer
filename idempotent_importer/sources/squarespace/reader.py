# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads a site's store from Squarespace's Commerce and Profiles APIs (cursor paging; discounts by offset),
ported from wp-woo-idempotent-importer's SquarespaceReader. Every node carries its identity as ``id``
(Squarespace's own id is kept as ``source_id``). Orders arrive with their transaction documents (payments,
refunds, per-item taxes) and their customer's profile. Products and orders are incremental: Squarespace
filters by a modification window with both ends, so the reader is given the time the run started.

Squarespace allows 300 requests a minute per API key, and after a 429 refuses requests for a minute without
sending Retry-After (developers.squarespace.com/commerce-apis/rate-limits): the client paces itself to the
rate and, if limited anyway, sits out the minute."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page

SOURCE_ID = "squarespace"
API_BASE = "https://api.squarespace.com"
# Squarespace requires a User-Agent naming the integration on every request.
USER_AGENT = "frappe-idempotent-importer"
RATE_LIMIT = RateLimit(requests_per_second=RateLimit.per_period(300, 60.0), cooldown_seconds=60.0)
DISCOUNT_PAGE_SIZE = 1000
# Every payment state: without the filter, Squarespace lists only NOT_CHARGED, AUTHORIZED, PAID and REFUNDED.
ORDER_PAYMENT_STATES = (
	"NOT_CHARGED,AUTHORIZED,PAID,REFUNDED,PENDING,FAILED,REFUND_PENDING,REFUND_FAILED,PARTIALLY_PAID"
)
# Profiles are read by id in batches (an order page holds at most 50 orders).
PROFILE_BATCH_SIZE = 50
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
INCREMENTAL = frozenset({Kind.PRODUCT, Kind.ORDER})
FIRST_OFFSET = 0

type Query = dict[str, QueryValue]


class SquarespaceAuthorizer:
	"""An API key (or an OAuth access token) as a bearer token, with the integration's User-Agent. The key
	is static: there is nothing to renew."""

	def __init__(self, api_key: str) -> None:
		if not api_key:
			raise SourceError("Squarespace is not configured: enter the API key.")
		self._key = api_key

	def headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self._key}", "User-Agent": USER_AGENT}

	def renew(self) -> bool:
		return False

	def __repr__(self) -> str:
		return "SquarespaceAuthorizer([redacted])"


def squarespace_time(moment: datetime) -> str:
	return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


class SquarespaceReader:
	def __init__(self, config: ConnectionConfig, client: RestClient, now: datetime) -> None:
		self._config = config
		self._client = client
		self._now = now

	def is_incremental(self, kind: Kind) -> bool:
		return kind in INCREMENTAL

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		match kind:
			case Kind.COLLECTION:
				return self._listed("/1.0/commerce/store_pages", {}, cursor, "storePages", "StorePage")
			case Kind.PRODUCT:
				return self._listed(
					"/v2/commerce/products", self._window(since), cursor, "products", "Product"
				)
			case Kind.CUSTOMER:
				query: Query = {"sortField": "createdOn", "sortDirection": "asc"}
				return self._listed("/1.0/profiles", query, cursor, "profiles", "Profile")
			case Kind.DISCOUNT:
				return self._discounts(cursor)
			case Kind.ORDER:
				query = {"paymentStates": ORDER_PAYMENT_STATES, **self._window(since)}
				orders, following = self._cursor_page("/1.0/commerce/orders", query, cursor, "result")
				return Page(self._orders(orders), following is not None, following)
			case _:
				raise SourceError(f"Squarespace has no {kind.value}.")

	def node(self, kind: Kind, gid: str) -> Node | None:
		source_id = Identity.parse(gid).id
		if not ID_PATTERN.match(source_id):
			raise SourceError(f"Not a Squarespace identity: {gid}")
		match kind:
			case Kind.PRODUCT:
				return self._first(f"/v2/commerce/products/{source_id}", "products", "Product")
			case Kind.CUSTOMER:
				return self._first(f"/1.0/profiles/{source_id}", "profiles", "Profile")
			case Kind.DISCOUNT:
				result = self._client.get(f"/v1/commerce/discounts/{source_id}")
				discount = result.node.child("discount") if result else None
				return None if discount is None else self._identified("Discount", discount.raw())
			case Kind.ORDER:
				result = self._client.get(f"/1.0/commerce/orders/{source_id}")
				return None if result is None else self._orders([result.node])[0]
			case _:
				raise SourceError(f"Squarespace can't read a single {kind.value}.")

	def _listed(self, path: str, filters: Query, cursor: str | None, list_key: str, type_: str) -> Page:
		items, following = self._cursor_page(path, filters, cursor, list_key)
		return Page([self._identified(type_, item.raw()) for item in items], following is not None, following)

	def _discounts(self, cursor: str | None) -> Page:
		"""Discounts of every status, oldest first; the cursor is the next offset."""
		offset = max(FIRST_OFFSET, int(cursor)) if cursor else FIRST_OFFSET
		query: Query = {
			"status": "ALL",
			"sortBy": "CREATED_ON",
			"sortDirection": "ASCENDING",
			"offset": offset,
			"limit": DISCOUNT_PAGE_SIZE,
		}
		response = self._required("/v1/commerce/discounts", query)
		discounts = response.children("discounts")
		more = response.get("hasNextPage") is True and bool(discounts)
		return Page(
			[self._identified("Discount", d.raw()) for d in discounts],
			more,
			str(offset + len(discounts)) if more else None,
		)

	def _orders(self, orders: list[Node]) -> list[Node]:
		"""Orders with their transaction documents (every page) and their customer's profile (None for a
		guest, or a customer Squarespace no longer has)."""
		profiles = self._profiles([order.optional_string("customerId") for order in orders])
		assembled = []
		for order in orders:
			documents = self._all("/1.0/commerce/transactions", {"orderId": order.string("id")}, "documents")
			profile = profiles.get(order.optional_string("customerId") or "")
			raw: dict[str, JsonValue] = {
				**order.raw(),
				"transactions": [document.raw() for document in documents],
				"customer_profile": profile.raw() if profile else None,
			}
			assembled.append(self._identified("Order", raw))
		return assembled

	def _profiles(self, ids: list[str | None]) -> dict[str, Node]:
		"""The profiles with these ids that Squarespace has, by id. A batch Squarespace answers with 404 (one
		of its profiles is gone) is read again one profile at a time."""
		wanted = sorted({i for i in ids if i and ID_PATTERN.match(i)})
		found: dict[str, Node] = {}
		for start in range(0, len(wanted), PROFILE_BATCH_SIZE):
			batch = wanted[start : start + PROFILE_BATCH_SIZE]
			result = self._client.get("/1.0/profiles/" + ",".join(batch))
			profiles = result.node.children("profiles") if result else []
			if result is None and len(batch) > 1:
				for profile_id in batch:
					single = self._client.get(f"/1.0/profiles/{profile_id}")
					profiles += single.node.children("profiles") if single else []
			found |= {profile.string("id"): profile for profile in profiles}
		return found

	def _cursor_page(
		self, path: str, filters: Query, cursor: str | None, list_key: str
	) -> tuple[list[Node], str | None]:
		"""One page of a cursor-paged list (filters go with the first page only) and the next cursor."""
		response = self._required(path, {"cursor": cursor} if cursor else filters)
		pagination = response.child("pagination")
		following = (
			pagination.optional_string("nextPageCursor")
			if pagination is not None and pagination.get("hasNextPage") is True
			else None
		)
		return response.children(list_key), following

	def _all(self, path: str, filters: Query, list_key: str) -> list[Node]:
		items: list[Node] = []
		cursor: str | None = None
		while True:
			page, cursor = self._cursor_page(path, filters, cursor, list_key)
			items += page
			if cursor is None:
				return items

	def _first(self, path: str, list_key: str, type_: str) -> Node | None:
		result = self._client.get(path)
		found = result.node.children(list_key) if result else []
		return self._identified(type_, found[0].raw()) if found else None

	def _window(self, since: datetime | None) -> Query:
		if since is None:
			return {}
		return {"modifiedAfter": squarespace_time(since), "modifiedBefore": squarespace_time(self._now)}

	def _required(self, path: str, query: Query) -> Node:
		result = self._client.get(path, query)
		if result is None:
			raise SourceError(f"Squarespace has no {path} for this site.")
		return result.node

	def _identified(self, type_: str, raw: Mapping[str, JsonValue]) -> Node:
		source_id = raw.get("id")
		if not isinstance(source_id, str):
			raise SourceError(f"A Squarespace {type_} has no id.")
		gid = self._config.identity(type_, source_id)
		return Node({**raw, "source_id": source_id, "id": gid}, gid)
