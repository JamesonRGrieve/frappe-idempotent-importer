# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads an account from NetSuite's SuiteTalk REST web services. Which records exist (and which changed
since a time) is asked of SuiteQL (``POST /services/rest/query/v1/suiteql``, paged by keyset on the
internal id, so a listing never reaches SuiteQL's 100,000-result cap); each record is then read whole from
the REST Record API (``GET /services/rest/record/v1/<type>/<id>?expandSubResources=true``, its sublists
included), whose shapes are those of the published REST API Browser (pinned in tools/specs.py). Every node
carries its identity as ``id`` (NetSuite's internal id is kept as ``source_id``) and ``record_type``, and
the reader adds what a record only refers to: a currency's ISO code, whether an account's parent is a
summary account, whether a department or location has children, a transaction's posting and voided flags
and each line's item type, and an opportunity's entity stage.

Incremental runs ask SuiteQL for ``lastmodifieddate`` on or after the day before the cursor (SuiteQL dates
are in the user's time zone, so a day's overlap covers any offset; an unchanged record is skipped by its
hash). Leads and prospects are customers at an earlier stage (``searchstage``): they are read as leads,
not customers. Job titles have no record: SuiteQL lists the distinct ones, as designations.

NetSuite governs web services per account by concurrent requests and by requests per 60 seconds and per
24 hours, answering 429 when either is exceeded
(https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1559222360.html). The reader sends
one request at a time, at most REQUESTS_PER_SECOND, and sits out the 60-second window after a 429 (by
pausing the run)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import SYNC_ORDER, Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import LIST_KEY, RestClient, query_string
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.netsuite.mapping import designation_key

SOURCE_ID = "netsuite"
API_HOST = "https://{account}.suitetalk.api.netsuite.com"
RECORD_PATH = "/services/rest/record/v1"
SUITEQL_PATH = "/services/rest/query/v1/suiteql"
# SuiteQL requires it (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_157909186990.html).
SUITEQL_HEADERS = {"Prefer": "transient"}
# Each listed id is then read on its own, so a page is kept small enough to finish well inside a step.
PAGE_SIZE = 100
# SuiteQL's largest page, for the short lists read whole (job titles, parents, item types).
FULL_PAGE_SIZE = 1000
REQUESTS_PER_SECOND = 4.0
WINDOW_SECONDS = 60.0
RATE_LIMIT = RateLimit(requests_per_second=REQUESTS_PER_SECOND, cooldown_seconds=WINDOW_SECONDS)
# An account id is digits, optionally with a sandbox or release-preview suffix ("1234567_SB1"); its host
# spells it in lower case with a dash.
ACCOUNT_ID = re.compile(r"^[0-9]+(?:_[A-Za-z0-9]+)?$")
SINCE_OVERLAP = timedelta(days=1)
TRUE = "T"
FIRST_ID = "0"

# The REST record type of each SuiteQL transaction type a kind reads.
TRANSACTION_RECORDS: dict[str, str] = {
	"Estimate": "estimate",
	"Opprtnty": "opportunity",
	"SalesOrd": "salesOrder",
	"CustInvc": "invoice",
	"CashSale": "cashSale",
	"CustCred": "creditMemo",
	"CashRfnd": "cashRefund",
	"CustPymt": "customerPayment",
	"CustRfnd": "customerRefund",
	"CustDep": "customerDeposit",
	"PurchOrd": "purchaseOrder",
	"VendBill": "vendorBill",
	"VendCred": "vendorCredit",
	"VendPymt": "vendorPayment",
	"Journal": "journalEntry",
	"Check": "check",
}
# The REST record type of a SuiteQL item (``itemtype``, the ids of the REST item records' itemType), then
# for the ones a subtype splits (sale, purchase, resale) and the lot-numbered and serialized variants of
# inventory and assembly items.
ITEM_RECORDS: dict[str, str] = {
	"InvtPart": "inventoryItem",
	"Assembly": "assemblyItem",
	"Kit": "kitItem",
	"GiftCert": "giftCertificateItem",
	"DwnLdItem": "downloadItem",
}
SUBTYPED_ITEMS: dict[str, str] = {
	"NonInvtPart": "nonInventory{}Item",
	"Service": "service{}Item",
	"OthCharge": "otherCharge{}Item",
}
ITEM_SUBTYPES = ("Sale", "Purchase", "Resale")
NUMBERED_ITEMS = {"InvtPart": "{}InventoryItem", "Assembly": "{}AssemblyItem"}
TREES = frozenset({"department", "location"})


@dataclass(frozen=True)
class Listing:
	"""One SuiteQL table a kind reads: the identity type its rows get, the condition that picks the kind's
	rows, the REST record type (None when each row's type says it: items and transactions), and the extra
	columns the listing reads."""

	identity: str
	table: str
	record: str | None
	condition: str = ""
	columns: tuple[str, ...] = ()


def quoted(values: tuple[str, ...] | list[str]) -> str:
	return ", ".join(f"'{value}'" for value in values)


def transactions(*types: str) -> Listing:
	return Listing(
		"Transaction", "transaction", None, f"type IN ({quoted(types)})", ("type", "posting", "voided")
	)


ITEMS = Listing(
	"Item",
	"item",
	None,
	f"itemtype IN ({quoted([*ITEM_RECORDS, *SUBTYPED_ITEMS])})",
	("itemtype", "subtype", "islotitem", "isserialitem"),
)
LISTINGS: dict[Kind, Listing] = {
	Kind.ACCOUNT: Listing("Account", "account", "account"),
	Kind.COST_CENTER: Listing("Department", "department", "department"),
	Kind.DEPARTMENT: Listing("Department", "department", "department"),
	Kind.WAREHOUSE: Listing("Location", "location", "location"),
	Kind.PRODUCT: ITEMS,
	Kind.CUSTOMER: Listing("Customer", "customer", "customer", "searchstage = 'Customer'"),
	Kind.LEAD: Listing("Customer", "customer", "customer", "searchstage IN ('Lead', 'Prospect')"),
	Kind.SUPPLIER: Listing("Vendor", "vendor", "vendor"),
	Kind.EMPLOYEE: Listing("Employee", "employee", "employee"),
	Kind.PROJECT: Listing("Job", "job", "job"),
	Kind.OPPORTUNITY: transactions("Opprtnty"),
	Kind.DRAFT_ORDER: transactions("Estimate"),
	Kind.SALES_ORDER: transactions("SalesOrd"),
	Kind.SALES_INVOICE: transactions("CustInvc", "CashSale", "CustCred", "CashRfnd"),
	Kind.PURCHASE_ORDER: transactions("PurchOrd"),
	Kind.PURCHASE_INVOICE: transactions("VendBill", "VendCred"),
	Kind.PAYMENT_ENTRY: transactions("CustPymt", "CustRfnd", "CustDep", "VendPymt", "CashSale", "CashRfnd"),
	Kind.JOURNAL_ENTRY: transactions("Journal", "Check"),
}
KINDS: tuple[Kind, ...] = tuple(kind for kind in SYNC_ORDER if kind == Kind.DESIGNATION or kind in LISTINGS)
# Job titles have no record; the few there are, are read in one listing by offset (Oracle SQL reads ""
# as NULL, so a keyset on text has no first key).
DESIGNATIONS_QUERY = "SELECT DISTINCT title FROM employee WHERE title IS NOT NULL ORDER BY title"
PARENTS_QUERY = "SELECT DISTINCT parent FROM {table} WHERE parent IS NOT NULL ORDER BY parent"
ITEM_TYPES_QUERY = "SELECT id, itemtype FROM item WHERE id > ? ORDER BY id"
STAGE_QUERY = "SELECT id, searchstage FROM customer WHERE id = ?"
SUBLISTS = ("item", "expense")


def item_record(row: Node) -> str:
	"""The REST record type of an item SuiteQL lists (``itemtype``, ``subtype``, ``islotitem``,
	``isserialitem``)."""
	item_type = row.string("itemtype")
	if item_type in SUBTYPED_ITEMS:
		subtype = row.optional_string("subtype") or ITEM_SUBTYPES[0]
		if subtype not in ITEM_SUBTYPES:
			raise SourceError(f"NetSuite item {row.string('id')} has an unknown subtype {subtype!r}.")
		return SUBTYPED_ITEMS[item_type].format(subtype)
	if item_type in NUMBERED_ITEMS:
		if row.optional_string("islotitem") == TRUE:
			return NUMBERED_ITEMS[item_type].format("lotNumbered")
		if row.optional_string("isserialitem") == TRUE:
			return NUMBERED_ITEMS[item_type].format("serialized")
	record = ITEM_RECORDS.get(item_type)
	if record is None:
		raise SourceError(f"NetSuite item {row.string('id')} has an unread type {item_type!r}.")
	return record


def transaction_record(row: Node) -> str:
	type_ = row.string("type")
	record = TRANSACTION_RECORDS.get(type_)
	if record is None:
		raise SourceError(f"NetSuite transaction {row.string('id')} has an unread type {type_!r}.")
	return record


def account_host(config: ConnectionConfig) -> str:
	account = config.account.strip()
	if not ACCOUNT_ID.match(account):
		raise SourceError(
			"NetSuite is not configured: enter the account ID (Setup → Company → Company Information, e.g. "
			"1234567 or 1234567_SB1) as the account."
		)
	return API_HOST.format(account=account.lower().replace("_", "-"))


class NetSuiteReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		self._config = config
		self._client = client
		self._currencies: dict[str, str] = {}
		self._summary_accounts: dict[str, bool] = {}
		self._parents: dict[str, set[str]] = {}
		self._item_types: dict[str, str] = {}
		self._stages: dict[str, str | None] = {}

	@staticmethod
	def base_url(config: ConnectionConfig) -> str:
		return (config.endpoint_override or account_host(config)).rstrip("/")

	def is_incremental(self, kind: Kind) -> bool:
		return kind in LISTINGS

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		if kind == Kind.DESIGNATION:
			return Page(self._designations(), False, None)
		listing = LISTINGS.get(kind)
		if listing is None:
			raise SourceError(f"NetSuite has no {kind.value}.")
		condition, params = "id > ?", [cursor or FIRST_ID]
		if since is not None:
			condition += " AND lastmodifieddate >= TO_DATE(?, 'YYYY-MM-DD')"
			params.append((since - SINCE_OVERLAP).strftime("%Y-%m-%d"))
		rows, has_more = self._suiteql(self._select(listing, condition), params, PAGE_SIZE)
		nodes = [node for row in rows if (node := self._record(listing, row)) is not None]
		following = rows[-1].string("id") if has_more and rows else None
		return Page(nodes, following is not None, following)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		if kind == Kind.DESIGNATION:
			return next((node for node in self._designations() if node.id() == gid), None)
		listing = LISTINGS.get(kind)
		if listing is None or listing.identity != identity.type or not identity.id.isdigit():
			raise SourceError(f"Not a NetSuite {kind.value} identity: {gid}")
		rows, _ = self._suiteql(self._select(listing, "id = ?"), [identity.id], 1)
		return self._record(listing, rows[0]) if rows else None

	# -- SuiteQL --------------------------------------------------------------------------------------

	@staticmethod
	def _select(listing: Listing, condition: str) -> str:
		columns = ", ".join(("id", *listing.columns))
		conditions = " AND ".join(part for part in (condition, listing.condition) if part)
		# Built only from this module's constant fragments; every value is a bound parameter.
		return f"SELECT {columns} FROM {listing.table} WHERE {conditions} ORDER BY id"  # noqa: S608

	def _suiteql(self, query: str, params: list[str], limit: int, offset: int = 0) -> tuple[list[Node], bool]:
		body: dict[str, JsonValue] = {"q": query}
		if params:
			body["params"] = list[JsonValue](params)
		path = SUITEQL_PATH + query_string({"limit": limit, "offset": offset})
		result = self._client.query(path, body, SUITEQL_HEADERS)
		if result is None:
			raise SourceError("NetSuite has no SuiteQL endpoint for this account.")
		return result.node.children(LIST_KEY), result.node.optional_flag("hasMore") is True

	def _all(self, query: str, params: list[str]) -> list[Node]:
		"""Every row of a short listing, page by page (by offset)."""
		rows: list[Node] = []
		while True:
			page, has_more = self._suiteql(query, params, FULL_PAGE_SIZE, len(rows))
			rows += page
			if not has_more or not page:
				return rows

	def _designations(self) -> list[Node]:
		titles = [row.string("title") for row in self._all(DESIGNATIONS_QUERY, [])]
		return [self._identified("Designation", designation_key(title), {"title": title}) for title in titles]

	# -- records --------------------------------------------------------------------------------------

	def _record(self, listing: Listing, row: Node) -> Node | None:
		"""The record a SuiteQL row names, read whole and annotated; None when it was deleted since."""
		record = listing.record
		if record is None:
			record = item_record(row) if listing.table == "item" else transaction_record(row)
		source_id = row.string("id")
		found = self._get(record, source_id)
		if found is None:
			return None
		extra: dict[str, JsonValue] = {"record_type": record}
		currency = found.child("currency")
		if currency is not None and currency.has("id"):
			extra["currency_code"] = self._currency_code(currency.string("id"))
		if record == "account":
			parent = found.child("parent")
			extra["parent_is_summary"] = (
				self._is_summary(parent.string("id")) if parent is not None and parent.has("id") else None
			)
		if record in TREES:
			extra["has_children"] = source_id in self._parents_of(record)
		if listing.table == "transaction":
			extra["posting"] = row.optional_string("posting")
			extra["voided"] = row.optional_string("voided")
			extra |= self._typed_lines(found)
		if record == "opportunity":
			entity = found.child("entity")
			extra["entity_stage"] = self._stage(entity.string("id")) if entity and entity.has("id") else None
		return self._identified(listing.identity, source_id, {**found.raw(), **extra})

	def _get(self, record: str, source_id: str) -> Node | None:
		result = self._client.get(f"{RECORD_PATH}/{record}/{source_id}", {"expandSubResources": True})
		return None if result is None else result.node

	def _currency_code(self, currency_id: str) -> str:
		"""A currency's ISO 4217 code (its ``symbol``), read once per run."""
		if currency_id not in self._currencies:
			currency = self._get("currency", currency_id)
			if currency is None:
				raise SourceError(f"NetSuite has no currency {currency_id}.")
			self._currencies[currency_id] = currency.string("symbol")
		return self._currencies[currency_id]

	def _is_summary(self, account_id: str) -> bool:
		if account_id not in self._summary_accounts:
			account = self._get("account", account_id)
			self._summary_accounts[account_id] = (
				account is not None and account.optional_flag("isSummary") is True
			)
		return self._summary_accounts[account_id]

	def _parents_of(self, table: str) -> set[str]:
		"""The ids of a tree's records that have children, read once per run."""
		if table not in self._parents:
			rows = self._all(PARENTS_QUERY.format(table=table), [])
			self._parents[table] = {row.string("parent") for row in rows}
		return self._parents[table]

	def _typed_lines(self, transaction: Node) -> dict[str, JsonValue]:
		"""The transaction's item and expense sublists with each line's ``item_type`` (SuiteQL's)."""
		sublists = {name: found for name in SUBLISTS if (found := transaction.child(name)) is not None}
		if not self._item_types:
			self._load_item_types()
		typed: dict[str, JsonValue] = {}
		for name, sublist in sublists.items():
			lines: list[JsonValue] = []
			for line in sublist.children("items"):
				item = line.child("item")
				item_id = item.optional_string("id") if item is not None else None
				lines.append({**line.raw(), "item_type": self._item_types.get(item_id or "")})
			typed[name] = {**sublist.raw(), "items": lines}
		return typed

	def _load_item_types(self) -> None:
		"""Every item's type (lines name items of every type, discounts and subtotals included), by keyset,
		read once per run (a line naming an item created since reads as an ordinary item's)."""
		after = FIRST_ID
		while True:
			rows, has_more = self._suiteql(ITEM_TYPES_QUERY, [after], FULL_PAGE_SIZE)
			for row in rows:
				self._item_types[row.string("id")] = row.string("itemtype")
			if not has_more or not rows:
				return
			after = rows[-1].string("id")

	def _stage(self, customer_id: str) -> str | None:
		"""A customer's stage (Lead, Prospect or Customer), read once per run."""
		if customer_id not in self._stages:
			rows, _ = self._suiteql(STAGE_QUERY, [customer_id], 1)
			self._stages[customer_id] = rows[0].optional_string("searchstage") if rows else None
		return self._stages[customer_id]

	def _identified(self, type_: str, id_: str, raw: Mapping[str, JsonValue]) -> Node:
		gid = self._config.identity(type_, id_)
		return Node({**raw, "source_id": id_, "id": gid}, gid)
