# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one Salesforce org through the REST API (``/services/data/v62.0``) with SOQL queries
(developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_query.htm): its users, products
with their standard price book prices, accounts with their contacts and notes, leads with their notes,
opportunities with their notes and the opportunity types, tasks, events, email messages, quotes with their
line items and orders with their products.

Salesforce publishes no static OpenAPI document for its standard objects (only a per-org beta generator), so
each object's fields come from its describe metadata
(developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/resources_sobject_describe.htm), read once
per run: the standard fields the mappers read that the org has (a multi-currency org's ``CurrencyIsoCode``, a
person account's fields), and every custom field (``custom`` true), whose values each node carries by label as
``custom`` (a picklist's values as their labels). The objects and fields are the Object Reference's
(developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference).

Each page is ``... ORDER BY Id LIMIT 200`` after the last id read (``Id > '<last>'``), so a run that pauses
for hours carries on where it was (a query locator expires after 15 minutes). A child relationship's records
come with their parent; when there are more than one answer holds (``done`` false), the rest are read from
its ``nextRecordsUrl``. Incremental runs read what changed since the last run (``SystemModstamp >=``); the
opportunity types (a picklist) are read whole.

Limits (developer.salesforce.com/docs/atlas.en-us.salesforce_app_limits_cheatsheet.meta/
salesforce_app_limits_cheatsheet/salesforce_app_limits_platform_api.htm): an org's API requests are counted
over 24 hours against its allocation, and every answer reports the count in ``Sforce-Limit-Info``
(``api-usage=<used>/<allocation>``, headers_api_usage.htm). The importer paces five requests a second, and
once the org has used 90% of its allocation it pauses the run for an hour rather than spend what the org's
other integrations need; a request refused for the limit (403 ``REQUEST_LIMIT_EXCEEDED``, errorcodes.htm)
pauses it the same way, never holds it as rejected credentials."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import (
	HTTP_FORBIDDEN,
	HTTP_TOO_MANY_REQUESTS,
	HttpResponse,
	HttpTransport,
	RateLimit,
)
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_text
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, Page

SOURCE_ID = "salesforce"
API_VERSION = "v62.0"
API_ROOT = f"/services/data/{API_VERSION}"
PAGE_SIZE = 200
REQUESTS_PER_SECOND = 5.0
RATE_LIMIT = RateLimit(requests_per_second=REQUESTS_PER_SECOND)
LIMIT_HEADER = "sforce-limit-info"
USAGE = re.compile(r"api-usage=(\d+)/(\d+)")
# The share of the org's daily allocation the importer leaves for everything else.
ALLOCATION_SHARE = Decimal("0.9")
SPENT_PAUSE_SECONDS = 3600
LIMIT_EXCEEDED = "REQUEST_LIMIT_EXCEEDED"
SOQL_TIME = "%Y-%m-%dT%H:%M:%SZ"
ID = re.compile(r"^[A-Za-z0-9]{15,18}$")
NOTES = "(SELECT Id, Title, Body, CreatedDate FROM Notes ORDER BY CreatedDate)"


@dataclass(frozen=True)
class SObject:
	"""One object queried: its API name (also its identity type), the standard fields the mappers read, its
	child relationship subqueries, and the condition that selects its records (beside paging and changes)."""

	name: str
	fields: tuple[str, ...]
	children: tuple[str, ...] = ()
	where: str | None = None


USER = SObject(
	"User",
	(
		"FirstName",
		"LastName",
		"Email",
		"Phone",
		"MobilePhone",
		"Title",
		"Department",
		"EmployeeNumber",
		"ManagerId",
		"IsActive",
	),
	where="UserType = 'Standard'",
)
PRODUCT = SObject(
	"Product2",
	("Name", "ProductCode", "Description", "IsActive", "Family", "StockKeepingUnit"),
	("(SELECT Id, UnitPrice FROM PricebookEntries WHERE Pricebook2.IsStandard = true AND IsActive = true)",),
)
ACCOUNT_FIELDS = (
	"Name",
	"Type",
	"Phone",
	"Website",
	"Industry",
	"Description",
	"AccountNumber",
	"BillingStreet",
	"BillingCity",
	"BillingState",
	"BillingPostalCode",
	"BillingCountry",
	"IsPersonAccount",
	"PersonEmail",
)
ACCOUNT_CHILDREN = (
	"(SELECT Id, FirstName, LastName, Email, Phone, MobilePhone, Title FROM Contacts ORDER BY Id)",
	NOTES,
)
ACCOUNT = SObject("Account", ACCOUNT_FIELDS, ACCOUNT_CHILDREN)
CUSTOMER = SObject(
	"Account",
	ACCOUNT_FIELDS,
	ACCOUNT_CHILDREN[:1],
	"(Type LIKE 'Customer%' OR Id IN (SELECT AccountId FROM Order))",
)
LEAD = SObject(
	"Lead",
	(
		"FirstName",
		"LastName",
		"Company",
		"Email",
		"Phone",
		"MobilePhone",
		"Title",
		"Website",
		"Status",
		"IsConverted",
		"Description",
		"Street",
		"City",
		"State",
		"PostalCode",
		"Country",
	),
	(NOTES,),
)
OPPORTUNITY = SObject(
	"Opportunity",
	(
		"Name",
		"AccountId",
		"Amount",
		"CloseDate",
		"StageName",
		"Probability",
		"IsClosed",
		"IsWon",
		"Type",
		"Description",
		"CreatedDate",
		"CurrencyIsoCode",
	),
	(NOTES,),
)
ACTIVITY = ("WhoId", "WhatId", "AccountId", "Subject", "Description", "CreatedDate")
TASK = SObject(
	"Task",
	(
		*ACTIVITY,
		"Status",
		"IsClosed",
		"ActivityDate",
		"TaskSubtype",
		"CallType",
		"CallDurationInSeconds",
		"CallDisposition",
	),
)
EVENT = SObject(
	"Event", (*ACTIVITY, "Location", "StartDateTime", "EndDateTime", "IsAllDayEvent", "ActivityDate")
)
EMAIL = SObject(
	"EmailMessage",
	("Subject", "TextBody", "FromAddress", "ToAddress", "Incoming", "MessageDate", "RelatedToId", "Status"),
)
QUOTE = SObject(
	"Quote",
	(
		"Name",
		"QuoteNumber",
		"Status",
		"AccountId",
		"ContactId",
		"ExpirationDate",
		"CreatedDate",
		"Subtotal",
		"TotalPrice",
		"Tax",
		"ShippingHandling",
		"GrandTotal",
		"CurrencyIsoCode",
	),
	(
		"(SELECT Id, Product2Id, Product2.Name, Description, Quantity, UnitPrice, TotalPrice "
		"FROM QuoteLineItems ORDER BY SortOrder, Id)",
	),
)
ORDER = SObject(
	"Order",
	(
		"OrderNumber",
		"AccountId",
		"EffectiveDate",
		"Status",
		"StatusCode",
		"TotalAmount",
		"Description",
		"PoNumber",
		"CurrencyIsoCode",
	),
	(
		"(SELECT Id, Product2Id, Product2.Name, Description, Quantity, UnitPrice, TotalPrice "
		"FROM OrderItems ORDER BY OrderItemNumber)",
	),
)
KIND_OBJECTS: dict[Kind, tuple[SObject, ...]] = {
	Kind.EMPLOYEE: (USER,),
	Kind.PRODUCT: (PRODUCT,),
	Kind.CUSTOMER: (CUSTOMER,),
	Kind.LEAD: (LEAD,),
	Kind.PROSPECT: (ACCOUNT,),
	Kind.OPPORTUNITY: (OPPORTUNITY,),
	Kind.COMMUNICATION: (TASK, EMAIL),
	Kind.EVENT: (EVENT,),
	Kind.TODO: (TASK,),
	Kind.DRAFT_ORDER: (QUOTE,),
	Kind.SALES_ORDER: (ORDER,),
}
KINDS: tuple[Kind, ...] = (*KIND_OBJECTS, Kind.OPPORTUNITY_TYPE)
OPPORTUNITY_TYPE = "OpportunityType"
# The records with amounts, which an org without multiple currencies keeps in its own.
PRICED = frozenset({"Opportunity", "Quote", "Order"})
ORGANISATION_QUERY = "SELECT DefaultCurrencyIsoCode FROM Organization"
CURSOR_SEPARATOR = ":"


def soql(
	sobject: SObject, fields: list[str], last: str | None, since: datetime | None, only: str | None = None
) -> str:
	"""One page's query: the fields and child subqueries, the object's own condition, changed since
	``since``, after the id ``last`` (or only the record ``only``), in id order."""
	conditions = [condition for condition in (sobject.where,) if condition]
	for record in (last, only):
		if record is not None and not ID.match(record):
			raise SourceError(f"Not a Salesforce id: {record}")
	if since is not None:
		conditions.append(f"SystemModstamp >= {since.astimezone(UTC).strftime(SOQL_TIME)}")
	if last is not None:
		conditions.append(f"Id > '{last}'")
	if only is not None:
		conditions.append(f"Id = '{only}'")
	selected = ", ".join(["Id", *fields, *sobject.children])
	where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
	# Every part is the importer's own or checked: ids match ID, fields are the org's describe names.
	return f"SELECT {selected} FROM {sobject.name}{where} ORDER BY Id LIMIT {PAGE_SIZE}"  # noqa: S608


@dataclass(frozen=True)
class Described:
	"""What an object's describe says the importer reads: the standard fields it has, and its custom fields'
	labels and picklist labels."""

	fields: list[str]
	custom: dict[str, CustomField]
	picklists: dict[str, dict[str, str]]


@dataclass(frozen=True)
class CustomField:
	label: str
	type: str
	options: dict[str, str]

	def shown(self, value: JsonValue) -> str | None:
		"""A field's value as text: a choice as its label (several joined with "; "), a checkbox as Yes or
		No, a number exactly as sent."""
		if value is None or value == "":
			return None
		if isinstance(value, bool):
			return "Yes" if value else "No"
		if isinstance(value, int | float):
			return decimal_text(Decimal(repr(value)) if isinstance(value, float) else Decimal(value))
		if isinstance(value, str):
			if self.type == "multipicklist":
				return "; ".join(self.options.get(choice, choice) for choice in value.split(";"))
			return self.options.get(value, value)
		return json.dumps(value, sort_keys=True)


def described(document: Node, wanted: tuple[str, ...]) -> Described:
	fields = {field.string("name"): field for field in document.children("fields")}
	custom = {
		name: CustomField(
			field.string("label"),
			field.string("type"),
			{choice.string("value"): choice.string("label") for choice in field.children("picklistValues")},
		)
		for name, field in fields.items()
		if field.flag("custom")
	}
	picklists = {
		name: {choice.string("value"): choice.string("label") for choice in field.children("picklistValues")}
		for name, field in fields.items()
		if field.children("picklistValues")
	}
	return Described([name for name in wanted if name in fields], custom, picklists)


class LimitGuard:
	"""An ``HttpTransport`` around the real one that keeps a run inside the org's daily allocation: once an
	answer reports 90% of it used, or a request is refused for the limit, every request answers 429 with an
	hour's Retry-After (so the run pauses in place) without reaching Salesforce."""

	def __init__(self, transport: HttpTransport) -> None:
		self._transport = transport
		self._spent = False

	def get(self, url: str, headers: dict[str, str], timeout_seconds: float) -> HttpResponse:
		return self._guarded(lambda: self._transport.get(url, headers, timeout_seconds))

	def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> HttpResponse:
		return self._guarded(lambda: self._transport.post(url, headers, body, timeout_seconds))

	def download(self, url: str, timeout_seconds: float, max_bytes: int) -> bytes:
		return self._transport.download(url, timeout_seconds, max_bytes)

	def _guarded(self, send: Callable[[], HttpResponse]) -> HttpResponse:
		if self._spent:
			return self._paused()
		response = send()
		usage = USAGE.search(response.header(LIMIT_HEADER) or "")
		if usage and Decimal(usage.group(1)) >= Decimal(usage.group(2)) * ALLOCATION_SHARE:
			self._spent = True
		if response.status == HTTP_FORBIDDEN and LIMIT_EXCEEDED.encode() in response.body:
			self._spent = True
			return self._paused()
		return response

	@staticmethod
	def _paused() -> HttpResponse:
		return HttpResponse(
			HTTP_TOO_MANY_REQUESTS,
			b'[{"errorCode":"REQUEST_LIMIT_EXCEEDED"}]',
			{"retry-after": str(SPENT_PAUSE_SECONDS)},
		)


class SalesforceReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		self._config = config
		self._client = client
		self._described: dict[str, Described] = {}
		self._currency: str | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in KIND_OBJECTS and kind is not Kind.CUSTOMER

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		if kind is Kind.OPPORTUNITY_TYPE:
			types = self._describe(OPPORTUNITY).picklists.get("Type", {})
			nodes = [self._type(value, label) for value, label in types.items()]
			return Page(nodes, False, None)
		objects = KIND_OBJECTS.get(kind)
		if objects is None:
			raise SourceError(f"Salesforce has no {kind.value}.")
		feed, last = 0, None
		if cursor:
			index, _, rest = cursor.partition(CURSOR_SEPARATOR)
			feed, last = int(index), rest or None
		records = self._query(objects[feed], last, since)
		if len(records) == PAGE_SIZE:
			return Page(records, True, f"{feed}{CURSOR_SEPARATOR}{records[-1].string('source_id')}")
		following = feed + 1
		more = following < len(objects)
		return Page(records, more, f"{following}{CURSOR_SEPARATOR}" if more else None)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		objects = KIND_OBJECTS.get(kind, ())
		sobject = next((candidate for candidate in objects if candidate.name == identity.type), None)
		if sobject is None:
			raise SourceError(f"Salesforce can't read a {identity.type} as a {kind.value} by itself.")
		described_ = self._describe(sobject)
		records = self._records(
			soql(sobject, [*described_.fields, *described_.custom], None, None, identity.id)
		)
		return records[0] if records else None

	# -- queries --------------------------------------------------------------------------------------------

	def _query(self, sobject: SObject, last: str | None, since: datetime | None) -> list[Node]:
		described_ = self._describe(sobject)
		return self._records(soql(sobject, [*described_.fields, *described_.custom], last, since))

	def _records(self, query: str) -> list[Node]:
		result = self._get(f"{API_ROOT}/query", {"q": query})
		nodes = []
		for record in result.children("records"):
			raw = record.raw()
			for name, value in record.items():
				if isinstance(value, dict) and "records" in value:
					raw[name] = {
						"records": list[JsonValue]([child.raw() for child in self._children(record, name)])
					}
			nodes.append(self._identified(record.required_child("attributes").string("type"), raw))
		return nodes

	def _children(self, record: Node, relationship: str) -> Iterator[Node]:
		"""A child relationship's records, all of them (a long list continues at its nextRecordsUrl)."""
		children = record.required_child(relationship)
		while True:
			yield from children.children("records")
			following = children.optional_string("nextRecordsUrl")
			if children.optional_flag("done") is not False or following is None:
				return
			children = self._get(following, {})

	def _describe(self, sobject: SObject) -> Described:
		if sobject.name not in self._described:
			document = self._get(f"{API_ROOT}/sobjects/{sobject.name}/describe", {})
			self._described[sobject.name] = described(document, sobject.fields)
		found = self._described[sobject.name]
		return Described(
			[name for name in sobject.fields if name in found.fields], found.custom, found.picklists
		)

	def _get(self, path: str, query: dict[str, str]) -> Node:
		result = self._client.get(path, query)
		if result is None:
			raise SourceError(f"Salesforce has no {path}.")
		return result.node

	def _identified(self, type_: str, record: dict[str, JsonValue]) -> Node:
		source_id = record.get("Id")
		if not isinstance(source_id, str):
			raise SourceError(f"A Salesforce {type_} has no Id.")
		described_ = self._described.get(type_)
		custom: dict[str, JsonValue] = {}
		if described_ is not None:
			for name, field in described_.custom.items():
				shown = field.shown(record.get(name))
				if shown is not None:
					custom[field.label] = shown
		picklists: dict[str, JsonValue] = {}
		if described_ is not None:
			for name, labels in described_.picklists.items():
				value = record.get(name)
				if isinstance(value, str) and value in labels:
					picklists[name] = labels[value]
		extra: dict[str, JsonValue] = {"custom": custom, "labels": picklists}
		if type_ in PRICED and "CurrencyIsoCode" not in record:
			extra["org_currency"] = self._org_currency()
		gid = self._config.identity(type_, source_id)
		return Node({**record, "id": gid, "source_id": source_id, **extra}, gid)

	def _org_currency(self) -> str:
		"""The currency of an org without multiple currencies, whose records name none."""
		if self._currency is None:
			organisations = self._get(f"{API_ROOT}/query", {"q": ORGANISATION_QUERY}).children("records")
			if not organisations:
				raise SourceError("Salesforce answered no Organization.")
			self._currency = organisations[0].string("DefaultCurrencyIsoCode")
		return self._currency

	def _type(self, value: str, label: str) -> Node:
		gid = type_identity(self._config, value)
		return Node({"id": gid, "value": value, "label": label}, gid)


def type_identity(config: ConnectionConfig, value: str) -> str:
	"""An opportunity type's identity: its picklist value (quoted, so a slash can't split the identity)."""
	return config.identity(OPPORTUNITY_TYPE, quote(value, safe=""))
