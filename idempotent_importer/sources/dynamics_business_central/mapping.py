# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Business Central (API v2.0) → desired ERPNext state, one mapper per kind, in the shared field
contract. Business Central's categories, statuses, line types and blank values are translated here. A
document in the company's currency names none (``currencyCode`` empty, or the local currency); the API
gives no exchange rate for one in another, so such a document isn't booked at a rate it never had."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from idempotent_importer.contract import (
	Address,
	Charge,
	Employee,
	Lead,
	LedgerAccount,
	Opportunity,
	Party,
	TradeDocument,
	TradeLine,
	TradeTax,
	account_fields,
	address_fields,
	charge_fields,
	employee_fields,
	lead_fields,
	line_fields,
	opportunity_fields,
	order_line,
	party_fields,
	project_fields,
	trade_fields,
	tree_fields,
	warehouse_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.dynamics.common import (
	amount,
	date_of,
	noon_of,
	reference,
	required_date,
	text,
)

# The chart's categories → ERPNext's root type and the account type a posting account of it takes.
CATEGORIES: dict[str, tuple[str, str | None]] = {
	"Assets": ("Asset", None),
	"Liabilities": ("Liability", None),
	"Equity": ("Equity", None),
	"Income": ("Income", "Income Account"),
	"CostOfGoodsSold": ("Expense", "Cost of Goods Sold"),
	"Expense": ("Expense", "Expense Account"),
}
CASH_SUBCATEGORY = "Cash"
POSTING = "Posting"
BLOCKED_ALL = "All"
ITEM_TYPES_IN_STOCK = frozenset({"Inventory"})
EMPLOYEE_STATUSES = {"Active": "Active", "Inactive": "Inactive", "Terminated": "Left"}
CUSTOMER_RELATION = "Customer"
OPPORTUNITY_STATUSES = {"Not Started": "Open", "In Progress": "Open", "Won": "Converted", "Lost": "Lost"}
DRAFT_STATUSES = frozenset({"Draft", "In Review"})
CANCELLED = "Canceled"
ITEM_LINE, ACCOUNT_LINE, COMMENT_LINE = "Item", "Account", "Comment"
VARIANT_ATTRIBUTE = "Variant"
TAX_DESCRIPTION = "Tax"
COMPANY_TYPE = "Company"


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, type_: str, id_: str | None) -> str | None:
		return self.config.identity(type_, id_) if id_ else None

	def required(self, type_: str, id_: str | None, what: str) -> str:
		gid = self.gid(type_, id_)
		if gid is None:
			raise Unsupported(f"it names no {what}")
		return gid


def address_of(node: Node, address_type: str, prefix: str = "", suffix: str = "") -> Fields | None:
	"""An address from Business Central's flattened fields (``addressLine1``, or ``billToAddressLine1`` …),
	when ERPNext can hold it (a street, a city and a country)."""

	def field(name: str) -> str:
		return f"{prefix}{name[0].upper()}{name[1:]}" if prefix else name

	line1 = text(node, field("addressLine1"))
	city = text(node, field("city"))
	country = text(node, field("country"))
	if not (line1 and city and country):
		return None
	return address_fields(
		Address(
			address_type=address_type,
			name=text(node, "displayName"),
			company=None,
			line1=line1,
			line2=text(node, field("addressLine2")),
			city=city,
			state=text(node, field("state")),
			postcode=text(node, field("postalCode")) or text(node, field("postCode")),
			country_code=country,
			email=text(node, "email"),
			phone=text(node, "phoneNumber"),
		)
	)


class AccountMapper:
	"""A posting account of the chart, flat under the company's root of its category. Headings and totals
	lay the chart out (their ranges aren't in the API) and have no ledger account."""

	kind = Kind.ACCOUNT
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.string("accountType") != POSTING:
			raise Unsupported(
				f"a {node.string('accountType')} line of the chart has no ERPNext ledger account"
			)
		root_type, account_type = CATEGORIES.get(node.string("category"), (None, None))
		if root_type is None:
			raise SourceError(f"{node.path}: unknown account category {node.string('category')}")
		if text(node, "subCategory") == CASH_SUBCATEGORY:
			account_type = "Bank"
		account = LedgerAccount(text(node, "number"), node.string("displayName"), root_type, account_type)
		return Desired(self.kind, node.id(), account_fields(account))


class DimensionMapper:
	"""A dimension as a group Cost Center, each of its values a Cost Center under it."""

	kind = Kind.COST_CENTER
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		name = " ".join(part for part in (text(node, "code"), text(node, "displayName")) if part)
		if Identity.parse(node.id()).type == "Dimension":
			return Desired(self.kind, node.id(), tree_fields(name, None, True))
		parent = self._context.required("Dimension", reference(node, "dimensionId"), "dimension")
		return Desired(self.kind, node.id(), tree_fields(name, parent))


class LocationMapper:
	kind = Kind.WAREHOUSE
	version = 1

	def map(self, node: Node) -> Desired | None:
		name = text(node, "displayName") or node.string("code")
		return Desired(
			self.kind, node.id(), warehouse_fields(name, None, False, address_of(node, "Warehouse"))
		)


class ItemCategoryMapper:
	kind = Kind.COLLECTION
	version = 1

	def map(self, node: Node) -> Desired | None:
		name = text(node, "displayName") or node.string("code")
		fields: Fields = {
			"name": name,
			"slug": slugify(name),
			"parent": None,
			"description": None,
			"image": None,
		}
		return Desired(self.kind, node.id(), fields)


class ItemMapper:
	"""An item as an Item: inventory items are stock items with their inventory; services and non-inventory
	items aren't. Its variants (a code and a description each; the API gives no variant prices or stock) are
	variants at the item's price. A blocked item stays enabled (its history still books) and unpublished;
	no item is published (the API says nothing of a web shop)."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		name = node.string("displayName")
		variants = node.children("itemVariants")
		stocked = node.string("type") in ITEM_TYPES_IN_STOCK
		price = amount(node, "unitPrice")
		warnings: list[str] = []
		mapped: list[Fields] = [
			{
				"gid": self._context.required("ItemVariant", variant.string("id"), "id"),
				"sku": f"{node.string('number')}-{variant.string('code')}",
				"barcode": None,
				"attributes": {VARIANT_ATTRIBUTE: variant.string("code")},
				"price": price,
				"sale": None,
				"stock_qty": None,
				"weight": None,
				"image": None,
				"enabled": True,
			}
			for variant in variants
		]
		if variants and stocked:
			warnings.append("the API gives no stock per variant (planned: card 386)")
		fields: Fields = {
			"type": "variable" if variants else "simple",
			"name": name,
			"slug": slugify(name),
			"description": None,
			"short_description": None,
			"sku": None if variants else node.string("number"),
			"barcode": None if variants else text(node, "gtin"),
			"brand": None,
			"collections": [
				gid for gid in (self._context.gid("ItemCategory", reference(node, "itemCategoryId")),) if gid
			],
			"images": [],
			"published": False,
			"is_stock_item": stocked and not variants,
			"weight": None,
			"price": None if variants else price,
			"sale": None,
			"stock_qty": amount(node, "inventory") if stocked and not variants else None,
			"attributes": [
				{
					"name": VARIANT_ATTRIBUTE,
					"values": list[Field](
						v["attributes"][VARIANT_ATTRIBUTE]
						for v in mapped
						if isinstance(v["attributes"], dict)
					),
				}
			]
			if variants
			else [],
			"specifications": [],
			"variants": list[Field](mapped),
			"tags": [],
		}
		return Desired(self.kind, node.id(), fields, warnings=tuple(warnings))


class PartyMapper:
	"""A customer or vendor as a Customer or Supplier: a company or a person, its address, email and phone,
	tax registration number and currency. Blocked for everything: disabled."""

	version = 1

	def __init__(self, kind: Kind) -> None:
		self.kind = kind

	def map(self, node: Node) -> Desired | None:
		name = node.string("displayName")
		company = text(node, "type") in (None, COMPANY_TYPE)
		address = address_of(node, "Billing")
		party = Party(
			name=name,
			type="Company" if company else "Individual",
			company=name if company else None,
			email=text(node, "email"),
			phone=text(node, "phoneNumber"),
			addresses=[address] if address else [],
			tax_id=text(node, "taxRegistrationNumber"),
			currency=text(node, "currencyCode"),
			disabled=text(node, "blocked") == BLOCKED_ALL,
		)
		return Desired(self.kind, node.id(), party_fields(party))


class EmployeeMapper:
	kind = Kind.EMPLOYEE
	version = 1

	def map(self, node: Node) -> Desired | None:
		status = EMPLOYEE_STATUSES.get(node.string("status"))
		if status is None:
			raise SourceError(f"{node.path}: unknown employee status {node.string('status')}")
		employee = Employee(
			first_name=text(node, "givenName") or node.string("displayName"),
			middle_name=text(node, "middleName"),
			last_name=text(node, "surname"),
			status=status,
			date_of_birth=date_of(node, "birthDate"),
			date_of_joining=date_of(node, "employmentDate"),
			relieving_date=date_of(node, "terminationDate"),
			employee_number=text(node, "number"),
			company_email=text(node, "email"),
			personal_email=text(node, "personalEmail"),
			phone=text(node, "mobilePhone") or text(node, "phoneNumber"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class ProjectMapper:
	"""A project (the API keeps its number and name only)."""

	kind = Kind.PROJECT
	version = 1

	def map(self, node: Node) -> Desired | None:
		name = " ".join(part for part in (text(node, "number"), text(node, "displayName")) if part)
		return Desired(self.kind, node.id(), project_fields(name, "Open"))


class ContactMapper:
	"""A CRM contact (a company or a person) as a Lead; one that is a customer, converted."""

	kind = Kind.LEAD
	version = 1

	def map(self, node: Node) -> Desired | None:
		person = text(node, "type") != COMPANY_TYPE
		lead = Lead(
			status="Converted" if text(node, "contactBusinessRelation") == CUSTOMER_RELATION else "Open",
			first_name=text(node, "displayName") if person else None,
			company=text(node, "companyName") or (None if person else text(node, "displayName")),
			email=text(node, "email"),
			phone=text(node, "phoneNumber"),
			mobile=text(node, "mobilePhoneNumber"),
			job_title=text(node, "jobTitle"),
			website=text(node, "website"),
		)
		return Desired(self.kind, node.id(), lead_fields(lead))


class OpportunityMapper:
	"""An opportunity with its contact's Lead: its estimated value and chance of success, estimated
	closing; won → Converted, lost → Lost."""

	kind = Kind.OPPORTUNITY
	version = 1

	def map(self, node: Node) -> Desired | None:
		contact = node.optional_string("contact")
		if contact is None:
			raise Unsupported(f"its contact {node.text('contactNumber')} isn't in the API")
		status = OPPORTUNITY_STATUSES.get(node.string("status"))
		if status is None:
			raise SourceError(f"{node.path}: unknown opportunity status {node.string('status')}")
		opportunity = Opportunity(
			party_kind=Kind.LEAD,
			party=contact,
			title=text(node, "description") or node.string("number"),
			status=status,
			date=required_date(node, "creationDate"),
			currency=node.string("company_currency"),
			amount=amount(node, "estimatedValue"),
			probability=amount(node, "chancesOfSuccessPrc"),
			expected_closing=date_of(node, "estimatedClosingDate"),
		)
		return Desired(self.kind, node.id(), opportunity_fields(opportunity))


def currency_of(node: Node) -> str:
	"""The document's currency: its own, or the company's when it names none (or names the company's)."""
	company = node.string("company_currency")
	currency = text(node, "currencyCode") or company
	if currency != company:
		raise Unsupported(
			f"it is in {currency}, and Business Central's API gives no exchange rate for it (planned: card 386)"
		)
	return currency


def document_lines(
	context: Context, node: Node, lines_field: str, sign: Decimal = Decimal(1)
) -> tuple[list[TradeLine], list[str]]:
	"""A document's lines (comments left out) at their net amounts: an item line on its item (or variant), an
	account line on its account; a resource, fixed asset or charge line on the connection's fallback item
	(ERPNext has no such line)."""
	lines: list[TradeLine] = []
	warnings: list[str] = []
	currency = node.string("company_currency")
	for line in node.children(lines_field):
		kind = line.string("lineType")
		if kind == COMMENT_LINE:
			continue
		item = reference(line, "itemId") if kind == ITEM_LINE else None
		variant = reference(line, "itemVariantId") if kind == ITEM_LINE else None
		if kind not in (ITEM_LINE, ACCOUNT_LINE):
			warnings.append(f"its {kind} line {line.text('lineObjectNumber')} books to the fallback item")
		description = text(line, "description") or text(line, "lineObjectNumber") or kind
		lines.append(
			TradeLine(
				order_line(
					context.required("Line", line.string("id"), "id"),
					context.gid("Item", item),
					context.gid("ItemVariant", variant) if variant else None,
					description,
					sign * amount(line, "quantity"),
					sign * amount(line, "netAmount"),
					currency,
				),
				account=context.gid("Account", reference(line, "accountId"))
				if kind == ACCOUNT_LINE
				else None,
			)
		)
	return lines, warnings


class TradeMapper:
	"""A sales or purchase order, invoice or credit memo as the ERPNext document of ``kind``: its lines,
	its tax total as one amount on the connection's tax account (the API gives the tax per line, not by tax),
	its total. A draft (or one in review) is a draft; a released order and a posted invoice are submitted;
	a cancelled invoice has no copy. A credit memo is a return against the invoice it corrects."""

	version = 1

	def __init__(self, kind: Kind, context: Context, selling: bool, order: bool) -> None:
		self.kind = kind
		self._context = context
		self._selling = selling
		self._order = order

	def map(self, node: Node) -> Desired | None:
		status = node.string("status")
		if status == CANCELLED:
			return None
		credit = Identity.parse(node.id()).type.endswith("CreditMemo")
		sign = Decimal(-1) if credit else Decimal(1)
		currency = currency_of(node)
		lines_field = next(name for name in node.raw() if name.endswith("Lines"))
		lines, warnings = document_lines(self._context, node, lines_field, sign)
		tax = sign * amount(node, "totalTaxAmount")
		party_field, party_type = ("customerId", "Customer") if self._selling else ("vendorId", "Vendor")
		date = (
			date_of(node, "orderDate")
			or date_of(node, "invoiceDate")
			or date_of(node, "creditMemoDate")
			or required_date(node, "postingDate")
		)
		returned = reference(node, "invoiceId") if credit else None
		document = TradeDocument(
			number=node.string("number"),
			date=date,
			party=self._context.required(party_type, reference(node, party_field), party_type.lower()),
			currency=currency,
			lines=lines,
			taxes=[TradeTax(TAX_DESCRIPTION, tax)] if tax else [],
			total=sign * amount(node, "totalAmountIncludingTax"),
			submitted=status not in DRAFT_STATUSES,
			due_date=(date_of(node, "requestedDeliveryDate") or date_of(node, "requestedReceiptDate"))
			if self._order
			else date_of(node, "dueDate"),
			is_return=credit,
			return_against=self._context.gid(
				"SalesInvoice" if self._selling else "PurchaseInvoice", returned
			),
			reference=text(node, "externalDocumentNumber")
			or text(node, "customerPurchaseOrderReference")
			or text(node, "vendorInvoiceNumber"),
		)
		return Desired(self.kind, node.id(), trade_fields(document), warnings=tuple(warnings))


class QuoteMapper:
	"""A sales quote as a draft Quotation, a proposal never booked."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		currency = currency_of(node)
		lines, _ = document_lines(self._context, node, "salesQuoteLines")
		tax = amount(node, "totalTaxAmount")
		fields: Fields = {
			"number": node.string("number"),
			"currency": currency,
			"created_at": noon_of(date_of(node, "documentDate") or required_date(node, "postingDate")),
			"customer": self._context.required("Customer", reference(node, "customerId"), "customer"),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": [line_fields(line.line) for line in lines],
			"discount": Decimal(0),
			"charges": [charge_fields(Charge("tax", TAX_DESCRIPTION, tax))] if tax else [],
			"total": amount(node, "totalAmountIncludingTax"),
			"remarks": None,
		}
		return Desired(self.kind, node.id(), fields)


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		AccountMapper(),
		DimensionMapper(context),
		LocationMapper(),
		ItemCategoryMapper(),
		ItemMapper(context),
		PartyMapper(Kind.CUSTOMER),
		PartyMapper(Kind.SUPPLIER),
		EmployeeMapper(),
		ProjectMapper(),
		ContactMapper(),
		OpportunityMapper(),
		QuoteMapper(context),
		TradeMapper(Kind.SALES_ORDER, context, selling=True, order=True),
		TradeMapper(Kind.PURCHASE_ORDER, context, selling=False, order=True),
		TradeMapper(Kind.SALES_INVOICE, context, selling=True, order=False),
		TradeMapper(Kind.PURCHASE_INVOICE, context, selling=False, order=False),
	]
