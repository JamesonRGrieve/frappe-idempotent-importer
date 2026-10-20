# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one Xero organisation (tenant) through the Accounting API (api.xro/2.0) and the Projects API
(projects.xro/2.0), as XeroAPI/Xero-OpenAPI documents them. Every request names the tenant
(``xero-tenant-id``). Paged collections are read 1000 at a time (``page`` / ``pageSize``); the others answer
whole. Incremental runs send ``If-Modified-Since``; deletions come only from a full pass. Archived contacts
are read too (``includeArchived``).

A kind Xero keeps as several collections reads them one after another (sales invoices: ACCREC invoices,
then ACCREC credit notes). A tracking category and each of its options are cost centers. Bank transactions
that are a prepayment or an overpayment are read as those (payments), not again as journals. Every node
carries its identity as ``id`` (``gid://xero/<connection>/<Type>/<GUID>``).

Limits (developer.xero.com/documentation/guides/oauth2/limits): per tenant 60 calls a minute, 5000 a day
and 5 at once, and 10,000 a minute per app; every response says what remains (X-MinLimit-Remaining,
X-DayLimit-Remaining) and a 429 carries Retry-After. The client paces a call a second; a spent day is a long
Retry-After, which pauses the run."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.common import chained

SOURCE_ID = "xero"
API_HOST = "https://api.xero.com"
ACCOUNTING = "/api.xro/2.0"
PROJECTS = "/projects.xro/2.0"
PAGE_SIZE = 1000
# Projects pages hold at most 500.
PROJECTS_PAGE_SIZE = 500
FIRST_PAGE = 1
TENANT_HEADER = "xero-tenant-id"
MODIFIED_SINCE = "If-Modified-Since"
SINCE_FORMAT = "%Y-%m-%dT%H:%M:%S"
CALLS_PER_MINUTE = 60
RATE_LIMIT = RateLimit(
	requests_per_second=RateLimit.per_period(CALLS_PER_MINUTE, 60.0),
	remaining_header="x-minlimit-remaining",
	window_seconds=60.0,
)
TRACKING_OPTION = "TrackingOption"
# The records whose lines are LineItems that may name their account by code alone.
LINED = frozenset(
	{"Invoice", "CreditNote", "Quote", "PurchaseOrder", "BankTransaction", "Prepayment", "Overpayment"}
)
TAXED = frozenset({"BankTransaction", "ManualJournal"})
ADVANCES = frozenset({"Prepayment", "Overpayment"})
TAX_ACCOUNT = "TaxAccountID"
ADVANCE_BANK_ACCOUNT = "BankAccount"
SYSTEM_PREFIX = "system:"
TAX_SYSTEM_ACCOUNTS = ("GST", "VAT")
# Spend and receive money; the halves of a bank transfer are read as the transfer.
PLAIN_BANK_TYPES = frozenset({"SPEND", "RECEIVE"})


@dataclass(frozen=True)
class Collection:
	"""One Xero collection a kind reads: its path, the key its records are under and their id field, the
	identity type, whether it pages and takes If-Modified-Since, its fixed query, and which records the kind
	takes (None: all)."""

	path: str
	key: str
	id_field: str
	type: str
	paged: bool = True
	incremental: bool = True
	query: tuple[tuple[str, QueryValue], ...] = ()
	take: Callable[[Node], bool] | None = field(default=None, compare=False)
	page_size: int = PAGE_SIZE


def is_supplier(contact: Node) -> bool:
	return contact.optional_flag("IsSupplier") is True


def is_customer(contact: Node) -> bool:
	"""A customer: flagged as one, or a contact that is no supplier (a contact Xero hasn't seen trade yet)."""
	return contact.optional_flag("IsCustomer") is True or not is_supplier(contact)


def is_plain_bank_transaction(transaction: Node) -> bool:
	"""Spend or receive money: not a prepayment or overpayment (read as payments) nor half of a transfer."""
	return transaction.optional_string("Type") in PLAIN_BANK_TYPES


def where_type(value: str) -> tuple[tuple[str, QueryValue], ...]:
	return (("where", f'Type=="{value}"'),)


CONTACTS = (("includeArchived", True),)
COLLECTIONS: dict[Kind, tuple[Collection, ...]] = {
	Kind.ACCOUNT: (Collection("/Accounts", "Accounts", "AccountID", "Account", paged=False),),
	Kind.COST_CENTER: (
		Collection(
			"/TrackingCategories",
			"TrackingCategories",
			"TrackingCategoryID",
			"TrackingCategory",
			paged=False,
			incremental=False,
			query=(("includeArchived", True),),
		),
	),
	Kind.PRODUCT: (Collection("/Items", "Items", "ItemID", "Item", paged=False),),
	Kind.CUSTOMER: (
		Collection("/Contacts", "Contacts", "ContactID", "Contact", query=CONTACTS, take=is_customer),
	),
	Kind.SUPPLIER: (
		Collection("/Contacts", "Contacts", "ContactID", "Contact", query=CONTACTS, take=is_supplier),
	),
	Kind.PROJECT: (
		Collection(
			PROJECTS + "/Projects",
			"items",
			"projectId",
			"Project",
			incremental=False,
			page_size=PROJECTS_PAGE_SIZE,
		),
	),
	Kind.DRAFT_ORDER: (Collection("/Quotes", "Quotes", "QuoteID", "Quote"),),
	Kind.PURCHASE_ORDER: (
		Collection("/PurchaseOrders", "PurchaseOrders", "PurchaseOrderID", "PurchaseOrder"),
	),
	Kind.SALES_INVOICE: (
		Collection("/Invoices", "Invoices", "InvoiceID", "Invoice", query=where_type("ACCREC")),
		Collection(
			"/CreditNotes", "CreditNotes", "CreditNoteID", "CreditNote", query=where_type("ACCRECCREDIT")
		),
	),
	Kind.PURCHASE_INVOICE: (
		Collection("/Invoices", "Invoices", "InvoiceID", "Invoice", query=where_type("ACCPAY")),
		Collection(
			"/CreditNotes", "CreditNotes", "CreditNoteID", "CreditNote", query=where_type("ACCPAYCREDIT")
		),
	),
	Kind.PAYMENT_ENTRY: (
		Collection("/Payments", "Payments", "PaymentID", "Payment"),
		Collection("/Prepayments", "Prepayments", "PrepaymentID", "Prepayment"),
		Collection("/Overpayments", "Overpayments", "OverpaymentID", "Overpayment"),
	),
	Kind.JOURNAL_ENTRY: (
		Collection("/ManualJournals", "ManualJournals", "ManualJournalID", "ManualJournal"),
		Collection(
			"/BankTransactions",
			"BankTransactions",
			"BankTransactionID",
			"BankTransaction",
			take=is_plain_bank_transaction,
		),
		Collection("/BankTransfers", "BankTransfers", "BankTransferID", "BankTransfer", paged=False),
	),
}


class XeroReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		if not config.account:
			raise SourceError("Xero is not connected: use Connect to Xero first (it names the organisation).")
		self._config = config
		self._client = client
		self._account_codes: dict[str, str] | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return any(collection.incremental for collection in COLLECTIONS.get(kind, ()))

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		collections = COLLECTIONS.get(kind)
		if collections is None:
			raise SourceError(f"Xero has no {kind.value}.")
		return chained(
			len(collections),
			cursor,
			lambda feed, position: self._collection(collections[feed], position, since),
		)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		collection = next((c for c in COLLECTIONS.get(kind, ()) if c.type == identity.type), None)
		if collection is None:
			raise SourceError(f"Xero can't read a {identity.type} as a {kind.value}.")
		result = self._client.get(self._path(collection) + f"/{identity.id}")
		if result is None:
			return None
		records = result.node.children(collection.key)
		nodes = [node for record in records for node in self._taken(collection, record)]
		return next((node for node in nodes if node.id() == gid), None)

	def _collection(self, collection: Collection, position: str | None, since: datetime | None) -> Page:
		page = int(position) if position else FIRST_PAGE
		query: dict[str, QueryValue] = dict(collection.query)
		if collection.paged:
			query |= {"page": page, "pageSize": collection.page_size}
		headers = (
			{MODIFIED_SINCE: since.astimezone(UTC).strftime(SINCE_FORMAT)}
			if since is not None and collection.incremental
			else {}
		)
		result = self._client.get(self._path(collection), query, headers)
		if result is None:
			raise SourceError(f"Xero has no {collection.path} for this organisation.")
		records = result.node.children(collection.key)
		nodes = [node for record in records for node in self._taken(collection, record)]
		more = collection.paged and self._has_more(result.node, page, len(records), collection.page_size)
		return Page(nodes, more, str(page + 1) if more else None)

	@staticmethod
	def _has_more(response: Node, page: int, count: int, page_size: int) -> bool:
		pagination = response.child("pagination")
		if pagination is not None and pagination.optional_integer("pageCount") is not None:
			return page < pagination.integer("pageCount")
		return count == page_size

	@staticmethod
	def _path(collection: Collection) -> str:
		return collection.path if collection.path.startswith(PROJECTS) else ACCOUNTING + collection.path

	def _taken(self, collection: Collection, record: Node) -> list[Node]:
		if collection.take is not None and not collection.take(record):
			return []
		node = self._identified(
			collection.type, record.string(collection.id_field), self._enriched(collection, record)
		)
		if collection.type != "TrackingCategory":
			return [node]
		category = record.string("TrackingCategoryID")
		options = [
			self._identified(
				TRACKING_OPTION,
				option.string("TrackingOptionID"),
				{**option.raw(), "TrackingCategoryID": category, "CategoryName": record.string("Name")},
			)
			for option in record.children("Options")
		]
		return [node, *options]

	def _enriched(self, collection: Collection, record: Node) -> dict[str, JsonValue]:
		"""The record with what its mapping needs that Xero keeps elsewhere: each line's account id (a line
		may name its account only by code), the organisation's tax account for a journal or bank transaction
		that carries tax (Xero posts it to its GST/VAT system account), and the bank account a prepayment
		or overpayment was paid through (its bank transaction's)."""
		raw = record.raw()
		if collection.type in LINED:
			raw["LineItems"] = [self._line_account(line) for line in record.children("LineItems")]
		if collection.type in TAXED and any(
			(line.optional_decimal("TaxAmount") or 0) != 0
			for line in [*record.children("LineItems"), *record.children("JournalLines")]
		):
			raw[TAX_ACCOUNT] = self._tax_account()
		if collection.type in ADVANCES:
			raw[ADVANCE_BANK_ACCOUNT] = self._advance_account(
				collection.type, record.string(collection.id_field)
			)
		return raw

	def _line_account(self, line: Node) -> JsonValue:
		raw = line.raw()
		code = line.optional_string("AccountCode")
		if not line.optional_string("AccountID") and code:
			found = self._accounts().get(code)
			if found is None:
				raise SourceError(
					f"{line.path} names account {code}, which the organisation has no account for."
				)
			raw["AccountID"] = found
		return raw

	def _accounts(self) -> dict[str, str]:
		"""The organisation's accounts by code (and system role, under ``SYSTEM_PREFIX``), read once."""
		if self._account_codes is None:
			result = self._client.get(ACCOUNTING + "/Accounts")
			codes: dict[str, str] = {}
			for account in result.node.children("Accounts") if result is not None else []:
				if account.optional_string("Code"):
					codes[account.string("Code")] = account.string("AccountID")
				if account.optional_string("SystemAccount"):
					codes[SYSTEM_PREFIX + account.string("SystemAccount")] = account.string("AccountID")
			self._account_codes = codes
		return self._account_codes

	def _tax_account(self) -> str:
		for role in TAX_SYSTEM_ACCOUNTS:
			found = self._accounts().get(SYSTEM_PREFIX + role)
			if found:
				return found
		raise SourceError(
			"Xero has no GST or VAT system account for the tax on a journal or bank transaction."
		)

	def _advance_account(self, type_: str, id_: str) -> JsonValue:
		"""The bank account of the bank transaction that took or paid a prepayment or overpayment."""
		result = self._client.get(ACCOUNTING + "/BankTransactions", {"where": f'{type_}ID==Guid("{id_}")'})
		for transaction in result.node.children("BankTransactions") if result is not None else []:
			account = transaction.child("BankAccount")
			if account is not None and account.optional_string("AccountID"):
				return {"AccountID": account.string("AccountID")}
		raise SourceError(f"Xero has no bank transaction for {type_} {id_}.")

	def _identified(self, type_: str, id_: str, raw: Mapping[str, JsonValue]) -> Node:
		gid = self._config.identity(type_, id_)
		return Node({**raw, "id": gid}, gid)
