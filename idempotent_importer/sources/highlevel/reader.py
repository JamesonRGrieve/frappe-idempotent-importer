# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one HighLevel sub-account (location) through API v2 (services.leadconnectorhq.com), as HighLevel's
OpenAPI documents describe it (GoHighLevel/highlevel-api-docs, apps/*.json, pinned in tools/specs.py): the
location's users, products and their prices, contacts with their notes, tasks and appointments, businesses
with their contacts, pipelines, opportunities, conversations with their messages, invoices, and payment
transactions. Every request carries the API version its document names (``Version``).

Paging is each endpoint's own: contacts after the last one read (``startAfterId`` and ``startAfter``, its
time added in milliseconds); opportunities after the ``meta`` cursor they answer with; businesses, a
business's contacts, products, prices, invoices and transactions by offset (``skip`` / ``offset``) until a
short page. Conversations are read in windows of the time they were added (``startDate`` / ``endDate``),
a window halved until it holds no more than one page answers; each conversation's messages page after the
last one read (``lastMessageId``) while ``nextPage``. HighLevel documents no modified-since filter for any
of these, so every run reads them whole and the change hash skips what didn't change.

Customers are the contacts HighLevel bills: those its invoices and transactions name, each read whole by
id. Every contact is a lead (``Contact/<id>``); a billed one is a customer too, under the same identity.
``GET /contacts/`` is the documented list (HighLevel marks it deprecated for ``POST /contacts/search``,
whose request and answer its documents don't describe), as is ``GET /users/`` for a location's users.

Limits (docs/oauth/Faqs.md): per app and location, 100 requests in 10 seconds and 200,000 a day; responses
say what remains (X-RateLimit-Remaining, in X-RateLimit-Interval-Milliseconds windows). The client paces ten
a second; a spent day is a long 429, which pauses the run. Every node carries its identity as ``id``
(``gid://highlevel/<connection>/<Type>/<id>``)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import RateLimit, Sleeper
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.core.times import parse_utc
from idempotent_importer.sources.common import chained

SOURCE_ID = "highlevel"
API_HOST = "https://services.leadconnectorhq.com"
# The API version each document names in its Version header.
VERSION = "2021-07-28"
CONVERSATIONS_VERSION = "2021-04-15"
VERSION_HEADER = "Version"
PAGE_SIZE = 100
REQUESTS_PER_WINDOW = 100
WINDOW_SECONDS = 10.0
RATE_LIMIT = RateLimit(
	requests_per_second=RateLimit.per_period(REQUESTS_PER_WINDOW, WINDOW_SECONDS),
	remaining_header="x-ratelimit-remaining",
	window_seconds=WINDOW_SECONDS,
)
MILLISECONDS = 1000
# Before any conversation a sub-account holds was added (HighLevel's whole history).
FIRST_CONVERSATION_MS = int(datetime(2015, 1, 1, tzinfo=UTC).timestamp() * MILLISECONDS)
CURSOR_SEPARATOR = "|"
CONTACT, REFUND = "Contact", "TransactionRefund"


@dataclass(frozen=True)
class Listing:
	"""One offset-paged list: its path, the key its records are under, the identity type of its records,
	its version, the names of its offset and page size parameters, and the location parameter's name."""

	path: str
	key: str
	type: str
	offset: str = "offset"
	location: str = "locationId"
	alt: bool = False


USERS = Listing("/users/", "users", "User")
PRODUCTS = Listing("/products/", "products", "Product")
BUSINESSES = Listing("/businesses/", "businesses", "Business", offset="skip")
INVOICES = Listing("/invoices/", "invoices", "Invoice", alt=True)
TRANSACTIONS = Listing("/payments/transactions", "data", "Transaction", alt=True)
# The kinds read from every contact in turn, and what each reads of a contact.
CONTACT_KINDS: dict[Kind, str] = {Kind.LEAD: "notes", Kind.EVENT: "appointments", Kind.TODO: "tasks"}
CONTACT_PARTS = {
	"notes": ("notes", "Note"),
	"appointments": ("events", "Appointment"),
	"tasks": ("tasks", "Task"),
}
KINDS: tuple[Kind, ...] = (
	Kind.EMPLOYEE,
	Kind.PRODUCT,
	Kind.CUSTOMER,
	*CONTACT_KINDS,
	Kind.PROSPECT,
	Kind.OPPORTUNITY_TYPE,
	Kind.OPPORTUNITY,
	Kind.COMMUNICATION,
	Kind.SALES_INVOICE,
	Kind.PAYMENT_ENTRY,
)


def milliseconds(value: str | None) -> int | None:
	return int(parse_utc(value).timestamp() * MILLISECONDS) if value else None


class HighLevelReader:
	def __init__(self, config: ConnectionConfig, client: RestClient, sleeper: Sleeper) -> None:
		if not config.account:
			raise SourceError(
				"HighLevel is not connected: use Connect to HighLevel for the sub-account first."
			)
		self._config = config
		self._client = client
		self._sleeper = sleeper
		self._location = config.account

	def is_incremental(self, kind: Kind) -> bool:
		return False

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		match kind:
			case Kind.EMPLOYEE:
				users = self._get(USERS.path, {"locationId": self._location}).children(USERS.key)
				return Page([self._identified(USERS.type, user.raw()) for user in users], False, None)
			case Kind.PRODUCT:
				return self._offset_page(PRODUCTS, cursor, self._with_prices)
			case Kind.CUSTOMER:
				return chained(2, cursor, self._billed)
			case Kind.LEAD | Kind.EVENT | Kind.TODO:
				return self._contacts_page(kind, cursor)
			case Kind.PROSPECT:
				return self._offset_page(BUSINESSES, cursor, self._with_contacts)
			case Kind.OPPORTUNITY_TYPE:
				pipelines = self._get("/opportunities/pipelines", {"locationId": self._location})
				nodes = [self._identified("Pipeline", p.raw()) for p in pipelines.children("pipelines")]
				return Page(nodes, False, None)
			case Kind.OPPORTUNITY:
				return self._opportunities(cursor)
			case Kind.COMMUNICATION:
				return self._conversations(cursor)
			case Kind.SALES_INVOICE:
				return self._offset_page(INVOICES, cursor)
			case Kind.PAYMENT_ENTRY:
				return self._offset_page(TRANSACTIONS, cursor, self._with_refund)
		raise SourceError(f"HighLevel has no {kind.value}.")

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		match kind, identity.type:
			case ((Kind.LEAD | Kind.CUSTOMER), "Contact"):
				contact = self._contact(identity.id)
				return self._with_notes(contact) if contact and kind is Kind.LEAD else contact
			case Kind.OPPORTUNITY, "Opportunity":
				result = self._client.get(f"/opportunities/{identity.id}", headers=self._version(VERSION))
				record = result.node.child("opportunity") if result else None
				return self._identified("Opportunity", record.raw()) if record else None
			case Kind.SALES_INVOICE, "Invoice":
				query = {"altId": self._location, "altType": "location"}
				result = self._client.get(f"/invoices/{identity.id}", query, self._version(VERSION))
				return self._identified("Invoice", result.node.raw()) if result else None
		raise SourceError(f"HighLevel can't read a {identity.type} as a {kind.value} by itself.")

	# -- lists ---------------------------------------------------------------------------------------------

	def _offset_page(
		self,
		listing: Listing,
		cursor: str | None,
		expand: Callable[[Node], list[Node]] | None = None,
	) -> Page:
		offset = int(cursor) if cursor else 0
		records = self._listing(listing, offset)
		nodes = [self._identified(listing.type, record.raw()) for record in records]
		if expand is not None:
			nodes = [expanded for node in nodes for expanded in expand(node)]
		more = len(records) == PAGE_SIZE
		return Page(nodes, more, str(offset + len(records)) if more else None)

	def _listing(self, listing: Listing, offset: int) -> list[Node]:
		query: dict[str, QueryValue] = {listing.offset: offset, "limit": PAGE_SIZE}
		if listing.alt:
			query |= {"altId": self._location, "altType": "location"}
		else:
			query[listing.location] = self._location
		return self._get(listing.path, query).children(listing.key)

	def _all(self, listing: Listing) -> Iterator[Node]:
		offset = 0
		while True:
			records = self._listing(listing, offset)
			yield from records
			if len(records) < PAGE_SIZE:
				return
			offset += len(records)

	def _contacts_page(self, kind: Kind, cursor: str | None) -> Page:
		query: dict[str, QueryValue] = {"locationId": self._location, "limit": PAGE_SIZE}
		if cursor:
			after_id, _, after = cursor.partition(CURSOR_SEPARATOR)
			query |= {"startAfterId": after_id, "startAfter": after}
		listed = self._get("/contacts/", query).children("contacts")
		nodes: list[Node] = []
		for summary in listed:
			if kind is Kind.LEAD:
				contact = self._contact(summary.string("id"))
				if contact is not None:
					nodes.append(self._with_notes(contact))
			else:
				nodes += self._parts(summary.string("id"), CONTACT_KINDS[kind])
		if len(listed) < PAGE_SIZE:
			return Page(nodes, False, None)
		last = listed[-1]
		added = milliseconds(last.optional_string("dateAdded"))
		return Page(nodes, True, f"{last.string('id')}{CURSOR_SEPARATOR}{added if added is not None else ''}")

	def _contact(self, contact_id: str) -> Node | None:
		result = self._client.get(f"/contacts/{contact_id}", headers=self._version(VERSION))
		record = result.node.child("contact") if result else None
		return self._identified(CONTACT, record.raw()) if record else None

	def _with_notes(self, contact: Node) -> Node:
		notes = self._get(f"/contacts/{contact.string('source_id')}/notes", {}).children("notes")
		return contact.with_fields(notes=[note.raw() for note in notes])

	def _parts(self, contact_id: str, part: str) -> list[Node]:
		key, type_ = CONTACT_PARTS[part]
		records = self._get(f"/contacts/{contact_id}/{part}", {}).children(key)
		return [self._identified(type_, {**record.raw(), "contactId": contact_id}) for record in records]

	def _billed(self, feed: int, position: str | None) -> Page:
		"""The contacts the invoices (feed 0), then the transactions (feed 1), name, each read whole."""
		listing = INVOICES if feed == 0 else TRANSACTIONS
		offset = int(position) if position else 0
		records = self._listing(listing, offset)
		named: dict[str, None] = {}
		for record in records:
			details = record.child("contactDetails")
			contact = details.optional_string("id") if details else record.optional_string("contactId")
			if contact:
				named[contact] = None
		nodes = [node for contact in named if (node := self._contact(contact)) is not None]
		more = len(records) == PAGE_SIZE
		return Page(nodes, more, str(offset + len(records)) if more else None)

	def _with_prices(self, product: Node) -> list[Node]:
		listing = Listing(f"/products/{product.string('source_id')}/price", "prices", "Price")
		prices = [price.raw() for price in self._all(listing)]
		return [product.with_fields(prices=list[JsonValue](prices))]

	def _with_contacts(self, business: Node) -> list[Node]:
		listing = Listing(
			f"/contacts/business/{business.string('source_id')}", "contacts", CONTACT, offset="skip"
		)
		contacts = [contact.string("id") for contact in self._all(listing)]
		return [business.with_fields(contact_ids=list[JsonValue](contacts))]

	def _with_refund(self, transaction: Node) -> list[Node]:
		"""A transaction, and what was refunded of it as a payment of its own."""
		refunded = transaction.optional_decimal("amountRefunded")
		if not refunded:
			return [transaction]
		refund_gid = self._config.identity(REFUND, transaction.string("source_id"))
		return [transaction, Node({**transaction.raw(), "id": refund_gid, "refund": True}, refund_gid)]

	def _opportunities(self, cursor: str | None) -> Page:
		query: dict[str, QueryValue] = {"location_id": self._location, "limit": PAGE_SIZE}
		if cursor:
			after_id, _, after = cursor.partition(CURSOR_SEPARATOR)
			query |= {"startAfterId": after_id, "startAfter": after}
		result = self._get("/opportunities/search", query)
		records = result.children("opportunities")
		nodes = [self._identified("Opportunity", record.raw()) for record in records]
		meta = result.child("meta")
		next_id = meta.optional_string("startAfterId") if meta else None
		next_after = meta.optional_decimal("startAfter") if meta else None
		if len(records) < PAGE_SIZE or not next_id or next_after is None:
			return Page(nodes, False, None)
		return Page(nodes, True, f"{next_id}{CURSOR_SEPARATOR}{int(next_after)}")

	def _conversations(self, cursor: str | None) -> Page:
		"""One window of conversations by when they were added, halved until a page holds it whole (cursor
		``<start>|<end of history>``), each conversation with all its messages."""
		start, until = FIRST_CONVERSATION_MS, self._now_ms()
		if cursor:
			start_text, _, until_text = cursor.partition(CURSOR_SEPARATOR)
			start, until = int(start_text), int(until_text)
		end = until
		while True:
			query: dict[str, QueryValue] = {
				"locationId": self._location,
				"limit": PAGE_SIZE,
				"startDate": start,
				"endDate": end,
				"sort": "asc",
			}
			result = self._get("/conversations/search", query, CONVERSATIONS_VERSION)
			conversations = result.children("conversations")
			total = result.optional_integer("total") or 0
			if len(conversations) >= total or end - start <= 1:
				if len(conversations) < total:
					raise SourceError(
						f"HighLevel has more than {len(conversations)} conversations added at {start}."
					)
				break
			end = start + (end - start) // 2
		nodes = [message for conversation in conversations for message in self._messages(conversation)]
		if end >= until:
			return Page(nodes, False, None)
		return Page(nodes, True, f"{end + 1}{CURSOR_SEPARATOR}{until}")

	def _messages(self, conversation: Node) -> Iterator[Node]:
		query: dict[str, QueryValue] = {"limit": PAGE_SIZE}
		with_conversation = {
			"conversationEmail": conversation.optional_string("email"),
			"conversationPhone": conversation.optional_string("phone"),
		}
		while True:
			result = self._get(
				f"/conversations/{conversation.string('id')}/messages", query, CONVERSATIONS_VERSION
			)
			for message in result.children("messages"):
				yield self._identified("Message", {**message.raw(), **with_conversation})
			last = result.optional_string("lastMessageId")
			if result.optional_flag("nextPage") is not True or not last:
				return
			query["lastMessageId"] = last

	# -- requests -----------------------------------------------------------------------------------------

	def _get(self, path: str, query: dict[str, QueryValue], version: str = VERSION) -> Node:
		result = self._client.get(path, query, self._version(version))
		if result is None:
			raise SourceError(f"HighLevel has no {path}.")
		return result.node

	@staticmethod
	def _version(version: str) -> dict[str, str]:
		return {VERSION_HEADER: version}

	def _identified(self, type_: str, record: dict[str, JsonValue]) -> Node:
		source_id = record.get("id") if isinstance(record.get("id"), str) else record.get("_id")
		if not isinstance(source_id, str):
			raise SourceError(f"A HighLevel {type_} has no id.")
		gid = self._config.identity(type_, source_id)
		return Node({**record, "id": gid, "source_id": source_id}, gid)

	def _now_ms(self) -> int:
		return int(self._sleeper.now() * MILLISECONDS)
