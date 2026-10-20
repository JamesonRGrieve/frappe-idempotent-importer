# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads a tenant through Acumatica's contract-based REST API, the Default endpoint 24.200.001
(``/entity/Default/24.200.001/<Entity>``): each kind's entities, with the linked and detail entities the
mappers read (``$expand``), page by page (``$top``/``$skip``, in the entity's key order, as the API lists
them). A kind read from several entities (stock and non-stock items; invoices and cash sales; customer
payments, checks and cash sales) reads them one after another; the cursor names the entity and the offset.

Incremental runs ask the entities that keep ``LastModifiedDateTime`` for those modified since the day
before the last clean run (a day's overlap covers the server's time zone; an unchanged entity is skipped by
its hash); the others (subaccounts) are read whole. Only general ledger batches (module GL) are journal
entries: every other module's batches are their documents' postings, mirrored as those documents (so
nothing is booked twice).

Every node carries its identity as ``id`` (Acumatica's own ``id`` is kept as ``entity_id``), and the reader
adds what an entity only refers to: an invoice's or cash sale's customer's currency (``customer_currency``,
and ``currency_override`` when the customer may bill in others: the Default endpoint's invoice names no
currency), and a batch's ledger's currency (``ledger_currency``). Departments and job titles have no
entity of their own: the employees' distinct ones are read as departments and designations.

Acumatica limits web service requests by license (requests per minute and concurrent requests, on the
License Monitoring Console, SM604000), answering 429 past them; the reader sends one request at a time, at
most one a second, and a 429 pauses the run."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import SYNC_ORDER, Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import LIST_KEY, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page

SOURCE_ID = "acumatica"
ENDPOINT_PATH = "/entity/Default/24.200.001"
PAGE_SIZE = 100
REQUESTS_PER_SECOND = 1.0
COOLDOWN_SECONDS = 60.0
RATE_LIMIT = RateLimit(requests_per_second=REQUESTS_PER_SECOND, cooldown_seconds=COOLDOWN_SECONDS)
SINCE_OVERLAP = timedelta(days=1)
CURSOR_SEPARATOR = "|"
KEY_SEPARATOR = ":"
KEY_LENGTH = 32
GL = "GL"
QUOTE_TYPE = "QT"
CONTACT = "ContactInfo"


@dataclass(frozen=True)
class Entity:
	"""One entity a kind reads: its name, the identity type its records get, the fields its key is made
	of, what to expand, the filter that picks the kind's records, and whether it keeps
	``LastModifiedDateTime``."""

	name: str
	identity: str
	keys: tuple[str, ...]
	expand: str = ""
	condition: str = ""
	modified: bool = True


DOCUMENT_KEY = ("Type", "ReferenceNbr")
ORDER_KEY = ("OrderType", "OrderNbr")
PARTY_EXPAND = (
	"MainContact,MainContact/Address,BillingContact,BillingContact/Address,ShippingContact,"
	"ShippingContact/Address,Contacts,Contacts/Contact"
)
VENDOR_EXPAND = "MainContact,MainContact/Address,Contacts,Contacts/Contact"
EMPLOYEES = Entity("Employee", "Employee", ("EmployeeID",), f"{CONTACT},EmployeeSettings")
CASH_SALES = Entity("CashSale", "CashSale", DOCUMENT_KEY, "Details")
ENTITIES: dict[Kind, tuple[Entity, ...]] = {
	Kind.ACCOUNT: (Entity("Account", "Account", ("AccountCD",)),),
	Kind.COST_CENTER: (Entity("Subaccount", "Subaccount", ("SubaccountCD",), modified=False),),
	Kind.WAREHOUSE: (Entity("Warehouse", "Warehouse", ("WarehouseID",)),),
	Kind.COLLECTION: (Entity("ItemClass", "ItemClass", ("ClassID",)),),
	Kind.PRODUCT: (
		Entity("StockItem", "Item", ("InventoryID",), "WarehouseDetails"),
		Entity("NonStockItem", "Item", ("InventoryID",)),
	),
	Kind.CUSTOMER: (Entity("Customer", "Customer", ("CustomerID",), PARTY_EXPAND),),
	Kind.SUPPLIER: (Entity("Vendor", "Vendor", ("VendorID",), VENDOR_EXPAND),),
	Kind.EMPLOYEE: (EMPLOYEES,),
	Kind.PROJECT: (Entity("Project", "Project", ("ProjectID",)),),
	Kind.LEAD: (Entity("Lead", "Lead", ("LeadID",), "Address"),),
	Kind.OPPORTUNITY: (Entity("Opportunity", "Opportunity", ("OpportunityID",)),),
	Kind.DRAFT_ORDER: (
		Entity(
			"SalesOrder", "SalesOrder", ORDER_KEY, "Details,TaxDetails,Totals", f"OrderType eq '{QUOTE_TYPE}'"
		),
	),
	Kind.SALES_ORDER: (
		Entity(
			"SalesOrder", "SalesOrder", ORDER_KEY, "Details,TaxDetails,Totals", f"OrderType ne '{QUOTE_TYPE}'"
		),
	),
	Kind.PURCHASE_ORDER: (
		Entity("PurchaseOrder", "PurchaseOrder", ("Type", "OrderNbr"), "Details,TaxDetails"),
	),
	Kind.SALES_INVOICE: (
		Entity(
			"Invoice", "Invoice", DOCUMENT_KEY, "Details,TaxDetails,DiscountDetails,ApplicationsCreditMemo"
		),
		CASH_SALES,
	),
	Kind.PURCHASE_INVOICE: (Entity("Bill", "Bill", DOCUMENT_KEY, "Details,TaxDetails,Applications"),),
	Kind.PAYMENT_ENTRY: (
		Entity("Payment", "Payment", DOCUMENT_KEY, "ApplicationHistory"),
		Entity("Check", "Check", DOCUMENT_KEY, "Details"),
		CASH_SALES,
	),
	Kind.JOURNAL_ENTRY: (
		Entity(
			"JournalTransaction", "JournalTransaction", ("Module", "BatchNbr"), "Details", f"Module eq '{GL}'"
		),
	),
}
# Departments and job titles: the employees' distinct ones.
DERIVED = (Kind.DEPARTMENT, Kind.DESIGNATION)
KINDS: tuple[Kind, ...] = tuple(kind for kind in SYNC_ORDER if kind in ENTITIES or kind in DERIVED)
CUSTOMER_DOCUMENTS = frozenset({"Invoice", "CashSale"})


def value(node: Node | None, field: str) -> JsonValue:
	"""A field's value (``{"value": …}``), or None."""
	holder = node.child(field) if node is not None else None
	return holder.get("value") if holder is not None and holder.has("value") else None


def text(node: Node | None, field: str) -> str | None:
	found = value(node, field)
	if found is None or isinstance(found, bool):
		return None
	return str(found).strip() or None


def key_of(entity: Entity, node: Node) -> str:
	"""A record's identity id from its key fields (each escaped, joined by ``:``)."""
	parts = [text(node, field) for field in entity.keys]
	if any(part is None for part in parts):
		raise SourceError(f"An Acumatica {entity.name} has no {', '.join(entity.keys)}.")
	return KEY_SEPARATOR.join(quote(str(part), safe=" -_.") for part in parts)


def derived_key(name: str) -> str:
	"""A department's or job title's identity (neither has a record)."""
	return hashlib.sha256(name.encode()).hexdigest()[:KEY_LENGTH]


def instance_url(config: ConnectionConfig) -> str:
	base = (config.endpoint_override or config.store_url).strip().rstrip("/")
	parts = urlsplit(base)
	if parts.scheme not in ("http", "https") or not parts.netloc:
		raise SourceError(
			"Acumatica is not configured: enter the instance's address (https://<company>.acumatica.com) as "
			"the Store URL."
		)
	return base


class AcumaticaReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		self._config = config
		self._client = client
		self._customers: dict[str, tuple[str | None, bool]] = {}
		self._ledgers: dict[str, str | None] = {}
		self._employees: list[Node] | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return any(entity.modified for entity in ENTITIES.get(kind, ()))

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		if kind in DERIVED:
			return Page(self._derived(kind), False, None)
		entities = ENTITIES.get(kind)
		if entities is None:
			raise SourceError(f"Acumatica has no {kind.value}.")
		index, skip = self._position(cursor, len(entities))
		entity = entities[index]
		records = self._list(entity, since, skip)
		nodes = [self._identified(entity, record) for record in records]
		if len(records) == PAGE_SIZE:
			return Page(nodes, True, f"{index}{CURSOR_SEPARATOR}{skip + PAGE_SIZE}")
		if index + 1 < len(entities):
			return Page(nodes, True, f"{index + 1}{CURSOR_SEPARATOR}0")
		return Page(nodes, False, None)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		if kind in DERIVED:
			return next((node for node in self._derived(kind) if node.id() == gid), None)
		entity = next((e for e in ENTITIES.get(kind, ()) if e.identity == identity.type), None)
		if entity is None:
			raise SourceError(f"Not an Acumatica {kind.value} identity: {gid}")
		parts = identity.id.split(KEY_SEPARATOR)
		if len(parts) != len(entity.keys):
			raise SourceError(f"Not an Acumatica {entity.name} key: {identity.id}")
		condition = " and ".join(
			f"{field} eq '{literal(part)}'" for field, part in zip(entity.keys, parts, strict=True)
		)
		query = {"$filter": condition}
		if entity.expand:
			query["$expand"] = entity.expand
		result = self._client.get(f"{ENDPOINT_PATH}/{entity.name}", query)
		found = result.node.children(LIST_KEY) if result is not None else []
		return self._identified(entity, found[0]) if found else None

	# -- listing -------------------------------------------------------------------------------------

	@staticmethod
	def _position(cursor: str | None, entities: int) -> tuple[int, int]:
		if not cursor:
			return 0, 0
		index, _, skip = cursor.partition(CURSOR_SEPARATOR)
		if not index.isdigit() or not skip.isdigit() or int(index) >= entities:
			raise SourceError(f"Not an Acumatica cursor: {cursor}")
		return int(index), int(skip)

	def _list(self, entity: Entity, since: datetime | None, skip: int) -> list[Node]:
		conditions = [entity.condition] if entity.condition else []
		if since is not None and entity.modified:
			moment = (since - SINCE_OVERLAP).strftime("%Y-%m-%dT%H:%M:%S")
			conditions.append(f"LastModifiedDateTime gt datetimeoffset'{moment}'")
		query: dict[str, str | int] = {"$top": PAGE_SIZE, "$skip": skip}
		if entity.expand:
			query["$expand"] = entity.expand
		if conditions:
			query["$filter"] = " and ".join(conditions)
		result = self._client.get(f"{ENDPOINT_PATH}/{entity.name}", query)
		if result is None:
			raise SourceError(f"Acumatica has no {entity.name} entity on the Default 24.200.001 endpoint.")
		return result.node.children(LIST_KEY)

	def _all(self, entity: Entity) -> list[Node]:
		records: list[Node] = []
		while True:
			page = self._list(entity, None, len(records))
			records += page
			if len(page) < PAGE_SIZE:
				return records

	# -- annotations ---------------------------------------------------------------------------------

	def _identified(self, entity: Entity, record: Node) -> Node:
		key = key_of(entity, record)
		extra: dict[str, JsonValue] = {"entity": entity.name, "entity_id": record.get("id")}
		if entity.name in CUSTOMER_DOCUMENTS:
			customer = text(record, "Customer") or text(record, "CustomerID")
			currency, override = self._customer(customer) if customer else (None, False)
			extra |= {"customer_currency": currency, "currency_override": override}
		if entity.name == "JournalTransaction":
			ledger = text(record, "LedgerID")
			extra["ledger_currency"] = self._ledger(ledger) if ledger else None
		gid = self._config.identity(entity.identity, key)
		return Node({**record.raw(), **extra, "source_key": key, "id": gid}, gid)

	def _customer(self, customer: str) -> tuple[str | None, bool]:
		"""A customer's currency and whether it may bill in others, read once per run."""
		if customer not in self._customers:
			result = self._client.get(
				f"{ENDPOINT_PATH}/Customer",
				{
					"$filter": f"CustomerID eq '{literal(customer)}'",
					"$select": "CustomerID,CurrencyID,EnableCurrencyOverride",
				},
			)
			found = result.node.children(LIST_KEY) if result is not None else []
			record = found[0] if found else None
			self._customers[customer] = (
				text(record, "CurrencyID"),
				value(record, "EnableCurrencyOverride") is True,
			)
		return self._customers[customer]

	def _ledger(self, ledger: str) -> str | None:
		if ledger not in self._ledgers:
			result = self._client.get(
				f"{ENDPOINT_PATH}/Ledger",
				{"$filter": f"LedgerID eq '{literal(ledger)}'", "$select": "LedgerID,Currency"},
			)
			found = result.node.children(LIST_KEY) if result is not None else []
			self._ledgers[ledger] = text(found[0], "Currency") if found else None
		return self._ledgers[ledger]

	def _derived(self, kind: Kind) -> list[Node]:
		"""The employees' distinct departments (as departments) or job titles (as designations)."""
		if self._employees is None:
			self._employees = self._all(EMPLOYEES)
		names: set[str] = set()
		for employee in self._employees:
			found = (
				text(employee.child("EmployeeSettings"), "DepartmentID")
				if kind == Kind.DEPARTMENT
				else text(employee.child(CONTACT), "JobTitle")
			)
			if found:
				names.add(found)
		type_ = "Department" if kind == Kind.DEPARTMENT else "Designation"
		nodes: list[Node] = []
		for name in sorted(names):
			gid = self._config.identity(type_, derived_key(name))
			nodes.append(Node({"name": name, "id": gid, "source_key": derived_key(name)}, gid))
		return nodes


def literal(text_: str) -> str:
	"""A string inside an OData filter's quotes (single quotes doubled)."""
	return text_.replace("'", "''")
