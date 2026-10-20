# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one Business Central company through API v2.0 (``<environment>/api/v2.0/companies(<id>)/…``, OData
v4): every list paged by the server's ``@odata.nextLink`` (asked for 200 at a time with ``Prefer:
odata.maxpagesize``), a document with its lines (``$expand``), an item with its variants. A kind read from
two entity sets (invoices and credit memos, cost centers' dimensions and their values) reads one, then the
other; the cursor is ``<entity set>|<next page>``. Every node carries its identity as ``id`` (Business
Central's id as ``source_id``) and the company's local currency as ``company_currency``; an opportunity, the
identity of its contact. Every kind with ``lastModifiedDateTime`` is incremental by it; dimensions,
projects and the company aren't filtered (the API doesn't filter dimensions by it)."""

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

SOURCE_ID = "dynamics_business_central"
API_HOST = "https://api.businesscentral.dynamics.com"
STORE_URL = re.compile(
	r"^https://api\.businesscentral\.dynamics\.com/v2\.0/([^/]+)/([^/]+)/?$", re.IGNORECASE
)
COMPANY_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PAGE_SIZE = 200
PHASE_SEPARATOR = "|"
LAST_MODIFIED = "lastModifiedDateTime"


@dataclass(frozen=True)
class EntitySet:
	"""One list of the API: its path under the company, the identity type it mints, what it expands, and
	whether it filters by modification time."""

	path: str
	type: str
	expand: str | None = None
	incremental: bool = True


# Each kind's entity sets, read in this order.
ENTITY_SETS: dict[Kind, tuple[EntitySet, ...]] = {
	Kind.ACCOUNT: (EntitySet("accounts", "Account"),),
	Kind.COST_CENTER: (
		EntitySet("dimensions", "Dimension", incremental=False),
		EntitySet("dimensionValues", "DimensionValue"),
	),
	Kind.WAREHOUSE: (EntitySet("locations", "Location"),),
	Kind.COLLECTION: (EntitySet("itemCategories", "ItemCategory"),),
	Kind.PRODUCT: (EntitySet("items", "Item", "itemVariants"),),
	Kind.CUSTOMER: (EntitySet("customers", "Customer"),),
	Kind.SUPPLIER: (EntitySet("vendors", "Vendor"),),
	Kind.EMPLOYEE: (EntitySet("employees", "Employee"),),
	Kind.PROJECT: (EntitySet("projects", "Project"),),
	Kind.LEAD: (EntitySet("contacts", "Contact"),),
	Kind.OPPORTUNITY: (EntitySet("opportunities", "Opportunity"),),
	Kind.DRAFT_ORDER: (EntitySet("salesQuotes", "SalesQuote", "salesQuoteLines"),),
	Kind.SALES_ORDER: (EntitySet("salesOrders", "SalesOrder", "salesOrderLines"),),
	Kind.PURCHASE_ORDER: (EntitySet("purchaseOrders", "PurchaseOrder", "purchaseOrderLines"),),
	Kind.SALES_INVOICE: (
		EntitySet("salesInvoices", "SalesInvoice", "salesInvoiceLines"),
		EntitySet("salesCreditMemos", "SalesCreditMemo", "salesCreditMemoLines"),
	),
	Kind.PURCHASE_INVOICE: (
		EntitySet("purchaseInvoices", "PurchaseInvoice", "purchaseInvoiceLines"),
		EntitySet("purchaseCreditMemos", "PurchaseCreditMemo", "purchaseCreditMemoLines"),
	),
}


def environment_of(config: ConnectionConfig) -> tuple[str, str]:
	"""The tenant and environment the connection's URL names."""
	match = STORE_URL.match(config.store_url.strip())
	if match is None:
		raise SourceError(
			"Business Central is not configured: enter the environment's API URL, "
			"https://api.businesscentral.dynamics.com/v2.0/<tenant>/<environment>."
		)
	return match.group(1), match.group(2)


def company_base(config: ConnectionConfig) -> str:
	"""The company's API v2.0 root."""
	tenant, environment = environment_of(config)
	company = config.account.strip()
	if not COMPANY_ID.match(company):
		raise SourceError(
			"Business Central is not configured: enter the company's id (a GUID) as the account."
		)
	host = (config.endpoint_override or API_HOST).rstrip("/")
	return f"{host}/v2.0/{tenant}/{environment}/api/v2.0/companies({company})"


def odata_time(moment: datetime) -> str:
	return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class BusinessCentralReader:
	def __init__(self, config: ConnectionConfig, odata: OData) -> None:
		self._config = config
		self._odata = odata
		self._currency: str | None = None
		self._contacts: dict[str, str] | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in ENTITY_SETS

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		sets = self._sets(kind)
		if cursor:
			name, _, path = cursor.partition(PHASE_SEPARATOR)
			index = next(i for i, entity_set in enumerate(sets) if entity_set.path == name)
		else:
			index, path = 0, ""
		entity_set = sets[index]
		page = self._odata.page(
			path or self._odata.first(f"/{entity_set.path}", self._query(entity_set, since))
		)
		nodes = [self._assemble(kind, entity_set, node) for node in page.nodes]
		if page.next_path:
			following: str | None = f"{entity_set.path}{PHASE_SEPARATOR}{page.next_path}"
		elif index + 1 < len(sets):
			following = f"{sets[index + 1].path}{PHASE_SEPARATOR}"
		else:
			following = None
		return Page(nodes, following is not None, following)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		entity_set = next((s for s in self._sets(kind) if s.type == identity.type), None)
		if entity_set is None or not COMPANY_ID.match(identity.id):
			raise SourceError(f"Not a Business Central {kind.value} identity: {gid}")
		query: dict[str, QueryValue] = {"$filter": f"id eq {identity.id}"}
		if entity_set.expand:
			query["$expand"] = entity_set.expand
		found = self._odata.all(f"/{entity_set.path}", query)
		return self._assemble(kind, entity_set, found[0]) if found else None

	def _sets(self, kind: Kind) -> tuple[EntitySet, ...]:
		sets = ENTITY_SETS.get(kind)
		if sets is None:
			raise SourceError(f"Business Central has no {kind.value}.")
		return sets

	def _query(self, entity_set: EntitySet, since: datetime | None) -> dict[str, QueryValue]:
		query: dict[str, QueryValue] = {}
		if entity_set.expand:
			query["$expand"] = entity_set.expand
		if since is not None and entity_set.incremental:
			query["$filter"] = f"{LAST_MODIFIED} gt {odata_time(since)}"
		return query

	def _assemble(self, kind: Kind, entity_set: EntitySet, node: Node) -> Node:
		extra: dict[str, JsonValue] = {"company_currency": self.company_currency()}
		if kind is Kind.OPPORTUNITY:
			contact = self._contact_ids().get(node.text("contactNumber"))
			extra["contact"] = self._config.identity("Contact", contact) if contact else None
		gid = self._config.identity(entity_set.type, node.string("id"))
		return Node({**node.raw(), **extra, "source_id": node.string("id"), "id": gid}, gid)

	def company_currency(self) -> str:
		"""The company's local currency (``companyInformation.currencyCode``); a document in it names no
		currency."""
		if self._currency is None:
			information = self._odata.all("/companyInformation", {})
			if not information or not information[0].optional_string("currencyCode"):
				raise SourceError("Business Central's company information names no currency.")
			self._currency = information[0].string("currencyCode")
		return self._currency

	def _contact_ids(self) -> Mapping[str, str]:
		"""Each contact's id by its number (an opportunity names its contact by number)."""
		if self._contacts is None:
			self._contacts = {
				contact.string("number"): contact.string("id")
				for contact in self._odata.all("/contacts", {"$select": "id,number"})
			}
		return self._contacts
