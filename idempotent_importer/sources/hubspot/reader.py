# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one HubSpot account through the CRM API v3 (api.hubapi.com), as HubSpot's OpenAPI documents
describe it (HubSpot/HubSpot-public-api-spec-collection, PublicApiSpecs/CRM/*/v3, pinned in tools/specs.py):
the owners, products, contacts, companies, the deal pipelines and deals, the calls, emails, notes, meetings
and tasks around them, quotes, orders and invoices with their line items, discounts, fees and taxes, and
commerce payments.

Every object is read with the properties the mappers use and every property the account's users defined
themselves (``hubspotDefined`` false in the properties API), whose values the node carries by label as
``custom``. Lists page after the cursor each answer gives (``paging.next.after``), 100 at a time, with the
associations each object needs (``associations=``). A contact, company or deal carries its notes, read in
batches (``batch/read``); a quote, order or invoice its line items, discounts, fees and taxes; a deal its
pipeline stage.

Incremental runs ask the search API for what changed since the last run (``hs_lastmodifieddate``, a
contact's ``lastmodifieddate``, at or after the time in milliseconds) in object id order, each page after the
last id read (so a query never reaches search's 10,000-result cap), and read each object whole by id (search
answers carry no associations). The customers are the contacts and companies at the customer lifecycle stage
(searched for) and the ones invoices, orders and payments are for: read whole, every run.

Limits (developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines): an OAuth app has 110
requests every 10 seconds per account, reported in X-HubSpot-RateLimit-Remaining over
X-HubSpot-RateLimit-Interval-Milliseconds; search has its own, five requests a second, and answers without
those headers. The client paces 100 every 10 seconds, search four a second."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.common import chained

SOURCE_ID = "hubspot"
API_HOST = "https://api.hubapi.com"
PAGE_SIZE = 100
BATCH_SIZE = 100
REQUESTS_PER_WINDOW = 100
WINDOW_SECONDS = 10.0
RATE_LIMIT = RateLimit(
	requests_per_second=RateLimit.per_period(REQUESTS_PER_WINDOW, WINDOW_SECONDS),
	remaining_header="x-hubspot-ratelimit-remaining",
	window_seconds=WINDOW_SECONDS,
)
SEARCH_REQUESTS_PER_SECOND = 4.0
SEARCH_RATE_LIMIT = RateLimit(requests_per_second=SEARCH_REQUESTS_PER_SECOND)
MILLISECONDS = 1000
CUSTOMER_STAGE = "customer"
REFUND = "PaymentRefund"
DISPUTE_LOST = "disputed_lost"
CONTACTS, COMPANIES, DEALS = "contacts", "companies", "deals"
NOTES, LINE_ITEMS = "notes", "line_items"
ADJUSTMENTS = ("discounts", "fees", "taxes")
PARTIES = (CONTACTS, COMPANIES)


@dataclass(frozen=True)
class ObjectType:
	"""One CRM object type: its path segment, its name in the properties and pipelines APIs, the identity
	type of its objects, the properties the mappers read, the associations they need, and the property that
	says when an object last changed."""

	path: str
	name: str
	type: str
	properties: tuple[str, ...]
	associations: tuple[str, ...] = ()
	modified: str = "hs_lastmodifieddate"
	# The parts read along with each object, by association: notes, line items, discounts, fees, taxes.
	parts: tuple[str, ...] = field(default=())


CONTACT = ObjectType(
	CONTACTS,
	CONTACTS,
	"Contact",
	(
		"firstname",
		"lastname",
		"email",
		"phone",
		"mobilephone",
		"company",
		"jobtitle",
		"website",
		"address",
		"city",
		"state",
		"zip",
		"country",
		"lifecyclestage",
		"hs_lead_status",
	),
	(NOTES,),
	"lastmodifieddate",
	(NOTES,),
)
COMPANY = ObjectType(
	COMPANIES,
	COMPANIES,
	"Company",
	(
		"name",
		"domain",
		"website",
		"phone",
		"address",
		"address2",
		"city",
		"state",
		"zip",
		"country",
		"description",
		"lifecyclestage",
	),
	(CONTACTS, NOTES),
	parts=(NOTES,),
)
DEAL = ObjectType(
	"0-3",
	DEALS,
	"Deal",
	(
		"dealname",
		"amount",
		"deal_currency_code",
		"closedate",
		"createdate",
		"pipeline",
		"dealstage",
		"hs_deal_stage_probability",
		"description",
	),
	(CONTACTS, COMPANIES, NOTES),
	parts=(NOTES,),
)
ENGAGED = (CONTACTS, DEALS, COMPANIES)
CALL = ObjectType(
	"calls",
	"calls",
	"Call",
	(
		"hs_timestamp",
		"hs_call_title",
		"hs_call_body",
		"hs_call_direction",
		"hs_call_duration",
		"hs_call_status",
		"hs_call_to_number",
		"hs_call_from_number",
	),
	ENGAGED,
)
EMAIL = ObjectType(
	"emails",
	"emails",
	"Email",
	(
		"hs_timestamp",
		"hs_email_subject",
		"hs_email_text",
		"hs_email_direction",
		"hs_email_to_email",
		"hs_email_from_email",
	),
	ENGAGED,
)
MEETING = ObjectType(
	"meetings",
	"meetings",
	"Meeting",
	(
		"hs_timestamp",
		"hs_meeting_title",
		"hs_meeting_body",
		"hs_internal_meeting_notes",
		"hs_meeting_location",
		"hs_meeting_start_time",
		"hs_meeting_end_time",
		"hs_meeting_outcome",
	),
	ENGAGED,
)
TASK = ObjectType(
	"tasks",
	"tasks",
	"Task",
	("hs_timestamp", "hs_task_subject", "hs_task_body", "hs_task_status", "hs_task_priority", "hs_task_type"),
	ENGAGED,
)
NOTE = ObjectType(NOTES, NOTES, "Note", ("hs_timestamp", "hs_note_body"))
PRODUCT = ObjectType(
	"products",
	"products",
	"Product",
	("name", "description", "price", "hs_sku", "hs_product_type", "hs_recurring_billing_period"),
)
LINE_ITEM = ObjectType(
	LINE_ITEMS,
	LINE_ITEMS,
	"LineItem",
	(
		"name",
		"quantity",
		"price",
		"amount",
		"hs_product_id",
		"hs_tax_rate_group_id",
		"hs_recurring_billing_period",
	),
)
ADJUSTMENT_PROPERTIES = ("hs_label", "hs_type", "hs_value", "hs_sort_order")
DOCUMENT_PARTS = (LINE_ITEMS, *ADJUSTMENTS)
QUOTE = ObjectType(
	"quotes",
	"quotes",
	"Quote",
	(
		"hs_title",
		"hs_quote_number",
		"hs_expiration_date",
		"hs_status",
		"hs_quote_amount",
		"hs_currency",
		"hs_createdate",
	),
	(*DOCUMENT_PARTS, CONTACTS, COMPANIES),
	parts=DOCUMENT_PARTS,
)
ORDER = ObjectType(
	"orders",
	"orders",
	"Order",
	("hs_order_name", "hs_currency_code", "hs_pipeline_stage", "hs_createdate"),
	(*DOCUMENT_PARTS, CONTACTS, COMPANIES),
	parts=DOCUMENT_PARTS,
)
INVOICE = ObjectType(
	"invoices",
	"invoices",
	"Invoice",
	(
		"hs_number",
		"hs_invoice_date",
		"hs_due_date",
		"hs_currency",
		"hs_invoice_status",
		"hs_purchase_order_number",
		"hs_comments",
	),
	(*DOCUMENT_PARTS, CONTACTS, COMPANIES),
	parts=DOCUMENT_PARTS,
)
PAYMENT = ObjectType(
	"commerce_payments",
	"commerce_payments",
	"Payment",
	(
		"hs_initial_amount",
		"hs_currency_code",
		"hs_latest_status",
		"hs_initiated_date",
		"hs_refunds_amount",
		"hs_payment_method_type",
		"hs_reference_number",
	),
	("invoices", CONTACTS, COMPANIES),
)
PART_TYPES = {
	NOTES: NOTE,
	LINE_ITEMS: LINE_ITEM,
	"discounts": ObjectType("discounts", "discounts", "Discount", ADJUSTMENT_PROPERTIES),
	"fees": ObjectType("fees", "fees", "Fee", ADJUSTMENT_PROPERTIES),
	"taxes": ObjectType("taxes", "taxes", "Tax", ADJUSTMENT_PROPERTIES),
}
BY_PATH = {
	object_type.name: object_type
	for object_type in (CONTACT, COMPANY, DEAL, CALL, EMAIL, MEETING, TASK, PRODUCT, QUOTE, ORDER, INVOICE)
}
# The object types of each kind, read one after another.
KIND_OBJECTS: dict[Kind, tuple[ObjectType, ...]] = {
	Kind.PRODUCT: (PRODUCT,),
	Kind.LEAD: (CONTACT,),
	Kind.PROSPECT: (COMPANY,),
	Kind.OPPORTUNITY: (DEAL,),
	Kind.COMMUNICATION: (CALL, EMAIL),
	Kind.EVENT: (MEETING,),
	Kind.TODO: (TASK,),
	Kind.DRAFT_ORDER: (QUOTE,),
	Kind.SALES_ORDER: (ORDER,),
	Kind.SALES_INVOICE: (INVOICE,),
	Kind.PAYMENT_ENTRY: (PAYMENT,),
}
# The documents whose parties are customers, read for them in turn after the customer lifecycle stage.
BILLED = (INVOICE, ORDER, PAYMENT)
KINDS: tuple[Kind, ...] = (Kind.EMPLOYEE, Kind.CUSTOMER, Kind.OPPORTUNITY_TYPE, *KIND_OBJECTS)
MULTIPLE_CHOICES = ";"
ARCHIVED_FEED = 1


@dataclass(frozen=True)
class CustomProperty:
	"""A property the account's users defined: its label, and its options' labels by value (a choice
	property's values are its options' internal values; a multiple choice joins them with ``;``)."""

	label: str
	options: dict[str, str]

	def shown(self, value: str) -> str:
		if not self.options:
			return value
		return "; ".join(self.options.get(choice, choice) for choice in value.split(MULTIPLE_CHOICES))


def associated(node: Node, name: str) -> list[str]:
	"""The ids of the objects of type ``name`` an object is associated with."""
	associations = node.child("associations")
	group = associations.child(name) if associations else None
	return list(dict.fromkeys(item.string("id") for item in group.children("results"))) if group else []


def billed_party(node: Node) -> tuple[ObjectType, str] | None:
	"""Who a document is for: its company, else its contact."""
	companies, contacts = associated(node, COMPANIES), associated(node, CONTACTS)
	if companies:
		return COMPANY, companies[0]
	if contacts:
		return CONTACT, contacts[0]
	return None


class HubSpotReader:
	def __init__(self, config: ConnectionConfig, client: RestClient, search: RestClient) -> None:
		self._config = config
		self._client = client
		self._search = search
		self._custom: dict[str, dict[str, CustomProperty]] = {}
		self._pipelines: list[Node] | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in KIND_OBJECTS

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		match kind:
			case Kind.EMPLOYEE:
				return chained(2, cursor, self._owners)
			case Kind.CUSTOMER:
				return chained(len(PARTIES) + len(BILLED), cursor, self._customers)
			case Kind.OPPORTUNITY_TYPE:
				return Page(
					[self._identified("Pipeline", p.raw()) for p in self._deal_pipelines()], False, None
				)
		objects = KIND_OBJECTS.get(kind)
		if objects is None:
			raise SourceError(f"HubSpot has no {kind.value}.")
		if since is not None:
			return chained(len(objects), cursor, lambda feed, at: self._changed(objects[feed], at, since))
		return chained(len(objects), cursor, lambda feed, at: self._listed(objects[feed], at))

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		object_type = next((o for o in BY_PATH.values() if o.type == identity.type), None)
		if object_type is None:
			raise SourceError(f"HubSpot can't read a {identity.type} by itself.")
		return self._by_id(object_type, identity.id)

	# -- owners ---------------------------------------------------------------------------------------------

	def _owners(self, feed: int, after: str | None) -> Page:
		"""The active owners (feed 0), then the archived ones (feed 1)."""
		query: dict[str, QueryValue] = {"limit": PAGE_SIZE, "archived": feed == ARCHIVED_FEED}
		if after:
			query["after"] = after
		result = self._get("/crm/v3/owners", query)
		nodes = [self._identified("Owner", owner.raw()) for owner in result.children("results")]
		return Page(nodes, *self._next(result))

	# -- objects --------------------------------------------------------------------------------------------

	def _listed(self, object_type: ObjectType, after: str | None) -> Page:
		query: dict[str, QueryValue] = {
			"limit": PAGE_SIZE,
			"archived": False,
			"properties": ",".join(self._properties(object_type)),
		}
		if object_type.associations:
			query["associations"] = ",".join(object_type.associations)
		if after:
			query["after"] = after
		result = self._get(f"/crm/v3/objects/{object_type.path}", query)
		nodes = self._complete(object_type, result.children("results"))
		return Page(nodes, *self._next(result))

	def _changed(self, object_type: ObjectType, position: str | None, since: datetime) -> Page:
		"""What changed since ``since``, each read whole by id, in id order after the last one read."""
		changed = int(since.timestamp() * MILLISECONDS)
		found = self._searched(object_type, [(object_type.modified, "GTE", str(changed))], position)
		nodes = [node for summary in found.results if (node := self._by_id(object_type, summary)) is not None]
		return Page(nodes, found.more, found.last)

	def _by_id(self, object_type: ObjectType, object_id: str) -> Node | None:
		query: dict[str, QueryValue] = {"properties": ",".join(self._properties(object_type))}
		if object_type.associations:
			query["associations"] = ",".join(object_type.associations)
		result = self._client.get(f"/crm/v3/objects/{object_type.path}/{object_id}", query)
		if result is None:
			return None
		return self._complete(object_type, [result.node])[0]

	def _complete(self, object_type: ObjectType, records: list[Node]) -> list[Node]:
		"""Each object identified, with its own fields by label, its parts and (a deal) its stage."""
		parts = {name: self._batch(PART_TYPES[name], records, name) for name in object_type.parts}
		labels = self._custom_labels(object_type)
		stages = self._stages() if object_type is DEAL else {}
		nodes: list[Node] = []
		for record in records:
			properties = record.child("properties")
			custom: dict[str, JsonValue] = {}
			for name, custom_property in labels.items():
				value = properties.optional_string(name) if properties else None
				if value is not None:
					custom[custom_property.label] = custom_property.shown(value)
			extra: dict[str, JsonValue] = {"custom": custom}
			for name, found in parts.items():
				extra[name] = [found[part].raw() for part in associated(record, name) if part in found]
			if object_type is DEAL and properties is not None:
				stage = stages.get((properties.text("pipeline"), properties.text("dealstage")))
				extra["stage"] = stage.raw() if stage else None
			node = self._identified(object_type.type, {**record.raw(), **extra})
			nodes += self._with_refund(node) if object_type is PAYMENT else [node]
		return nodes

	def _with_refund(self, payment: Node) -> list[Node]:
		"""A payment, and what it gave back (a refund, or a lost dispute) as a payment of its own."""
		properties = payment.required_child("properties")
		refunded = properties.optional_decimal("hs_refunds_amount")
		if not refunded and properties.optional_string("hs_latest_status") != DISPUTE_LOST:
			return [payment]
		refund = self._config.identity(REFUND, payment.string("source_id"))
		return [payment, Node({**payment.raw(), "id": refund}, refund)]

	def _batch(self, object_type: ObjectType, records: list[Node], name: str) -> dict[str, Node]:
		"""The parts (notes, line items, …) a page of objects is associated with, read in batches."""
		wanted = list(dict.fromkeys(part for record in records for part in associated(record, name)))
		found: dict[str, Node] = {}
		for start in range(0, len(wanted), BATCH_SIZE):
			body: dict[str, JsonValue] = {
				"properties": list[JsonValue](object_type.properties),
				"inputs": [{"id": part} for part in wanted[start : start + BATCH_SIZE]],
			}
			result = self._client.query(f"/crm/v3/objects/{object_type.path}/batch/read", body)
			if result is None:
				raise SourceError(f"HubSpot has no {object_type.path} to read.")
			found |= {part.string("id"): part for part in result.node.children("results")}
		return found

	# -- customers ------------------------------------------------------------------------------------------

	def _customers(self, feed: int, position: str | None) -> Page:
		"""The contacts (feed 0) and companies (feed 1) at the customer lifecycle stage, then the contacts and
		companies the invoices, orders and payments are for (feeds 2 to 4), each read whole."""
		if feed < len(PARTIES):
			object_type = CONTACT if feed == 0 else COMPANY
			found = self._searched(object_type, [("lifecyclestage", "EQ", CUSTOMER_STAGE)], position)
			nodes = [node for summary in found.results if (node := self._party(object_type, summary))]
			return Page(nodes, found.more, found.last)
		document = BILLED[feed - len(PARTIES)]
		query: dict[str, QueryValue] = {
			"limit": PAGE_SIZE,
			"archived": False,
			"associations": "contacts,companies",
		}
		if position:
			query["after"] = position
		result = self._get(f"/crm/v3/objects/{document.path}", query)
		billed = dict.fromkeys(
			party for record in result.children("results") if (party := billed_party(record))
		)
		nodes = [node for object_type, party in billed if (node := self._party(object_type, party))]
		return Page(nodes, *self._next(result))

	def _party(self, object_type: ObjectType, party_id: str) -> Node | None:
		"""A customer: a contact, or a company with its people (read in a batch)."""
		party = self._by_id(object_type, party_id)
		if party is None or object_type is CONTACT:
			return party
		people = self._batch(CONTACT, [party], CONTACTS)
		return party.with_fields(people=[people[p].raw() for p in associated(party, CONTACTS) if p in people])

	# -- pipelines ------------------------------------------------------------------------------------------

	def _deal_pipelines(self) -> list[Node]:
		if self._pipelines is None:
			self._pipelines = self._get(f"/crm/v3/pipelines/{DEALS}", {}).children("results")
		return self._pipelines

	def _stages(self) -> dict[tuple[str, str], Node]:
		return {
			(pipeline.string("id"), stage.string("id")): stage
			for pipeline in self._deal_pipelines()
			for stage in pipeline.children("stages")
		}

	# -- properties -----------------------------------------------------------------------------------------

	def _custom_labels(self, object_type: ObjectType) -> dict[str, CustomProperty]:
		"""The properties the account's users defined on an object type, by name."""
		if object_type.name not in self._custom:
			result = self._get(f"/crm/v3/properties/{object_type.name}", {"archived": False})
			self._custom[object_type.name] = {
				prop.string("name"): CustomProperty(
					prop.string("label"),
					{option.string("value"): option.string("label") for option in prop.children("options")},
				)
				for prop in result.children("results")
				if prop.optional_flag("hubspotDefined") is not True
			}
		return self._custom[object_type.name]

	def _properties(self, object_type: ObjectType) -> list[str]:
		return list(dict.fromkeys([*object_type.properties, *self._custom_labels(object_type)]))

	# -- search ---------------------------------------------------------------------------------------------

	def _searched(
		self, object_type: ObjectType, filters: list[tuple[str, str, str]], last: str | None
	) -> Searched:
		"""One page of objects matching ``filters``, in id order after the id ``last``."""
		conditions: list[JsonValue] = [
			{"propertyName": name, "operator": operator, "value": value} for name, operator, value in filters
		]
		if last:
			conditions.append({"propertyName": "hs_object_id", "operator": "GT", "value": last})
		body: dict[str, JsonValue] = {
			"filterGroups": [{"filters": conditions}],
			"sorts": [{"propertyName": "hs_object_id", "direction": "ASCENDING"}],
			"properties": ["hs_object_id"],
			"limit": PAGE_SIZE,
		}
		result = self._search.query(f"/crm/v3/objects/{object_type.path}/search", body)
		if result is None:
			raise SourceError(f"HubSpot can't search {object_type.path}.")
		ids = [record.string("id") for record in result.node.children("results")]
		more = len(ids) == PAGE_SIZE
		return Searched(ids, more, ids[-1] if more else None)

	# -- requests -------------------------------------------------------------------------------------------

	def _get(self, path: str, query: dict[str, QueryValue]) -> Node:
		result = self._client.get(path, query)
		if result is None:
			raise SourceError(f"HubSpot has no {path}.")
		return result.node

	@staticmethod
	def _next(result: Node) -> tuple[bool, str | None]:
		paging = result.child("paging")
		following = paging.child("next") if paging else None
		after = following.optional_string("after") if following else None
		return after is not None, after

	def _identified(self, type_: str, record: dict[str, JsonValue]) -> Node:
		source_id = record.get("id")
		if not isinstance(source_id, str):
			raise SourceError(f"A HubSpot {type_} has no id.")
		gid = self._config.identity(type_, source_id)
		return Node({**record, "id": gid, "source_id": source_id}, gid)


@dataclass(frozen=True)
class Searched:
	results: list[str]
	more: bool
	last: str | None
