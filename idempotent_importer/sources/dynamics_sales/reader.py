# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one Dynamics 365 Sales environment through the Dataverse Web API (``<org>/api/data/v9.2``, OData
v4): each table's columns the adapter maps (``$select``), lists paged by ``@odata.nextLink`` (200 at a time,
``Prefer: odata.maxpagesize``), lookups annotated with the table they name (``Prefer:
odata.include-annotations``: a customer is an account or a contact), an account with its contacts and a
document with its lines (``$expand``). Customers are accounts, then contacts (the cursor is ``<entity
set>|<next page>``). Every node carries its identity as ``id`` (the row's id as ``source_id``), the
organisation's base currency as ``company_currency`` and its own currency's ISO code as ``currency``. Every
table is incremental by ``modifiedon``."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import QueryValue
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.dynamics.odata import OData

SOURCE_ID = "dynamics_sales"
API_PATH = "/api/data/v9.2"
ORGANISATION_URL = re.compile(r"^https://[a-z0-9-]+\.crm[0-9]*\.dynamics\.com/?$", re.IGNORECASE)
PAGE_SIZE = 200
PHASE_SEPARATOR = "|"
MODIFIED = "modifiedon"
CURRENCY_LOOKUP = "_transactioncurrencyid_value"
ANNOTATIONS = 'odata.include-annotations="Microsoft.Dynamics.CRM.lookuplogicalname"'

# The columns read of each table, and of each document's lines (the schema in tests/schemas follows them).
ACCOUNT_COLUMNS = (
	"accountid",
	"name",
	"accountnumber",
	"emailaddress1",
	"telephone1",
	"websiteurl",
	"address1_line1",
	"address1_line2",
	"address1_city",
	"address1_stateorprovince",
	"address1_postalcode",
	"address1_country",
	"statecode",
	"_transactioncurrencyid_value",
	"modifiedon",
)
CONTACT_COLUMNS = (
	"contactid",
	"firstname",
	"middlename",
	"lastname",
	"fullname",
	"jobtitle",
	"emailaddress1",
	"telephone1",
	"mobilephone",
	"address1_line1",
	"address1_line2",
	"address1_city",
	"address1_stateorprovince",
	"address1_postalcode",
	"address1_country",
	"_parentcustomerid_value",
	"statecode",
	"modifiedon",
)
LEAD_COLUMNS = (
	"leadid",
	"subject",
	"firstname",
	"lastname",
	"companyname",
	"jobtitle",
	"emailaddress1",
	"telephone1",
	"mobilephone",
	"websiteurl",
	"description",
	"statecode",
	"modifiedon",
)
OPPORTUNITY_COLUMNS = (
	"opportunityid",
	"name",
	"_customerid_value",
	"_originatingleadid_value",
	"estimatedvalue",
	"closeprobability",
	"estimatedclosedate",
	"description",
	"statecode",
	"_transactioncurrencyid_value",
	"createdon",
	"modifiedon",
)
PRODUCT_COLUMNS = (
	"productid",
	"name",
	"productnumber",
	"description",
	"price",
	"quantityonhand",
	"producttypecode",
	"productstructure",
	"statecode",
	"modifiedon",
)
DOCUMENT_COLUMNS = (
	"name",
	"_customerid_value",
	"_transactioncurrencyid_value",
	"exchangerate",
	"totallineitemamount",
	"discountamount",
	"discountpercentage",
	"totaldiscountamount",
	"freightamount",
	"totaltax",
	"totalamount",
	"totalamount_base",
	"description",
	"statecode",
	"createdon",
	"modifiedon",
)
LINE_COLUMNS = (
	"_productid_value",
	"productdescription",
	"isproductoverridden",
	"quantity",
	"priceperunit",
	"baseamount",
	"manualdiscountamount",
	"volumediscountamount",
	"tax",
	"extendedamount",
	"sequencenumber",
)


@dataclass(frozen=True)
class Table:
	"""One table the adapter lists: its entity set, the identity type it mints, its columns, and what it
	expands (a document's lines, an account's contacts)."""

	entity_set: str
	type: str
	columns: tuple[str, ...]
	expand: str | None = None


def lines_of(navigation: str, key: str) -> str:
	return f"{navigation}($select={','.join((key, *LINE_COLUMNS))})"


TABLES: dict[Kind, tuple[Table, ...]] = {
	Kind.PRODUCT: (Table("products", "Product", PRODUCT_COLUMNS),),
	Kind.CUSTOMER: (
		Table(
			"accounts",
			"Account",
			ACCOUNT_COLUMNS,
			"contact_customer_accounts($select=contactid,fullname,jobtitle,emailaddress1,telephone1,statecode)",
		),
		Table("contacts", "Contact", CONTACT_COLUMNS),
	),
	Kind.LEAD: (Table("leads", "Lead", LEAD_COLUMNS),),
	Kind.OPPORTUNITY: (Table("opportunities", "Opportunity", OPPORTUNITY_COLUMNS),),
	Kind.DRAFT_ORDER: (
		Table(
			"quotes",
			"Quote",
			("quoteid", "quotenumber", "effectivefrom", *DOCUMENT_COLUMNS),
			lines_of("quote_details", "quotedetailid"),
		),
	),
	Kind.SALES_ORDER: (
		Table(
			"salesorders",
			"SalesOrder",
			("salesorderid", "ordernumber", "datefulfilled", "requestdeliveryby", *DOCUMENT_COLUMNS),
			lines_of("order_details", "salesorderdetailid"),
		),
	),
	Kind.SALES_INVOICE: (
		Table(
			"invoices",
			"Invoice",
			("invoiceid", "invoicenumber", "duedate", *DOCUMENT_COLUMNS),
			lines_of("invoice_details", "invoicedetailid"),
		),
	),
}


def organisation_of(config: ConnectionConfig) -> str:
	"""The environment's URL (``https://<org>.crm<n>.dynamics.com``)."""
	url = config.store_url.strip().rstrip("/")
	if not ORGANISATION_URL.match(url):
		raise SourceError(
			"Dynamics 365 Sales is not configured: enter the environment's URL, https://<org>.crm.dynamics.com."
		)
	return url


def api_base(config: ConnectionConfig) -> str:
	return (config.endpoint_override or organisation_of(config)).rstrip("/") + API_PATH


def odata_time(moment: datetime) -> str:
	return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class DataverseReader:
	def __init__(self, config: ConnectionConfig, odata: OData) -> None:
		self._config = config
		self._odata = odata
		self._currencies: dict[str, str] | None = None
		self._base_currency: str | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in TABLES

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		tables = self._tables(kind)
		name, _, path = (cursor or f"{tables[0].entity_set}{PHASE_SEPARATOR}").partition(PHASE_SEPARATOR)
		index = next(i for i, table in enumerate(tables) if table.entity_set == name)
		table = tables[index]
		page = self._odata.page(path or self._odata.first(f"/{table.entity_set}", self._query(table, since)))
		nodes = [self._assemble(table, node) for node in page.nodes]
		if page.next_path:
			following: str | None = f"{table.entity_set}{PHASE_SEPARATOR}{page.next_path}"
		elif index + 1 < len(tables):
			following = f"{tables[index + 1].entity_set}{PHASE_SEPARATOR}"
		else:
			following = None
		return Page(nodes, following is not None, following)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		table = next((t for t in self._tables(kind) if t.type == identity.type), None)
		if table is None:
			raise SourceError(f"Not a Dynamics 365 Sales {kind.value} identity: {gid}")
		query = self._query(table, None)
		query["$filter"] = f"{table.columns[0]} eq {identity.id}"
		found = self._odata.all(f"/{table.entity_set}", query)
		return self._assemble(table, found[0]) if found else None

	def _tables(self, kind: Kind) -> tuple[Table, ...]:
		tables = TABLES.get(kind)
		if tables is None:
			raise SourceError(f"Dynamics 365 Sales has no {kind.value}.")
		return tables

	def _query(self, table: Table, since: datetime | None) -> dict[str, QueryValue]:
		query: dict[str, QueryValue] = {"$select": ",".join(table.columns)}
		if table.expand:
			query["$expand"] = table.expand
		if since is not None:
			query["$filter"] = f"{MODIFIED} gt {odata_time(since)}"
		return query

	def _assemble(self, table: Table, node: Node) -> Node:
		currency_id = node.optional_string(CURRENCY_LOOKUP)
		extra: dict[str, JsonValue] = {
			"company_currency": self.base_currency(),
			"currency": self._currency_codes().get(currency_id) if currency_id else None,
		}
		key = node.string(table.columns[0])
		gid = self._config.identity(table.type, key)
		return Node({**node.raw(), **extra, "source_id": key, "id": gid}, gid)

	def _currency_codes(self) -> Mapping[str, str]:
		if self._currencies is None:
			self._currencies = {
				currency.string("transactioncurrencyid"): currency.string("isocurrencycode")
				for currency in self._odata.all(
					"/transactioncurrencies",
					{"$select": "transactioncurrencyid,isocurrencycode,currencyname"},
				)
			}
		return self._currencies

	def base_currency(self) -> str:
		"""The organisation's base currency, which every money column's ``_base`` twin is in."""
		if self._base_currency is None:
			organisations = self._odata.all(
				"/organizations", {"$select": "organizationid,name,_basecurrencyid_value"}
			)
			base = organisations[0].optional_string("_basecurrencyid_value") if organisations else None
			code = self._currency_codes().get(base) if base else None
			if code is None:
				raise SourceError("Dynamics 365 Sales' organisation names no base currency.")
			self._base_currency = code
		return self._base_currency
