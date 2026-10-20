# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Dynamics 365 Sales (Dataverse) → desired ERPNext state, one mapper per kind, in the shared field
contract. Dataverse's choices (``statecode``, product types) are translated here; a customer lookup names its
table in its ``@Microsoft.Dynamics.CRM.lookuplogicalname`` annotation (an account or a contact). Money columns
are in the row's own currency, their ``_base`` twins in the organisation's base currency: a document in
another currency is booked at the rate its two totals imply, its base total checked exactly."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from idempotent_importer.contract import (
	SHIPPING,
	Address,
	Charge,
	ContactPerson,
	Lead,
	Opportunity,
	Party,
	TradeDocument,
	TradeLine,
	TradeTax,
	address_fields,
	charge_fields,
	lead_fields,
	line_fields,
	opportunity_fields,
	order_line,
	party_fields,
	trade_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Fields
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.dynamics.common import amount, date_of, noon_of, required_date, text

LOOKUP = "@Microsoft.Dynamics.CRM.lookuplogicalname"
CUSTOMER_TABLES = {"account": "Account", "contact": "Contact"}
INACTIVE = 1
LEAD_QUALIFIED, LEAD_DISQUALIFIED = 1, 2
OPPORTUNITY_STATUSES = {0: "Open", 1: "Converted", 2: "Lost"}
QUOTE_OPEN = frozenset({0, 1})
ORDER_DRAFT, ORDER_CANCELLED = 0, 2
INVOICE_CANCELLED = 3
PRODUCT_STRUCTURE = 1
SALES_INVENTORY = 1
ISO_COUNTRY_LENGTH = 2
PERCENT = Decimal(100)
EXCHANGE_RATE_PLACES = 9
TAX_DESCRIPTION, FREIGHT_DESCRIPTION = "Tax", "Freight"


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, type_: str, id_: str | None) -> str | None:
		return self.config.identity(type_, id_) if id_ else None


def state_of(node: Node) -> int:
	return node.integer("statecode")


def address_of(node: Node, address_type: str, name: str | None) -> tuple[Fields | None, str | None]:
	"""A row's first address, when ERPNext can hold it: Dataverse keeps the country as free text, so only a
	two-letter ISO code places it (else a warning)."""
	line1, city, country = (
		text(node, "address1_line1"),
		text(node, "address1_city"),
		text(node, "address1_country"),
	)
	if not (line1 and city and country):
		return None, None
	if len(country) != ISO_COUNTRY_LENGTH or not country.isalpha():
		return None, f"its address's country \"{country}\" isn't an ISO code (planned: card 386)"
	return (
		address_fields(
			Address(
				address_type=address_type,
				name=name,
				company=None,
				line1=line1,
				line2=text(node, "address1_line2"),
				city=city,
				state=text(node, "address1_stateorprovince"),
				postcode=text(node, "address1_postalcode"),
				country_code=country,
				email=text(node, "emailaddress1"),
				phone=text(node, "telephone1"),
			)
		),
		None,
	)


def customer_of(context: Context, node: Node) -> str:
	"""The account or contact a document or opportunity is for."""
	table = CUSTOMER_TABLES.get(node.text(f"_customerid_value{LOOKUP}"))
	customer = context.gid(table, text(node, "_customerid_value")) if table else None
	if customer is None:
		raise Unsupported("it names no account or contact as its customer")
	return customer


class CustomerMapper:
	"""An account as a Customer (a company, with its contacts as its people); a contact as a Customer (a
	person: Dataverse's documents can be for a contact). Inactive: disabled."""

	kind = Kind.CUSTOMER
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		account = Identity.parse(node.id()).type == "Account"
		name = node.string("name" if account else "fullname")
		address, warning = address_of(node, "Billing", name)
		people = [
			ContactPerson(
				self._context.config.identity("Contact", person.string("contactid")),
				person.string("fullname"),
				text(person, "emailaddress1"),
				text(person, "telephone1"),
				text(person, "jobtitle"),
			)
			for person in node.children("contact_customer_accounts")
			if person.optional_integer("statecode") != INACTIVE
		]
		party = Party(
			name=name,
			type="Company" if account else "Individual",
			first_name=None if account else text(node, "firstname"),
			last_name=None if account else text(node, "lastname"),
			company=name if account else None,
			email=text(node, "emailaddress1"),
			phone=text(node, "telephone1") or text(node, "mobilephone"),
			addresses=[address] if address else [],
			currency=node.optional_string("currency"),
			contacts=people,
			disabled=state_of(node) == INACTIVE,
		)
		return Desired(self.kind, node.id(), party_fields(party), warnings=(warning,) if warning else ())


class LeadMapper:
	"""A lead: open; qualified → converted; disqualified → disabled."""

	kind = Kind.LEAD
	version = 1

	def map(self, node: Node) -> Desired | None:
		state = state_of(node)
		lead = Lead(
			status="Converted" if state == LEAD_QUALIFIED else "Open",
			first_name=text(node, "firstname"),
			last_name=text(node, "lastname"),
			company=text(node, "companyname"),
			email=text(node, "emailaddress1"),
			phone=text(node, "telephone1"),
			mobile=text(node, "mobilephone"),
			job_title=text(node, "jobtitle"),
			website=text(node, "websiteurl"),
			notes=" — ".join(part for part in (text(node, "subject"), text(node, "description")) if part)
			or None,
			disabled=state == LEAD_DISQUALIFIED,
		)
		return Desired(self.kind, node.id(), lead_fields(lead))


class OpportunityMapper:
	"""An opportunity with its customer (an account or contact), else the lead it came from: its estimated
	revenue and close probability and date; won → Converted, lost → Lost."""

	kind = Kind.OPPORTUNITY
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if text(node, "_customerid_value"):
			party_kind, party = Kind.CUSTOMER, customer_of(self._context, node)
		else:
			lead = self._context.gid("Lead", text(node, "_originatingleadid_value"))
			if lead is None:
				raise Unsupported("it has neither a customer nor a lead")
			party_kind, party = Kind.LEAD, lead
		opportunity = Opportunity(
			party_kind=party_kind,
			party=party,
			title=node.string("name"),
			status=OPPORTUNITY_STATUSES[state_of(node)],
			date=required_date(node, "createdon"),
			currency=node.optional_string("currency") or node.string("company_currency"),
			amount=amount(node, "estimatedvalue"),
			probability=amount(node, "closeprobability"),
			expected_closing=date_of(node, "estimatedclosedate"),
			notes=text(node, "description"),
		)
		return Desired(self.kind, node.id(), opportunity_fields(opportunity))


class ProductMapper:
	"""A product as an Item: a sales-inventory product is a stock item with its quantity on hand; charges,
	services and flat fees aren't. Product families and bundles are planned. A retired product stays enabled
	(its history still books); none is published (Dataverse has no web shop)."""

	kind = Kind.PRODUCT
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.optional_integer("productstructure") not in (None, PRODUCT_STRUCTURE):
			raise Unsupported("a product family or bundle (planned: card 386)")
		name = node.string("name")
		stocked = node.optional_integer("producttypecode") == SALES_INVENTORY
		fields: Fields = {
			"type": "simple",
			"name": name,
			"slug": slugify(name),
			"description": text(node, "description"),
			"short_description": None,
			"sku": text(node, "productnumber"),
			"barcode": None,
			"brand": None,
			"collections": [],
			"images": [],
			"published": False,
			"is_stock_item": stocked,
			"weight": None,
			"price": amount(node, "price"),
			"sale": None,
			"stock_qty": amount(node, "quantityonhand") if stocked else None,
			"attributes": [],
			"specifications": [],
			"variants": [],
			"tags": [],
		}
		return Desired(self.kind, node.id(), fields)


def currency_and_rate(node: Node) -> tuple[str, Decimal | None, Decimal | None]:
	"""The document's currency, and in another than the base, the rate its totals imply and its base total."""
	base = node.string("company_currency")
	currency = node.optional_string("currency") or base
	if currency == base:
		return currency, None, None
	total, base_total = amount(node, "totalamount"), amount(node, "totalamount_base")
	if not total:
		return currency, None, None
	return currency, (base_total / total).quantize(Decimal(1).scaleb(-EXCHANGE_RATE_PLACES)), base_total


def document_lines(context: Context, node: Node, navigation: str, key: str) -> list[TradeLine]:
	"""A document's lines at their net amounts (less their manual and volume discounts): a catalog product's
	on its item, a write-in product's on the connection's fallback item."""
	lines: list[TradeLine] = []
	for line in sorted(
		node.children(navigation), key=lambda row: row.optional_integer("sequencenumber") or 0
	):
		net = (
			amount(line, "baseamount")
			- amount(line, "manualdiscountamount")
			- amount(line, "volumediscountamount")
		)
		product = None if line.optional_flag("isproductoverridden") else text(line, "_productid_value")
		lines.append(
			TradeLine(
				order_line(
					context.config.identity("Line", line.string(key)),
					context.gid("Product", product),
					None,
					text(line, "productdescription") or "Product",
					amount(line, "quantity"),
					net,
					node.optional_string("currency") or node.string("company_currency"),
				)
			)
		)
	return lines


def discount_of(node: Node) -> Decimal:
	"""The document's own discount: its amount, and its percentage of the lines' total."""
	return (
		amount(node, "discountamount")
		+ amount(node, "totallineitemamount") * amount(node, "discountpercentage") / PERCENT
	)


class DocumentMapper:
	"""A sales order or invoice as a Sales Order or Sales Invoice: its lines, its discount, freight (on the
	connection's shipping account) and tax total (on its tax account), its total checked exactly. An active
	order is a draft, a submitted, fulfilled or invoiced one submitted; an invoice is submitted (Dataverse
	keeps no payments: an invoice's paid state is reported, not booked); cancelled, none."""

	version = 1

	def __init__(self, kind: Kind, context: Context, number_field: str, navigation: str, key: str) -> None:
		self.kind = kind
		self._context = context
		self._number = number_field
		self._navigation = navigation
		self._key = key

	def map(self, node: Node) -> Desired | None:
		state = state_of(node)
		order = self.kind is Kind.SALES_ORDER
		if state == (ORDER_CANCELLED if order else INVOICE_CANCELLED):
			return None
		currency, rate, base = currency_and_rate(node)
		taxes = [TradeTax(FREIGHT_DESCRIPTION, amount(node, "freightamount"), role=SHIPPING)]
		taxes = [tax for tax in taxes if tax.amount] + (
			[TradeTax(TAX_DESCRIPTION, amount(node, "totaltax"))] if amount(node, "totaltax") else []
		)
		document = TradeDocument(
			number=node.string(self._number),
			date=required_date(node, "createdon"),
			party=customer_of(self._context, node),
			currency=currency,
			lines=document_lines(self._context, node, self._navigation, self._key),
			taxes=taxes,
			total=amount(node, "totalamount"),
			submitted=state != ORDER_DRAFT if order else True,
			due_date=date_of(node, "requestdeliveryby") if order else date_of(node, "duedate"),
			exchange_rate=rate,
			base_total=base,
			remarks=text(node, "name"),
			discount=discount_of(node) or None,
		)
		return Desired(self.kind, node.id(), trade_fields(document))


class QuoteMapper:
	"""A draft or active quote as a draft Quotation, a proposal never booked; won or closed, none."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if state_of(node) not in QUOTE_OPEN:
			return None
		currency, rate, _ = currency_and_rate(node)
		lines = document_lines(self._context, node, "quote_details", "quotedetailid")
		charges = [
			Charge("shipping", FREIGHT_DESCRIPTION, amount(node, "freightamount")),
			Charge("tax", TAX_DESCRIPTION, amount(node, "totaltax")),
		]
		fields: Fields = {
			"number": node.string("quotenumber"),
			"currency": currency,
			"created_at": noon_of(date_of(node, "effectivefrom") or required_date(node, "createdon")),
			"customer": customer_of(self._context, node),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": [line_fields(line.line) for line in lines],
			"discount": discount_of(node),
			"charges": [charge_fields(charge) for charge in charges if charge.amount],
			"total": amount(node, "totalamount"),
			"remarks": text(node, "name"),
		}
		if rate is not None:
			fields["exchange_rate"] = rate
		return Desired(self.kind, node.id(), fields)


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		ProductMapper(),
		CustomerMapper(context),
		LeadMapper(),
		OpportunityMapper(context),
		QuoteMapper(context),
		DocumentMapper(Kind.SALES_ORDER, context, "ordernumber", "order_details", "salesorderdetailid"),
		DocumentMapper(Kind.SALES_INVOICE, context, "invoicenumber", "invoice_details", "invoicedetailid"),
	]
