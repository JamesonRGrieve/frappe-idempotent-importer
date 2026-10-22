# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one Wave business through Wave's public GraphQL API (gql.waveapps.com/graphql/public): its chart of
accounts, sales taxes, products and services, customers, vendors, estimates, invoices, and the payments
recorded against its invoices (operations.py). Wave's public API exposes no read of its other transactions
(bills, expenses, journal transactions: ``moneyTransactionCreate`` only writes them).

Every list is a page at a time (``page``, ``pageSize`` 45, Wave's default; ``pageInfo.totalPages``), oldest
first where it can be ordered so a record created mid-run doesn't shift the pages. Incremental runs ask for
what changed since the last run (``modifiedAtAfter``) of the lists that take it; the chart of accounts is read
whole and skipped by its hash. A payment is read with its invoice, so an incremental run reads the payments of
the invoices that changed.

Wave's ids are opaque global ids (base64 of ``Business:<uuid>;<Type>:<id>``); an identity carries the record's
own id (the part after its type), else the global id made path-safe. A GraphQL error answer is a failed read:
one Wave marks unauthenticated holds the connection like any rejected credential."""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime

from idempotent_importer.core.errors import CredentialsRejected, SourceError
from idempotent_importer.core.http import RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.wave import operations
from idempotent_importer.sources.wave.operations import Operation

SOURCE_ID = "wave"
API_HOST = "https://gql.waveapps.com"
API_PATH = "/graphql/public"
PAGE_SIZE = 45
REQUESTS_PER_SECOND = 2.0
RATE_LIMIT = RateLimit(requests_per_second=REQUESTS_PER_SECOND)
CHANGED_SINCE = "%Y-%m-%dT%H:%M:%SZ"
UNAUTHENTICATED = frozenset({"UNAUTHENTICATED", "FORBIDDEN"})
KIND_OPERATIONS: dict[Kind, tuple[Operation, str]] = {
	Kind.ACCOUNT: (operations.ACCOUNTS, "Account"),
	Kind.TAX_RATE: (operations.SALES_TAXES, "SalesTax"),
	Kind.PRODUCT: (operations.PRODUCTS, "Product"),
	Kind.CUSTOMER: (operations.CUSTOMERS, "Customer"),
	Kind.SUPPLIER: (operations.VENDORS, "Vendor"),
	Kind.DRAFT_ORDER: (operations.ESTIMATES, "Estimate"),
	Kind.SALES_INVOICE: (operations.INVOICES, "Invoice"),
}
KINDS: tuple[Kind, ...] = (*KIND_OPERATIONS, Kind.PAYMENT_ENTRY)
INCREMENTAL = frozenset(
	[kind for kind, (operation, _) in KIND_OPERATIONS.items() if operation.changed] + [Kind.PAYMENT_ENTRY]
)


def local_id(global_id: str) -> str:
	"""A Wave global id's own id: the part after its type in ``Business:<uuid>;<Type>:<id>``, else the
	global id with base64's ``/`` and ``+`` made path-safe."""
	try:
		decoded = base64.b64decode(global_id, validate=True).decode()
	except binascii.Error, UnicodeDecodeError:
		decoded = ""
	own = decoded.rpartition(";")[2].partition(":")[2]
	return own or global_id.replace("/", "_").replace("+", "-")


def graphql(client: RestClient, operation: Operation, variables: dict[str, JsonValue]) -> Node:
	"""One operation's ``data``; a GraphQL error answer raises."""
	body: dict[str, JsonValue] = {
		"operationName": operation.name,
		"query": operation.document,
		"variables": variables,
	}
	result = client.query(API_PATH, body)
	if result is None:
		raise SourceError("Wave has no public GraphQL API at this address.")
	errors = result.node.children("errors")
	if errors:
		codes = {
			extensions.optional_string("code")
			for error in errors
			if (extensions := error.child("extensions")) is not None
		}
		messages = "; ".join(error.optional_string("message") or "an error" for error in errors)
		if codes & UNAUTHENTICATED:
			raise CredentialsRejected(f"Wave refused {operation.name}: {messages}")
		raise SourceError(f"Wave answered {operation.name} with errors: {messages}")
	return result.node.required_child("data")


class WaveReader:
	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		if not config.account:
			raise SourceError("Wave is not connected: use Connect to Wave to choose the business.")
		self._config = config
		self._client = client

	def is_incremental(self, kind: Kind) -> bool:
		return kind in INCREMENTAL

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		if kind is Kind.PAYMENT_ENTRY:
			return self._payments(cursor, since)
		found = KIND_OPERATIONS.get(kind)
		if found is None:
			raise SourceError(f"Wave has no {kind.value}.")
		operation, type_ = found
		records, more, next_page = self._listed(operation, cursor, since)
		return Page([self._identified(type_, record) for record in records], more, next_page)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		raise SourceError(f"Wave can't read a {identity.type} by itself.")

	def _listed(
		self, operation: Operation, cursor: str | None, since: datetime | None
	) -> tuple[list[dict[str, JsonValue]], bool, str | None]:
		page = int(cursor) if cursor else 1
		variables: dict[str, JsonValue] = {
			"business": self._config.account,
			"page": page,
			"pageSize": PAGE_SIZE,
		}
		if operation.changed and since is not None:
			variables["since"] = since.astimezone(UTC).strftime(CHANGED_SINCE)
		business = graphql(self._client, operation, variables).child("business")
		if business is None:
			raise CredentialsRejected("Wave didn't open the business: connect it again.")
		listed = business.required_child(operation.field)
		info = listed.required_child("pageInfo")
		records = [
			node.raw() for edge in listed.children("edges") if (node := edge.child("node")) is not None
		]
		more = page < (info.optional_integer("totalPages") or 1)
		return records, more, str(page + 1) if more else None

	def _payments(self, cursor: str | None, since: datetime | None) -> Page:
		"""The payments recorded against each invoice, each carrying its invoice, customer and currency."""
		invoices, more, next_page = self._listed(operations.INVOICE_PAYMENTS, cursor, since)
		nodes: list[Node] = []
		for invoice in invoices:
			found = Node(invoice)
			for payment in found.children("payments"):
				extra: dict[str, JsonValue] = {
					"invoice": found.string("id"),
					"invoice_customer": invoice.get("customer"),
					"invoice_currency": invoice.get("currency"),
				}
				nodes.append(self._identified("InvoicePayment", payment.raw() | extra))
		return Page(nodes, more, next_page)

	def _identified(self, type_: str, record: dict[str, JsonValue]) -> Node:
		global_id = record.get("id")
		if not isinstance(global_id, str) or not global_id:
			raise SourceError(f"A Wave {type_} has no id.")
		gid = self._config.identity(type_, local_id(global_id))
		return Node({**record, "id": gid, "source_id": global_id}, gid)
