# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one Zoho CRM organisation through the CRM API v8 (``www.zohoapis.<dc>/crm/v8``), as Zoho's OpenAPI
documents describe it (zoho/crm-oas, ``v8.0/*/*.yaml``, pinned in tools/specs.py): the users, the modules the
organisation has (``/settings/modules``) and each one's fields (``/settings/fields``): products, vendors,
leads, accounts with their contacts, the accounts that are customers (or that sales orders and invoices are
for), the deal pipelines and deals, calls, notes, events and tasks, quotes, sales orders, invoices and
purchase orders.

A module is read with the standard fields its mapper uses that the organisation has and every custom field
(``custom_field`` true), whose values each node carries by label as ``custom`` (a picklist's values as their
display values). Zoho returns at most 50 fields a request, so a module needing more reads its pages with the
first 50 and the rest by the page's ids (``ids``), merged. Pages hold 100 records (``per_page``) from ``page``
1; past what pages reach, after the ``next_page_token`` Zoho gives (``info.more_records``). An activity or note
on a contact carries the contact's account (contacts are read once by id). Incremental runs ask for what
changed since the last run (``If-Modified-Since``); the users, pipelines and customers are read whole.

Limits (www.zoho.com/crm/developer/docs/api/v8/api-limits.html): API credits over a rolling 24 hours by
edition and licences (a records request costs one), and 5 to 25 calls at once; past either Zoho answers
``TOO_MANY_REQUESTS``. The client paces five requests a second; once ``X-API-CREDITS-REMAINING`` (sent from
half the day's credits on) falls to a reserve, the run pauses for an hour, leaving the rest to the
organisation's other integrations."""

from __future__ import annotations

import json
from collections.abc import Iterator
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
from idempotent_importer.sources.zoho.client import Product

SOURCE_ID = "zoho_crm"
API_PATH = "/crm/v8"
PER_PAGE = 100
FIELDS_PER_REQUEST = 50
REQUESTS_PER_SECOND = 5.0
CREDITS_RESERVE = 1000
CREDITS_PAUSE_SECONDS = 3600.0
RATE_LIMIT = RateLimit(
	requests_per_second=REQUESTS_PER_SECOND,
	remaining_header="x-api-credits-remaining",
	reserve=CREDITS_RESERVE,
	window_seconds=CREDITS_PAUSE_SECONDS,
)
MODIFIED_SINCE = "%Y-%m-%dT%H:%M:%S+00:00"
CONTINUATION = "t:"
PRODUCT = Product(
	"Zoho CRM",
	API_PATH,
	("ZohoCRM.modules.READ", "ZohoCRM.settings.READ", "ZohoCRM.users.READ", "ZohoCRM.org.READ"),
)
CUSTOMER_TYPE = "Customer"
CONTACTS = "Contacts"


@dataclass(frozen=True)
class Module:
	"""One module: its API name and the standard fields its mapper reads."""

	name: str
	fields: tuple[str, ...]

	@property
	def type(self) -> str:
		"""Its records' identity type: its API name without underscores (``SalesOrders``)."""
		return self.name.replace("_", "")


ADDRESS = ("Street", "City", "State", "Zip_Code", "Country")
BILLING = ("Billing_Street", "Billing_City", "Billing_State", "Billing_Code", "Billing_Country")
ACTIVITY = ("What_Id", "$se_module", "Who_Id", "Description", "Created_Time")
LINE_TOTALS = ("Sub_Total", "Discount", "Tax", "Adjustment", "Grand_Total", "Currency", "Exchange_Rate")
PRODUCTS = Module(
	"Products",
	("Product_Name", "Product_Code", "Unit_Price", "Description", "Product_Active", "Qty_in_Stock"),
)
VENDORS = Module("Vendors", ("Vendor_Name", "Email", "Phone", "Website", "Description", *ADDRESS))
LEADS = Module(
	"Leads",
	(
		"First_Name",
		"Last_Name",
		"Company",
		"Email",
		"Phone",
		"Mobile",
		"Designation",
		"Website",
		"Lead_Status",
		"Description",
		*ADDRESS,
	),
)
ACCOUNTS = Module(
	"Accounts",
	("Account_Name", "Account_Type", "Phone", "Website", "Description", "Account_Number", *BILLING),
)
CONTACT = Module("Contacts", ("First_Name", "Last_Name", "Email", "Phone", "Mobile", "Title", "Account_Name"))
DEALS = Module(
	"Deals",
	(
		"Deal_Name",
		"Account_Name",
		"Contact_Name",
		"Amount",
		"Stage",
		"Probability",
		"Closing_Date",
		"Pipeline",
		"Type",
		"Description",
		"Created_Time",
		"Currency",
	),
)
CALLS = Module(
	"Calls", ("Subject", "Call_Type", "Call_Start_Time", "Call_Duration_in_seconds", "Call_Result", *ACTIVITY)
)
NOTES = Module("Notes", ("Note_Title", "Note_Content", "Parent_Id", "Created_Time"))
EVENTS = Module(
	"Events",
	("Event_Title", "Start_DateTime", "End_DateTime", "All_day", "Venue", "Participants", *ACTIVITY),
)
TASKS = Module("Tasks", ("Subject", "Due_Date", "Status", "Priority", *ACTIVITY))
QUOTES = Module(
	"Quotes",
	("Subject", "Quote_Stage", "Account_Name", "Valid_Till", "Created_Time", "Quoted_Items", *LINE_TOTALS),
)
SALES_ORDERS = Module(
	"Sales_Orders",
	(
		"Subject",
		"SO_Number",
		"Status",
		"Account_Name",
		"Due_Date",
		"Created_Time",
		"Ordered_Items",
		*LINE_TOTALS,
	),
)
INVOICES = Module(
	"Invoices",
	(
		"Subject",
		"Invoice_Number",
		"Status",
		"Account_Name",
		"Invoice_Date",
		"Due_Date",
		"Invoiced_Items",
		*LINE_TOTALS,
	),
)
PURCHASE_ORDERS = Module(
	"Purchase_Orders",
	("Subject", "PO_Number", "Status", "Vendor_Name", "PO_Date", "Due_Date", "Purchased_Items", *LINE_TOTALS),
)
KIND_MODULES: dict[Kind, tuple[Module, ...]] = {
	Kind.PRODUCT: (PRODUCTS,),
	Kind.SUPPLIER: (VENDORS,),
	Kind.LEAD: (LEADS,),
	Kind.PROSPECT: (ACCOUNTS,),
	Kind.OPPORTUNITY: (DEALS,),
	Kind.COMMUNICATION: (CALLS, NOTES),
	Kind.EVENT: (EVENTS,),
	Kind.TODO: (TASKS,),
	Kind.DRAFT_ORDER: (QUOTES,),
	Kind.SALES_ORDER: (SALES_ORDERS,),
	Kind.SALES_INVOICE: (INVOICES,),
	Kind.PURCHASE_ORDER: (PURCHASE_ORDERS,),
}
BILLED = (SALES_ORDERS, INVOICES)
KINDS: tuple[Kind, ...] = (Kind.EMPLOYEE, Kind.CUSTOMER, Kind.OPPORTUNITY_TYPE, *KIND_MODULES)
# Records whose activities name a contact rather than an account carry the contact's account.
ON_CONTACTS = frozenset({"Calls", "Notes", "Events", "Tasks"})
PICKLISTS = frozenset({"picklist", "multiselectpicklist"})
# The modules with amounts, which an organisation without multiple currencies keeps in its own.
PRICED = frozenset({"Deals", "Quotes", "Sales_Orders", "Invoices", "Purchase_Orders"})


@dataclass(frozen=True)
class Field:
	label: str
	type: str
	options: dict[str, str]

	def shown(self, value: JsonValue) -> str | None:
		"""A value as text: a picklist's as its display value (several joined with "; "), a lookup as its
		name, a checkbox as Yes or No, a number exactly as sent."""
		if value is None or value in ("", []):
			return None
		if isinstance(value, bool):
			return "Yes" if value else "No"
		if isinstance(value, int | float):
			return decimal_text(decimal_of(value))
		if isinstance(value, list):
			return "; ".join(
				self.options.get(str(item), str(item)) for item in value if isinstance(item, str)
			)
		if isinstance(value, dict):
			name = value.get("name")
			return name if isinstance(name, str) else json.dumps(value, sort_keys=True)
		return self.options.get(value, value)


@dataclass(frozen=True)
class Fields:
	"""What a module's fields metadata says the importer reads: its standard fields the organisation has, its
	custom fields, and every picklist's display values."""

	standard: list[str]
	custom: dict[str, Field]
	picklists: dict[str, dict[str, str]]

	def names(self) -> list[str]:
		return ["id", *self.standard, *self.custom]


class ZohoCrmReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		self._config = config
		self._client = client
		self._modules: set[str] | None = None
		self._fields: dict[str, Fields] = {}
		self._accounts: dict[str, str | None] = {}
		self._currency: str | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in KIND_MODULES

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		match kind:
			case Kind.EMPLOYEE:
				return self._users(cursor)
			case Kind.CUSTOMER:
				return chained(1 + len(BILLED), cursor, self._customers)
			case Kind.OPPORTUNITY_TYPE:
				pipelines = self._fields_of(DEALS).picklists.get("Pipeline", {})
				nodes = [
					self._identified("Pipeline", {"id": value, "label": label})
					for value, label in pipelines.items()
				]
				return Page(nodes, False, None)
		modules = KIND_MODULES.get(kind)
		if modules is None:
			raise SourceError(f"Zoho CRM has no {kind.value}.")
		return chained(len(modules), cursor, lambda feed, at: self._listed(modules[feed], at, since))

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		module = next((m for m in KIND_MODULES.get(kind, ()) if m.type == identity.type), None)
		if module is None or not identity.id.isdigit():
			raise SourceError(f"Zoho CRM can't read a {identity.type} as a {kind.value} by itself.")
		return self._by_id(module, identity.id)

	# -- modules --------------------------------------------------------------------------------------------

	def _available(self, module: Module) -> bool:
		"""Whether the organisation has the module (an edition without inventory has no quotes)."""
		if self._modules is None:
			listed = self._get("/settings/modules", {})
			self._modules = {
				entry.string("api_name")
				for entry in listed.children("modules")
				if entry.optional_flag("api_supported")
			}
		return module.name in self._modules

	def _listed(self, module: Module, cursor: str | None, since: datetime | None) -> Page:
		if not self._available(module):
			return Page([], False, None)
		fields = self._fields_of(module).names()
		query: dict[str, QueryValue] = {"fields": ",".join(fields[:FIELDS_PER_REQUEST]), "per_page": PER_PAGE}
		if cursor and cursor.startswith(CONTINUATION):
			query["page_token"] = cursor.removeprefix(CONTINUATION)
		else:
			query["page"] = int(cursor or 1)
		headers = {"If-Modified-Since": since.astimezone(UTC).strftime(MODIFIED_SINCE)} if since else {}
		result = self._client.get(f"{API_PATH}/{module.name}", query, headers)
		listed = result.node if result is not None else Node({})
		records = self._merged(module, listed.children("data"), fields)
		nodes = [self._complete(module, record) for record in records]
		info = listed.child("info")
		if info is None or info.optional_flag("more_records") is not True:
			return Page(nodes, False, None)
		token = info.optional_string("next_page_token")
		following = f"{CONTINUATION}{token}" if token else str(int(cursor or 1) + 1)
		return Page(nodes, True, following)

	def _merged(self, module: Module, records: list[Node], fields: list[str]) -> list[Node]:
		"""A page's records with every field: beyond the first 50, the rest read by the page's ids."""
		merged = {record.string("id"): record.raw() for record in records}
		if not merged:
			return []
		for start in range(FIELDS_PER_REQUEST, len(fields), FIELDS_PER_REQUEST - 1):
			chunk = ["id", *fields[start : start + FIELDS_PER_REQUEST - 1]]
			query: dict[str, QueryValue] = {"ids": ",".join(merged), "fields": ",".join(chunk)}
			for record in self._get(f"/{module.name}", query).children("data"):
				merged.setdefault(record.string("id"), {}).update(record.raw())
		return [Node(record) for record in merged.values()]

	def _by_id(self, module: Module, record_id: str) -> Node | None:
		fields = self._fields_of(module).names()
		query: dict[str, QueryValue] = {"ids": record_id, "fields": ",".join(fields[:FIELDS_PER_REQUEST])}
		result = self._client.get(f"{API_PATH}/{module.name}", query)
		records = self._merged(module, result.node.children("data"), fields) if result else []
		return self._complete(module, records[0]) if records else None

	def _complete(self, module: Module, record: Node) -> Node:
		"""A record identified, with its custom fields and picklists by label; an activity on a contact with
		the contact's account; an account with its contacts."""
		raw = record.raw()
		if module.name in ON_CONTACTS:
			contact = lookup_id(record, "Who_Id") or self._parent(record, CONTACTS)
			if contact:
				raw["contact_account"] = self._account_of(contact)
		if module is ACCOUNTS:
			raw["contacts"] = list[JsonValue]([c.raw() for c in self._related(record.string("id"), CONTACTS)])
		if module.name in PRICED and not record.optional_string("Currency"):
			raw["org_currency"] = self._org_currency()
		return self._identified(module.type, raw, self._fields_of(module))

	def _org_currency(self) -> str:
		"""The currency of an organisation without multiple currencies, whose records name none."""
		if self._currency is None:
			organisations = self._get("/org", {}).children("org")
			if not organisations:
				raise SourceError("Zoho CRM answered no organisation.")
			self._currency = organisations[0].string("iso_code")
		return self._currency

	@staticmethod
	def _parent(record: Node, module: str) -> str | None:
		"""A note's parent, when it is a record of ``module``."""
		parent = record.child("Parent_Id")
		of = parent.child("module") if parent else None
		return (
			parent.optional_string("id")
			if parent and of and of.optional_string("api_name") == module
			else None
		)

	def _account_of(self, contact: str) -> str | None:
		if contact not in self._accounts:
			found = self._by_id(CONTACT, contact)
			self._accounts[contact] = lookup_id(found, "Account_Name") if found else None
		return self._accounts[contact]

	def _related(self, account: str, related: str) -> Iterator[Node]:
		"""An account's contacts (its related list), page by page."""
		page = 1
		fields = ",".join(["id", *self._fields_of(CONTACT).standard])
		while True:
			result = self._client.get(
				f"{API_PATH}/Accounts/{account}/{related}",
				{"fields": fields, "per_page": PER_PAGE, "page": page},
			)
			listed = result.node if result is not None else Node({})
			yield from listed.children("data")
			info = listed.child("info")
			if info is None or info.optional_flag("more_records") is not True:
				return
			page += 1

	# -- customers and users --------------------------------------------------------------------------------

	def _customers(self, feed: int, position: str | None) -> Page:
		"""The customer accounts (feed 0), then the accounts sales orders (1) and invoices (2) are for, each
		read whole."""
		if feed == 0:
			page = self._listed(ACCOUNTS, position, None)
			nodes = [node for node in page.nodes if node.optional_string("Account_Type") == CUSTOMER_TYPE]
			return Page(nodes, page.has_next_page, page.end_cursor)
		document = BILLED[feed - 1]
		page = self._listed(document, position, None)
		accounts = dict.fromkeys(a for node in page.nodes if (a := lookup_id(node, "Account_Name")))
		nodes = [node for account in accounts if (node := self._by_id(ACCOUNTS, account)) is not None]
		return Page(nodes, page.has_next_page, page.end_cursor)

	def _users(self, cursor: str | None) -> Page:
		page = int(cursor or 1)
		listed = self._get("/users", {"type": "AllUsers", "page": page, "per_page": PER_PAGE})
		nodes = [self._identified("User", user.raw()) for user in listed.children("users")]
		info = listed.child("info")
		more = info is not None and info.optional_flag("more_records") is True
		return Page(nodes, more, str(page + 1) if more else None)

	# -- fields ---------------------------------------------------------------------------------------------

	def _fields_of(self, module: Module) -> Fields:
		if module.name not in self._fields:
			described = self._get("/settings/fields", {"module": module.name}).children("fields")
			by_name = {field.string("api_name"): field for field in described}
			custom = {
				name: Field(field.string("field_label"), field.string("data_type"), picklist(field))
				for name, field in by_name.items()
				if field.optional_flag("custom_field") is True
			}
			picklists = {
				name: picklist(field)
				for name, field in by_name.items()
				if field.optional_string("data_type") in PICKLISTS
			}
			self._fields[module.name] = Fields(
				[name for name in module.fields if name in by_name and name != "id"], custom, picklists
			)
		return self._fields[module.name]

	# -- requests -------------------------------------------------------------------------------------------

	def _get(self, path: str, query: dict[str, QueryValue]) -> Node:
		result = self._client.get(API_PATH + path, query)
		if result is None:
			raise SourceError(f"Zoho CRM has no {path}.")
		return result.node

	def _identified(self, type_: str, record: dict[str, JsonValue], fields: Fields | None = None) -> Node:
		source_id = record.get("id")
		if not isinstance(source_id, str):
			raise SourceError(f"A Zoho CRM {type_} has no id.")
		extra: dict[str, JsonValue] = {}
		if fields is not None:
			custom: dict[str, JsonValue] = {}
			for name, field in fields.custom.items():
				shown = field.shown(record.get(name))
				if shown is not None:
					custom[field.label] = shown
			labels: dict[str, JsonValue] = {}
			for name, options in fields.picklists.items():
				value = record.get(name)
				if isinstance(value, str) and value in options:
					labels[name] = options[value]
			extra = {"custom": custom, "labels": labels}
		gid = self._config.identity(type_, source_id)
		return Node({**record, **extra, "id": gid, "source_id": source_id}, gid)


def picklist(field: Node) -> dict[str, str]:
	"""A picklist's display values by their actual values."""
	return {
		choice.string("actual_value"): choice.string("display_value")
		for choice in field.children("pick_list_values")
	}


def lookup_id(record: Node, field: str) -> str | None:
	"""The id of the record a lookup field names (``{"name", "id"}``)."""
	value = record.child(field) if isinstance(record.get(field), dict) else None
	return value.optional_string("id") if value else None
