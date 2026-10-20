# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reads one Odoo company through the external API (``client.OdooApi``): every list is a ``search_read``
ordered by id and paged by offset, with the archived records too (``active_test`` off, so an archived record
is read and retired, not lost), and what a record's mapping needs assembled around it. Only fields the
database has are asked for (``fields_get``), so one reader serves Odoo 17, 18 and 19 and the apps a database
has installed; a model whose app isn't installed, or that the user may not read, is a KindUnavailable.

Every node carries its identity as ``id`` (Odoo's id as ``source_id``) and the company's currency as
``company_currency``. A many2one arrives as Odoo sends it (``[id, "display name"]`` or ``false``).

- Customers are the commercial partners (no parent) that are customers or not suppliers, suppliers those that
  are suppliers; the company's own partner and its users' partners are neither. Each arrives with its child
  partners (contacts and invoice, delivery and other addresses).
- Products are templates with their variants (``product.product``) and each variant's attribute values.
- Sales and purchase orders arrive with their lines; quotations and orders are one ``sale.order`` list read
  for both kinds (the mapping keeps each in its own).
- Journal items (``account.move``) arrive with their lines and, for a line the source reconciled, the
  documents it settles (``settles``: the counterpart moves through ``account.partial.reconcile``); customer
  invoices, vendor bills and journal entries are three filters of one model. A journal entry made by a
  payment to or from a partner is that payment's (a Payment Entry), so it is left out of the journals.
- Payments that post their own journal entry arrive with what their receivable or payable line settles.
- Leads and opportunities arrive with whether their partner is mirrored as a customer.

Every kind is incremental by ``write_date`` (UTC), except partners' children, whose changes Odoo doesn't
propagate to their parent: a full run catches those."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.odoo.client import OdooApi

SOURCE_ID = "odoo"
PAGE_SIZE = 200
ORDER = "id asc"
CONTEXT: dict[str, JsonValue] = {"active_test": False}
ODOO_TIME = "%Y-%m-%d %H:%M:%S"
COMPANY_SEPARATOR = ":"
SALES_MOVES = ("out_invoice", "out_refund", "out_receipt")
PURCHASE_MOVES = ("in_invoice", "in_refund", "in_receipt")
ENTRY = "entry"
RECONCILABLE = ("asset_receivable", "liability_payable")
# Odoo 17's company-wide outstanding receipts and payments accounts (18 keeps them on payment methods only).
COMPANY_PAYMENT_ACCOUNTS = (
	"account_journal_payment_debit_account_id",
	"account_journal_payment_credit_account_id",
)

type Domain = list[JsonValue]


@dataclass(frozen=True)
class Listing:
	"""One kind's ``search_read``: its model, the identity type it mints, the fields it wants (those the
	database has are read), and its filter."""

	model: str
	type: str
	fields: tuple[str, ...]
	domain: tuple[JsonValue, ...] = ()
	company_field: str | None = "company_id"


PARTNER_FIELDS = (
	"name",
	"is_company",
	"parent_id",
	"type",
	"street",
	"street2",
	"city",
	"state_id",
	"zip",
	"country_code",
	"email",
	"phone",
	"mobile",
	"vat",
	"ref",
	"website",
	"function",
	"active",
	"write_date",
)
MOVE_FIELDS = (
	"name",
	"move_type",
	"state",
	"partner_id",
	"commercial_partner_id",
	"invoice_date",
	"invoice_date_due",
	"date",
	"currency_id",
	"amount_total",
	"amount_total_signed",
	"ref",
	"narration",
	"reversed_entry_id",
	"journal_id",
	"write_date",
)
MOVE_LINE_FIELDS = (
	"move_id",
	"sequence",
	"display_type",
	"product_id",
	"name",
	"quantity",
	"price_unit",
	"discount",
	"price_subtotal",
	"account_id",
	"partner_id",
	"analytic_distribution",
	"debit",
	"credit",
	"amount_currency",
	"currency_id",
	"matched_debit_ids",
	"matched_credit_ids",
)
ORDER_LINE_FIELDS = (
	"order_id",
	"sequence",
	"display_type",
	"product_id",
	"name",
	"product_uom_qty",
	"product_qty",
	"price_unit",
	"discount",
	"price_subtotal",
	"price_tax",
	"price_total",
)
LISTINGS: dict[Kind, Listing] = {
	Kind.ACCOUNT: Listing(
		"account.account",
		"Account",
		("code", "name", "account_type", "currency_id", "reconcile", "deprecated", "active", "write_date"),
		company_field=None,
	),
	Kind.COST_CENTER: Listing(
		"account.analytic.account", "AnalyticAccount", ("name", "code", "plan_id", "active", "write_date")
	),
	Kind.WAREHOUSE: Listing(
		"stock.warehouse", "Warehouse", ("name", "code", "partner_id", "active", "write_date")
	),
	Kind.COLLECTION: Listing(
		"product.category", "Category", ("name", "parent_id", "write_date"), company_field=None
	),
	Kind.PRODUCT: Listing(
		"product.template",
		"Product",
		(
			"name",
			"default_code",
			"barcode",
			"list_price",
			"type",
			"detailed_type",
			"is_storable",
			"categ_id",
			"description_sale",
			"sale_ok",
			"purchase_ok",
			"active",
			"weight",
			"qty_available",
			"product_variant_count",
			"is_published",
			"write_date",
		),
		company_field=None,
	),
	Kind.CUSTOMER: Listing(
		"res.partner",
		"Partner",
		PARTNER_FIELDS,
		(["parent_id", "=", False], "|", ["customer_rank", ">", 0], ["supplier_rank", "=", 0]),
		company_field=None,
	),
	Kind.SUPPLIER: Listing(
		"res.partner",
		"Partner",
		PARTNER_FIELDS,
		(["parent_id", "=", False], ["supplier_rank", ">", 0]),
		company_field=None,
	),
	Kind.DEPARTMENT: Listing("hr.department", "Department", ("name", "parent_id", "active", "write_date")),
	Kind.DESIGNATION: Listing("hr.job", "Job", ("name", "active", "write_date")),
	Kind.EMPLOYEE: Listing(
		"hr.employee",
		"Employee",
		(
			"name",
			"gender",
			"sex",
			"birthday",
			"department_id",
			"job_id",
			"parent_id",
			"work_email",
			"private_email",
			"mobile_phone",
			"work_phone",
			"active",
			"departure_date",
			"contract_date_start",
			"write_date",
		),
	),
	Kind.PROJECT: Listing(
		"project.project",
		"Project",
		(
			"name",
			"partner_id",
			"date_start",
			"date",
			"active",
			"description",
			"last_update_status",
			"analytic_account_id",
			"account_id",
			"write_date",
		),
	),
	Kind.LEAD: Listing(
		"crm.lead",
		"CrmLead",
		(
			"name",
			"type",
			"partner_id",
			"partner_name",
			"contact_name",
			"email_from",
			"phone",
			"mobile",
			"function",
			"website",
			"probability",
			"expected_revenue",
			"date_deadline",
			"active",
			"description",
			"create_date",
			"write_date",
		),
	),
	Kind.SALES_ORDER: Listing(
		"sale.order",
		"SaleOrder",
		(
			"name",
			"state",
			"partner_id",
			"date_order",
			"commitment_date",
			"validity_date",
			"currency_id",
			"amount_untaxed",
			"amount_tax",
			"amount_total",
			"client_order_ref",
			"note",
			"write_date",
		),
	),
	Kind.PURCHASE_ORDER: Listing(
		"purchase.order",
		"PurchaseOrder",
		(
			"name",
			"state",
			"partner_id",
			"date_order",
			"date_planned",
			"currency_id",
			"amount_untaxed",
			"amount_tax",
			"amount_total",
			"partner_ref",
			"notes",
			"note",
			"write_date",
		),
	),
	Kind.SALES_INVOICE: Listing(
		"account.move", "Move", MOVE_FIELDS, (["move_type", "in", list(SALES_MOVES)],)
	),
	Kind.PURCHASE_INVOICE: Listing(
		"account.move", "Move", MOVE_FIELDS, (["move_type", "in", list(PURCHASE_MOVES)],)
	),
	Kind.JOURNAL_ENTRY: Listing("account.move", "Move", MOVE_FIELDS, (["move_type", "=", ENTRY],)),
	Kind.PAYMENT_ENTRY: Listing(
		"account.payment",
		"Payment",
		(
			"name",
			"payment_type",
			"partner_type",
			"partner_id",
			"amount",
			"currency_id",
			"date",
			"state",
			"ref",
			"memo",
			"move_id",
			"outstanding_account_id",
			"destination_account_id",
			"write_date",
		),
		(["partner_id", "!=", False], ["move_id", "!=", False]),
	),
}
# Opportunities are the same crm.lead records; quotations the same sale.order ones.
LISTINGS[Kind.OPPORTUNITY] = LISTINGS[Kind.LEAD]
LISTINGS[Kind.DRAFT_ORDER] = LISTINGS[Kind.SALES_ORDER]


def m2o_id(node: Node, field: str) -> int | None:
	"""A many2one's id: Odoo sends ``[id, "name"]``, or ``false`` when unset."""
	value = node.get(field)
	if isinstance(value, list) and value and isinstance(value[0], int) and not isinstance(value[0], bool):
		return value[0]
	if value is None or value is False:
		return None
	raise SourceError(f"{node.path}.{field} is not a many2one")


def m2o_name(node: Node, field: str) -> str | None:
	value = node.get(field)
	if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str):
		return value[1]
	return None


def ids_of(node: Node, field: str) -> list[int]:
	"""A one2many's or many2many's ids."""
	return node.integers(field)


def odoo_time(moment: datetime) -> str:
	return moment.astimezone(UTC).strftime(ODOO_TIME)


def company_of(config: ConnectionConfig) -> tuple[str, int | None]:
	"""The database and, for a multi-company database, the company's id: ``<database>[:<company id>]``."""
	database, _, company = config.account.strip().partition(COMPANY_SEPARATOR)
	if company and not company.isdigit():
		raise SourceError(
			"Odoo is not configured: the account is the database, then optionally :<company id>."
		)
	return database, int(company) if company else None


class OdooReader:
	def __init__(self, config: ConnectionConfig, api: OdooApi) -> None:
		self._config = config
		self._api = api
		_, self._company_id = company_of(config)
		self._fields: dict[str, frozenset[str]] = {}
		self._company: Node | None = None
		self._own: list[int] | None = None

	def is_incremental(self, kind: Kind) -> bool:
		return kind in LISTINGS

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		listing = self._listing(kind)
		offset = int(cursor) if cursor else 0
		domain = self._domain(kind, listing)
		if since is not None:
			domain.append(["write_date", ">=", odoo_time(since)])
		records = self._search(listing, domain, offset)
		nodes = self._assemble(kind, listing, records)
		following = str(offset + PAGE_SIZE) if len(records) == PAGE_SIZE else None
		return Page(nodes, following is not None, following)

	def node(self, kind: Kind, gid: str) -> Node | None:
		listing = self._listing(kind)
		source_id = Identity.parse(gid).id
		if not source_id.isdigit():
			raise SourceError(f"Not an Odoo identity: {gid}")
		domain: Domain = [*self._domain(kind, listing), ["id", "=", int(source_id)]]
		records = self._search(listing, domain, 0)
		return self._assemble(kind, listing, records)[0] if records else None

	# -- listing -----------------------------------------------------------------------------------------

	def _listing(self, kind: Kind) -> Listing:
		listing = LISTINGS.get(kind)
		if listing is None:
			raise SourceError(f"Odoo has no {kind.value}.")
		return listing

	def _domain(self, kind: Kind, listing: Listing) -> Domain:
		domain: Domain = list(listing.domain)
		if (
			listing.company_field
			and self._company_id is not None
			and listing.company_field in self._has(listing.model)
		):
			domain.append([listing.company_field, "in", [self._company_id, False]])
		if kind in (Kind.CUSTOMER, Kind.SUPPLIER):
			domain.append(["id", "not in", self._own_partners()])
		if kind is Kind.JOURNAL_ENTRY:
			# A partner's payment is a Payment Entry; its journal entry isn't mirrored again.
			payment = (
				"origin_payment_id" if "origin_payment_id" in self._has("account.move") else "payment_id"
			)
			domain += ["|", [payment, "=", False], [f"{payment}.partner_id", "=", False]]
		return domain

	def _search(self, listing: Listing, domain: Domain, offset: int) -> list[Node]:
		return self.search_read(listing.model, domain, listing.fields, offset, PAGE_SIZE)

	def search_read(
		self, model: str, domain: Domain, fields: Iterable[str], offset: int = 0, limit: int | None = None
	) -> list[Node]:
		params: dict[str, JsonValue] = {
			"domain": domain,
			"fields": [name for name in fields if name in self._has(model)],
			"offset": offset,
			"order": ORDER,
			"context": dict(CONTEXT),
		}
		if limit is not None:
			params["limit"] = limit
		return self._records(self._api.call(model, "search_read", None, params), model)

	def read(self, model: str, ids: Iterable[int], fields: Iterable[str]) -> list[Node]:
		wanted = sorted(set(ids))
		if not wanted:
			return []
		params: dict[str, JsonValue] = {
			"fields": [name for name in fields if name in self._has(model)],
			"context": dict(CONTEXT),
		}
		return self._records(self._api.call(model, "read", wanted, params), model)

	def _has(self, model: str) -> frozenset[str]:
		"""The fields the database's ``model`` has."""
		if model not in self._fields:
			described = self._api.call(model, "fields_get", None, {"attributes": ["type"]})
			if not isinstance(described, dict):
				raise SourceError(f"Odoo described no fields of {model}.")
			self._fields[model] = frozenset(described)
		return self._fields[model]

	def _records(self, answer: JsonValue, model: str) -> list[Node]:
		if not isinstance(answer, list) or not all(isinstance(record, dict) for record in answer):
			raise SourceError(f"Odoo answered {model} with no list of records.")
		return [
			Node(record, f"$.{model}[{index}]")
			for index, record in enumerate(answer)
			if isinstance(record, dict)
		]

	# -- the company -------------------------------------------------------------------------------------

	def _company_node(self) -> Node:
		if self._company is None:
			domain: Domain = [["id", "=", self._company_id]] if self._company_id is not None else []
			companies = self.search_read("res.company", domain, ("name", "currency_id", "partner_id"))
			if len(companies) != 1:
				raise SourceError(
					"Odoo is not configured: the user sees several companies; enter the account as "
					"<database>:<company id>."
					if companies
					else "Odoo has no such company."
				)
			self._company = companies[0]
		return self._company

	def company_currency(self) -> str:
		currency = m2o_name(self._company_node(), "currency_id")
		if currency is None:
			raise SourceError("Odoo's company has no currency.")
		return currency

	def _own_partners(self) -> list[JsonValue]:
		"""The company's partner and its users' partners: neither a customer nor a supplier."""
		if self._own is None:
			users = self.search_read("res.users", [], ("partner_id",))
			ids = {m2o_id(user, "partner_id") for user in users} | {
				m2o_id(self._company_node(), "partner_id")
			}
			self._own = sorted(i for i in ids if i is not None)
		return list[JsonValue](self._own)

	# -- assembly ----------------------------------------------------------------------------------------

	def _assemble(self, kind: Kind, listing: Listing, records: list[Node]) -> list[Node]:
		extra: dict[int, dict[str, JsonValue]] = {record.integer("id"): {} for record in records}
		match kind:
			case Kind.ACCOUNT:
				taxes, payments = self._tax_accounts(), self._payment_accounts()
				for record in records:
					extra[record.integer("id")] |= {
						"is_tax_account": record.integer("id") in taxes,
						"is_payment_account": record.integer("id") in payments,
					}
			case Kind.CUSTOMER | Kind.SUPPLIER:
				self._children(records, extra)
			case Kind.PRODUCT:
				self._variants(records, extra)
			case Kind.WAREHOUSE:
				self._addresses(records, "partner_id", extra)
			case Kind.SALES_ORDER | Kind.DRAFT_ORDER | Kind.PURCHASE_ORDER:
				self._order_lines(listing.model, records, extra)
			case Kind.SALES_INVOICE | Kind.PURCHASE_INVOICE | Kind.JOURNAL_ENTRY:
				self._move_lines(records, extra)
			case Kind.PAYMENT_ENTRY:
				self._payment_settlements(records, extra)
			case Kind.LEAD | Kind.OPPORTUNITY | Kind.PROJECT:
				self._parties(records, "partner_id", extra)
		currency = self.company_currency()
		return [
			self._identified(
				listing.type,
				record.integer("id"),
				{**record.raw(), **extra[record.integer("id")], "company_currency": currency},
			)
			for record in records
		]

	def _tax_accounts(self) -> set[int]:
		"""The accounts the company's taxes book to (ERPNext books a tax only to a Tax account)."""
		lines = self.search_read(
			"account.tax.repartition.line", [["account_id", "!=", False]], ("account_id",)
		)
		return {i for line in lines if (i := m2o_id(line, "account_id")) is not None}

	def _payment_accounts(self) -> set[int]:
		"""The outstanding receipts and payments accounts payments post to until the bank statement matches
		them (ERPNext pays only into or out of a Bank or Cash account): each payment method's, and in Odoo 17
		the company's defaults."""
		lines = self.search_read(
			"account.payment.method.line", [["payment_account_id", "!=", False]], ("payment_account_id",)
		)
		found = {i for line in lines if (i := m2o_id(line, "payment_account_id")) is not None}
		company = self._company_node()
		for field in COMPANY_PAYMENT_ACCOUNTS:
			if field in self._has("res.company"):
				account = self.read("res.company", [company.integer("id")], (field,))
				found |= {i for row in account if (i := m2o_id(row, field)) is not None}
		return found

	def _children(self, partners: list[Node], extra: dict[int, dict[str, JsonValue]]) -> None:
		ids = list[JsonValue](sorted(extra))
		if not ids:
			return
		children = self.search_read("res.partner", [["parent_id", "in", ids]], PARTNER_FIELDS)
		for child in children:
			parent = m2o_id(child, "parent_id")
			if parent in extra:
				extra[parent].setdefault("children", [])
				listed = extra[parent]["children"]
				if isinstance(listed, list):
					listed.append(child.raw())
		for partner in partners:
			extra[partner.integer("id")].setdefault("children", [])

	def _variants(self, templates: list[Node], extra: dict[int, dict[str, JsonValue]]) -> None:
		ids = list[JsonValue](sorted(extra))
		if not ids:
			return
		variants = self.search_read(
			"product.product",
			[["product_tmpl_id", "in", ids]],
			(
				"product_tmpl_id",
				"default_code",
				"barcode",
				"lst_price",
				"active",
				"qty_available",
				"weight",
				"product_template_attribute_value_ids",
			),
		)
		value_ids = {
			value for variant in variants for value in ids_of(variant, "product_template_attribute_value_ids")
		}
		values = {
			value.integer("id"): value.raw()
			for value in self.read("product.template.attribute.value", value_ids, ("name", "attribute_id"))
		}
		for template in templates:
			extra[template.integer("id")]["variants"] = []
		for variant in variants:
			template_id = m2o_id(variant, "product_tmpl_id")
			listed = extra.get(template_id or 0, {}).get("variants")
			if isinstance(listed, list):
				chosen: list[JsonValue] = [
					values[value]
					for value in ids_of(variant, "product_template_attribute_value_ids")
					if value in values
				]
				listed.append({**variant.raw(), "attribute_values": chosen})

	def _addresses(self, records: list[Node], field: str, extra: dict[int, dict[str, JsonValue]]) -> None:
		partner_ids = {i for record in records if (i := m2o_id(record, field)) is not None}
		partners = {
			partner.integer("id"): partner.raw()
			for partner in self.read("res.partner", partner_ids, PARTNER_FIELDS)
		}
		for record in records:
			partner = m2o_id(record, field)
			extra[record.integer("id")]["address"] = partners.get(partner) if partner else None

	def _parties(self, records: list[Node], field: str, extra: dict[int, dict[str, JsonValue]]) -> None:
		"""Each record's partner's commercial partner (a contact's company) as ``party_id``, and whether it is
		mirrored as a customer or a supplier (``party_is_customer``, ``party_is_supplier``: see their
		listings)."""
		partner_ids = {i for record in records if (i := m2o_id(record, field)) is not None}
		commercial = {
			partner.integer("id"): m2o_id(partner, "commercial_partner_id")
			for partner in self.read("res.partner", partner_ids, ("commercial_partner_id",))
		}
		own = set(self._own_partners())
		ranks = {
			partner.integer("id"): (
				partner.integer("id") not in own
				and (
					(partner.optional_integer("customer_rank") or 0) > 0
					or (partner.optional_integer("supplier_rank") or 0) == 0
				),
				partner.integer("id") not in own and (partner.optional_integer("supplier_rank") or 0) > 0,
			)
			for partner in self.read(
				"res.partner",
				{i for i in commercial.values() if i is not None},
				("customer_rank", "supplier_rank"),
			)
		}
		for record in records:
			partner = m2o_id(record, field)
			party = commercial.get(partner) if partner else None
			is_customer, is_supplier = ranks.get(party, (False, False)) if party else (False, False)
			extra[record.integer("id")] |= {
				"party_id": party,
				"party_is_customer": is_customer,
				"party_is_supplier": is_supplier,
			}

	def _order_lines(self, model: str, orders: list[Node], extra: dict[int, dict[str, JsonValue]]) -> None:
		self._parties(orders, "partner_id", extra)
		ids = list[JsonValue](sorted(extra))
		lines = self.search_read(f"{model}.line", [["order_id", "in", ids]], ORDER_LINE_FIELDS) if ids else []
		products = self._products(line for line in lines)
		for order in orders:
			extra[order.integer("id")]["lines"] = []
		for line in lines:
			listed = extra.get(m2o_id(line, "order_id") or 0, {}).get("lines")
			if isinstance(listed, list):
				listed.append({**line.raw(), "product": products.get(m2o_id(line, "product_id") or 0)})

	def _products(self, lines: Iterable[Node]) -> dict[int, JsonValue]:
		"""The template of each product a line names, and whether it has several variants."""
		product_ids = {m2o_id(line, "product_id") for line in lines} - {None}
		return {
			product.integer("id"): {
				"template_id": m2o_id(product, "product_tmpl_id"),
				"variant_count": product.optional_integer("product_variant_count") or 1,
			}
			for product in self.read(
				"product.product",
				(i for i in product_ids if i is not None),
				("product_tmpl_id", "product_variant_count"),
			)
		}

	def _move_lines(self, moves: list[Node], extra: dict[int, dict[str, JsonValue]]) -> None:
		ids = list[JsonValue](sorted(extra))
		lines = (
			self.search_read("account.move.line", [["move_id", "in", ids]], MOVE_LINE_FIELDS) if ids else []
		)
		products = self._products(lines)
		settles = self._settlements(lines)
		accounts = self._account_types(lines)
		for move in moves:
			extra[move.integer("id")]["lines"] = []
		for line in lines:
			listed = extra.get(m2o_id(line, "move_id") or 0, {}).get("lines")
			if isinstance(listed, list):
				listed.append(
					{
						**line.raw(),
						"product": products.get(m2o_id(line, "product_id") or 0),
						"account_type": accounts.get(m2o_id(line, "account_id") or 0),
						"settles": settles.get(line.integer("id"), []),
					}
				)

	def _account_types(self, lines: list[Node]) -> dict[int, str]:
		account_ids = {m2o_id(line, "account_id") for line in lines} - {None}
		return {
			account.integer("id"): account.string("account_type")
			for account in self.read(
				"account.account", (i for i in account_ids if i is not None), ("account_type",)
			)
		}

	def _settlements(self, lines: list[Node]) -> dict[int, list[JsonValue]]:
		"""What each reconciled line settles: the counterpart moves (their id and type) and the amount (in
		the company's currency) of each partial reconciliation."""
		partial_ids = {
			partial
			for line in lines
			for partial in (*ids_of(line, "matched_debit_ids"), *ids_of(line, "matched_credit_ids"))
		}
		partials = self.read(
			"account.partial.reconcile", partial_ids, ("debit_move_id", "credit_move_id", "amount")
		)
		line_ids = {line.integer("id") for line in lines}
		counterpart_ids = {
			other
			for partial in partials
			for other in (m2o_id(partial, "debit_move_id"), m2o_id(partial, "credit_move_id"))
			if other is not None
		}
		counterparts = {
			line.integer("id"): m2o_id(line, "move_id")
			for line in self.read("account.move.line", counterpart_ids, ("move_id",))
		}
		move_ids = {move for move in counterparts.values() if move is not None}
		move_types = {
			move.integer("id"): move.string("move_type")
			for move in self.read("account.move", move_ids, ("move_type",))
		}
		settled: dict[int, list[JsonValue]] = {}
		for partial in partials:
			debit, credit = m2o_id(partial, "debit_move_id"), m2o_id(partial, "credit_move_id")
			for own, other in ((debit, credit), (credit, debit)):
				if own in line_ids and other is not None and other in counterparts:
					move = counterparts[other]
					settled.setdefault(own, []).append(
						{
							"move": move,
							"move_type": move_types.get(move or 0),
							"amount": partial.get("amount"),
						}
					)
		return settled

	def _payment_settlements(self, payments: list[Node], extra: dict[int, dict[str, JsonValue]]) -> None:
		"""The receivable or payable line of each payment's journal entry, and what it settles."""
		move_ids = {m2o_id(payment, "move_id"): payment.integer("id") for payment in payments}
		moves = list[JsonValue](sorted(i for i in move_ids if i is not None))
		lines = (
			self.search_read("account.move.line", [["move_id", "in", moves]], MOVE_LINE_FIELDS)
			if moves
			else []
		)
		accounts = self._account_types(lines)
		reconcilable = [
			line for line in lines if accounts.get(m2o_id(line, "account_id") or 0) in RECONCILABLE
		]
		settles = self._settlements(reconcilable)
		for payment in payments:
			extra[payment.integer("id")]["settles"] = []
		for line in reconcilable:
			payment_id = move_ids.get(m2o_id(line, "move_id"))
			listed = extra.get(payment_id or 0, {}).get("settles")
			if isinstance(listed, list):
				listed += settles.get(line.integer("id"), [])

	def _identified(self, type_: str, id_: int, raw: Mapping[str, JsonValue]) -> Node:
		gid = self._config.identity(type_, id_)
		return Node({**raw, "source_id": str(id_), "id": gid}, gid)
