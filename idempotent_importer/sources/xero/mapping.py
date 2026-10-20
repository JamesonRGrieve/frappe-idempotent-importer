# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Xero → desired ERPNext state, one mapper per kind, in the shared field contract. Xero's records
(developer.xero.com/documentation/api/accounting) are translated here: its ``/Date(ms+0000)/`` dates, line
items (net of tax, whatever ``LineAmountTypes`` the document uses), tracking options as cost centers,
statuses (a deleted or voided record has no copy; a draft is a draft) and the money it moves without a
document of ERPNext's own (spend and receive money, transfers, manual journals) as balanced journals."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal

from idempotent_importer.contract import (
	CUSTOMER_PARTY,
	PAY,
	RECEIVE,
	SUPPLIER_PARTY,
	TAX,
	Address,
	Allocation,
	Charge,
	JournalLine,
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
	journal_fields,
	line_fields,
	party_fields,
	payment_fields,
	project_fields,
	trade_fields,
	tree_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import exact_quotient, minor_units
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.ledger import book_item, country_code, day, midday

MS_DATE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")
MILLISECONDS = 1000
# ERPNext holds an exchange rate to nine decimal places.
RATE_PLACES = 9
ROOT_TYPES = {
	"ASSET": "Asset",
	"LIABILITY": "Liability",
	"EQUITY": "Equity",
	"REVENUE": "Income",
	"EXPENSE": "Expense",
}
ACCOUNT_TYPES = {
	"BANK": "Bank",
	"FIXED": "Fixed Asset",
	"INVENTORY": "Stock",
	"DIRECTCOSTS": "Cost of Goods Sold",
	"EXPENSE": "Expense Account",
	"OVERHEADS": "Expense Account",
	"REVENUE": "Income Account",
	"SALES": "Income Account",
	"OTHERINCOME": "Income Account",
	"EQUITY": "Equity",
	"DEPRECIATN": "Depreciation",
}
SYSTEM_ACCOUNT_TYPES = {"DEBTORS": "Receivable", "CREDITORS": "Payable", "GST": "Tax", "VAT": "Tax"}
GONE = frozenset({"DELETED", "VOIDED"})
DRAFTS = frozenset({"DRAFT", "SUBMITTED"})
INCLUSIVE = frozenset({"Inclusive", "INCLUSIVE"})
SALES_DOCUMENTS = frozenset({"ACCREC", "ACCRECCREDIT"})
CREDIT_NOTES = frozenset({"ACCRECCREDIT", "ACCPAYCREDIT"})
RECEIVING = frozenset({"RECEIVE", "RECEIVE-PREPAYMENT", "RECEIVE-OVERPAYMENT"})
PROJECT_STATUSES = {"INPROGRESS": "Open", "CLOSED": "Completed"}
PLANNED = "planned: card 386"
# Payments Xero makes of an invoice, a credit note (a refund) — and their direction and party.
PAYMENT_TYPES: dict[str, tuple[str, str, str, Kind]] = {
	"ACCRECPAYMENT": (RECEIVE, CUSTOMER_PARTY, "Invoice", Kind.SALES_INVOICE),
	"ACCPAYPAYMENT": (PAY, SUPPLIER_PARTY, "Invoice", Kind.PURCHASE_INVOICE),
	"ARCREDITPAYMENT": (PAY, CUSTOMER_PARTY, "CreditNote", Kind.SALES_INVOICE),
	"APCREDITPAYMENT": (RECEIVE, SUPPLIER_PARTY, "CreditNote", Kind.PURCHASE_INVOICE),
}


def xero_day(value: str | None) -> str | None:
	"""A Xero date (``/Date(1739145600000+0000)/``, or ISO text) as ``YYYY-MM-DD``."""
	if value is None or value == "":
		return None
	match = MS_DATE.match(value.strip())
	if match is None:
		return day(value)
	return datetime.fromtimestamp(int(match.group(1)) / MILLISECONDS, UTC).strftime("%Y-%m-%d")


def required_xero_day(node: Node, field: str) -> str:
	found = xero_day(node.optional_string(field))
	if found is None:
		raise SourceError(f"{node.path}.{field} is missing.")
	return found


def base_rate(node: Node) -> Decimal | None:
	"""ERPNext's rate (company currency per document currency) from Xero's CurrencyRate (document currency
	per one of the organisation's), to ERPNext's nine places; None for the organisation's own currency."""
	rate = node.optional_decimal("CurrencyRate")
	if rate is None or rate in (0, 1):
		return None
	return (Decimal(1) / rate).quantize(Decimal(1).scaleb(-RATE_PLACES))


class Xero:
	"""The connection's identities of Xero's references."""

	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config

	def of(self, type_: str, node: Node | None, field: str) -> str | None:
		value = node.optional_string(field) if node is not None else None
		return self.config.identity(type_, value) if value else None

	def required(self, type_: str, node: Node | None, field: str) -> str:
		found = self.of(type_, node, field)
		if found is None:
			raise Unsupported(f"{node.path if node else 'a record'} has no {field}")
		return found

	def cost_center(self, line: Node) -> tuple[str | None, list[str]]:
		"""A line's first tracking option as its cost center; a second category's option is planned as an
		accounting dimension (ERPNext books a line to one cost center)."""
		tracking = line.children("Tracking")
		if not tracking:
			return None, []
		warnings = [f"its tracking option {t.text('Option')} ({PLANNED})" for t in tracking[1:]]
		return self.of("TrackingOption", tracking[0], "TrackingOptionID"), warnings


def mappers(config: ConnectionConfig) -> list[Mapper]:
	xero = Xero(config)
	return [
		AccountMapper(xero),
		TrackingMapper(xero),
		ItemMapper(xero),
		ContactMapper(xero, Kind.CUSTOMER),
		ContactMapper(xero, Kind.SUPPLIER),
		ProjectMapper(xero),
		QuoteMapper(xero),
		TradeMapper(xero, Kind.PURCHASE_ORDER),
		TradeMapper(xero, Kind.SALES_INVOICE),
		TradeMapper(xero, Kind.PURCHASE_INVOICE),
		PaymentMapper(xero),
		JournalMapper(xero),
	]


def amount(node: Node, field: str) -> Decimal:
	return node.optional_decimal(field) or Decimal(0)


class AccountMapper:
	kind = Kind.ACCOUNT
	version = 1

	def __init__(self, xero: Xero) -> None:
		self.xero = xero

	def map(self, node: Node) -> Desired | None:
		if node.text("Status") == "DELETED":
			return None
		account = LedgerAccount(
			number=node.optional_string("Code"),
			name=node.string("Name"),
			root_type=ROOT_TYPES[node.string("Class")],
			account_type=SYSTEM_ACCOUNT_TYPES.get(node.text("SystemAccount"))
			or ACCOUNT_TYPES.get(node.text("Type")),
			currency=node.optional_string("CurrencyCode"),
		)
		return Desired(self.kind, node.id(), account_fields(account))


class TrackingMapper:
	"""A tracking category as a group cost center, and each of its options as a cost center in it."""

	kind = Kind.COST_CENTER
	version = 1

	def __init__(self, xero: Xero) -> None:
		self.xero = xero

	def map(self, node: Node) -> Desired | None:
		if node.text("Status") == "DELETED":
			return None
		if node.has("TrackingOptionID") and not node.has("Options"):
			parent = self.xero.of("TrackingCategory", node, "TrackingCategoryID")
			return Desired(self.kind, node.id(), tree_fields(node.string("Name"), parent))
		return Desired(self.kind, node.id(), tree_fields(node.string("Name"), None, is_group=True))


class ItemMapper:
	kind = Kind.PRODUCT
	version = 1

	def __init__(self, xero: Xero) -> None:
		self.xero = xero

	def map(self, node: Node) -> Desired:
		stock = node.optional_flag("IsTrackedAsInventory") is True
		sales = node.child("SalesDetails")
		fields = book_item(
			name=node.optional_string("Name") or node.string("Code"),
			sku=node.string("Code"),
			description=node.optional_string("Description"),
			price=sales.optional_decimal("UnitPrice") if sales else None,
			is_stock_item=stock,
			stock_qty=node.optional_decimal("QuantityOnHand") if stock else None,
		)
		return Desired(self.kind, node.id(), fields)


class ContactMapper:
	"""A contact as a customer or a supplier (one that is both is both, under one identity per kind)."""

	version = 1

	def __init__(self, xero: Xero, kind: Kind) -> None:
		self.xero = xero
		self.kind = kind

	def map(self, node: Node) -> Desired:
		email = node.optional_string("EmailAddress")
		phones = node.children("Phones")
		phone = next(
			(
				f"{p.text('PhoneAreaCode')} {p.text('PhoneNumber')}".strip()
				for p in phones
				if p.optional_string("PhoneNumber") and p.text("PhoneType") in ("DEFAULT", "MOBILE")
			),
			None,
		)
		found: list[Fields] = []
		warnings: list[str] = []
		for address in node.children("Addresses"):
			if not address.optional_string("AddressLine1") or not address.optional_string("City"):
				continue
			code = country_code(address.optional_string("Country"))
			if code is None:
				warnings.append(f"an address names no country ERPNext knows ({address.text('Country')})")
				continue
			found.append(
				address_fields(
					Address(
						address_type="Billing" if address.text("AddressType") == "POBOX" else "Shipping",
						name=address.optional_string("AttentionTo"),
						company=None,
						line1=address.string("AddressLine1"),
						line2=address.optional_string("AddressLine2"),
						city=address.string("City"),
						state=address.optional_string("Region"),
						postcode=address.optional_string("PostalCode"),
						country_code=code,
						email=email,
						phone=phone,
					)
				)
			)
		first, last = node.optional_string("FirstName"), node.optional_string("LastName")
		person = bool(first) and node.string("Name") == " ".join(part for part in (first, last) if part)
		party = Party(
			name=node.string("Name"),
			type="Individual" if person else "Company",
			first_name=first,
			last_name=last,
			company=None if person else node.string("Name"),
			email=email,
			phone=phone,
			addresses=found,
			tax_id=node.optional_string("TaxNumber"),
			currency=node.optional_string("DefaultCurrency"),
		)
		return Desired(self.kind, node.id(), party_fields(party), warnings=tuple(warnings))


class ProjectMapper:
	kind = Kind.PROJECT
	version = 1

	def __init__(self, xero: Xero) -> None:
		self.xero = xero

	def map(self, node: Node) -> Desired:
		fields = project_fields(
			name=node.string("name"),
			status=PROJECT_STATUSES.get(node.text("status"), "Open"),
			customer=self.xero.of("Contact", node, "contactId"),
			end=xero_day(node.optional_string("deadlineUtc")),
		)
		return Desired(self.kind, node.id(), fields)


class Lines:
	"""A document's line items as trade lines net of tax, and its taxes by tax type."""

	def __init__(self, xero: Xero, node: Node, sign: Decimal) -> None:
		self.xero = xero
		self.node = node
		self.sign = sign
		self.currency = node.optional_string("CurrencyCode") or ""
		self.inclusive = node.text("LineAmountTypes") in INCLUSIVE
		self.warnings: list[str] = []

	def trade(self) -> list[TradeLine]:
		lines: list[TradeLine] = []
		for index, line in enumerate(self.node.children("LineItems")):
			tax = amount(line, "TaxAmount")
			net = amount(line, "LineAmount") - (tax if self.inclusive else Decimal(0))
			qty = line.optional_decimal("Quantity") or Decimal(1)
			unit = line.optional_decimal("UnitAmount")
			rate = (
				unit
				if unit is not None and unit * qty == net
				else exact_quotient(net, qty, minor_units(self.currency))
			)
			name = line.optional_string("Description") or line.optional_string("ItemCode") or "Line"
			if rate is None:
				raise Unsupported(f'line "{name}" ({net} for {qty}) has no exact unit price')
			cost_center, warnings = self.xero.cost_center(line)
			self.warnings += warnings
			item = line.child("Item")
			lines.append(
				TradeLine(
					OrderLine(
						line.optional_string("LineItemID") or str(index),
						self.xero.of("Item", item, "ItemID"),
						None,
						name,
						self.sign * qty,
						rate,
					),
					account=self.xero.of("Account", line, "AccountID"),
					cost_center=cost_center,
				)
			)
		return lines

	def taxes(self) -> list[TradeTax]:
		by_type: dict[str, Decimal] = {}
		for line in self.node.children("LineItems"):
			tax = amount(line, "TaxAmount")
			if tax:
				key = line.optional_string("TaxType") or "Tax"
				by_type[key] = by_type.get(key, Decimal(0)) + tax
		return [TradeTax(f"Tax {key}", self.sign * value) for key, value in by_type.items()]


class TradeMapper:
	"""Purchase orders, invoices (sales: ACCREC; purchase: ACCPAY) and credit notes (credit and debit
	notes) as ERPNext's orders and invoices; a deleted or voided one has no copy, a draft stays a draft."""

	version = 1

	def __init__(self, xero: Xero, kind: Kind) -> None:
		self.xero = xero
		self.kind = kind

	def map(self, node: Node) -> Desired | None:
		if node.text("Status") in GONE:
			return None
		type_ = node.text("Type")
		credit = type_ in CREDIT_NOTES
		sign = Decimal(-1) if credit else Decimal(1)
		lines = Lines(self.xero, node, sign)
		number = (
			node.optional_string("InvoiceNumber")
			or node.optional_string("CreditNoteNumber")
			or node.optional_string("PurchaseOrderNumber")
			or node.id().rsplit("/", 1)[-1]
		)
		document = TradeDocument(
			number=number,
			date=required_xero_day(node, "Date"),
			party=self.xero.required("Contact", node.child("Contact"), "ContactID"),
			currency=node.string("CurrencyCode"),
			lines=lines.trade(),
			taxes=lines.taxes(),
			total=sign * amount(node, "Total"),
			submitted=node.text("Status") not in DRAFTS,
			due_date=xero_day(node.optional_string("DueDate") or node.optional_string("DeliveryDate")),
			exchange_rate=base_rate(node),
			is_return=credit,
			reference=node.optional_string("Reference"),
		)
		return Desired(self.kind, node.id(), trade_fields(document), warnings=tuple(lines.warnings))


class QuoteMapper:
	"""A quote as a draft Quotation (the shared ``draft_order``); a deleted or declined one has none."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def __init__(self, xero: Xero) -> None:
		self.xero = xero

	def map(self, node: Node) -> Desired | None:
		if node.text("Status") in ("DELETED", "DECLINED"):
			return None
		lines = Lines(self.xero, node, Decimal(1))
		fields: Fields = {
			"number": node.optional_string("QuoteNumber") or node.id().rsplit("/", 1)[-1],
			"currency": node.string("CurrencyCode"),
			"created_at": midday(required_xero_day(node, "Date")),
			"customer": self.xero.required("Contact", node.child("Contact"), "ContactID"),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": list[Field](line_fields(trade.line) for trade in lines.trade()),
			"discount": Decimal(0),
			"charges": [charge_fields(Charge(TAX, tax.description, tax.amount)) for tax in lines.taxes()],
			"total": amount(node, "Total"),
			"remarks": node.optional_string("Summary"),
		}
		rate = base_rate(node)
		if rate is not None:
			fields["exchange_rate"] = rate
		return Desired(self.kind, node.id(), fields, warnings=tuple(lines.warnings))


class PaymentMapper:
	"""Payments of invoices and refunds of credit notes, and prepayments and overpayments (money received or
	paid ahead of an invoice) with the invoices they were allocated to. A deleted or voided one has none; a
	refund of a prepayment or overpayment is planned."""

	kind = Kind.PAYMENT_ENTRY
	version = 1

	def __init__(self, xero: Xero) -> None:
		self.xero = xero

	def map(self, node: Node) -> Desired | None:
		if node.text("Status") in GONE:
			return None
		advance = Identity.parse(node.id()).type in ("Prepayment", "Overpayment")
		payment = self._advance(node) if advance else self._payment(node)
		return Desired(self.kind, node.id(), payment_fields(payment))

	def _payment(self, node: Node) -> Payment:
		known = PAYMENT_TYPES.get(node.text("PaymentType"))
		if known is None:
			raise Unsupported(f"a {node.text('PaymentType')} payment ({PLANNED})")
		direction, party_type, document_field, kind = known
		document = node.required_child(document_field)
		document_gid = self.xero.required(document_field, document, f"{document_field}ID")
		value = amount(node, "Amount")
		return Payment(
			payment_type=direction,
			party_type=party_type,
			party=self.xero.required("Contact", document.child("Contact"), "ContactID"),
			date=required_xero_day(node, "Date"),
			amount=value,
			currency=document.string("CurrencyCode"),
			allocations=[Allocation(kind, document_gid, value)],
			exchange_rate=base_rate(node),
			account=self.xero.of("Account", node.child("Account"), "AccountID"),
			reference=node.optional_string("Reference"),
		)

	def _advance(self, node: Node) -> Payment:
		receiving = node.text("Type") in RECEIVING
		allocations = [
			Allocation(
				Kind.SALES_INVOICE if receiving else Kind.PURCHASE_INVOICE,
				self.xero.required("Invoice", allocation.child("Invoice"), "InvoiceID"),
				amount(allocation, "Amount"),
			)
			for allocation in node.children("Allocations")
			if allocation.optional_flag("IsDeleted") is not True
		]
		return Payment(
			payment_type=RECEIVE if receiving else PAY,
			party_type=CUSTOMER_PARTY if receiving else SUPPLIER_PARTY,
			party=self.xero.required("Contact", node.child("Contact"), "ContactID"),
			date=required_xero_day(node, "Date"),
			amount=amount(node, "Total"),
			currency=node.string("CurrencyCode"),
			allocations=allocations,
			exchange_rate=base_rate(node),
			account=self.xero.of("Account", node.child("BankAccount"), "AccountID"),
			reference=node.optional_string("Reference"),
		)


class JournalMapper:
	"""Manual journals as they post (a positive line a debit, a negative one a credit), spend and receive
	money as the journal they post (the bank account against each line's account, and the tax to the
	organisation's tax account), and transfers between bank accounts. A deleted or voided one has none."""

	kind = Kind.JOURNAL_ENTRY
	version = 1

	def __init__(self, xero: Xero) -> None:
		self.xero = xero

	def map(self, node: Node) -> Desired | None:
		if node.text("Status") in GONE | {"ARCHIVED"}:
			return None
		warnings: list[str] = []
		if node.has("JournalLines"):
			lines, warnings = self._manual(node)
			number, remarks = node.id().rsplit("/", 1)[-1], node.optional_string("Narration")
			submitted = node.text("Status") == "POSTED"
		elif node.has("FromBankAccount"):
			lines = self._transfer(node)
			number, remarks, submitted = node.id().rsplit("/", 1)[-1], node.optional_string("Reference"), True
		else:
			lines, warnings = self._bank(node)
			number, remarks, submitted = node.id().rsplit("/", 1)[-1], node.optional_string("Reference"), True
		fields = journal_fields(number, required_xero_day(node, "Date"), lines, submitted, remarks)
		return Desired(self.kind, node.id(), fields, warnings=tuple(warnings))

	def _tax_line(self, node: Node, debit: Decimal, credit: Decimal) -> JournalLine:
		return JournalLine(self.xero.required("Account", node, "TaxAccountID"), debit, credit)

	def _manual(self, node: Node) -> tuple[list[JournalLine], list[str]]:
		lines: list[JournalLine] = []
		warnings: list[str] = []
		inclusive = node.text("LineAmountTypes") in INCLUSIVE
		for line in node.children("JournalLines"):
			tax = amount(line, "TaxAmount")
			value = amount(line, "LineAmount") - (tax if inclusive else Decimal(0))
			cost_center, more = self.xero.cost_center(line)
			warnings += more
			account = self.xero.required("Account", line, "AccountID")
			lines.append(
				JournalLine(account, max(value, Decimal(0)), max(-value, Decimal(0)), cost_center=cost_center)
			)
			if tax:
				lines.append(self._tax_line(node, max(tax, Decimal(0)), max(-tax, Decimal(0))))
		return lines, warnings

	def _bank(self, node: Node) -> tuple[list[JournalLine], list[str]]:
		receiving = node.text("Type") == "RECEIVE"
		party = self.xero.of("Contact", node.child("Contact"), "ContactID")
		inclusive = node.text("LineAmountTypes") in INCLUSIVE
		total = amount(node, "Total")
		lines = [
			JournalLine(
				self.xero.required("Account", node.child("BankAccount"), "AccountID"),
				total if receiving else Decimal(0),
				Decimal(0) if receiving else total,
			)
		]
		warnings: list[str] = []
		tax_total = Decimal(0)
		for line in node.children("LineItems"):
			tax = amount(line, "TaxAmount")
			tax_total += tax
			net = amount(line, "LineAmount") - (tax if inclusive else Decimal(0))
			cost_center, more = self.xero.cost_center(line)
			warnings += more
			lines.append(
				JournalLine(
					self.xero.required("Account", line, "AccountID"),
					Decimal(0) if receiving else net,
					net if receiving else Decimal(0),
					party_type=(CUSTOMER_PARTY if receiving else SUPPLIER_PARTY) if party else None,
					party=party,
					cost_center=cost_center,
				)
			)
		if tax_total:
			lines.append(
				self._tax_line(
					node, Decimal(0) if receiving else tax_total, tax_total if receiving else Decimal(0)
				)
			)
		return lines, warnings

	def _transfer(self, node: Node) -> list[JournalLine]:
		if base_rate(node) is not None:
			raise Unsupported(f"a transfer between accounts in different currencies ({PLANNED})")
		value = amount(node, "Amount")
		return [
			JournalLine(
				self.xero.required("Account", node.child("ToBankAccount"), "AccountID"), value, Decimal(0)
			),
			JournalLine(
				self.xero.required("Account", node.child("FromBankAccount"), "AccountID"), Decimal(0), value
			),
		]
