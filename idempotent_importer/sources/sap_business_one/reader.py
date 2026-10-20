# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads a company from SAP Business One's Service Layer (OData v3, ``/b1s/v1``): each kind's entity sets,
in key order, page by page through ``odata.nextLink`` at up to PAGE_SIZE entities a page (asked for with
``Prefer: odata.maxpagesize``), every entity whole with its collections. A kind read from several entity
sets (invoices and credit notes; incoming and outgoing payments) reads them one after another; the cursor
names the set and its next link.

Incremental runs ask the entity sets that keep ``UpdateDate`` for those updated on or after the day before
the last clean run (``UpdateDate`` is the server's local date, so a day's overlap covers any offset; an
unchanged entity is skipped by its hash); the others are read whole every run. Every node carries its
identity as ``id`` (its key as ``source_key``), and the reader adds what an entity only refers to: an
account's top-level drawer (``drawer``, its root account), and the type (customer, supplier or lead) of
the business partner of an opportunity or a journal line (``card_type``).

Journal entries: the Service Layer lists every posting, each document's included; only manual ones
(``OriginalJournal`` ``ttJournalEntry``) are journal entries of their own, the rest are their documents'
postings, mirrored as those documents (so nothing is booked twice).

The Service Layer documents no request limit (it is the company's own server); the reader sends one
request at a time, at most REQUESTS_PER_SECOND, and backs off on 429 and 5xx."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import quote, unquote, urlsplit

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import SYNC_ORDER, Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import RestClient, query_string
from idempotent_importer.core.source import ConnectionConfig, Page

SOURCE_ID = "sap_business_one"
SERVICE_PATH = "/b1s/v1"
PAGE_SIZE = 100
PREFER = {"Prefer": f"odata.maxpagesize={PAGE_SIZE}"}
REQUESTS_PER_SECOND = 5.0
RATE_LIMIT = RateLimit(requests_per_second=REQUESTS_PER_SECOND)
NEXT_LINK = "odata.nextLink"
VALUE = "value"
SINCE_OVERLAP = timedelta(days=1)
CURSOR_SEPARATOR = "|"
MANUAL_JOURNAL = "ttJournalEntry"


@dataclass(frozen=True)
class EntitySet:
	"""One entity set a kind reads: its name, the identity type its entities get, its key property (a
	number, or text), a filter that picks the kind's entities, and whether it keeps ``UpdateDate``."""

	name: str
	identity: str
	key: str
	condition: str = ""
	updated: bool = True
	numeric_key: bool = True


def documents(*names: tuple[str, str]) -> tuple[EntitySet, ...]:
	return tuple(EntitySet(name, identity, "DocEntry") for name, identity in names)


def partners(card_type: str) -> tuple[EntitySet, ...]:
	return (
		EntitySet(
			"BusinessPartners", "BusinessPartner", "CardCode", f"CardType eq '{card_type}'", numeric_key=False
		),
	)


SETS: dict[Kind, tuple[EntitySet, ...]] = {
	Kind.ACCOUNT: (EntitySet("ChartOfAccounts", "Account", "Code", updated=False, numeric_key=False),),
	Kind.COST_CENTER: (
		EntitySet("ProfitCenters", "ProfitCenter", "CenterCode", updated=False, numeric_key=False),
	),
	Kind.WAREHOUSE: (
		EntitySet("Warehouses", "Warehouse", "WarehouseCode", updated=False, numeric_key=False),
	),
	Kind.COLLECTION: (EntitySet("ItemGroups", "ItemGroup", "Number", updated=False),),
	Kind.PRODUCT: (EntitySet("Items", "Item", "ItemCode", numeric_key=False),),
	Kind.CUSTOMER: partners("cCustomer"),
	Kind.SUPPLIER: partners("cSupplier"),
	Kind.DEPARTMENT: (EntitySet("Departments", "Department", "Code", updated=False),),
	Kind.DESIGNATION: (EntitySet("EmployeePosition", "Position", "PositionID", updated=False),),
	Kind.EMPLOYEE: (EntitySet("EmployeesInfo", "Employee", "EmployeeID"),),
	Kind.PROJECT: (EntitySet("Projects", "Project", "Code", updated=False, numeric_key=False),),
	Kind.LEAD: partners("cLid"),
	Kind.OPPORTUNITY: (EntitySet("SalesOpportunities", "Opportunity", "SequentialNo"),),
	Kind.DRAFT_ORDER: documents(("Quotations", "Quotation")),
	Kind.SALES_ORDER: documents(("Orders", "Order")),
	Kind.PURCHASE_ORDER: documents(("PurchaseOrders", "PurchaseOrder")),
	Kind.SALES_INVOICE: documents(("Invoices", "Invoice"), ("CreditNotes", "CreditNote")),
	Kind.PURCHASE_INVOICE: documents(
		("PurchaseInvoices", "PurchaseInvoice"), ("PurchaseCreditNotes", "PurchaseCreditNote")
	),
	Kind.PAYMENT_ENTRY: (
		EntitySet("IncomingPayments", "IncomingPayment", "DocEntry", updated=False),
		EntitySet("VendorPayments", "VendorPayment", "DocEntry", updated=False),
	),
	Kind.JOURNAL_ENTRY: (EntitySet("JournalEntries", "JournalEntry", "JdtNum", updated=False),),
}
KINDS: tuple[Kind, ...] = tuple(kind for kind in SYNC_ORDER if kind in SETS)


def key_text(value: JsonValue) -> str:
	"""An entity's key as an identity's id: codes may hold any character, so a ``/`` is escaped."""
	if isinstance(value, bool) or not isinstance(value, str | int):
		raise SourceError(f"Not a Service Layer key: {value!r}")
	return quote(str(value), safe=" ")


def key_literal(entity_set: EntitySet, id_: str) -> str:
	"""An identity's id as the OData literal its key is filtered by."""
	text = unquote(id_)
	if not entity_set.numeric_key:
		return "'" + text.replace("'", "''") + "'"
	if not text.isdigit():
		raise SourceError(f"Not a {entity_set.name} key: {text}")
	return text


def service_url(config: ConnectionConfig) -> str:
	"""The Service Layer's ``/b1s/v1`` root, from the Store URL (``https://<server>:50000``, with or
	without the root)."""
	base = (config.endpoint_override or config.store_url).strip().rstrip("/")
	parts = urlsplit(base)
	if parts.scheme not in ("http", "https") or not parts.netloc:
		raise SourceError(
			"SAP Business One is not configured: enter the Service Layer's address "
			"(https://<server>:50000) as the Store URL."
		)
	return base if base.endswith(SERVICE_PATH) else base + SERVICE_PATH


class ServiceLayerReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		self._config = config
		self._client = client
		self._drawers: dict[str, str] | None = None
		self._card_types: dict[str, str | None] = {}

	def is_incremental(self, kind: Kind) -> bool:
		return any(entity_set.updated for entity_set in SETS.get(kind, ()))

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		sets = SETS.get(kind)
		if sets is None:
			raise SourceError(f"SAP Business One has no {kind.value}.")
		index, link = self._position(cursor, len(sets))
		entity_set = sets[index]
		path = link or self._first(entity_set, since)
		result = self._client.get(path, None, PREFER)
		if result is None:
			raise SourceError(f"SAP Business One has no {entity_set.name}.")
		nodes = [
			self._identified(kind, entity_set, entity)
			for entity in result.node.children(VALUE)
			if self._wanted(kind, entity)
		]
		following = result.node.optional_string(NEXT_LINK)
		if following:
			return Page(nodes, True, f"{index}{CURSOR_SEPARATOR}{self._relative(following)}")
		if index + 1 < len(sets):
			return Page(nodes, True, f"{index + 1}{CURSOR_SEPARATOR}")
		return Page(nodes, False, None)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		entity_set = next((s for s in SETS.get(kind, ()) if s.identity == identity.type), None)
		if entity_set is None:
			raise SourceError(f"Not a SAP Business One {kind.value} identity: {gid}")
		condition = f"{entity_set.key} eq {key_literal(entity_set, identity.id)}"
		result = self._client.get(f"/{entity_set.name}", {"$filter": condition}, PREFER)
		found = result.node.children(VALUE) if result is not None else []
		wanted = [entity for entity in found if self._wanted(kind, entity)]
		return self._identified(kind, entity_set, wanted[0]) if wanted else None

	# -- listing -------------------------------------------------------------------------------------

	@staticmethod
	def _position(cursor: str | None, sets: int) -> tuple[int, str]:
		if not cursor:
			return 0, ""
		index, _, link = cursor.partition(CURSOR_SEPARATOR)
		if not index.isdigit() or int(index) >= sets:
			raise SourceError(f"Not a SAP Business One cursor: {cursor}")
		return int(index), link

	@staticmethod
	def _first(entity_set: EntitySet, since: datetime | None) -> str:
		conditions = [entity_set.condition] if entity_set.condition else []
		if since is not None and entity_set.updated:
			conditions.append(f"UpdateDate ge '{(since - SINCE_OVERLAP).strftime('%Y-%m-%d')}'")
		query: dict[str, str] = {"$orderby": entity_set.key}
		if conditions:
			query["$filter"] = " and ".join(conditions)
		return f"/{entity_set.name}" + query_string(query)

	def _relative(self, link: str) -> str:
		"""A next link (relative to the service root, or absolute) as a path under the root."""
		parts = urlsplit(link)
		path = (
			parts.path.split(SERVICE_PATH, 1)[1]
			if SERVICE_PATH in parts.path
			else "/" + parts.path.lstrip("/")
		)
		return path + (f"?{parts.query}" if parts.query else "")

	@staticmethod
	def _wanted(kind: Kind, entity: Node) -> bool:
		return kind != Kind.JOURNAL_ENTRY or entity.optional_string("OriginalJournal") == MANUAL_JOURNAL

	# -- annotations ---------------------------------------------------------------------------------

	def _identified(self, kind: Kind, entity_set: EntitySet, entity: Node) -> Node:
		key = key_text(entity.get(entity_set.key))
		extra: dict[str, JsonValue] = {}
		if kind == Kind.ACCOUNT:
			extra["drawer"] = self._drawer(entity.string("Code"))
		if kind == Kind.OPPORTUNITY:
			card = entity.optional_string("CardCode")
			extra["card_type"] = self._card_type(card) if card else None
		if kind == Kind.JOURNAL_ENTRY:
			extra["JournalEntryLines"] = [
				{**line.raw(), "card_type": self._card_type(line.string("ShortName"))}
				if line.optional_string("ShortName") not in (None, line.optional_string("AccountCode"))
				else line.raw()
				for line in entity.children("JournalEntryLines")
			]
		gid = self._config.identity(entity_set.identity, key)
		return Node({**entity.raw(), **extra, "source_key": key, "id": gid}, gid)

	def _drawer(self, code: str) -> str:
		"""The account's top-level ancestor (its drawer), from the chart read once per run."""
		if self._drawers is None:
			fathers: dict[str, str | None] = {}
			path: str | None = "/ChartOfAccounts" + query_string(
				{"$select": "Code,FatherAccountKey", "$orderby": "Code"}
			)
			while path is not None:
				result = self._client.get(path, None, PREFER)
				if result is None:
					raise SourceError("SAP Business One has no chart of accounts.")
				for account in result.node.children(VALUE):
					fathers[account.string("Code")] = account.optional_string("FatherAccountKey")
				following = result.node.optional_string(NEXT_LINK)
				path = self._relative(following) if following else None
			self._drawers = {account: top_of(account, fathers) for account in fathers}
		return self._drawers.get(code, code)

	def _card_type(self, card: str) -> str | None:
		"""A business partner's type (customer, supplier or lead), read once per run."""
		if card not in self._card_types:
			literal = "'" + card.replace("'", "''") + "'"
			result = self._client.get(
				"/BusinessPartners", {"$filter": f"CardCode eq {literal}", "$select": "CardCode,CardType"}
			)
			found = result.node.children(VALUE) if result is not None else []
			self._card_types[card] = found[0].optional_string("CardType") if found else None
		return self._card_types[card]


def top_of(code: str, fathers: Mapping[str, str | None]) -> str:
	"""The top of an account's chain of fathers (a cycle or a missing father ends it)."""
	seen = {code}
	while (father := fathers.get(code)) and father not in seen:
		seen.add(father)
		code = father
	return code
