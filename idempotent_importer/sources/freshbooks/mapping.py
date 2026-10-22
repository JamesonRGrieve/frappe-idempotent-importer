# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure FreshBooks → desired ERPNext state: the chart of accounts, taxes as Sales Taxes and Charges Templates,
items and services as Items, clients as Customers, bill vendors as Suppliers, projects and their time entries
as Projects and Timesheets, estimates as draft Quotations, invoices and credit notes as Sales Invoices, bills
as Purchase Invoices, payments and bill payments as Payment Entries, and the manual journal entries, expenses
and other income as Journal Entries.

Field meanings are FreshBooks' API reference's (www.freshbooks.com/api). Amounts are exact decimal strings in
their ``code``'s currency. An invoice's or estimate's lines are quantity x unit cost; its discount is its
``discount_total``; each tax (a line's ``taxName``/``taxAmount``, a percent) is charged on the discounted
lines it is on, rounded to the cent, and the lines, discount and taxes must make its amount exactly. A
record FreshBooks deleted (``vis_state`` 1) isn't kept; an archived one (2) stays, disabled where it can be.
What ERPNext can't hold exactly is Unsupported, naming the card that plans it."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from idempotent_importer.contract import (
	PAY,
	RECEIVE,
	TAX,
	Address,
	Allocation,
	Charge,
	ContactPerson,
	JournalLine,
	LedgerAccount,
	OrderLine,
	Party,
	Payment,
	TimeLog,
	TradeDocument,
	TradeLine,
	TradeTax,
	account_fields,
	address_fields,
	charge_fields,
	journal_fields,
	line_fields,
	order_line,
	party_fields,
	payment_fields,
	project_fields,
	tax_rate_fields,
	timesheet_fields,
	trade_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_text, minor_units
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.ledger import book_item, country_code, day, midday, required_day

PLANNED = "planned: card 386"
HUNDRED = Decimal(100)
SECONDS_PER_HOUR = Decimal(3600)
HOURS_PLACES = Decimal("0.000001")
ACTIVE, DELETED, ARCHIVED = 0, 1, 2
ROOT_TYPES = {
	"asset": "Asset",
	"liability": "Liability",
	"equity": "Equity",
	"income": "Income",
	"expense": "Expense",
}
DRAFT_INVOICES = frozenset({"draft", "created"})
DEBIT = "TYPE_DEBIT"
TAX_SLOTS = (("taxName1", "taxAmount1"), ("taxName2", "taxAmount2"))
BILL_TAX_SLOTS = (("tax_name1", "tax_amount1"), ("tax_name2", "tax_amount2"))


class Context:
	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config

	def gid(self, type_: str, id_: int | str) -> str:
		return self.config.identity(type_, id_)


def deleted(node: Node) -> bool:
	return node.optional_integer("vis_state") == DELETED


def amount_of(node: Node, field: str) -> Decimal:
	"""A FreshBooks money object's amount (``{amount, code}``), zero when it has none."""
	money = node.child(field)
	return (money.optional_decimal("amount") if money else None) or Decimal(0)


def code_of(node: Node, field: str) -> str | None:
	money = node.child(field)
	return money.optional_string("code") if money else None


def address_of(
	node: Node, prefix: str, name: str, company: str | None, fields: tuple[str, ...]
) -> Fields | None:
	street, street2, city, province, code, country = (
		node.optional_string(f"{prefix}{field}") for field in fields
	)
	if not (street and city):
		return None
	return address_fields(
		Address(
			"Billing", name, company, street, street2, city, province, code, country_code(country), None, None
		)
	)


CLIENT_ADDRESS = ("street", "street2", "city", "province", "code", "country")
VENDOR_ADDRESS = ("street", "street2", "city", "province", "postal_code", "country")


class AccountMapper:
	"""An account of the chart (a sub-account under its account), its number, name and type; an account with
	sub-accounts is their group."""

	kind = Kind.ACCOUNT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		kind = node.string("account_type")
		if kind not in ROOT_TYPES:
			raise Unsupported(f'an account of type "{kind}"')
		parent = node.optional_string("parent")
		account = LedgerAccount(
			number=node.optional_string("account_number"),
			name=node.string("account_name"),
			root_type=ROOT_TYPES[kind],
			parent=self.context.gid("Account", parent) if parent else None,
			is_group=node.flag("is_group"),
			currency=node.optional_string("currency_code"),
		)
		return Desired(self.kind, node.id(), account_fields(account))


class TaxMapper:
	"""A tax at its rate, with its registration number. A compound tax (charged on other taxes) is planned."""

	kind = Kind.TAX_RATE
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("compound"):
			raise Unsupported(f"a compound tax ({PLANNED}: a template row on the previous total)")
		return Desired(
			self.kind,
			node.id(),
			tax_rate_fields(node.string("name"), node.decimal("amount"), node.optional_string("number")),
		)


class ProductMapper:
	"""An item at its unit cost (a stock item when FreshBooks counts its inventory), or a service at its
	rate."""

	kind = Kind.PRODUCT
	version = 1

	def map(self, node: Node) -> Desired | None:
		if deleted(node):
			return None
		if Identity.parse(node.id()).type == "Service":
			rate = node.child("rate")
			fields = book_item(
				node.string("name"), None, None, rate.optional_decimal("rate") if rate else None, False
			)
			return Desired(self.kind, node.id(), fields)
		inventory = node.optional_decimal("inventory")
		fields = book_item(
			node.string("name"),
			node.optional_string("sku"),
			node.optional_string("description"),
			amount_of(node, "unit_cost"),
			inventory is not None,
			inventory,
		)
		return Desired(self.kind, node.id(), fields)


class ClientMapper:
	"""A client: an organisation (else a person) with its contact, email, phone, billing address, currency
	and VAT number; an archived client is disabled."""

	kind = Kind.CUSTOMER
	version = 1

	def map(self, node: Node) -> Desired | None:
		if deleted(node):
			return None
		first, last = node.optional_string("fname"), node.optional_string("lname")
		person = " ".join(part for part in (first, last) if part) or None
		organisation = node.optional_string("organization")
		name = organisation or person or node.optional_string("email")
		if name is None:
			raise Unsupported("a client with no name, organisation or email")
		address = address_of(node, "p_", name, organisation, CLIENT_ADDRESS)
		party = Party(
			name=name,
			type="Company" if organisation else "Individual",
			first_name=first,
			last_name=last,
			company=organisation,
			email=node.optional_string("email"),
			phone=node.optional_string("bus_phone")
			or node.optional_string("mob_phone")
			or node.optional_string("home_phone"),
			addresses=[address] if address else [],
			tax_id=node.optional_string("vat_number"),
			currency=node.optional_string("currency_code"),
			disabled=node.optional_integer("vis_state") == ARCHIVED,
		)
		return Desired(self.kind, node.id(), party_fields(party))


class VendorMapper:
	"""A bill vendor with its primary contact, phone, address, currency and account number; archived is
	disabled."""

	kind = Kind.SUPPLIER
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if deleted(node):
			return None
		name = node.string("vendor_name")
		first, last = (
			node.optional_string("primary_contact_first_name"),
			node.optional_string("primary_contact_last_name"),
		)
		contact = " ".join(part for part in (first, last) if part)
		address = address_of(node, "", name, name, VENDOR_ADDRESS)
		party = Party(
			name=name,
			type="Company",
			first_name=first,
			last_name=last,
			company=name,
			email=node.optional_string("primary_contact_email"),
			phone=node.optional_string("phone"),
			addresses=[address] if address else [],
			currency=node.optional_string("currency_code"),
			contacts=[
				ContactPerson(
					self.context.gid("VendorContact", node.string("source_id")),
					contact,
					node.optional_string("primary_contact_email"),
				)
			]
			if contact
			else [],
			disabled=node.optional_integer("vis_state") == ARCHIVED,
		)
		return Desired(self.kind, node.id(), party_fields(party))


class ProjectMapper:
	"""A project for its client: completed, active (Open) or on hold, due on its date, with its
	description."""

	kind = Kind.PROJECT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("complete"):
			status = "Completed"
		elif node.optional_flag("active") is False:
			status = "On hold"
		else:
			status = "Open"
		client = node.optional_integer("client_id")
		fields = project_fields(
			node.string("title"),
			status,
			self.context.gid("Client", client) if client else None,
			end=day(node.optional_string("due_date")),
			notes=node.optional_string("description"),
		)
		return Desired(self.kind, node.id(), fields)


class TimeEntryMapper:
	"""A time entry as a Timesheet: from its start for its duration (in hours, to the microhour), on its
	project for its client, billable or not, with its note; a running timer is a draft, a deleted entry isn't
	kept."""

	kind = Kind.TIMESHEET
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("active") is False:
			return None
		duration = node.optional_integer("duration") or 0
		project, client = node.optional_integer("project_id"), node.optional_integer("client_id")
		log = TimeLog(
			start=node.time("started_at"),
			hours=(Decimal(duration) / SECONDS_PER_HOUR).quantize(HOURS_PLACES, ROUND_HALF_UP),
			description=node.optional_string("note"),
			billable=node.optional_flag("billable") is True,
			submitted=node.optional_flag("is_logged") is not False,
			customer=self.context.gid("Client", client) if client else None,
			project=self.context.gid("Project", project) if project else None,
		)
		return Desired(self.kind, node.id(), timesheet_fields(log))


class Amounts:
	"""An invoice's or estimate's lines, discount and taxes, which must make its amount exactly."""

	def __init__(self, node: Node, currency: str) -> None:
		cent = Decimal(1).scaleb(-minor_units(currency))
		self.lines: list[OrderLine] = []
		bases: dict[tuple[str, Decimal], Decimal] = {}
		subtotal = Decimal(0)
		for line in node.children("lines"):
			qty = line.decimal("qty")
			amount = amount_of(line, "amount")
			if amount == 0 and qty:
				amount = qty * amount_of(line, "unit_cost")
			self.lines.append(
				order_line(
					str(line.optional_integer("lineid") or len(self.lines) + 1),
					None,
					None,
					line.optional_string("name") or line.optional_string("description") or "Line",
					qty,
					amount,
					currency,
				)
			)
			subtotal += amount
			for name_field, rate_field in TAX_SLOTS:
				name, rate = line.optional_string(name_field), line.optional_decimal(rate_field)
				if name and rate:
					bases[(name, rate)] = bases.get((name, rate), Decimal(0)) + amount
		self.discount = amount_of(node, "discount_total")
		share = (subtotal - self.discount) / subtotal if subtotal else Decimal(1)
		self.taxes = [
			(f"{name} {decimal_text(rate)}%", (base * share * rate / HUNDRED).quantize(cent, ROUND_HALF_UP))
			for (name, rate), base in sorted(bases.items())
		]
		self.total = amount_of(node, "amount")
		made = subtotal - self.discount + sum((amount for _, amount in self.taxes), Decimal(0))
		if made != self.total:
			raise Unsupported(
				f"the lines, discount and taxes come to {decimal_text(made)}, not the amount {decimal_text(self.total)}"
			)


def currency_of(node: Node) -> str:
	return node.optional_string("currency_code") or code_of(node, "amount") or ""


class EstimateMapper:
	"""An estimate as a draft Quotation to its client: its lines, discount and taxes, which must make its
	amount."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if deleted(node):
			return None
		currency = currency_of(node)
		amounts = Amounts(node, currency)
		fields: Fields = {
			"number": node.optional_string("estimate_number") or node.string("source_id"),
			"currency": currency,
			"created_at": midday(required_day(node.string("create_date"), node.path)),
			"customer": self.context.gid("Client", node.integer("customerid")),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": list[Field]([line_fields(line) for line in amounts.lines]),
			"discount": amounts.discount,
			"charges": list[Field](
				[charge_fields(Charge(TAX, name, amount)) for name, amount in amounts.taxes if amount]
			),
			"total": amounts.total,
			"remarks": node.optional_string("notes"),
		}
		return Desired(self.kind, node.id(), fields)


class InvoiceMapper:
	"""An invoice to its client (a draft while FreshBooks' is, else submitted) or a credit note (a return of
	its amount as one line: FreshBooks documents no credit note lines)."""

	kind = Kind.SALES_INVOICE
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if deleted(node):
			return None
		if Identity.parse(node.id()).type == "CreditNote":
			return self._credit_note(node)
		currency = currency_of(node)
		amounts = Amounts(node, currency)
		status = node.optional_string("v3_status") or ""
		document = TradeDocument(
			number=node.optional_string("invoice_number") or node.string("source_id"),
			date=required_day(node.string("create_date"), node.path),
			party=self.context.gid("Client", node.integer("customerid")),
			currency=currency,
			lines=[TradeLine(line) for line in amounts.lines],
			taxes=[TradeTax(name, amount) for name, amount in amounts.taxes if amount],
			total=amounts.total,
			submitted=status not in DRAFT_INVOICES,
			due_date=day(node.optional_string("due_date")),
			discount=amounts.discount or None,
		)
		return Desired(self.kind, node.id(), trade_fields(document))

	def _credit_note(self, node: Node) -> Desired:
		amount = amount_of(node, "amount")
		currency = code_of(node, "amount") or ""
		number = node.optional_string("credit_number") or node.string("source_id")
		kind = node.optional_string("credit_type") or "credit"
		document = TradeDocument(
			number=number,
			date=required_day(node.string("create_date"), node.path),
			party=self.context.gid("Client", node.string("clientid")),
			currency=currency,
			lines=[
				TradeLine(
					order_line(
						"credit", None, None, f"Credit note {number} ({kind})", Decimal(-1), -amount, currency
					)
				)
			],
			taxes=[],
			total=-amount,
			submitted=True,
			is_return=True,
		)
		return Desired(self.kind, node.id(), trade_fields(document))


class BillMapper:
	"""A bill from its vendor: its lines, their taxes (the amounts FreshBooks computed), which must make its
	total exactly; its number is the vendor's reference."""

	kind = Kind.PURCHASE_INVOICE
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if deleted(node):
			return None
		currency = currency_of(node)
		lines: list[TradeLine] = []
		taxes: dict[str, Decimal] = {}
		for index, line in enumerate(node.children("lines"), start=1):
			category = line.child("category")
			name = (
				line.optional_string("description")
				or (category.optional_string("category") if category else None)
				or "Line"
			)
			lines.append(
				TradeLine(
					order_line(
						str(line.optional_integer("id") or index),
						None,
						None,
						name,
						line.decimal("quantity"),
						amount_of(line, "amount"),
						currency,
					)
				)
			)
			for name_field, amount_field in BILL_TAX_SLOTS:
				tax_name = line.optional_string(name_field)
				tax = amount_of(line, amount_field) if line.has(amount_field) else Decimal(0)
				if tax_name and tax:
					taxes[tax_name] = taxes.get(tax_name, Decimal(0)) + tax
		number = node.optional_string("bill_number") or node.string("source_id")
		document = TradeDocument(
			number=number,
			date=required_day(node.string("issue_date"), node.path),
			party=self.context.gid("Vendor", node.integer("vendorid")),
			currency=currency,
			lines=lines,
			taxes=[TradeTax(name, amount) for name, amount in sorted(taxes.items())],
			total=amount_of(node, "total_amount"),
			submitted=True,
			due_date=day(node.optional_string("due_date")),
			reference=node.optional_string("bill_number"),
		)
		return Desired(self.kind, node.id(), trade_fields(document))


class PaymentMapper:
	"""A payment from a client allocated to the invoice it pays, or a bill payment to a vendor allocated to its
	bill. A payment made from a client's credit moved no money (its application to the invoice is planned)."""

	kind = Kind.PAYMENT_ENTRY
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if deleted(node):
			return None
		amount = amount_of(node, "amount")
		currency = code_of(node, "amount") or ""
		if Identity.parse(node.id()).type == "BillPayment":
			vendor = node.optional_integer("vendorid")
			if vendor is None:
				raise Unsupported("a bill payment whose bill names no vendor")
			payment = Payment(
				payment_type=PAY,
				party_type="Supplier",
				party=self.context.gid("Vendor", vendor),
				date=required_day(node.string("paid_date"), node.path),
				amount=amount,
				currency=currency,
				allocations=[
					Allocation(
						Kind.PURCHASE_INVOICE, self.context.gid("Bill", node.integer("billid")), amount
					)
				],
				reference=node.optional_string("payment_type"),
				remarks=node.optional_string("note"),
			)
			return Desired(self.kind, node.id(), payment_fields(payment))
		if node.optional_flag("from_credit"):
			return None
		invoice = node.optional_integer("invoiceid")
		payment = Payment(
			payment_type=RECEIVE,
			party_type="Customer",
			party=self.context.gid("Client", node.integer("clientid")),
			date=required_day(node.string("date"), node.path),
			amount=amount,
			currency=currency,
			allocations=[Allocation(Kind.SALES_INVOICE, self.context.gid("Invoice", invoice), amount)]
			if invoice
			else [],
			reference=node.optional_string("type"),
			remarks=node.optional_string("note"),
		)
		return Desired(self.kind, node.id(), payment_fields(payment))


class JournalMapper:
	"""A manual journal entry, line by line; and an expense or other income as its postings in the ledger."""

	kind = Kind.JOURNAL_ENTRY
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if Identity.parse(node.id()).type == "JournalEntry":
			entered = node.required_child("userEnteredDate")
			date = (
				f"{entered.integer('year'):04d}-{entered.integer('month'):02d}-{entered.integer('day'):02d}"
			)
			lines = [
				JournalLine(
					account=self.context.gid("Account", detail.string("accountId")),
					debit=amount_of(detail, "amount") if detail.string("type") == DEBIT else Decimal(0),
					credit=Decimal(0) if detail.string("type") == DEBIT else amount_of(detail, "amount"),
				)
				for detail in node.children("details")
			]
			remarks = " — ".join(
				p for p in (node.optional_string("name"), node.optional_string("description")) if p
			)
			fields = journal_fields(
				node.optional_string("journalEntryNumber") or node.string("source_id"),
				date,
				lines,
				True,
				remarks or None,
			)
			return Desired(self.kind, node.id(), fields)
		postings = node.children("postings")
		if not postings:
			return None
		what = "Expense" if Identity.parse(node.id()).type == "Expense" else "Other income"
		lines = [
			JournalLine(
				account=self.context.gid("Account", posting.string("account")),
				debit=amount_of(posting, "debit"),
				credit=amount_of(posting, "credit"),
			)
			for posting in postings
		]
		first = postings[0]
		fields = journal_fields(
			f"{what} {node.string('source_id')}",
			required_day(first.string("date"), node.path),
			lines,
			True,
			first.optional_string("description") or first.optional_string("reference"),
		)
		return Desired(self.kind, node.id(), fields)


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		AccountMapper(context),
		TaxMapper(),
		ProductMapper(),
		ClientMapper(),
		VendorMapper(context),
		ProjectMapper(context),
		TimeEntryMapper(context),
		EstimateMapper(context),
		InvoiceMapper(context),
		BillMapper(context),
		PaymentMapper(context),
		JournalMapper(context),
	]
