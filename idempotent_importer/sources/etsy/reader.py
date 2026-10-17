# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads the connected shop from Etsy's Open API v3 (offset paging, 100 per page), ported from
wp-woo-idempotent-importer's EtsyReader. Every node carries its identity as ``id``. Listings are read in
every state Etsy keeps, one state after another (Etsy can't filter them by update time). Receipts and
reviews are incremental. Customers are the buyers on receipts: Etsy exposes only a buyer's id and email.
The shop's written policies and structured return policies are policy pages.

Etsy's limits are per app and reported on every response: requests left this second (and today); a 429
carries Retry-After (developers.etsy.com/documentation/essentials/rate-limits). The client waits out the
second when it is spent; a spent day's quota is a long Retry-After, which pauses the run."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.common import plain_html
from idempotent_importer.sources.etsy.auth import EtsyAuthorizer

SOURCE_ID = "etsy"
API = "/v3/application"
PAGE_SIZE = 100
LISTING_STATES = ("active", "inactive", "sold_out", "draft", "expired")
LISTING_INCLUDES = ("Images", "Inventory", "Videos", "Translations", "Shipping", "Personalization")
CURSOR_SEPARATOR = ":"
INCREMENTAL = frozenset({Kind.CUSTOMER, Kind.ORDER, Kind.REVIEW})
RATE_LIMIT = RateLimit(remaining_header="x-remaining-this-second", window_seconds=1.0)
# Etsy's written policy fields → page title.
POLICIES = {
	"policy_welcome": "Welcome",
	"policy_payment": "Payment policy",
	"policy_shipping": "Shipping policy",
	"policy_refunds": "Refund policy",
	"policy_additional": "Additional policies",
	"policy_seller_info": "Seller information",
	"policy_privacy": "Privacy policy",
}
NODE_PATHS = {Kind.PRODUCT: "Listing", Kind.ORDER: "Receipt", Kind.COLLECTION: "Section"}


class EtsyReader:
	def __init__(self, config: ConnectionConfig, client: RestClient, owner: EtsyAuthorizer) -> None:
		self._config = config
		self._client = client
		self._owner = owner
		self._shop: Node | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in INCREMENTAL

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		shop = f"/shops/{self._shop_id()}"
		match kind:
			case Kind.POLICY:
				return Page(self._policies(), False, None)
			case Kind.COLLECTION:
				return self._list(f"{shop}/sections", {}, cursor, "Section", "shop_section_id")
			case Kind.PRODUCT:
				return self._listings(cursor)
			case Kind.ORDER:
				return self._list(f"{shop}/receipts", receipt_query(since), cursor, "Receipt", "receipt_id")
			case Kind.CUSTOMER:
				return self._buyers(
					self._list(f"{shop}/receipts", receipt_query(since), cursor, "Receipt", "receipt_id")
				)
			case Kind.REVIEW:
				query: dict[str, QueryValue] = (
					{} if since is None else {"min_created": int(since.timestamp())}
				)
				return self._list(f"{shop}/reviews", query, cursor, "Review", "transaction_id")
			case _:
				raise SourceError(f"Etsy has no {kind.value}.")

	def node(self, kind: Kind, gid: str) -> Node | None:
		identifier = Identity.parse(gid).id
		match kind:
			case Kind.PRODUCT:
				path, query, field = (
					f"/listings/{identifier}",
					{"includes": list(LISTING_INCLUDES)},
					"listing_id",
				)
			case Kind.ORDER:
				path, query, field = f"/shops/{self._shop_id()}/receipts/{identifier}", {}, "receipt_id"
			case Kind.COLLECTION:
				path, query, field = f"/shops/{self._shop_id()}/sections/{identifier}", {}, "shop_section_id"
			case _:
				raise SourceError(f"Etsy can't read a single {kind.value}.")
		result = self._client.get(API + path, query)
		if result is None:
			return None
		return self._identified(NODE_PATHS[kind], result.node.integer(field), result.node.raw())

	def _listings(self, cursor: str | None) -> Page:
		"""Listings, one state after another; the cursor is ``<state index>:<offset>``."""
		state, offset = (
			(0, 0) if cursor is None else (int(part) for part in cursor.split(CURSOR_SEPARATOR, 1))
		)
		state = min(state, len(LISTING_STATES) - 1)
		page = self._list(
			f"/shops/{self._shop_id()}/listings",
			{"state": LISTING_STATES[state], "includes": list(LISTING_INCLUDES)},
			str(offset),
			"Listing",
			"listing_id",
		)
		if page.has_next_page:
			return Page(page.nodes, True, f"{state}{CURSOR_SEPARATOR}{page.end_cursor}")
		following = state + 1
		return Page(page.nodes, following < len(LISTING_STATES), f"{following}{CURSOR_SEPARATOR}0")

	def _list(
		self, path: str, query: Mapping[str, QueryValue], cursor: str | None, type_: str, id_field: str
	) -> Page:
		"""One page of an offset-paged collection; the cursor is the next offset."""
		offset = int(cursor) if cursor else 0
		result = self._required(path, {**query, "limit": PAGE_SIZE, "offset": offset})
		nodes = [
			self._identified(type_, item.integer(id_field), item.raw()) for item in result.children("results")
		]
		following = offset + len(nodes)
		more = bool(nodes) and following < result.integer("count")
		return Page(nodes, more, str(following) if more else None)

	def _buyers(self, receipts: Page) -> Page:
		"""Each receipt's buyer (once per page), as an identity and an email; guest receipts have none."""
		buyers: dict[int, Node] = {}
		for receipt in receipts.nodes:
			user_id = receipt.optional_integer("buyer_user_id")
			if user_id is None or user_id < 1 or user_id in buyers:
				continue
			buyers[user_id] = self._identified(
				"User",
				user_id,
				{"buyer_user_id": user_id, "buyer_email": receipt.optional_string("buyer_email")},
			)
		return Page(list(buyers.values()), receipts.has_next_page, receipts.end_cursor)

	def _policies(self) -> list[Node]:
		"""The shop's written policies and each structured return policy, as pages."""
		shop = self._owner_shop()
		shop_id = shop.integer("shop_id")
		policies: list[Node] = []
		for field, title in POLICIES.items():
			text = shop.optional_string(field)
			if text is not None:
				policies.append(
					self._identified(
						"Policy",
						f"{shop_id}-{field}",
						{
							"title": title,
							"slug": f"policies/{field.removeprefix('policy_')}",
							"html": plain_html(text),
						},
					)
				)
		returns = self._required(f"/shops/{shop_id}/policies/return", {})
		for policy in returns.children("results"):
			policies.append(
				self._identified(
					"ReturnPolicy",
					policy.integer("return_policy_id"),
					{
						"title": "Returns and exchanges",
						"slug": f"policies/returns-{policy.integer('return_policy_id')}",
						"html": return_policy_html(policy),
					},
				)
			)
		return policies

	def _owner_shop(self) -> Node:
		if self._shop is None:
			result = self._client.get(f"{API}/users/{self._owner.user_id()}/shops", {})
			if result is None:
				raise SourceError("The connected Etsy account has no shop.")
			self._shop = result.node
		return self._shop

	def _shop_id(self) -> int:
		return self._owner_shop().integer("shop_id")

	def _required(self, path: str, query: Mapping[str, QueryValue]) -> Node:
		result = self._client.get(API + path, query)
		if result is None:
			raise SourceError(f"Etsy has no {path} for this shop.")
		return result.node

	def _identified(self, type_: str, id_: int | str, raw: Mapping[str, JsonValue]) -> Node:
		gid = self._config.identity(type_, id_)
		return Node({**raw, "id": gid}, gid)


def receipt_query(since: datetime | None) -> dict[str, QueryValue]:
	"""Receipts in creation order, optionally only those changed since ``since``."""
	query: dict[str, QueryValue] = {"sort_on": "created", "sort_order": "asc"}
	if since is not None:
		query["min_last_modified"] = int(since.timestamp())
	return query


def return_policy_html(policy: Node) -> str:
	"""A structured return policy written out as a sentence (its fields are Etsy's; the wording is ours)."""
	returns, exchanges = policy.flag("accepts_returns"), policy.flag("accepts_exchanges")
	deadline = policy.optional_integer("return_deadline")
	if returns and exchanges:
		accepted = "Returns and exchanges are accepted"
	elif returns:
		accepted = "Returns are accepted"
	elif exchanges:
		accepted = "Exchanges are accepted"
	else:
		return plain_html("Returns and exchanges are not accepted.")
	return plain_html(accepted + ("." if deadline is None else f" within {deadline} days."))
