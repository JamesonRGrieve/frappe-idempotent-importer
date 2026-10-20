# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Odoo → desired ERPNext state, one mapper per kind, in the shared field contract. Odoo sends an
unset field as ``false``, a many2one as ``[id, "name"]``, dates as ``YYYY-MM-DD`` and times as UTC
``YYYY-MM-DD HH:MM:SS``, and amounts as JSON numbers (read exactly as spelled). Odoo's own vocabulary
(account types, move types, states, partner types) is translated here.

Journal items follow Odoo's signs: an invoice's lines are credits (``amount_currency`` negative) and a vendor
bill's debits, so a line's amount on its document is ``-amount_currency`` for sales and ``amount_currency``
for purchases; a credit note's come out negative, as an ERPNext return's are. Archived records stay live
(disabled where ERPNext can disable them), since the history that names them must still book."""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

from idempotent_importer.contract import (
	CUSTOMER_PARTY,
	PAY,
	RECEIVE,
	SUPPLIER_PARTY,
	Address,
	Allocation,
	Charge,
	ContactPerson,
	Employee,
	JournalLine,
	Lead,
	LedgerAccount,
	Opportunity,
	Party,
	Payment,
	TradeDocument,
	TradeLine,
	TradeTax,
	account_fields,
	address_fields,
	charge_fields,
	employee_fields,
	journal_fields,
	lead_fields,
	line_fields,
	opportunity_fields,
	order_line,
	party_fields,
	payment_fields,
	project_fields,
	trade_fields,
	tree_fields,
	variant_attributes,
	warehouse_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.paths import slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.core.times import iso_utc
from idempotent_importer.naming import plain_text
from idempotent_importer.sources.common import plain_html
from idempotent_importer.sources.odoo.reader import PURCHASE_MOVES, SALES_MOVES, m2o_id, m2o_name

# Odoo's account types → ERPNext's root type and account type (None: an ordinary account).
ACCOUNT_TYPES: dict[str, tuple[str, str | None]] = {
	"asset_receivable": ("Asset", "Receivable"),
	"asset_cash": ("Asset", "Bank"),
	"asset_current": ("Asset", None),
	"asset_non_current": ("Asset", None),
	"asset_prepayments": ("Asset", None),
	"asset_fixed": ("Asset", "Fixed Asset"),
	"liability_payable": ("Liability", "Payable"),
	"liability_credit_card": ("Liability", "Bank"),
	"liability_current": ("Liability", None),
	"liability_non_current": ("Liability", None),
	"equity": ("Equity", None),
	"equity_unaffected": ("Equity", None),
	"income": ("Income", "Income Account"),
	"income_other": ("Income", "Income Account"),
	"expense": ("Expense", "Expense Account"),
	"expense_other": ("Expense", "Expense Account"),
	"expense_depreciation": ("Expense", "Depreciation"),
	"expense_direct_cost": ("Expense", "Cost of Goods Sold"),
}
OFF_BALANCE = "off_balance"
# A tax account keeps its own type when it has one that matters more (a receivable never books a tax).
TAX, BANK = "Tax", "Bank"
RECEIVABLE, PAYABLE = "asset_receivable", "liability_payable"
COMPANY = "company"
CONTACT, INVOICE_ADDRESS, DELIVERY_ADDRESS = "contact", "invoice", "delivery"
ADDRESS_TYPES = {INVOICE_ADDRESS: "Billing", DELIVERY_ADDRESS: "Shipping"}
OTHER_ADDRESS = "Other"
GENDERS = {"male": "Male", "female": "Female", "other": "Other"}
PROJECT_DONE, PROJECT_ON_HOLD = "done", "on_hold"
OPPORTUNITY = "opportunity"
WON_PROBABILITY = Decimal(100)
QUOTATION_STATES = frozenset({"draft", "sent"})
SALE_STATES = frozenset({"sale", "done"})
PURCHASE_DRAFT_STATES = frozenset({"draft", "sent", "to approve"})
PURCHASE_STATES = frozenset({"purchase", "done"})
POSTED, DRAFT = "posted", "draft"
# Payment states: 17's are its journal entry's; 18 and 19 have their own.
PAYMENT_BOOKED = frozenset({"posted", "in_process", "paid"})
INBOUND = "inbound"
PRODUCT_LINE = "product"
# Lines that only lay a document out, or post what ERPNext books elsewhere: the receivable or payable
# total (the document's party account) and anglo-saxon cost of goods sold (stock is mirrored at zero value).
LAYOUT_LINES = frozenset({"line_section", "line_subsection", "line_note"})
PARTY_LINE, COGS_LINE = "payment_term", "cogs"
SETTLED_KINDS = {
	**dict.fromkeys(SALES_MOVES, Kind.SALES_INVOICE),
	**dict.fromkeys(PURCHASE_MOVES, Kind.PURCHASE_INVOICE),
}
RETURNS = frozenset({"out_refund", "in_refund"})
FULL_SHARE = Decimal(100)
STORABLE = "product"
WEIGHT_UNIT = "kg"
EXCHANGE_RATE_PLACES = 9
DATE_LENGTH = len("YYYY-MM-DD")


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, type_: str, id_: int | None) -> str | None:
		return None if id_ is None else self.config.identity(type_, id_)

	def required(self, type_: str, id_: int | None, what: str) -> str:
		gid = self.gid(type_, id_)
		if gid is None:
			raise Unsupported(f"it has no {what}")
		return gid


def text(node: Node, field: str) -> str | None:
	"""A char or text field: Odoo sends ``false`` (or nothing) when it is empty."""
	value = node.get(field)
	if value is False or value is None or value == "":
		return None
	if not isinstance(value, str):
		raise SourceError(f"{node.path}.{field} is not text")
	return value.strip() or None


def required_text(node: Node, field: str) -> str:
	value = text(node, field)
	if value is None:
		raise SourceError(f"{node.path}.{field} is missing")
	return value


def html_text(node: Node, field: str) -> str | None:
	"""An Html field (a note, a description) as plain text."""
	return plain_text(text(node, field)) or None


def number(node: Node, field: str) -> Decimal:
	"""A numeric field (Odoo sends floats; ``false`` is never a number but some computed fields send it as
	zero)."""
	value = node.get(field)
	return Decimal(0) if value is False else node.decimal(field)


def flag(node: Node, field: str) -> bool:
	return node.get(field) is True


def date_of(node: Node, field: str) -> str | None:
	"""A date, or a UTC datetime's date."""
	value = text(node, field)
	return value[:DATE_LENGTH] if value else None


def time_of(node: Node, field: str) -> str:
	value = text(node, field)
	moment = iso_utc(f"{value}Z" if value and len(value) > DATE_LENGTH else value)
	if moment is None:
		raise SourceError(f"{node.path}.{field} is missing")
	return moment


def address_of(node: Node | None, address_type: str) -> Fields | None:
	"""A partner's address, when it is one ERPNext can hold (a street, a city and a country)."""
	if node is None or not text(node, "street") or not text(node, "city") or not text(node, "country_code"):
		return None
	return address_fields(
		Address(
			address_type=address_type,
			name=text(node, "name"),
			company=None,
			line1=required_text(node, "street"),
			line2=text(node, "street2"),
			city=required_text(node, "city"),
			state=m2o_name(node, "state_id"),
			postcode=text(node, "zip"),
			country_code=text(node, "country_code"),
			email=text(node, "email"),
			phone=text(node, "phone") or text(node, "mobile"),
		)
	)


def currency_of(node: Node, field: str = "currency_id") -> str:
	currency = m2o_name(node, field)
	if currency is None:
		raise SourceError(f"{node.path}.{field} names no currency")
	return currency


class AccountMapper:
	"""An account of the chart, flat under the company's root of its root type. An archived (deprecated)
	account stays enabled: the history booked to it is mirrored too."""

	kind = Kind.ACCOUNT
	version = 1

	def map(self, node: Node) -> Desired | None:
		odoo_type = node.string("account_type")
		if odoo_type == OFF_BALANCE:
			raise Unsupported("an off-balance account has no ERPNext home")
		root_type, account_type = ACCOUNT_TYPES.get(odoo_type, (None, None))
		if root_type is None:
			raise SourceError(f"{node.path}: unknown account type {odoo_type}")
		if account_type is None and flag(node, "is_tax_account"):
			account_type = TAX
		elif account_type is None and flag(node, "is_payment_account"):
			# An outstanding receipts or payments account: ERPNext pays only into or out of a Bank account.
			account_type = BANK
		account = LedgerAccount(
			number=text(node, "code"),
			name=required_text(node, "name"),
			root_type=root_type,
			account_type=account_type,
			currency=m2o_name(node, "currency_id"),
		)
		return Desired(self.kind, node.id(), account_fields(account))


class AnalyticAccountMapper:
	"""An analytic account as a Cost Center (its code first, as Odoo shows it). Archived ones stay
	enabled for the history that names them."""

	kind = Kind.COST_CENTER
	version = 1

	def map(self, node: Node) -> Desired | None:
		name = " ".join(part for part in (text(node, "code"), required_text(node, "name")) if part)
		return Desired(self.kind, node.id(), tree_fields(name, None))


class WarehouseMapper:
	kind = Kind.WAREHOUSE
	version = 1

	def map(self, node: Node) -> Desired | None:
		fields = warehouse_fields(
			required_text(node, "name"), None, False, address_of(node.child("address"), "Warehouse")
		)
		return Desired(
			self.kind, node.id(), fields | ({"disabled": True} if node.get("active") is False else {})
		)


class CategoryMapper:
	kind = Kind.COLLECTION
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		name = required_text(node, "name")
		fields: Fields = {
			"name": name,
			"slug": slugify(name),
			"parent": self._context.gid("Category", m2o_id(node, "parent_id")),
			"description": None,
			"image": None,
		}
		return Desired(self.kind, node.id(), fields)


class ProductMapper:
	"""A product template as an Item: one item when it has a single variant, else a template with an Item
	per variant (its attribute values, price with the variant's extra, stock). Stock is the storable
	product's quantity on hand in the company (``qty_available``); a service or consumable isn't a stock item.
	Published only when the website shows it. An archived product stays enabled (its history still books)
	and unpublished."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		name = required_text(node, "name")
		storable = flag(node, "is_storable") or node.get("detailed_type") == STORABLE
		variants = node.children("variants")
		simple = len(variants) <= 1
		mapped = [self._variant(variant, storable) for variant in variants]
		first: Fields = mapped[0] if mapped else {}
		weight = node.optional_decimal("weight") if node.get("weight") is not False else None
		fields: Fields = {
			"type": "simple" if simple else "variable",
			"name": name,
			"slug": slugify(name),
			"description": plain_html(text(node, "description_sale")) or None,
			"short_description": None,
			"sku": text(node, "default_code") if simple else None,
			"barcode": text(node, "barcode") if simple else None,
			"brand": None,
			"collections": [gid for gid in (self._context.gid("Category", m2o_id(node, "categ_id")),) if gid],
			"images": [],
			"published": flag(node, "is_published") and node.get("active") is not False,
			"is_stock_item": storable,
			"weight": {"value": weight, "unit": WEIGHT_UNIT} if weight and simple else None,
			"price": number(node, "list_price") if simple else None,
			"sale": None,
			"stock_qty": (first.get("stock_qty") if mapped else number(node, "qty_available"))
			if simple and storable
			else None,
			"attributes": [] if simple else variant_attributes(mapped),
			"specifications": [],
			"variants": [] if simple else list[Field](mapped),
			"tags": [],
		}
		return Desired(self.kind, node.id(), fields)

	def _variant(self, variant: Node, storable: bool) -> Fields:
		chosen: dict[str, Field] = {}
		for value in variant.children("attribute_values"):
			attribute = m2o_name(value, "attribute_id")
			if attribute is not None:
				chosen[attribute] = required_text(value, "name")
		weight = variant.optional_decimal("weight") if variant.get("weight") is not False else None
		return {
			"gid": self._context.required("Variant", variant.integer("id"), "id"),
			"sku": text(variant, "default_code"),
			"barcode": text(variant, "barcode"),
			"attributes": chosen,
			"price": number(variant, "lst_price"),
			"sale": None,
			"stock_qty": number(variant, "qty_available") if storable else None,
			"weight": {"value": weight, "unit": WEIGHT_UNIT} if weight else None,
			"image": None,
			"enabled": variant.get("active") is not False,
		}


class PartnerMapper:
	"""A commercial partner as a Customer or a Supplier: a company or a person, its own address and its
	child partners' (invoice and delivery addresses; a contact child is one of its people). Archived:
	disabled."""

	version = 1

	def __init__(self, kind: Kind, context: Context) -> None:
		self.kind = kind
		self._context = context

	def map(self, node: Node) -> Desired | None:
		name = required_text(node, "name")
		company = node.get("is_company") is True
		children = node.children("children")
		addresses = [address for address in (address_of(node, "Billing"),) if address]
		for child in children:
			kind = text(child, "type") or CONTACT
			if kind != CONTACT:
				address = address_of(child, ADDRESS_TYPES.get(kind, OTHER_ADDRESS))
				if address:
					addresses.append(address)
		people = [
			ContactPerson(
				self._context.required("Partner", child.integer("id"), "id"),
				required_text(child, "name"),
				text(child, "email"),
				text(child, "phone") or text(child, "mobile"),
				text(child, "function"),
			)
			for child in children
			if (text(child, "type") or CONTACT) == CONTACT and child.get("active") is not False
		]
		party = Party(
			name=name,
			type="Company" if company else "Individual",
			company=name if company else None,
			email=text(node, "email"),
			phone=text(node, "phone") or text(node, "mobile"),
			addresses=addresses,
			tax_id=text(node, "vat"),
			contacts=people,
			disabled=node.get("active") is False,
		)
		return Desired(self.kind, node.id(), party_fields(party))


class DepartmentMapper:
	kind = Kind.DEPARTMENT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		parent = self._context.gid("Department", m2o_id(node, "parent_id"))
		fields = tree_fields(required_text(node, "name"), parent, False, node.get("active") is False)
		return Desired(self.kind, node.id(), fields)


class JobMapper:
	"""A job position as a Designation."""

	kind = Kind.DESIGNATION
	version = 1

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), {"name": required_text(node, "name")})


class EmployeeMapper:
	"""An employee, under their full name (Odoo keeps one name; ERPNext's first name holds it whole rather
	than guess where it splits). Gender is 17 and 18's ``gender``, 19's ``sex``; joining is 19's contract
	start (17 and 18 keep none without the contracts app). Archived: left (with a departure date) or
	inactive."""

	kind = Kind.EMPLOYEE
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		gender = text(node, "gender") or text(node, "sex")
		departed = date_of(node, "departure_date")
		active = node.get("active") is not False
		employee = Employee(
			first_name=required_text(node, "name"),
			status="Active" if active else "Left" if departed else "Inactive",
			gender=GENDERS.get(gender) if gender else None,
			date_of_birth=date_of(node, "birthday"),
			date_of_joining=date_of(node, "contract_date_start"),
			relieving_date=departed,
			department=self._context.gid("Department", m2o_id(node, "department_id")),
			designation=self._context.gid("Job", m2o_id(node, "job_id")),
			reports_to=self._context.gid("Employee", m2o_id(node, "parent_id")),
			company_email=text(node, "work_email"),
			personal_email=text(node, "private_email"),
			phone=text(node, "mobile_phone") or text(node, "work_phone"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class ProjectMapper:
	kind = Kind.PROJECT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		update = text(node, "last_update_status")
		if node.get("active") is False:
			status = "Cancelled"
		elif update == PROJECT_DONE:
			status = "Completed"
		else:
			status = "On hold" if update == PROJECT_ON_HOLD else "Open"
		analytic = (
			m2o_id(node, "account_id") if node.has("account_id") else m2o_id(node, "analytic_account_id")
		)
		fields = project_fields(
			required_text(node, "name"),
			status,
			customer=self._context.gid("Partner", node.optional_integer("party_id"))
			if node.get("party_is_customer") is True
			else None,
			start=date_of(node, "date_start"),
			end=date_of(node, "date"),
			notes=html_text(node, "description"),
			cost_center=self._context.gid("AnalyticAccount", analytic),
		)
		return Desired(self.kind, node.id(), fields)


class LeadMapper:
	"""Every CRM lead and opportunity as a Lead (an opportunity's is what it stands with when its partner
	isn't a customer). A lost lead is disabled; a won opportunity's lead is converted."""

	kind = Kind.LEAD
	version = 1

	def map(self, node: Node) -> Desired | None:
		active = node.get("active") is not False
		opportunity = text(node, "type") == OPPORTUNITY
		won = number(node, "probability") == WON_PROBABILITY
		status = "Converted" if opportunity and won else "Opportunity" if opportunity else "Open"
		lead = Lead(
			status=status,
			first_name=text(node, "contact_name"),
			company=text(node, "partner_name"),
			email=text(node, "email_from"),
			phone=text(node, "phone"),
			mobile=text(node, "mobile"),
			job_title=text(node, "function"),
			website=text(node, "website"),
			notes=html_text(node, "description"),
			disabled=not active and not opportunity,
		)
		return Desired(self.kind, node.id(), lead_fields(lead))


class OpportunityMapper:
	"""An opportunity, with its customer when its partner is one, else with its own lead. Won: converted;
	lost (archived): lost."""

	kind = Kind.OPPORTUNITY
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if text(node, "type") != OPPORTUNITY:
			return None
		won = number(node, "probability") == WON_PROBABILITY
		lost = node.get("active") is False
		customer = node.optional_integer("party_id") if node.get("party_is_customer") is True else None
		opportunity = Opportunity(
			party_kind=Kind.CUSTOMER if customer else Kind.LEAD,
			party=self._context.required("Partner", customer, "partner") if customer else node.id(),
			title=required_text(node, "name"),
			status="Lost" if lost else "Converted" if won else "Open",
			date=date_of(node, "create_date") or time_of(node, "write_date")[:DATE_LENGTH],
			currency=node.string("company_currency"),
			amount=number(node, "expected_revenue"),
			probability=number(node, "probability"),
			expected_closing=date_of(node, "date_deadline"),
			notes=html_text(node, "description"),
		)
		return Desired(self.kind, node.id(), opportunity_fields(opportunity))


def ordered_lines(
	context: Context, node: Node, quantity_field: str, currency: str, sign: Decimal = Decimal(1)
) -> list[TradeLine]:
	"""An order's or invoice's product lines (layout lines left out), each at its exact net unit price."""
	lines: list[TradeLine] = []
	for line in node.children("lines"):
		if text(line, "display_type") in LAYOUT_LINES:
			continue
		product = line.child("product")
		template = product.optional_integer("template_id") if product else None
		several = product is not None and (product.optional_integer("variant_count") or 1) > 1
		product_id = m2o_id(line, "product_id")
		amount = sign * subtotal(line)
		qty = sign * number(line, quantity_field)
		if qty == 0 and amount == 0:
			continue
		lines.append(
			TradeLine(
				order_line(
					context.required("Line", line.integer("id"), "id"),
					context.gid("Product", template),
					context.gid("Variant", product_id) if several else None,
					text(line, "name") or m2o_name(line, "product_id") or "",
					qty,
					amount,
					currency,
				),
				account=context.gid("Account", m2o_id(line, "account_id"))
				if line.has("account_id")
				else None,
				cost_center=analytic_center(context, line),
			)
		)
	return lines


def subtotal(line: Node) -> Decimal:
	"""A product line's net amount, from its journal item when it is one (signed as the document is)."""
	return number(line, "price_subtotal")


def analytic_center(context: Context, line: Node) -> str | None:
	"""The cost center a line's analytic distribution puts it all on; a line split across several analytic
	accounts has no single ERPNext cost center (its share is a planned gap)."""
	raw = line.get("analytic_distribution")
	if raw is None or raw is False:
		return None
	distribution = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else None
	if not isinstance(distribution, dict) or len(distribution) != 1:
		return None
	((key, share),) = distribution.items()
	if not key.isdigit() or Decimal(str(share)) != FULL_SHARE:
		return None
	return context.gid("AnalyticAccount", int(key))


def split_distribution(line: Node) -> bool:
	raw = line.get("analytic_distribution")
	return raw not in (None, False) and analytic_center_key_count(raw) > 1


def analytic_center_key_count(raw: JsonValue) -> int:
	distribution = raw if isinstance(raw, dict) else json.loads(raw) if isinstance(raw, str) else {}
	return len(distribution) if isinstance(distribution, dict) else 0


def order_party(context: Context, node: Node, selling: bool) -> str:
	field = "party_is_customer" if selling else "party_is_supplier"
	party = node.optional_integer("party_id")
	if party is None or node.get(field) is not True:
		raise Unsupported(f"its partner isn't mirrored as a {'customer' if selling else 'supplier'}")
	return context.required("Partner", party, "partner")


def foreign(currency: str, node: Node) -> tuple[Decimal | None, Decimal | None]:
	"""For a document in another currency than the company's, the rate the source booked it at (its
	total in the company's currency over its total) and that total, checked exactly once booked."""
	if currency == node.string("company_currency"):
		return None, None
	total, base = number(node, "amount_total"), abs(number(node, "amount_total_signed"))
	if total == 0:
		return None, None
	return (base / total).quantize(Decimal(1).scaleb(-EXCHANGE_RATE_PLACES)), base


class SaleOrderMapper:
	"""A confirmed sale order as a submitted Sales Order (its taxes one amount on the connection's tax
	account: Odoo keeps no tax lines on an order); a quotation is the draft order kind's, a cancelled one
	none."""

	kind = Kind.SALES_ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if node.string("state") not in SALE_STATES:
			return None
		currency = currency_of(node)
		tax = number(node, "amount_tax")
		rate, base = foreign(currency, node)
		document = TradeDocument(
			number=required_text(node, "name"),
			date=date_of(node, "date_order") or "",
			party=order_party(self._context, node, True),
			currency=currency,
			lines=ordered_lines(self._context, node, "product_uom_qty", currency),
			taxes=[TradeTax("Taxes", tax)] if tax else [],
			total=number(node, "amount_total"),
			submitted=True,
			due_date=date_of(node, "commitment_date"),
			exchange_rate=rate,
			base_total=base,
			reference=text(node, "client_order_ref"),
			remarks=html_text(node, "note"),
		)
		return Desired(self.kind, node.id(), trade_fields(document))


class QuotationMapper:
	"""A quotation (draft or sent) as a draft Quotation, a proposal never booked; confirmed or cancelled,
	none (the order is the Sales Order kind's)."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if node.string("state") not in QUOTATION_STATES:
			return None
		currency = currency_of(node)
		lines = [line.line for line in ordered_lines(self._context, node, "product_uom_qty", currency)]
		tax = number(node, "amount_tax")
		rate, _ = foreign(currency, node)
		fields: Fields = {
			"number": required_text(node, "name"),
			"currency": currency,
			"created_at": time_of(node, "date_order"),
			"customer": order_party(self._context, node, True),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": [line_fields(line) for line in lines],
			"discount": Decimal(0),
			"charges": [charge_fields(Charge("tax", "Taxes", tax))] if tax else [],
			"total": number(node, "amount_total"),
			"remarks": html_text(node, "note"),
		}
		if rate is not None:
			fields["exchange_rate"] = rate
		return Desired(self.kind, node.id(), fields)


class PurchaseOrderMapper:
	"""A purchase order (an RFQ: draft) as a Purchase Order, submitted once confirmed; cancelled, none."""

	kind = Kind.PURCHASE_ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		state = node.string("state")
		if state not in PURCHASE_STATES | PURCHASE_DRAFT_STATES:
			return None
		currency = currency_of(node)
		tax = number(node, "amount_tax")
		rate, base = foreign(currency, node)
		document = TradeDocument(
			number=required_text(node, "name"),
			date=date_of(node, "date_order") or "",
			party=order_party(self._context, node, False),
			currency=currency,
			lines=ordered_lines(self._context, node, "product_qty", currency),
			taxes=[TradeTax("Taxes", tax)] if tax else [],
			total=number(node, "amount_total"),
			submitted=state in PURCHASE_STATES,
			due_date=date_of(node, "date_planned"),
			exchange_rate=rate,
			base_total=base,
			reference=text(node, "partner_ref"),
			remarks=html_text(node, "notes") or html_text(node, "note"),
		)
		return Desired(self.kind, node.id(), trade_fields(document))


class InvoiceMapper:
	"""A customer invoice, credit note or sales receipt (``SALES_MOVES``) as a Sales Invoice, or a vendor
	bill, refund or purchase receipt (``PURCHASE_MOVES``) as a Purchase Invoice: its product lines at their
	net amounts on their accounts, every other line it posts besides its party's total (taxes, cash
	rounding, early-payment discounts) as an amount on its account, its party's own receivable or payable
	account, and a credit note against the invoice it reverses. Posted: submitted; draft: a draft;
	cancelled: none."""

	version = 1

	def __init__(self, kind: Kind, context: Context) -> None:
		self.kind = kind
		self._context = context
		self._selling = kind is Kind.SALES_INVOICE

	def map(self, node: Node) -> Desired | None:
		state = node.string("state")
		if state not in (POSTED, DRAFT):
			return None
		move_type = node.string("move_type")
		is_return = move_type in RETURNS
		# A document's amounts in its own currency, as ERPNext signs them: credits on a sale, debits on a
		# purchase, negated again for a return (whose lines are negative quantities).
		sign = Decimal(-1) if self._selling else Decimal(1)
		currency = currency_of(node)
		lines: list[TradeLine] = []
		taxes: list[TradeTax] = []
		party_account: str | None = None
		reconciled: set[int | None] = set()
		warnings: list[str] = []
		for line in node.children("lines"):
			kind = text(line, "display_type")
			amount = sign * number(line, "amount_currency")
			account = self._context.gid("Account", m2o_id(line, "account_id"))
			if kind in LAYOUT_LINES or kind == COGS_LINE:
				continue
			if kind == PARTY_LINE:
				party_account = account
				reconciled |= {settled.optional_integer("move") for settled in line.children("settles")}
				continue
			if split_distribution(line):
				warnings.append(
					f"line {line.integer('id')} is split across analytic accounts (planned: card 386)"
				)
			if kind == PRODUCT_LINE:
				product = line.child("product")
				template = product.optional_integer("template_id") if product else None
				several = product is not None and (product.optional_integer("variant_count") or 1) > 1
				qty = number(line, "quantity") * (Decimal(-1) if is_return else Decimal(1))
				lines.append(
					TradeLine(
						order_line(
							self._context.required("MoveLine", line.integer("id"), "id"),
							self._context.gid("Product", template),
							self._context.gid("Variant", m2o_id(line, "product_id")) if several else None,
							text(line, "name") or m2o_name(line, "product_id") or "",
							qty,
							amount,
							currency,
						),
						account=account,
						cost_center=analytic_center(self._context, line),
					)
				)
			elif amount:
				taxes.append(TradeTax(text(line, "name") or kind or "Charge", amount, account))
		total = number(node, "amount_total") * (Decimal(-1) if is_return else Decimal(1))
		rate, base = foreign(currency, node)
		party = m2o_id(node, "commercial_partner_id")
		document = TradeDocument(
			number=text(node, "name") or f"Draft {node.string('source_id')}",
			date=date_of(node, "invoice_date") or required_date_of(node, "date"),
			party=self._context.required("Partner", party, "partner"),
			currency=currency,
			lines=lines,
			taxes=taxes,
			total=total,
			submitted=state == POSTED,
			due_date=date_of(node, "invoice_date_due"),
			exchange_rate=rate,
			is_return=is_return,
			return_against=self._context.gid("Move", m2o_id(node, "reversed_entry_id"))
			if is_return
			else None,
			reference=text(node, "ref"),
			remarks=html_text(node, "narration"),
			party_account=party_account,
			base_total=-base if base is not None and is_return else base,
			# A credit note Odoo reconciled with the invoice it reverses settles it; one refunded on its own
			# keeps its own balance.
			return_settles=m2o_id(node, "reversed_entry_id") in reconciled,
		)
		return Desired(self.kind, node.id(), trade_fields(document), warnings=tuple(warnings))


def required_date_of(node: Node, field: str) -> str:
	value = date_of(node, field)
	if value is None:
		raise SourceError(f"{node.path}.{field} is missing")
	return value


class JournalEntryMapper:
	"""A journal entry (a miscellaneous entry, a bank statement line's, an opening balance, a partner-less
	transfer) as a Journal Entry in the company's currency, each line on its account with its partner
	(on a receivable or payable account) and its cost center; a receivable or payable line the source
	reconciled with invoices is split to settle each. Posted: submitted; draft: a draft; cancelled: none."""

	kind = Kind.JOURNAL_ENTRY
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		state = node.string("state")
		if state not in (POSTED, DRAFT):
			return None
		lines: list[JournalLine] = []
		for line in node.children("lines"):
			lines += self._lines(line)
		fields = journal_fields(
			text(node, "name") or f"Draft {node.string('source_id')}",
			required_date_of(node, "date"),
			lines,
			state == POSTED,
			text(node, "ref") or html_text(node, "narration"),
		)
		return Desired(self.kind, node.id(), fields)

	def _lines(self, line: Node) -> list[JournalLine]:
		debit, credit = number(line, "debit"), number(line, "credit")
		account = self._context.required("Account", m2o_id(line, "account_id"), "account")
		account_type = text(line, "account_type")
		party_type = {RECEIVABLE: CUSTOMER_PARTY, PAYABLE: SUPPLIER_PARTY}.get(account_type or "")
		partner = m2o_id(line, "partner_id")
		party = self._context.gid("Partner", partner) if party_type else None
		if party_type and party is None:
			raise Unsupported(
				f"its line {line.integer('id')} on a receivable or payable account names no partner"
			)
		center = analytic_center(self._context, line)
		base = JournalLine(account, debit, credit, party_type if party else None, party, center)
		parts: list[JournalLine] = []
		left = debit or credit
		for settled in line.children("settles") if party else []:
			kind = SETTLED_KINDS.get(text(settled, "move_type") or "")
			amount = number(settled, "amount")
			if kind is None or amount <= 0 or amount > left:
				continue
			document = self._context.required("Move", settled.optional_integer("move"), "move")
			parts.append(
				JournalLine(
					account,
					amount if debit else Decimal(0),
					amount if credit else Decimal(0),
					party_type,
					party,
					center,
					against=(kind, document),
				)
			)
			left -= amount
		if left:
			parts.append(
				JournalLine(
					account,
					left if debit else Decimal(0),
					left if credit else Decimal(0),
					base.party_type,
					party,
					center,
				)
			)
		return parts


class PaymentMapper:
	"""A payment to or from a partner that posts its own journal entry, as a Payment Entry into or out of
	its outstanding account (where Odoo books it until the bank statement matches it), on its party's
	receivable or payable account, allocated to the invoices it settles (anything else is an advance).
	Booked: submitted; draft: a draft; cancelled or rejected: none."""

	kind = Kind.PAYMENT_ENTRY
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		state = node.string("state")
		if state not in PAYMENT_BOOKED | {DRAFT}:
			return None
		customer = node.string("partner_type") == "customer"
		inbound = node.string("payment_type") == INBOUND
		allocations: list[Allocation] = []
		warnings: list[str] = []
		for settled in node.children("settles"):
			kind = SETTLED_KINDS.get(text(settled, "move_type") or "")
			if kind is None:
				warnings.append("it settles a journal entry, which a Payment Entry can't allocate to")
				continue
			allocations.append(
				Allocation(
					kind,
					self._context.required("Move", settled.optional_integer("move"), "move"),
					number(settled, "amount"),
				)
			)
		payment = Payment(
			payment_type=RECEIVE if inbound else PAY,
			party_type=CUSTOMER_PARTY if customer else SUPPLIER_PARTY,
			party=self._context.required("Partner", m2o_id(node, "partner_id"), "partner"),
			date=required_date_of(node, "date"),
			amount=number(node, "amount"),
			currency=currency_of(node),
			allocations=allocations,
			account=self._context.gid("Account", m2o_id(node, "outstanding_account_id")),
			party_account=self._context.gid("Account", m2o_id(node, "destination_account_id")),
			reference=text(node, "memo") or text(node, "ref") or text(node, "name"),
			submitted=state in PAYMENT_BOOKED,
		)
		return Desired(self.kind, node.id(), payment_fields(payment), warnings=tuple(warnings))


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		AccountMapper(),
		AnalyticAccountMapper(),
		WarehouseMapper(),
		CategoryMapper(context),
		ProductMapper(context),
		PartnerMapper(Kind.CUSTOMER, context),
		PartnerMapper(Kind.SUPPLIER, context),
		DepartmentMapper(context),
		JobMapper(),
		EmployeeMapper(context),
		ProjectMapper(context),
		LeadMapper(),
		OpportunityMapper(context),
		QuotationMapper(context),
		SaleOrderMapper(context),
		PurchaseOrderMapper(context),
		InvoiceMapper(Kind.SALES_INVOICE, context),
		InvoiceMapper(Kind.PURCHASE_INVOICE, context),
		PaymentMapper(context),
		JournalEntryMapper(context),
	]
