# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Wave → desired ERPNext state: the chart of accounts, sales taxes as Sales Taxes and Charges Templates,
products and services as Items, customers and vendors as Customers and Suppliers, estimates as draft
Quotations, invoices as Sales Invoices, and the payments recorded against them as Payment Entries.

Field meanings are Wave's GraphQL schema's (tests/schemas/wave.json). Money is ``{value, currency}``: an
exact decimal string in its currency. An invoice's or estimate's lines are their subtotals (quantity x unit
price); its discount is its ``discountTotal``; its taxes are the amounts Wave computed on each line, summed by
sales tax; the lines less the discount plus the taxes must make its total exactly. A draft invoice stays a
draft; any other (saved, sent, viewed, paid, ...) is posted, as Wave posts it. An archived customer or vendor
stays, disabled; a deleted estimate or an inactive payment isn't kept. What ERPNext can't hold exactly is
Unsupported, naming the card that plans it."""

from __future__ import annotations

from decimal import Decimal

from idempotent_importer.contract import (
	RECEIVE,
	TAX,
	Address,
	Allocation,
	Charge,
	LedgerAccount,
	OrderLine,
	Party,
	Payment,
	TradeDocument,
	TradeLine,
	TradeTax,
	account_fields,
	address_fields,
	charge_fields,
	line_fields,
	order_line,
	party_fields,
	payment_fields,
	tax_rate_fields,
	trade_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_text
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.ledger import book_item, country_code, day, midday, required_day
from idempotent_importer.sources.wave.reader import local_id

PLANNED = "planned: card 386"
ROOT_TYPES = {
	"ASSET": "Asset",
	"LIABILITY": "Liability",
	"EQUITY": "Equity",
	"INCOME": "Income",
	"EXPENSE": "Expense",
}
ACCOUNT_TYPES = {
	"CASH_AND_BANK": "Bank",
	"MONEY_IN_TRANSIT": "Cash",
	"INVENTORY": "Stock",
	"PROPERTY_PLANT_EQUIPMENT": "Fixed Asset",
	"RECEIVABLE": "Receivable",
	"RECEIVABLE_INVOICES": "Receivable",
	"PAYABLE": "Payable",
	"PAYABLE_BILLS": "Payable",
	"SALES_TAX": "Tax",
	"COST_OF_GOODS_SOLD": "Cost of Goods Sold",
	"DEPRECIATION_AND_AMORTIZATION": "Depreciation",
	"INCOME": "Income Account",
	"OTHER_INCOME": "Income Account",
	"UNCATEGORIZED_INCOME": "Income Account",
	"EXPENSE": "Expense Account",
	"PAYROLL_EXPENSES": "Expense Account",
	"PAYMENT_PROCESSING_FEES": "Expense Account",
	"UNCATEGORIZED_EXPENSE": "Expense Account",
	"RETAINED_EARNINGS": "Equity",
	"NON_RETAINED_EARNINGS": "Equity",
}
DRAFT = "DRAFT"
DELETED_ESTIMATE = "DELETED"


class Context:
	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config

	def gid(self, type_: str, global_id: str) -> str:
		return self.config.identity(type_, local_id(global_id))

	def ref(self, type_: str, node: Node, field: str) -> str:
		"""The identity of the record ``node.field`` points to (``{id}``)."""
		return self.gid(type_, node.required_child(field).string("id"))


def money(node: Node, field: str) -> Decimal:
	"""A Wave Money's value, zero when it has none."""
	found = node.child(field)
	return (found.optional_decimal("value") if found else None) or Decimal(0)


def currency_of(node: Node, field: str = "currency") -> str:
	return node.required_child(field).string("code")


class AccountMapper:
	"""An account of the chart, its number and name, under the company's root of its type, of the ERPNext
	type its subtype is (a bank, receivable, tax, ... account)."""

	kind = Kind.ACCOUNT
	version = 1

	def map(self, node: Node) -> Desired | None:
		kind = node.required_child("type").string("value")
		if kind not in ROOT_TYPES:
			raise Unsupported(f'an account of type "{kind}"')
		subtype = node.required_child("subtype").string("value")
		account = LedgerAccount(
			number=node.optional_string("displayId"),
			name=node.string("name"),
			root_type=ROOT_TYPES[kind],
			account_type=ACCOUNT_TYPES.get(subtype),
			currency=currency_of(node),
		)
		return Desired(self.kind, node.id(), account_fields(account))


class SalesTaxMapper:
	"""A sales tax at its current rate, with its tax number; an archived tax isn't kept. A compound tax
	(charged on other taxes) is planned."""

	kind = Kind.TAX_RATE
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.flag("isArchived"):
			return None
		if node.flag("isCompound"):
			raise Unsupported(f"a compound sales tax ({PLANNED}: a template row on the previous total)")
		name = node.optional_string("abbreviation") or node.string("name")
		return Desired(
			self.kind,
			node.id(),
			tax_rate_fields(name, node.decimal("rate") * 100, node.optional_string("taxNumber")),
		)


class ProductMapper:
	"""A product or service at its unit price, with its description (Wave counts no stock)."""

	kind = Kind.PRODUCT
	version = 1

	def map(self, node: Node) -> Desired | None:
		fields = book_item(
			node.string("name"), None, node.optional_string("description"), node.decimal("unitPrice"), False
		)
		return Desired(self.kind, node.id(), fields)


class PartyMapper:
	"""A customer or vendor: a business (else a person) with its contact, email, phone, address and currency;
	archived is disabled."""

	version = 1

	def __init__(self, kind: Kind) -> None:
		self.kind = kind

	def map(self, node: Node) -> Desired | None:
		name = node.string("name")
		first, last = node.optional_string("firstName"), node.optional_string("lastName")
		person = " ".join(part for part in (first, last) if part)
		company = None if person == name else name
		address = self._address(node, name, company)
		currency = node.child("currency")
		party = Party(
			name=name,
			type="Company" if company else "Individual",
			first_name=first,
			last_name=last,
			company=company,
			email=node.optional_string("email"),
			phone=node.optional_string("phone")
			or node.optional_string("mobile")
			or node.optional_string("tollFree"),
			addresses=[address] if address else [],
			currency=currency.optional_string("code") if currency else None,
			disabled=node.optional_flag("isArchived") is True,
		)
		return Desired(self.kind, node.id(), party_fields(party))

	@staticmethod
	def _address(node: Node, name: str, company: str | None) -> Fields | None:
		found = node.child("address")
		if found is None:
			return None
		line1, city = found.optional_string("addressLine1"), found.optional_string("city")
		if not (line1 and city):
			return None
		province, country = found.child("province"), found.child("country")
		return address_fields(
			Address(
				"Billing",
				name,
				company,
				line1,
				found.optional_string("addressLine2"),
				city,
				province.optional_string("name") if province else None,
				found.optional_string("postalCode"),
				country_code(country.optional_string("code")) if country else None,
				None,
				None,
			)
		)


class Amounts:
	"""An invoice's or estimate's lines, discount and taxes (Wave's, summed by sales tax), which must make
	its total exactly."""

	def __init__(self, node: Node, context: Context, currency: str) -> None:
		self.lines: list[OrderLine] = []
		taxes: dict[str, Decimal] = {}
		subtotal = Decimal(0)
		for index, item in enumerate(node.children("items"), start=1):
			amount = money(item, "subtotal")
			product = item.child("product")
			name = item.optional_string("description") or (
				product.optional_string("name") if product else None
			)
			self.lines.append(
				order_line(
					local_id(item.optional_string("id") or str(index)),
					context.gid("Product", product.string("id")) if product else None,
					None,
					name or "Line",
					item.decimal("quantity"),
					amount,
					currency,
				)
			)
			subtotal += amount
			for tax in item.children("taxes"):
				sales_tax = tax.required_child("salesTax")
				label = sales_tax.optional_string("abbreviation") or sales_tax.string("name")
				taxes[label] = taxes.get(label, Decimal(0)) + money(tax, "amount")
		self.taxes = sorted((name, amount) for name, amount in taxes.items() if amount)
		self.discount = money(node, "discountTotal")
		self.total = money(node, "total")
		made = subtotal - self.discount + sum((amount for _, amount in self.taxes), Decimal(0))
		if made != self.total:
			raise Unsupported(
				f"the lines, discount and taxes come to {decimal_text(made)}, not the total {decimal_text(self.total)}"
			)


def exchange_rate(node: Node) -> Decimal | None:
	rate = node.optional_decimal("exchangeRate")
	return None if rate is None or rate == 1 else rate


class EstimateMapper:
	"""An estimate as a draft Quotation to its customer: its lines, discount and taxes, which must make its
	total; a deleted estimate isn't kept."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.string("status") == DELETED_ESTIMATE:
			return None
		currency = currency_of(node)
		amounts = Amounts(node, self.context, currency)
		remarks = " — ".join(p for p in (node.optional_string("title"), node.optional_string("memo")) if p)
		fields: Fields = {
			"number": node.string("estimateNumber"),
			"currency": currency,
			"created_at": midday(required_day(node.string("estimateDate"), node.path)),
			"customer": self.context.ref("Customer", node, "customer"),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": list[Field]([line_fields(line) for line in amounts.lines]),
			"discount": amounts.discount,
			"charges": list[Field](
				[charge_fields(Charge(TAX, name, amount)) for name, amount in amounts.taxes]
			),
			"total": amounts.total,
			"remarks": remarks or None,
		}
		return Desired(self.kind, node.id(), fields)


class InvoiceMapper:
	"""An invoice to its customer: its lines, discount and taxes, which must make its total; a draft while
	Wave's is, else submitted."""

	kind = Kind.SALES_INVOICE
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		currency = currency_of(node)
		amounts = Amounts(node, self.context, currency)
		document = TradeDocument(
			number=node.string("invoiceNumber"),
			date=required_day(node.string("invoiceDate"), node.path),
			party=self.context.ref("Customer", node, "customer"),
			currency=currency,
			lines=[TradeLine(line) for line in amounts.lines],
			taxes=[TradeTax(name, amount) for name, amount in amounts.taxes],
			total=amounts.total,
			submitted=node.string("status") != DRAFT,
			due_date=day(node.optional_string("dueDate")),
			exchange_rate=exchange_rate(node),
			reference=node.optional_string("poNumber"),
			remarks=node.optional_string("memo"),
			discount=amounts.discount or None,
		)
		return Desired(self.kind, node.id(), trade_fields(document))


class PaymentMapper:
	"""A payment recorded against an invoice: from its customer into the account Wave deposited it to,
	allocated to the invoice; an inactive (removed) payment isn't kept."""

	kind = Kind.PAYMENT_ENTRY
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("active") is False:
			return None
		amount = node.decimal("amount")
		if amount < 0:
			raise Unsupported(f"a refund of an invoice ({PLANNED}: a payment out against a credit note)")
		customer = node.child("customer") or node.required_child("invoice_customer")
		paid_in = node.child("paymentCurrency") or node.required_child("invoice_currency")
		payment = Payment(
			payment_type=RECEIVE,
			party_type="Customer",
			party=self.context.gid("Customer", customer.string("id")),
			date=required_day(node.optional_string("paymentDate"), node.path),
			amount=amount,
			currency=paid_in.string("code"),
			allocations=[
				Allocation(Kind.SALES_INVOICE, self.context.gid("Invoice", node.string("invoice")), amount)
			],
			exchange_rate=exchange_rate(node),
			account=self.context.ref("Account", node, "account"),
			reference=node.optional_string("paymentMethod"),
			remarks=node.optional_string("memo"),
		)
		return Desired(self.kind, node.id(), payment_fields(payment))


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		AccountMapper(),
		SalesTaxMapper(),
		ProductMapper(),
		PartyMapper(Kind.CUSTOMER),
		PartyMapper(Kind.SUPPLIER),
		EstimateMapper(context),
		InvoiceMapper(context),
		PaymentMapper(context),
	]
