# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Sage Intacct → desired ERPNext state, one mapper per kind, in the shared field contract. The
gateway's 2.1 objects (as its response DTD declares them) are translated here: IDs become identities,
``active``/``inactive`` statuses disabled flags, ``year``/``month``/``day`` dates ISO dates, line items and
GL entries trade lines and journal lines.

The chart's root types: an account's type (balance sheet or income statement) and normal balance settle
asset, liability, income or expense; equity accounts are balance sheet credit accounts too, so the
connection's Field Map names any account that is equity (``{"root_types": {"3000": "Equity"}}``)."""

from __future__ import annotations

from decimal import Decimal

from idempotent_importer.contract import (
	CUSTOMER_PARTY,
	EMPLOYEE_PARTY,
	PAY,
	RECEIVE,
	SUPPLIER_PARTY,
	Address,
	Allocation,
	ContactPerson,
	Employee,
	JournalLine,
	LedgerAccount,
	OrderLine,
	Party,
	Payment,
	TradeDocument,
	TradeLine,
	account_fields,
	address_fields,
	employee_fields,
	journal_fields,
	line_fields,
	party_fields,
	payment_fields,
	project_fields,
	trade_fields,
	tree_fields,
	warehouse_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import exact_quotient, minor_units
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.ledger import book_item, country_code, midday
from idempotent_importer.sources.sage_intacct.reader import FieldMap, derived_key

PLANNED = "planned: card 386"
ACTIVE, INACTIVE = "active", "inactive"
DEBIT, CREDIT = "debit", "credit"
ROOTS = {
	("balancesheet", DEBIT): "Asset",
	("balancesheet", CREDIT): "Liability",
	("incomestatement", CREDIT): "Income",
	("incomestatement", DEBIT): "Expense",
}
ACCOUNT_TYPES = {"Income": "Income Account", "Expense": "Expense Account"}
STOCK_TYPES = frozenset({"Inventory", "Stockable Kit"})
GENDERS = {"male": "Male", "female": "Female"}
PROJECT_STATUSES = {"Completed": "Completed", "Cancelled": "Cancelled", "On Hold": "On hold"}
# A document's state: drafts aren't booked yet, reversed ones are gone.
DRAFT_STATES = frozenset({"Draft", "Pending", "Submitted", "Partially Approved"})
GONE_STATES = frozenset({"Reversed", "Reversal", "Declined", "Voided", "Closed by user"})


class Company:
	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config
		self.map = FieldMap.of(config.field_map)

	def of(self, type_: str, key: str | None) -> str | None:
		return self.config.identity(type_, key) if key and "/" not in key else None


def amount(node: Node | None, field: str) -> Decimal:
	text = node.text(field) if node is not None else ""
	return Decimal(text) if text else Decimal(0)


def date_of(node: Node | None, field: str) -> str | None:
	"""A ``year``/``month``/``day`` date as ``YYYY-MM-DD``."""
	holder = node.child(field) if node is not None else None
	if holder is None or not holder.text("year"):
		return None
	return f"{int(holder.text('year')):04d}-{int(holder.text('month')):02d}-{int(holder.text('day')):02d}"


def required_date(node: Node, field: str) -> str:
	found = date_of(node, field)
	if found is None:
		raise SourceError(f"Sage Intacct record {node.string('source_key')} has no {field}.")
	return found


def items_of(node: Node, holder: str, item: str) -> list[Node]:
	group = node.child(holder)
	if group is None:
		return []
	return (
		group.children(item) if isinstance(group.get(item), list) else [c for c in [group.child(item)] if c]
	)


def contact_of(holder: Node | None) -> Node | None:
	"""A ``contactinfo``/``billto``/``shipto``/``personalinfo``'s contact (when given whole)."""
	return holder.child("contact") if holder is not None else None


def mappers(config: ConnectionConfig) -> list[Mapper]:
	company = Company(config)
	return [
		AccountMapper(company),
		DepartmentMapper(Kind.COST_CENTER, company),
		DepartmentMapper(Kind.DEPARTMENT, company),
		WarehouseMapper(company),
		ItemMapper(company),
		PartyMapper(Kind.CUSTOMER, company),
		PartyMapper(Kind.SUPPLIER, company),
		DesignationMapper(company),
		EmployeeMapper(company),
		ProjectMapper(company),
		TransactionMapper(Kind.DRAFT_ORDER, company),
		TransactionMapper(Kind.SALES_ORDER, company),
		TransactionMapper(Kind.PURCHASE_ORDER, company),
		InvoiceMapper(Kind.SALES_INVOICE, company),
		InvoiceMapper(Kind.PURCHASE_INVOICE, company),
		PaymentMapper(company),
		JournalMapper(company),
	]


class AccountMapper:
	kind = Kind.ACCOUNT
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		number = node.text("glaccountno")
		root = self.company.map.root_types.get(number) or ROOTS.get(
			(node.text("accounttype"), node.text("normalbalance"))
		)
		if root is None:
			raise Unsupported(
				f"an account of type {node.text('accounttype') or 'none'} and normal balance {node.text('normalbalance') or 'none'}"
			)
		account = LedgerAccount(
			number=number,
			name=node.text("title") or number,
			root_type=root,
			account_type=ACCOUNT_TYPES.get(root),
		)
		fields = account_fields(account)
		if node.text("status") == INACTIVE:
			fields |= {"disabled": True}
		return Desired(self.kind, node.id(), fields)


class DepartmentMapper:
	"""A department as a Cost Center (top-level, never a group: Intacct posts to a parent department) and as
	an HR Department under its parent."""

	def __init__(self, kind: Kind, company: Company) -> None:
		self.kind = kind
		self.version = 1
		self.company = company

	def map(self, node: Node) -> Desired | None:
		name = node.text("title") or node.string("source_key")
		inactive = node.text("status") == INACTIVE
		if self.kind == Kind.COST_CENTER:
			return Desired(self.kind, node.id(), tree_fields(name, None, False, inactive))
		parent = self.company.of("Department", node.text("parentid"))
		return Desired(self.kind, node.id(), tree_fields(name, parent, False, inactive))


def address_of(contact: Node | None, kind: str, name: str | None) -> Fields | None:
	address = contact.child("mailaddress") if contact is not None else None
	line1, city = (address.text("address1"), address.text("city")) if address is not None else ("", "")
	if address is None or not line1 or not city:
		return None
	return address_fields(
		Address(
			address_type=kind,
			name=name,
			company=None,
			line1=line1,
			line2=address.text("address2") or None,
			city=city,
			state=address.text("state") or None,
			postcode=address.text("zip") or None,
			country_code=country_code(address.text("isocountrycode") or address.text("country")),
			email=None,
			phone=None,
		)
	)


class WarehouseMapper:
	kind = Kind.WAREHOUSE
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		name = node.text("name") or node.string("source_key")
		fields = warehouse_fields(
			name, None, False, address_of(contact_of(node.child("shipto")), "Warehouse", name)
		)
		return Desired(
			self.kind, node.id(), fields | ({"disabled": True} if node.text("status") == INACTIVE else {})
		)


class ItemMapper:
	"""An item: name, ID, extended description, UPC, and whether it is stocked. An inactive item is retired.
	Prices live in price lists (planned)."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		if node.text("status") == INACTIVE:
			return None
		fields = book_item(
			name=node.text("itemname") or node.string("source_key"),
			sku=node.text("itemid"),
			description=node.text("extdescription") or None,
			price=None,
			is_stock_item=node.text("itemtype") in STOCK_TYPES,
		)
		fields["barcode"] = node.text("upc") or None
		return Desired(self.kind, node.id(), fields)


class PartyMapper:
	"""A customer or vendor: name, its contact's email and phone, its bill-to (or pay-to) and ship-to (or
	return-to) addresses, tax ID, currency and its contact list; an inactive one stays, disabled."""

	def __init__(self, kind: Kind, company: Company) -> None:
		self.kind = kind
		self.version = 1
		self.company = company

	def map(self, node: Node) -> Desired | None:
		name = node.text("name") or node.string("source_key")
		main = contact_of(node.child("contactinfo"))
		billing = contact_of(node.child("billto") or node.child("payto")) or main
		shipping = contact_of(node.child("shipto") or node.child("returnto"))
		addresses = [
			found
			for found in (address_of(billing, "Billing", name), address_of(shipping, "Shipping", name))
			if found is not None
		]
		people = [
			ContactPerson(
				gid=self.company.config.identity("Contact", derived_key(contact)),
				name=contact,
				job_title=item.text("category") or None,
			)
			for item in items_of(node, "contactlist", "contactitem")
			if (contact := item.text("contactname"))
		]
		party = Party(
			name=name,
			type="Company",
			company=name,
			first_name=main.text("firstname") or None if main is not None else None,
			last_name=main.text("lastname") or None if main is not None else None,
			email=main.text("email1") or None if main is not None else None,
			phone=main.text("phone1") or None if main is not None else None,
			addresses=addresses,
			tax_id=node.text("taxid") or None,
			currency=node.text("currency") or None,
			contacts=people,
			disabled=node.text("status") == INACTIVE,
		)
		return Desired(self.kind, node.id(), party_fields(party))


class DesignationMapper:
	kind = Kind.DESIGNATION
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), {"name": node.string("name")})


class EmployeeMapper:
	kind = Kind.EMPLOYEE
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		person = contact_of(node.child("personalinfo"))
		first = (person.text("firstname") if person is not None else "") or node.string("source_key")
		ended = date_of(node, "enddate")
		title = node.text("title")
		employee = Employee(
			first_name=first,
			last_name=(person.text("lastname") or None) if person is not None else None,
			gender=GENDERS.get(node.text("gender").lower()),
			date_of_birth=date_of(node, "birthdate"),
			date_of_joining=date_of(node, "startdate"),
			relieving_date=ended,
			status="Active" if node.text("status") != INACTIVE else ("Left" if ended else "Inactive"),
			employee_number=node.text("employeeid"),
			department=self.company.of("Department", node.text("departmentid")),
			designation=self.company.config.identity("Designation", derived_key(title)) if title else None,
			reports_to=self.company.of("Employee", node.text("supervisorid")),
			company_email=(person.text("email1") or None) if person is not None else None,
			phone=(person.text("phone1") or None) if person is not None else None,
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class ProjectMapper:
	kind = Kind.PROJECT
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		status = PROJECT_STATUSES.get(node.text("projectstatus"), "Open")
		if node.text("status") == INACTIVE and status == "Open":
			status = "Completed"
		fields = project_fields(
			name=node.text("name") or node.string("source_key"),
			status=status,
			customer=self.company.of("Customer", node.text("customerid")),
			start=date_of(node, "begindate"),
			end=date_of(node, "enddate"),
			notes=node.text("description") or None,
		)
		return Desired(self.kind, node.id(), fields)


def state_of(node: Node, field: str) -> bool | None:
	"""Booked (True), a draft (False) or gone (None), by the record's state."""
	state = node.text(field)
	if state in GONE_STATES:
		return None
	return state not in DRAFT_STATES


def foreign(node: Node) -> bool:
	currency, base = node.text("currency"), node.text("basecurr")
	return bool(currency and base and currency != base)


class TransactionMapper:
	"""An order entry quote (a draft Quotation) or sales order, or a purchase order: its items' quantities and
	prices, in the transaction's currency at its exchange rate."""

	def __init__(self, kind: Kind, company: Company) -> None:
		self.kind = kind
		self.version = 1
		self.company = company

	def map(self, node: Node) -> Desired | None:
		booked = state_of(node, "transactionstate")
		if booked is None:
			return None
		sale = node.string("object") == "sotransaction"
		holder, item = ("sotransitems", "sotransitem") if sale else ("potransitems", "potransitem")
		currency = node.text("currency") or node.text("basecurr")
		if not currency:
			raise SourceError(f"Sage Intacct transaction {node.string('source_key')} has no currency.")
		lines: list[TradeLine] = []
		for index, entry in enumerate(items_of(node, holder, item)):
			qty = amount(entry, "quantity") or Decimal(1)
			total = amount(entry, "trx_value") if foreign(node) else amount(entry, "totalamount")
			name = entry.text("itemdesc") or entry.text("itemid") or "Line"
			rate = exact_quotient(total, qty, minor_units(currency))
			if rate is None:
				raise Unsupported(f'line "{name}" ({total} for {qty}) has no exact unit price in {currency}')
			lines.append(
				TradeLine(
					OrderLine(
						f"line:{entry.text('line_no') or index}",
						self.company.of("Item", entry.text("itemid")),
						None,
						name,
						qty,
						rate,
					),
					cost_center=self.company.of("Department", entry.text("departmentid")),
				)
			)
		total = sum((line.line.qty * line.line.rate for line in lines), Decimal(0))
		party = self.company.of(
			"Customer" if sale else "Vendor", node.text("customerid") or node.text("vendorid")
		)
		if party is None:
			raise SourceError(f"Sage Intacct transaction {node.string('source_key')} has no party.")
		exchange = node.text("exchrate")
		document = TradeDocument(
			number=node.text("documentnumber") or node.text("transactionid") or node.string("source_key"),
			date=required_date(node, "datecreated"),
			party=party,
			currency=currency,
			lines=lines,
			taxes=[],
			total=total,
			submitted=booked,
			due_date=date_of(node, "datedue"),
			exchange_rate=Decimal(exchange) if foreign(node) and exchange else None,
			reference=node.text("referenceno")
			or node.text("customerponumber")
			or node.text("vendordocno")
			or None,
			remarks=node.text("message") or None,
		)
		if self.kind != Kind.DRAFT_ORDER:
			return Desired(self.kind, node.id(), trade_fields(document))
		fields: Fields = {
			"number": document.number,
			"currency": currency,
			"created_at": midday(document.date),
			"customer": party,
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": list[Field](line_fields(trade.line) for trade in lines),
			"discount": Decimal(0),
			"charges": [],
			"total": total,
			"remarks": document.remarks,
		}
		if document.exchange_rate is not None:
			fields["exchange_rate"] = document.exchange_rate
		return Desired(self.kind, node.id(), fields)


class InvoiceMapper:
	"""An AR invoice or an AP bill: each line item's amount on its GL account and department, the
	transaction's total (in its currency; a negative one is a credit, booked as a return)."""

	def __init__(self, kind: Kind, company: Company) -> None:
		self.kind = kind
		self.version = 1
		self.company = company

	def map(self, node: Node) -> Desired | None:
		booked = state_of(node, "state")
		if booked is None:
			return None
		sale = node.string("object") == "invoice"
		currency = node.text("currency") or node.text("basecurr")
		if not currency:
			raise SourceError(f"Sage Intacct document {node.string('source_key')} has no currency.")
		is_foreign = foreign(node)
		total = amount(node, "trx_totalamount") if is_foreign else amount(node, "totalamount")
		sign = Decimal(-1) if total < 0 else Decimal(1)
		holder = "invoiceitems" if sale else "billitems"
		lines = [
			TradeLine(
				OrderLine(
					f"line:{item.text('line_num') or index}",
					None,
					None,
					item.text("memo") or item.text("glaccountno") or "Line",
					sign,
					(amount(item, "trx_amount") if is_foreign else amount(item, "amount")) * sign,
				),
				account=self.company.of("GLAccount", item.text("glaccountno")),
				cost_center=self.company.of("Department", item.text("departmentid")),
			)
			for index, item in enumerate(items_of(node, holder, "lineitem"))
		]
		base_total = amount(node, "totalamount") if is_foreign else None
		rate = (
			exact_quotient(base_total, total, 9) if is_foreign and base_total is not None and total else None
		)
		if is_foreign and rate is None:
			raise Unsupported(f"the document's exchange rate isn't recorded exactly ({PLANNED})")
		party = self.company.of(
			"Customer" if sale else "Vendor", node.text("customerid") or node.text("vendorid")
		)
		if party is None:
			raise SourceError(f"Sage Intacct document {node.string('source_key')} has no party.")
		document = TradeDocument(
			number=node.text("invoiceno") or node.text("billno") or node.string("source_key"),
			date=required_date(node, "datecreated"),
			party=party,
			currency=currency,
			lines=lines,
			taxes=[],
			total=total,
			submitted=booked,
			due_date=date_of(node, "datedue"),
			exchange_rate=rate,
			is_return=sign < 0,
			reference=node.text("ponumber") or None,
			remarks=node.text("description") or None,
			base_total=base_total,
		)
		return Desired(self.kind, node.id(), trade_fields(document))


class PaymentMapper:
	"""An AR payment (received into its undeposited funds account) or an AP payment, allocated to the
	invoices or bills it pays; a reversed or voided one is gone."""

	kind = Kind.PAYMENT_ENTRY
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		received = node.string("object") == "arpayment"
		booked = state_of(node, "state" if received else "transactionstate")
		if booked is None:
			return None
		items = (
			items_of(node, "arpaymentitems", "arpaymentitem")
			if received
			else items_of(node, "appaymentitems", "appaymentitem")
		)
		allocations = [
			Allocation(
				Kind.SALES_INVOICE if received else Kind.PURCHASE_INVOICE,
				document,
				amount(item, "amount"),
			)
			for item in items
			if (
				document := self.company.of(
					"Invoice" if received else "Bill", item.text("invoicekey" if received else "billkey")
				)
			)
			and amount(item, "amount")
		]
		party = self.company.of(
			"Customer" if received else "Vendor", node.text("customerid") or node.text("vendorid")
		)
		if party is None:
			raise SourceError(f"Sage Intacct payment {node.string('source_key')} has no party.")
		currency = node.text("currency") or node.optional_string("customer_currency")
		if (
			not received
			and currency
			and node.text("paymenttrxamount")
			and amount(node, "paymenttrxamount") != amount(node, "paymentamount")
		):
			raise Unsupported(f"a payment in another currency than the company's ({PLANNED})")
		payment = Payment(
			payment_type=RECEIVE if received else PAY,
			party_type=CUSTOMER_PARTY if received else SUPPLIER_PARTY,
			party=party,
			date=required_date(node, "datereceived" if received else "paymentdate"),
			amount=amount(node, "paymentamount"),
			currency=currency or self._currency(),
			allocations=allocations,
			account=self.company.of("GLAccount", node.text("undepglaccountno")) if received else None,
			reference=node.text("refid") or node.string("source_key"),
			submitted=bool(booked),
		)
		return Desired(self.kind, node.id(), payment_fields(payment))

	@staticmethod
	def _currency() -> str:
		raise Unsupported(f"the payment names no currency ({PLANNED})")


class JournalMapper:
	"""A GL transaction of a journal the Field Map names (the general journal by default): each entry's
	account, debit or credit (``trtype``), department and customer, vendor or employee. An entry's project is
	planned: the 2.1 project list keys projects by record, the entries by project ID."""

	kind = Kind.JOURNAL_ENTRY
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		lines: list[JournalLine] = []
		for entry in items_of(node, "gltransactionentries", "glentry"):
			account = self.company.of("GLAccount", entry.text("glaccountno"))
			if account is None:
				raise SourceError(f"A Sage Intacct GL entry of {node.string('source_key')} has no account.")
			value = amount(entry, "amount")
			debit = entry.text("trtype") == DEBIT
			party_type, party = None, None
			for field_, type_, identity in (
				("customerid", CUSTOMER_PARTY, "Customer"),
				("vendorid", SUPPLIER_PARTY, "Vendor"),
				("employeeid", EMPLOYEE_PARTY, "Employee"),
			):
				if entry.text(field_):
					party_type, party = type_, self.company.of(identity, entry.text(field_))
					break
			lines.append(
				JournalLine(
					account=account,
					debit=value if debit else Decimal(0),
					credit=Decimal(0) if debit else value,
					party_type=party_type,
					party=party,
					cost_center=self.company.of("Department", entry.text("departmentid")),
				)
			)
		fields = journal_fields(
			f"{node.text('journalid')} {node.text('batchno')}",
			required_date(node, "datecreated"),
			lines,
			True,
			node.text("description") or None,
		)
		return Desired(self.kind, node.id(), fields)
