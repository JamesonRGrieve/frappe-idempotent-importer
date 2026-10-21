# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads a Sage Intacct company through the XML gateway's ``get_list`` (schema 2.1): each kind's object in
key order, ``maxitems`` at a time from ``start``, until the list's ``total`` (from ``listtype``'s
``start``/``end``/``total``). Incremental runs ask for ``whenmodified`` on or after the day before the last
clean run (the company's time zone: a day's overlap; an unchanged record is skipped by its hash); objects
without ``whenmodified`` (warehouses, GL transactions) are read whole.

Order entry and purchasing transactions are company-defined (each transaction definition is the company's
own); the quotes, orders and purchase orders read are those of the definitions the connection's Field Map
names (Intacct's standard ones by default), and the GL transactions those of the journals it names (the
general journal, GJ, by default): the subledgers' journals and the order entry invoices are the
postings of the AR and AP documents mirrored on their own, so nothing is booked twice."""

from __future__ import annotations

import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import SYNC_ORDER, Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.sage_intacct.gateway import Gateway

SOURCE_ID = "sage_intacct"
PAGE_SIZE = 100
SINCE_OVERLAP = timedelta(days=1)
TIMESTAMP = "%m/%d/%Y %H:%M:%S"
KEY_LENGTH = 32
KEY_SEPARATOR = ":"
QUOTES, ORDERS, PURCHASE_ORDERS, JOURNALS = "quotes", "sales_orders", "purchase_orders", "journals"
DEFAULTS: dict[str, tuple[str, ...]] = {
	QUOTES: ("Sales Quote",),
	ORDERS: ("Sales Order",),
	PURCHASE_ORDERS: ("Purchase Order",),
	JOURNALS: ("GJ",),
}


@dataclass(frozen=True)
class Listing:
	"""One object a kind reads: its name, the identity type its records get, its key field, whether it
	keeps ``whenmodified``, and the Field Map list that picks its records (by ``field``)."""

	object: str
	identity: str
	key: str
	modified: bool = True
	choice: str = ""
	field: str = ""


LISTINGS: dict[Kind, Listing] = {
	Kind.ACCOUNT: Listing("glaccount", "GLAccount", "glaccountno"),
	Kind.COST_CENTER: Listing("department", "Department", "departmentid"),
	Kind.DEPARTMENT: Listing("department", "Department", "departmentid"),
	Kind.WAREHOUSE: Listing("warehouse", "Warehouse", "warehouseid", modified=False),
	Kind.PRODUCT: Listing("icitem", "Item", "itemid"),
	Kind.CUSTOMER: Listing("customer", "Customer", "customerid"),
	Kind.SUPPLIER: Listing("vendor", "Vendor", "vendorid"),
	Kind.EMPLOYEE: Listing("employee", "Employee", "employeeid"),
	Kind.PROJECT: Listing("project", "Project", "key"),
	Kind.DRAFT_ORDER: Listing(
		"sotransaction", "SOTransaction", "key", choice=QUOTES, field="transactiontype"
	),
	Kind.SALES_ORDER: Listing(
		"sotransaction", "SOTransaction", "key", choice=ORDERS, field="transactiontype"
	),
	Kind.PURCHASE_ORDER: Listing(
		"potransaction", "POTransaction", "key", choice=PURCHASE_ORDERS, field="transactiontype"
	),
	Kind.SALES_INVOICE: Listing("invoice", "Invoice", "key"),
	Kind.PURCHASE_INVOICE: Listing("bill", "Bill", "key"),
	Kind.JOURNAL_ENTRY: Listing(
		"gltransaction", "GLTransaction", "batchno", modified=False, choice=JOURNALS, field="journalid"
	),
}
PAYMENTS = (Listing("arpayment", "ARPayment", "key"), Listing("appayment", "APPayment", "key"))
KINDS: tuple[Kind, ...] = tuple(
	kind for kind in SYNC_ORDER if kind in LISTINGS or kind in (Kind.PAYMENT_ENTRY, Kind.DESIGNATION)
)


@dataclass(frozen=True)
class FieldMap:
	"""Which transaction definitions are quotes, sales orders and purchase orders, which journals are read,
	and the root type of any account the gateway's account type and normal balance don't settle."""

	lists: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(DEFAULTS))
	root_types: dict[str, str] = field(default_factory=dict)

	@classmethod
	def of(cls, text: str) -> FieldMap:
		if not text.strip():
			return cls()
		try:
			raw = json.loads(text)
		except ValueError:
			raise SourceError("The Sage Intacct Field Map isn't JSON.") from None
		if not isinstance(raw, dict):
			raise SourceError("The Sage Intacct Field Map must be a JSON object.")
		lists = dict(DEFAULTS)
		for name in DEFAULTS:
			given = raw.get(name)
			if given is None:
				continue
			if not isinstance(given, list) or not all(isinstance(item, str) for item in given):
				raise SourceError(f"The Sage Intacct Field Map's {name} must be a list of names.")
			lists[name] = tuple(given)
		roots = raw.get("root_types", {})
		if not isinstance(roots, dict) or not all(isinstance(v, str) for v in roots.values()):
			raise SourceError(
				'The Sage Intacct Field Map\'s root_types must be {"<account>": "<root type>"}.'
			)
		return cls(lists, {str(k): v for k, v in roots.items()})


def derived_key(name: str) -> str:
	return hashlib.sha256(name.encode()).hexdigest()[:KEY_LENGTH]


def records_of(data: Node | None, object_: str) -> list[Node]:
	if data is None:
		return []
	return (
		data.children(object_)
		if isinstance(data.get(object_), list)
		else [c for c in [data.child(object_)] if c]
	)


class IntacctReader:
	def __init__(self, config: ConnectionConfig, gateway: Gateway) -> None:
		self._config = config
		self._gateway = gateway
		self.field_map = FieldMap.of(config.field_map)
		self._currencies: dict[str, str | None] = {}

	def is_incremental(self, kind: Kind) -> bool:
		if kind == Kind.PAYMENT_ENTRY:
			return True
		listing = LISTINGS.get(kind)
		return listing is not None and listing.modified

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		if kind == Kind.DESIGNATION:
			return Page(self._designations(), False, None)
		listings = PAYMENTS if kind == Kind.PAYMENT_ENTRY else (LISTINGS[kind],) if kind in LISTINGS else ()
		if not listings:
			raise SourceError(f"Sage Intacct has no {kind.value}.")
		index, start = self._position(cursor, len(listings))
		listing = listings[index]
		records, following = self._list(listing, since, start)
		nodes = [self._identified(listing, record) for record in records if self._wanted(listing, record)]
		if following is not None:
			return Page(nodes, True, f"{index}|{following}")
		if index + 1 < len(listings):
			return Page(nodes, True, f"{index + 1}|0")
		return Page(nodes, False, None)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		if kind == Kind.DESIGNATION:
			return next((n for n in self._designations() if n.id() == gid), None)
		listings = PAYMENTS if kind == Kind.PAYMENT_ENTRY else (LISTINGS[kind],) if kind in LISTINGS else ()
		listing = next((item for item in listings if item.identity == identity.type), None)
		if listing is None:
			raise SourceError(f"Not a Sage Intacct {kind.value} identity: {gid}")
		journal, _, key = identity.id.rpartition(KEY_SEPARATOR)
		records, _ = self._list(listing, None, 0, (listing.key, "=", key))
		found = [
			record
			for record in records
			if self._wanted(listing, record) and (not journal or record.text("journalid") == journal)
		]
		return self._identified(listing, found[0]) if found else None

	@staticmethod
	def _position(cursor: str | None, count: int) -> tuple[int, int]:
		if not cursor:
			return 0, 0
		index, _, start = cursor.partition("|")
		if not index.isdigit() or not start.isdigit() or int(index) >= count:
			raise SourceError(f"Not a Sage Intacct cursor: {cursor}")
		return int(index), int(start)

	def _list(
		self,
		listing: Listing,
		since: datetime | None,
		start: int,
		condition: tuple[str, str, str] | None = None,
	) -> tuple[list[Node], int | None]:
		expressions: list[tuple[str, str, str]] = [condition] if condition else []
		if since is not None and listing.modified:
			expressions.append(("whenmodified", ">=", (since - SINCE_OVERLAP).strftime(TIMESTAMP)))

		def get_list(function: ET.Element) -> None:
			holder = ET.SubElement(
				function, "get_list", object=listing.object, start=str(start), maxitems=str(PAGE_SIZE)
			)
			if len(expressions) == 1:
				expression(ET.SubElement(holder, "filter"), expressions[0])
			elif expressions:
				logical = ET.SubElement(ET.SubElement(holder, "filter"), "logical", logical_operator="and")
				for item in expressions:
					expression(logical, item)
			ET.SubElement(ET.SubElement(holder, "sorts"), "sortfield", order="asc").text = listing.key

		result = self._gateway.call(get_list)
		records = records_of(result.child("data"), listing.object)
		listtype = result.child("listtype")
		end = int(listtype.text("@end") or "-1") if listtype is not None else -1
		total = int(listtype.text("@total") or "0") if listtype is not None else 0
		return records, (end + 1 if end + 1 < total and records else None)

	def _wanted(self, listing: Listing, record: Node) -> bool:
		return not listing.choice or record.text(listing.field) in self.field_map.lists[listing.choice]

	def _identified(self, listing: Listing, record: Node) -> Node:
		key = record.text(listing.key)
		if not key or "/" in key or KEY_SEPARATOR in key:
			raise SourceError(f"A Sage Intacct {listing.object} has no usable {listing.key}: {key!r}.")
		if listing.object == "gltransaction":
			# A batch number is unique within its journal.
			key = f"{record.text('journalid')}{KEY_SEPARATOR}{key}"
		gid = self._config.identity(listing.identity, key)
		extra: dict[str, JsonValue] = {"object": listing.object, "source_key": key, "id": gid}
		if listing.object == "arpayment":
			# The 2.1 AR payment names no currency: it is its customer's.
			extra["customer_currency"] = self._customer_currency(record.text("customerid"))
		return Node({**record.raw(), **extra}, gid)

	def _customer_currency(self, customer: str) -> str | None:
		if customer not in self._currencies:
			listing = LISTINGS[Kind.CUSTOMER]
			records, _ = self._list(listing, None, 0, (listing.key, "=", customer))
			self._currencies[customer] = (records[0].text("currency") or None) if records else None
		return self._currencies[customer]

	def _designations(self) -> list[Node]:
		"""The employees' distinct titles (Intacct has no position record)."""
		titles: set[str] = set()
		start: int | None = 0
		while start is not None:
			records, start = self._list(LISTINGS[Kind.EMPLOYEE], None, start)
			titles |= {title for record in records if (title := record.text("title"))}
		nodes: list[Node] = []
		for title in sorted(titles):
			gid = self._config.identity("Designation", derived_key(title))
			nodes.append(Node({"name": title, "id": gid, "source_key": derived_key(title)}, gid))
		return nodes


def expression(parent: ET.Element, item: tuple[str, str, str]) -> None:
	holder = ET.SubElement(parent, "expression")
	for tag, text in zip(("field", "operator", "value"), item, strict=True):
		ET.SubElement(holder, tag).text = text
