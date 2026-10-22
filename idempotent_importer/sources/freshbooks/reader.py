# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one FreshBooks business through its API (api.freshbooks.com), as FreshBooks' API reference
(www.freshbooks.com/api) documents it: the chart of accounts, taxes, items and services (with their rates),
clients, bill vendors, projects and time entries, estimates, invoices and credit notes, bills, payments and
bill payments, the manual journal entries, and the expenses and other income as the general ledger posted
them.

A business is three ids (``users/me``'s business membership): the account id the ``/accounting/account/<id>``
endpoints take, the business id the projects, time tracking and services endpoints take, and the business
uuid the chart of accounts, reports and journal entries take; the connection's account holds all three
(``<account id>|<business id>|<business uuid>``).

Accounting lists answer ``response.result`` with their records and ``page``/``pages``, 100 a page
(``per_page``, its cap: www.freshbooks.com/api/parameters); the projects, time entries and services lists
answer their records beside ``meta``. Lines come with invoices and estimates when asked for
(``include[]=lines``). An expense or other income is read as its postings in the general ledger
(``reports/general_ledger``, every account's transactions from the start of the books to today), grouped by
the expense or income they belong to, so it books exactly as FreshBooks booked it. A bill payment carries its
bill's vendor (bills are read by id, once).

Incremental runs ask for what changed since the last run where a list takes it: clients, invoices,
estimates and payments (``search[updated_min]``, a date: the whole day is read again), projects and time
entries (``updated_since``). The rest is read whole and skipped by its hash.

FreshBooks documents no request limit beyond rate limiting "too many calls within a short period"
(www.freshbooks.com/api/limits); the client paces two requests a second and backs off on 429."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit, Sleeper
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.common import chained

SOURCE_ID = "freshbooks"
API_HOST = "https://api.freshbooks.com"
PER_PAGE = 100
REQUESTS_PER_SECOND = 2.0
RATE_LIMIT = RateLimit(requests_per_second=REQUESTS_PER_SECOND)
ACCOUNT_SEPARATOR = "|"
UPDATED_DATE = "%Y-%m-%d"
UPDATED_TIME = "%Y-%m-%dT%H:%M:%SZ"
LEDGER_START = "2000-01-01"
JOURNAL_API_VERSION = ("x-api-version", "2023-09-25")
EXPENSE, INCOME = "expenseid", "incomeid"


@dataclass(frozen=True)
class Listing:
	"""One list: the API it is under (``accounting``: ``/accounting/account/<account id>``; ``business``:
	``/<service>/business/<business id>``), its path there, the key its records are under, the identity type
	of its records and their id field, the includes it asks for, and how it filters by change (None: it
	doesn't)."""

	api: str
	path: str
	key: str
	type: str
	id_field: str = "id"
	includes: tuple[str, ...] = ()
	changed: str | None = None


ACCOUNTING, BUSINESS = "accounting", "business"
UPDATED_MIN = "search[updated_min]"
UPDATED_SINCE = "updated_since"
TAXES = Listing(ACCOUNTING, "/taxes/taxes", "taxes", "Tax", "taxid")
ITEMS = Listing(ACCOUNTING, "/items/items", "items", "Item", "itemid")
SERVICES = Listing(BUSINESS, "/comments/business/{business}/services", "services", "Service")
CLIENTS = Listing(ACCOUNTING, "/users/clients", "clients", "Client", "id", changed=UPDATED_MIN)
VENDORS = Listing(ACCOUNTING, "/bill_vendors/bill_vendors", "bill_vendors", "Vendor", "vendorid")
PROJECTS = Listing(
	BUSINESS, "/projects/business/{business}/projects", "projects", "Project", changed=UPDATED_SINCE
)
TIME_ENTRIES = Listing(
	BUSINESS,
	"/timetracking/business/{business}/time_entries",
	"time_entries",
	"TimeEntry",
	changed=UPDATED_SINCE,
)
ESTIMATES = Listing(
	ACCOUNTING, "/estimates/estimates", "estimates", "Estimate", "estimateid", ("lines",), UPDATED_MIN
)
INVOICES = Listing(
	ACCOUNTING, "/invoices/invoices", "invoices", "Invoice", "invoiceid", ("lines",), UPDATED_MIN
)
CREDIT_NOTES = Listing(ACCOUNTING, "/credit_notes/credit_notes", "credit_notes", "CreditNote", "creditid")
BILLS = Listing(ACCOUNTING, "/bills/bills", "bills", "Bill")
PAYMENTS = Listing(ACCOUNTING, "/payments/payments", "payments", "Payment", changed=UPDATED_MIN)
BILL_PAYMENTS = Listing(ACCOUNTING, "/bill_payments/bill_payments", "bill_payments", "BillPayment")
KIND_LISTINGS: dict[Kind, tuple[Listing, ...]] = {
	Kind.TAX_RATE: (TAXES,),
	Kind.PRODUCT: (ITEMS, SERVICES),
	Kind.CUSTOMER: (CLIENTS,),
	Kind.SUPPLIER: (VENDORS,),
	Kind.PROJECT: (PROJECTS,),
	Kind.TIMESHEET: (TIME_ENTRIES,),
	Kind.DRAFT_ORDER: (ESTIMATES,),
	Kind.SALES_INVOICE: (INVOICES, CREDIT_NOTES),
	Kind.PURCHASE_INVOICE: (BILLS,),
	Kind.PAYMENT_ENTRY: (PAYMENTS, BILL_PAYMENTS),
}
KINDS: tuple[Kind, ...] = (Kind.ACCOUNT, *KIND_LISTINGS, Kind.JOURNAL_ENTRY)
INCREMENTAL = frozenset(
	kind for kind, listings in KIND_LISTINGS.items() if all(listing.changed for listing in listings)
)


@dataclass(frozen=True)
class Business:
	"""The three ids of one business."""

	account_id: str
	business_id: str
	business_uuid: str

	@classmethod
	def of(cls, account: str) -> Business:
		parts = account.split(ACCOUNT_SEPARATOR)
		if len(parts) != len(("account", "business", "uuid")) or not all(parts):
			raise SourceError(
				"FreshBooks is not connected: use Connect to FreshBooks to choose the business."
			)
		return cls(*parts)

	def account(self) -> str:
		return ACCOUNT_SEPARATOR.join((self.account_id, self.business_id, self.business_uuid))


class FreshBooksReader:
	def __init__(self, config: ConnectionConfig, client: RestClient, sleeper: Sleeper) -> None:
		self._config = config
		self._client = client
		self._sleeper = sleeper
		self._business = Business.of(config.account)
		self._vendors: dict[str, JsonValue] = {}

	def is_incremental(self, kind: Kind) -> bool:
		return kind in INCREMENTAL

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		match kind:
			case Kind.ACCOUNT:
				return Page(self._accounts(), False, None)
			case Kind.JOURNAL_ENTRY:
				return chained(2, cursor, self._journals)
		listings = KIND_LISTINGS.get(kind)
		if listings is None:
			raise SourceError(f"FreshBooks has no {kind.value}.")
		return chained(len(listings), cursor, lambda feed, at: self._listed(listings[feed], at, since))

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		raise SourceError(f"FreshBooks can't read a {identity.type} by itself.")

	# -- lists ----------------------------------------------------------------------------------------------

	def _listed(self, listing: Listing, position: str | None, since: datetime | None) -> Page:
		page = int(position) if position else 1
		query: dict[str, QueryValue] = {"page": page, "per_page": PER_PAGE}
		if listing.includes:
			query["include[]"] = list(listing.includes)
		if since is not None and listing.changed == UPDATED_MIN:
			query[UPDATED_MIN] = since.astimezone(UTC).strftime(UPDATED_DATE)
		elif since is not None and listing.changed == UPDATED_SINCE:
			query[UPDATED_SINCE] = since.astimezone(UTC).strftime(UPDATED_TIME)
		if listing.api == ACCOUNTING:
			found = self._get(f"/accounting/account/{self._business.account_id}{listing.path}", query)
			result = found.required_child("response").required_child("result")
			pages = result.optional_integer("pages") or 1
		else:
			result = self._get(listing.path.format(business=self._business.business_id), query)
			meta = result.child("meta")
			pages = (meta.optional_integer("pages") if meta else None) or 1
		records = result.children(listing.key)
		nodes = [node for record in records for node in self._expanded(listing, record)]
		more = page < pages
		return Page(nodes, more, str(page + 1) if more else None)

	def _expanded(self, listing: Listing, record: Node) -> list[Node]:
		extra: dict[str, JsonValue] = {}
		if listing is SERVICES:
			rate = self._client.get(
				f"/comments/business/{self._business.business_id}/service/{record.integer('id')}/rate"
			)
			found = rate.node.child("service_rate") if rate else None
			extra["rate"] = found.raw() if found else None
		elif listing is BILL_PAYMENTS:
			extra["vendorid"] = self._vendor_of(record.integer("billid"))
		return [self._identified(listing.type, record.raw() | extra, listing.id_field)]

	def _vendor_of(self, bill: int) -> JsonValue:
		"""The vendor of a bill a payment pays (the bill read by id, once)."""
		key = str(bill)
		if key not in self._vendors:
			found = self._get(f"/accounting/account/{self._business.account_id}/bills/bills/{bill}", {})
			result = found.required_child("response").required_child("result").required_child("bill")
			self._vendors[key] = result.optional_integer("vendorid")
		return self._vendors[key]

	# -- the books ------------------------------------------------------------------------------------------

	def _accounts(self) -> list[Node]:
		"""The chart of accounts: each account, and each sub-account under its account."""
		found = self._get(
			f"/accounting/businesses/{self._business.business_uuid}/reports/chart_of_accounts",
			{"use_ledger_entries": True},
		)
		accounts = (
			found.required_child("response").required_child("result").children("journal_entry_accounts")
		)
		nodes: list[Node] = []
		for account in accounts:
			subs = account.children("sub_accounts")
			nodes.append(
				self._identified("Account", account.raw() | {"is_group": bool(subs)}, "account_uuid")
			)
			nodes += [
				self._identified(
					"Account",
					sub.raw() | {"parent": account.string("account_uuid"), "is_group": False},
					"account_uuid",
				)
				for sub in subs
			]
		return nodes

	def _journals(self, feed: int, position: str | None) -> Page:
		"""The manual journal entries (feed 0), then the expenses and other income as the ledger posted them
		(feed 1)."""
		if feed == 0:
			page = int(position) if position else 1
			found = self._client.get(
				f"/accounting/businesses/{self._business.business_uuid}/journal_entries",
				{"page_number": page, "page_size": PER_PAGE},
				dict([JOURNAL_API_VERSION]),
			)
			result = found.node if found else Node({})
			entries = result.children("manualJournalEntries")
			nodes = [self._identified("JournalEntry", entry.raw()) for entry in entries]
			paging = result.child("page")
			more = page < ((paging.optional_integer("pages") if paging else None) or 1)
			return Page(nodes, more, str(page + 1) if more else None)
		return Page(self._ledger_groups(), False, None)

	def _ledger_groups(self) -> list[Node]:
		"""Every expense's and other income's postings in the general ledger, one node each."""
		today = datetime.fromtimestamp(self._sleeper.now(), UTC).strftime(UPDATED_DATE)
		found = self._get(
			f"/accounting/businesses/{self._business.business_uuid}/reports/general_ledger",
			{"start_date": LEDGER_START, "end_date": today},
		)
		ledger = found.required_child("response").required_child("result").required_child("general_ledger")
		groups: dict[tuple[str, str], list[JsonValue]] = {}
		for account in ledger.children("data"):
			subs = account.children("sub_accounts")
			holders = [(sub.string("sub_accountid"), sub) for sub in subs] or [
				(account.string("accountid"), account)
			]
			for account_id, holder in holders:
				for transaction in holder.children("transactions"):
					for field, type_ in ((EXPENSE, "Expense"), (INCOME, "OtherIncome")):
						entity = transaction.optional_integer(field)
						if entity is not None:
							groups.setdefault((type_, str(entity)), []).append(
								transaction.raw() | {"account": account_id}
							)
		return [
			self._identified(type_, {"id": entity, "postings": postings})
			for (type_, entity), postings in sorted(groups.items())
		]

	# -- requests -------------------------------------------------------------------------------------------

	def _get(self, path: str, query: dict[str, QueryValue]) -> Node:
		result = self._client.get(path, query)
		if result is None:
			raise SourceError(f"FreshBooks has no {path}.")
		return result.node

	def _identified(self, type_: str, record: dict[str, JsonValue], id_field: str = "id") -> Node:
		source_id = record.get(id_field)
		if isinstance(source_id, bool) or not isinstance(source_id, int | str) or source_id == "":
			raise SourceError(f"A FreshBooks {type_} has no {id_field}.")
		gid = self._config.identity(type_, source_id)
		return Node({**record, "id": gid, "source_id": str(source_id)}, gid)
