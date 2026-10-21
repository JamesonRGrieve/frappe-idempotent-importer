# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one Pipedrive company through its API (``<api_domain>/api/v2`` and, for what v2 doesn't have yet,
``/api/v1``), as Pipedrive's OpenAPI documents describe it (developers.pipedrive.com/docs/api/v1/openapi.yaml
and openapi-v2.yaml, pinned in tools/specs.py): the users, products with their prices and variations, the
persons, the organizations with their persons, the organizations and persons of won deals (the customers),
the pipelines and their stages, deals (archived too) and leads (archived too), activities, notes and call
logs.

Every object carries its custom fields' values (``custom_fields``, keyed by the field's 40-character hash),
which the node carries by the field's name as ``custom``, a choice as its option's label; the field
definitions come from the deal, person, organization, product and activity field lists, read once per run.
A deal carries its stage's name and probability.

Paging: v2 lists after the ``next_cursor`` their answers give; v1 lists by ``start`` while
``more_items_in_collection``. Incremental runs ask for what changed since the last run (``updated_since``)
where the list takes it (deals, persons, organizations, products, activities, leads, notes); the users,
pipelines, call logs and customers are read whole.

Limits (pipedrive.readme.io/docs/core-api-concepts-rate-limiting): a burst limit per token over a rolling two
seconds (80 requests for an OAuth app on the smallest plan) and a daily token budget for the company, each
request costing tokens; answers report ``x-ratelimit-remaining`` and ``x-ratelimit-reset`` (seconds), and a
spent budget answers 429 until midnight. The client paces ten requests a second and quiets when the window
runs out."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_of, decimal_text
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.common import chained

SOURCE_ID = "pipedrive"
V1, V2 = "/api/v1", "/api/v2"
PAGE_SIZE = 100
REQUESTS_PER_SECOND = 10.0
RATE_LIMIT = RateLimit(
	requests_per_second=REQUESTS_PER_SECOND,
	remaining_header="x-ratelimit-remaining",
	reset_header="x-ratelimit-reset",
)
UPDATED_SINCE = "%Y-%m-%dT%H:%M:%SZ"
WON = "won"
# The field lists of each object type, and the identity type of its objects.
FIELD_LISTS = {
	"Deal": "/dealFields",
	"Person": "/personFields",
	"Organization": "/organizationFields",
	"Product": "/productFields",
	"Activity": "/activityFields",
}
# A lead's custom fields are the deals' (Pipedrive's leads share the deal fields).
LEAD_FIELDS = "Deal"


@dataclass(frozen=True)
class Listing:
	"""One list: its version root and path, the identity type of its records, whether it takes
	``updated_since``, and extra query parameters."""

	root: str
	path: str
	type: str
	incremental: bool = True
	query: tuple[tuple[str, str], ...] = ()


USERS = Listing(V1, "/users", "User", incremental=False)
PRODUCTS = Listing(V2, "/products", "Product")
PERSONS = Listing(V2, "/persons", "Person")
ORGANIZATIONS = Listing(V2, "/organizations", "Organization")
PIPELINES = Listing(V2, "/pipelines", "Pipeline", incremental=False)
STAGES = Listing(V2, "/stages", "Stage", incremental=False)
DEALS = Listing(V2, "/deals", "Deal")
ARCHIVED_DEALS = Listing(V2, "/deals/archived", "Deal")
WON_DEALS = Listing(V2, "/deals", "Deal", incremental=False, query=(("status", WON),))
LEADS = Listing(V1, "/leads", "Lead")
ARCHIVED_LEADS = Listing(V1, "/leads/archived", "Lead", incremental=False)
ACTIVITIES = Listing(V2, "/activities", "Activity")
NOTES = Listing(V1, "/notes", "Note")
CALL_LOGS = Listing(V1, "/callLogs", "CallLog", incremental=False)
ACTIVITY_TYPES = Listing(V1, "/activityTypes", "ActivityType", incremental=False)
KIND_LISTINGS: dict[Kind, tuple[Listing, ...]] = {
	Kind.EMPLOYEE: (USERS,),
	Kind.PRODUCT: (PRODUCTS,),
	Kind.LEAD: (PERSONS,),
	Kind.PROSPECT: (ORGANIZATIONS,),
	Kind.OPPORTUNITY_TYPE: (PIPELINES,),
	Kind.OPPORTUNITY: (DEALS, ARCHIVED_DEALS, LEADS, ARCHIVED_LEADS),
	Kind.COMMUNICATION: (ACTIVITIES, NOTES, CALL_LOGS),
	Kind.EVENT: (ACTIVITIES,),
	Kind.TODO: (ACTIVITIES,),
}
KINDS: tuple[Kind, ...] = (*KIND_LISTINGS, Kind.CUSTOMER)
INCREMENTAL = frozenset(
	kind for kind, listings in KIND_LISTINGS.items() if any(listing.incremental for listing in listings)
)
CHOICE_TYPES = frozenset({"enum", "set"})


@dataclass(frozen=True)
class CustomField:
	"""A custom field: its name, its type, and its options' labels by id."""

	name: str
	type: str
	options: dict[str, str]

	def shown(self, value: JsonValue) -> str | None:
		"""A value as text: an option as its label (several joined with "; "), a monetary value with its
		currency, an address as its full text, a number exactly as sent."""
		if value is None or value in ("", []):
			return None
		if self.type in CHOICE_TYPES:
			chosen = value if isinstance(value, list) else str(value).split(",")
			return "; ".join(self.options.get(str(choice).strip(), str(choice).strip()) for choice in chosen)
		if isinstance(value, bool):
			return "Yes" if value else "No"
		if isinstance(value, int | float):
			return decimal_text(decimal_of(value))
		if isinstance(value, dict):
			amount, currency = value.get("value"), value.get("currency")
			if isinstance(amount, int | float | str) and isinstance(currency, str):
				return f"{decimal_text(decimal_of(amount))} {currency}"
			if isinstance(amount, str):
				return amount
			return json.dumps(value, sort_keys=True)
		return str(value) if isinstance(value, str) else json.dumps(value, sort_keys=True)


class PipedriveReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		if not config.account and not config.endpoint_override:
			raise SourceError("Pipedrive is not connected: use Connect to Pipedrive first.")
		self._config = config
		self._client = client
		self._fields: dict[str, dict[str, CustomField]] = {}
		self._stages: dict[int, Node] | None = None
		self._activity_types: dict[str, str] | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in INCREMENTAL

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		if kind is Kind.CUSTOMER:
			return self._customers(cursor)
		listings = KIND_LISTINGS.get(kind)
		if listings is None:
			raise SourceError(f"Pipedrive has no {kind.value}.")
		return chained(len(listings), cursor, lambda feed, at: self._listed(listings[feed], at, since))

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		paths = {
			"Person": "/persons",
			"Organization": "/organizations",
			"Deal": "/deals",
			"Product": "/products",
		}
		path = paths.get(identity.type)
		if path is None or not identity.id.isdigit():
			raise SourceError(f"Pipedrive can't read a {identity.type} by itself.")
		return self._by_id(identity.type, f"{V2}{path}/{identity.id}")

	# -- lists ----------------------------------------------------------------------------------------------

	def _listed(self, listing: Listing, position: str | None, since: datetime | None) -> Page:
		query: dict[str, QueryValue] = {"limit": PAGE_SIZE, **dict(listing.query)}
		if since is not None and listing.incremental:
			query["updated_since"] = since.astimezone(UTC).strftime(UPDATED_SINCE)
		start = 0
		if listing.root == V2:
			if position:
				query["cursor"] = position
		else:
			start = int(position or 0)
			query["start"] = start
		result = self._get(f"{listing.root}{listing.path}", query)
		records = result.children("data")
		nodes = [node for record in records for node in self._expanded(listing, record)]
		return Page(nodes, *self._next(listing, result, start + len(records)))

	def _next(self, listing: Listing, result: Node, read: int) -> tuple[bool, str | None]:
		"""Whether a list goes on, and where: v2's next cursor, or v1's offset after what was read."""
		extra = result.child("additional_data")
		if listing.root == V2:
			following = extra.optional_string("next_cursor") if extra else None
			return following is not None, following
		pagination = (extra.child("pagination") if extra else None) or extra
		more = pagination.optional_flag("more_items_in_collection") is True if pagination else False
		return more, str(read) if more else None

	def _all(self, path: str, query: dict[str, QueryValue]) -> list[Node]:
		"""Every record of a v2 list (stages, an organization's persons, a product's variations)."""
		found: list[Node] = []
		cursor: str | None = None
		while True:
			page_query: dict[str, QueryValue] = {"limit": PAGE_SIZE, **query}
			if cursor:
				page_query["cursor"] = cursor
			result = self._get(path, page_query)
			found += result.children("data")
			extra = result.child("additional_data")
			cursor = extra.optional_string("next_cursor") if extra else None
			if cursor is None:
				return found

	def _expanded(self, listing: Listing, record: Node) -> list[Node]:
		"""A record identified, with its custom fields by name and what its mapper needs beside it."""
		extra: dict[str, JsonValue] = {}
		match listing.type:
			case "Deal":
				stage = self._stage(record.optional_integer("stage_id"))
				extra["stage"] = stage.raw() if stage else None
			case "Organization":
				members = self._all(f"{V2}/persons", {"org_id": record.integer("id")})
				extra["persons"] = [member.raw() for member in members]
			case "Product":
				variations = self._all(f"{V2}/products/{record.integer('id')}/variations", {})
				extra["variations"] = [variation.raw() for variation in variations]
			case "Activity":
				extra["category"] = self._activity_category(record.optional_string("type"))
		return [self._identified(listing.type, record.raw() | extra)]

	def _by_id(self, type_: str, path: str) -> Node | None:
		result = self._client.get(path, {})
		record = result.node.child("data") if result else None
		if record is None:
			return None
		listing = {"Organization": ORGANIZATIONS, "Person": PERSONS, "Deal": DEALS, "Product": PRODUCTS}[
			type_
		]
		return self._expanded(listing, record)[0]

	# -- customers ------------------------------------------------------------------------------------------

	def _customers(self, cursor: str | None) -> Page:
		"""The organizations (else persons) won deals are with, each read whole."""
		page = self._listed(WON_DEALS, cursor, None)
		parties: dict[tuple[str, int], None] = {}
		for deal in page.nodes:
			org, person = deal.optional_integer("org_id"), deal.optional_integer("person_id")
			if org is not None:
				parties[("Organization", org)] = None
			elif person is not None:
				parties[("Person", person)] = None
		paths = {"Organization": "/organizations", "Person": "/persons"}
		nodes = [
			node
			for type_, party in parties
			if (node := self._by_id(type_, f"{V2}{paths[type_]}/{party}")) is not None
		]
		return Page(nodes, page.has_next_page, page.end_cursor)

	# -- definitions ----------------------------------------------------------------------------------------

	def _stage(self, stage_id: int | None) -> Node | None:
		if self._stages is None:
			self._stages = {stage.integer("id"): stage for stage in self._all(f"{V2}{STAGES.path}", {})}
		return self._stages.get(stage_id) if stage_id is not None else None

	def _activity_category(self, key: str | None) -> str | None:
		"""An activity type's name, as the account calls it (its mapper decides by the type's key)."""
		if self._activity_types is None:
			types = self._get(f"{V1}{ACTIVITY_TYPES.path}", {}).children("data")
			self._activity_types = {t.string("key_string"): t.string("name") for t in types}
		return self._activity_types.get(key or "")

	def _custom_fields(self, type_: str) -> dict[str, CustomField]:
		"""The custom fields of an object type, by hash."""
		if type_ not in self._fields:
			definitions = self._all(f"{V2}{FIELD_LISTS[type_]}", {})
			self._fields[type_] = {
				field.string("field_code"): CustomField(
					field.string("field_name"),
					field.string("field_type"),
					{str(option.get("id")): option.text("label") for option in field.children("options")},
				)
				for field in definitions
				if field.optional_flag("is_custom_field") is True
			}
		return self._fields[type_]

	# -- requests -------------------------------------------------------------------------------------------

	def _get(self, path: str, query: dict[str, QueryValue]) -> Node:
		result = self._client.get(path, query)
		if result is None:
			raise SourceError(f"Pipedrive has no {path}.")
		return result.node

	def _identified(self, type_: str, record: dict[str, JsonValue]) -> Node:
		source_id = record.get("id")
		if not isinstance(source_id, int | str) or isinstance(source_id, bool):
			raise SourceError(f"A Pipedrive {type_} has no id.")
		custom: dict[str, JsonValue] = {}
		fields_type = LEAD_FIELDS if type_ == "Lead" else type_
		if fields_type in FIELD_LISTS:
			values = record.get("custom_fields")
			found = values if isinstance(values, dict) else record
			for code, field in self._custom_fields(fields_type).items():
				shown = field.shown(found.get(code))
				if shown is not None:
					custom[field.name] = shown
		gid = self._config.identity(type_, source_id)
		return Node({**record, "id": gid, "source_id": str(source_id), "custom": custom}, gid)
