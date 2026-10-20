# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Acumatica → desired ERPNext state, one mapper per kind, in the shared field contract. The Default
endpoint's entities (every field ``{"value": …}``) are translated here: natural keys become identities, a
document's details its lines, its tax details its taxes, its freight and discounts their own rows, and a
payment's applications its allocations.

Statuses (the API sends the screens' labels): a document on hold, balanced or pending approval is a draft;
one released (open or closed) is booked; a voided, canceled or rejected one is gone."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import quote

from idempotent_importer.contract import (
	CUSTOMER_PARTY,
	PAY,
	RECEIVE,
	SHIPPING,
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
	OrderLine,
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
from idempotent_importer.core.paths import slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.acumatica.reader import KEY_SEPARATOR, derived_key, text, value
from idempotent_importer.sources.ledger import book_item, country_code, day, midday, required_day

PLANNED = "planned: card 386"
ROOT_TYPES = {"Asset": "Asset", "Liability": "Liability", "Income": "Income", "Expense": "Expense"}
ACCOUNT_TYPES = {"Income": "Income Account", "Expense": "Expense Account"}
INACTIVE = "Inactive"
GONE_ITEMS = frozenset({"Inactive", "Marked for Deletion"})
BOOKED = frozenset({"Open", "Closed", "Completed", "Released", "Posted", "Pending Print", "Pending Email"})
GONE = frozenset({"Voided", "Canceled", "Cancelled", "Rejected"})
CREDIT_MEMO, DEBIT_MEMO, INVOICE = "Credit Memo", "Debit Memo", "Invoice"
# A/P documents: a debit adjustment returns goods (a debit note); a bill and a credit adjustment owe more.
BILLS = frozenset({"Bill", "Credit Adj."})
DEBIT_ADJUSTMENT = "Debit Adj."
CASH_RETURN = "Cash Return"
CUSTOMER_PAYMENTS = {"Payment": RECEIVE, "Prepayment": RECEIVE, "Refund": PAY}
VENDOR_PAYMENTS = {"Payment": PAY, "Prepayment": PAY, "Quick Check": PAY, "Refund": RECEIVE}
SETTLED_BY_CUSTOMERS = {INVOICE: (Kind.SALES_INVOICE, "Invoice"), DEBIT_MEMO: (Kind.SALES_INVOICE, "Invoice")}
SETTLED_BY_VENDORS = {"Bill": (Kind.PURCHASE_INVOICE, "Bill"), "Credit Adj.": (Kind.PURCHASE_INVOICE, "Bill")}
PROJECT_STATUSES = {
	"Completed": "Completed",
	"Canceled": "Cancelled",
	"Suspended": "On hold",
	"On Hold": "On hold",
}
OPPORTUNITY_STATUSES = {"Won": "Converted", "Lost": "Lost"}
LEAD_STATUSES = {"Converted": "Converted", "Disqualified": "Do Not Contact"}
GENDERS = {"Male": "Male", "Female": "Female"}
DRAFT_ORDERS = frozenset({"On Hold", "Pending Approval", "Credit Hold"})


def key(*parts: str) -> str:
	return KEY_SEPARATOR.join(quote(part, safe=" -_.") for part in parts)


def amount(node: Node | None, field: str) -> Decimal:
	found = value(node, field)
	if found is None or isinstance(found, bool):
		return Decimal(0)
	return Decimal(str(found))


def optional_amount(node: Node | None, field: str) -> Decimal | None:
	found = value(node, field)
	return None if found is None or isinstance(found, bool) else Decimal(str(found))


class Tenant:
	"""The connection's identities of Acumatica's natural keys."""

	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config

	def of(self, type_: str, *parts: str | None) -> str | None:
		if any(not part for part in parts):
			return None
		return self.config.identity(type_, key(*(str(part) for part in parts)))


def mappers(config: ConnectionConfig) -> list[Mapper]:
	tenant = Tenant(config)
	return [
		AccountMapper(tenant),
		SubaccountMapper(tenant),
		WarehouseMapper(tenant),
		ItemClassMapper(tenant),
		ItemMapper(tenant),
		PartyMapper(Kind.CUSTOMER, tenant),
		PartyMapper(Kind.SUPPLIER, tenant),
		NamedMapper(Kind.DEPARTMENT, tenant),
		NamedMapper(Kind.DESIGNATION, tenant),
		EmployeeMapper(tenant),
		ProjectMapper(tenant),
		LeadMapper(tenant),
		OpportunityMapper(tenant),
		SalesOrderMapper(Kind.DRAFT_ORDER, tenant),
		SalesOrderMapper(Kind.SALES_ORDER, tenant),
		PurchaseOrderMapper(tenant),
		SalesDocumentMapper(tenant),
		BillMapper(tenant),
		PaymentMapper(tenant),
		JournalMapper(tenant),
	]


# -- the books -------------------------------------------------------------------------------------------


class AccountMapper:
	"""A GL account: its code, description, type (Acumatica's four: equity accounts are liabilities), cash
	accounts as Bank, currency; an inactive one disabled. Acumatica's chart is flat."""

	kind = Kind.ACCOUNT
	version = 1

	def __init__(self, tenant: Tenant) -> None:
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		type_ = text(node, "Type") or ""
		root = ROOT_TYPES.get(type_)
		if root is None:
			raise Unsupported(f"an account of type {type_ or 'none'}")
		account = LedgerAccount(
			number=text(node, "AccountCD"),
			name=text(node, "Description") or node.string("source_key"),
			root_type=root,
			account_type="Bank" if value(node, "CashAccount") is True else ACCOUNT_TYPES.get(type_),
			currency=text(node, "CurrencyID"),
		)
		fields = account_fields(account)
		if value(node, "Active") is False:
			fields |= {"disabled": True}
		return Desired(self.kind, node.id(), fields)


class SubaccountMapper:
	"""A subaccount as a Cost Center (a line books to its subaccount); an inactive one disabled."""

	kind = Kind.COST_CENTER
	version = 1

	def __init__(self, tenant: Tenant) -> None:
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		name = text(node, "Description") or node.string("source_key")
		return Desired(self.kind, node.id(), tree_fields(name, None, disabled=value(node, "Active") is False))


class WarehouseMapper:
	kind = Kind.WAREHOUSE
	version = 1

	def __init__(self, tenant: Tenant) -> None:
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		name = text(node, "Description") or text(node, "WarehouseID") or node.string("source_key")
		fields = warehouse_fields(name, None, False, None)
		return Desired(
			self.kind, node.id(), fields | ({"disabled": True} if value(node, "Active") is False else {})
		)


class ItemClassMapper:
	kind = Kind.COLLECTION
	version = 1

	def __init__(self, tenant: Tenant) -> None:
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		name = text(node, "Description") or node.string("source_key")
		fields: Fields = {
			"name": name,
			"slug": slugify(name),
			"parent": None,
			"description": None,
			"image": None,
		}
		return Desired(self.kind, node.id(), fields)


class ItemMapper:
	"""A stock or non-stock item in its class: description, default price, and for a stock item its quantity
	on hand across warehouses. An inactive item, or one marked for deletion, is retired (disabled)."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, tenant: Tenant) -> None:
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		if text(node, "ItemStatus") in GONE_ITEMS:
			return None
		stock = node.string("entity") == "StockItem"
		item_class = self.tenant.of("ItemClass", text(node, "ItemClass"))
		on_hand = sum((amount(w, "QtyOnHand") for w in node.children("WarehouseDetails")), Decimal(0))
		fields = book_item(
			name=text(node, "Description") or node.string("source_key"),
			sku=text(node, "InventoryID"),
			description=None,
			price=optional_amount(node, "DefaultPrice"),
			is_stock_item=stock,
			stock_qty=on_hand if stock else None,
			collections=[item_class] if item_class else [],
		)
		return Desired(self.kind, node.id(), fields)


# -- parties ---------------------------------------------------------------------------------------------


def address_of(contact: Node | None, kind: str, name: str | None) -> Fields | None:
	address = contact.child("Address") if contact is not None else None
	line1, city = text(address, "AddressLine1"), text(address, "City")
	if not line1 or not city:
		return None
	return address_fields(
		Address(
			address_type=kind,
			name=name,
			company=None,
			line1=line1,
			line2=text(address, "AddressLine2"),
			city=city,
			state=text(address, "State"),
			postcode=text(address, "PostalCode"),
			country_code=country_code(text(address, "Country")),
			email=None,
			phone=None,
		)
	)


class PartyMapper:
	"""A customer or a vendor: name, individual or organisation, its main contact's email and phone, billing
	and shipping (else main) addresses, tax registration, currency, and its contacts; an inactive one
	stays, disabled, for the documents that name it."""

	def __init__(self, kind: Kind, tenant: Tenant) -> None:
		self.kind = kind
		self.version = 1
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		name = text(node, "CustomerName") or text(node, "VendorName") or node.string("source_key")
		main = node.child("MainContact")
		addresses = [
			found
			for found in (
				address_of(node.child("BillingContact") or main, "Billing", name),
				address_of(node.child("ShippingContact"), "Shipping", name),
			)
			if found is not None
		]
		if not addresses and (main_address := address_of(main, "Billing", name)) is not None:
			addresses.append(main_address)
		individual = (text(node, "CustomerCategory") or "").startswith("Individual")
		party = Party(
			name=name,
			type="Individual" if individual else "Company",
			company=None if individual else name,
			email=text(main, "Email") or text(node, "Email"),
			phone=text(main, "Phone1"),
			addresses=addresses,
			tax_id=text(node, "TaxRegistrationID"),
			currency=text(node, "CurrencyID"),
			contacts=self._contacts(node),
			disabled=text(node, "Status") == INACTIVE,
		)
		return Desired(self.kind, node.id(), party_fields(party))

	def _contacts(self, node: Node) -> list[ContactPerson]:
		found: list[ContactPerson] = []
		for entry in node.children("Contacts"):
			contact = entry.child("Contact")
			code = value(entry, "ContactID") or value(contact, "ContactID")
			name = text(contact, "DisplayName") or " ".join(
				p for p in (text(contact, "FirstName"), text(contact, "LastName")) if p
			)
			if code is None or not name or value(contact, "Active") is False:
				continue
			found.append(
				ContactPerson(
					gid=self.tenant.config.identity("Contact", str(code)),
					name=name,
					email=text(contact, "Email"),
					phone=text(contact, "Phone1"),
					job_title=text(contact, "JobTitle"),
				)
			)
		return found


class LeadMapper:
	kind = Kind.LEAD
	version = 1

	def __init__(self, tenant: Tenant) -> None:
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		lead = Lead(
			status=LEAD_STATUSES.get(text(node, "Status") or "", "Lead"),
			first_name=text(node, "FirstName"),
			last_name=text(node, "LastName"),
			company=text(node, "CompanyName"),
			email=text(node, "Email"),
			phone=text(node, "Phone1"),
			mobile=text(node, "Phone2"),
			job_title=text(node, "JobTitle"),
			website=text(node, "WebSite"),
			notes=text(node, "Description"),
			disabled=value(node, "Active") is False,
		)
		return Desired(self.kind, node.id(), lead_fields(lead))


# -- people ----------------------------------------------------------------------------------------------


class NamedMapper:
	"""An employees' department or job title (neither has a record): a Department or a Designation."""

	def __init__(self, kind: Kind, tenant: Tenant) -> None:
		self.kind = kind
		self.version = 1
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		name = node.string("name")
		fields: Fields = tree_fields(name, None) if self.kind == Kind.DEPARTMENT else {"name": name}
		return Desired(self.kind, node.id(), fields)


class EmployeeMapper:
	kind = Kind.EMPLOYEE
	version = 1

	def __init__(self, tenant: Tenant) -> None:
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		contact = node.child("ContactInfo")
		settings = node.child("EmployeeSettings")
		department = text(settings, "DepartmentID")
		title = text(contact, "JobTitle")
		first = text(contact, "FirstName") or text(node, "EmployeeName") or node.string("source_key")
		employee = Employee(
			first_name=first,
			middle_name=text(contact, "MiddleName"),
			last_name=text(contact, "LastName"),
			gender=GENDERS.get(text(contact, "Gender") or ""),
			date_of_birth=day(text(contact, "DateOfBirth")),
			status="Inactive" if text(node, "Status") == INACTIVE else "Active",
			employee_number=text(node, "EmployeeID"),
			department=self.tenant.config.identity("Department", derived_key(department))
			if department
			else None,
			designation=self.tenant.config.identity("Designation", derived_key(title)) if title else None,
			reports_to=self.tenant.of("Employee", text(settings, "ReportsTo")),
			company_email=text(contact, "Email"),
			phone=text(contact, "Phone1"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class ProjectMapper:
	kind = Kind.PROJECT
	version = 1

	def __init__(self, tenant: Tenant) -> None:
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		fields = project_fields(
			name=text(node, "Description") or node.string("source_key"),
			status=PROJECT_STATUSES.get(text(node, "Status") or "", "Open"),
			customer=self.tenant.of("Customer", text(node, "Customer")),
		)
		return Desired(self.kind, node.id(), fields)


class OpportunityMapper:
	"""An opportunity with its business account (as a customer) or, before there is one, its lead; dated its
	estimated close (the Default endpoint names no other date)."""

	kind = Kind.OPPORTUNITY
	version = 1

	def __init__(self, tenant: Tenant) -> None:
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		customer = self.tenant.of("Customer", text(node, "BusinessAccount"))
		lead = None if customer else self.tenant.of("Lead", text(node, "ContactID"))
		if customer is None and lead is None:
			raise Unsupported("an opportunity with neither a business account nor a lead")
		estimated = day(text(node, "Estimation"))
		opportunity = Opportunity(
			party_kind=Kind.CUSTOMER if customer else Kind.LEAD,
			party=customer or lead or "",
			title=text(node, "Subject") or node.string("source_key"),
			status=OPPORTUNITY_STATUSES.get(text(node, "Status") or "", "Open"),
			date=required_day(estimated, node.path),
			currency=text(node, "CurrencyID"),
			amount=optional_amount(node, "Total"),
			expected_closing=estimated,
			notes=text(node, "Details"),
		)
		return Desired(self.kind, node.id(), opportunity_fields(opportunity))


# -- documents -------------------------------------------------------------------------------------------


def status_of(node: Node) -> bool | None:
	"""Whether a document is booked (released), a draft (False), or gone (None)."""
	status = text(node, "Status") or ""
	if status in GONE:
		return None
	return status in BOOKED


class Documents:
	"""What every document mapper shares: details as lines, tax details as taxes."""

	def __init__(self, tenant: Tenant) -> None:
		self.tenant = tenant

	def lines(
		self, node: Node, currency: str, sign: Decimal, qty_field: str, name_fields: tuple[str, ...]
	) -> list[TradeLine]:
		found: list[TradeLine] = []
		for index, line in enumerate(node.children("Details")):
			total = amount(line, "Amount") or amount(line, "ExtendedPrice") or amount(line, "ExtendedCost")
			item = text(line, "InventoryID")
			qty = optional_amount(line, qty_field) or Decimal(1)
			name = next((t for f in name_fields if (t := text(line, f))), None) or item or "Line"
			rate = exact_quotient(total, qty, minor_units(currency)) if qty else None
			if rate is None:
				raise Unsupported(f'line "{name}" ({total} for {qty}) has no exact unit price in {currency}')
			number = value(line, "LineNbr")
			found.append(
				TradeLine(
					OrderLine(
						f"line:{number if number is not None else index}",
						self.tenant.of("Item", item),
						None,
						name,
						qty * sign,
						rate,
					),
					account=self.tenant.of("Account", text(line, "Account")),
					cost_center=self.tenant.of("Subaccount", text(line, "Subaccount")),
					project=self.tenant.of("Project", text(line, "Project") or text(node, "Project")),
				)
			)
		return found

	@staticmethod
	def taxes(node: Node, sign: Decimal) -> list[TradeTax]:
		"""Each tax's amount (the tax details), else the tax total."""
		by_tax: dict[str, Decimal] = {}
		for detail in node.children("TaxDetails"):
			tax = text(detail, "TaxID") or "Tax"
			by_tax[tax] = by_tax.get(tax, Decimal(0)) + amount(detail, "TaxAmount")
		total = amount(node, "TaxTotal") or amount(node.child("Totals"), "TaxTotal")
		if sum(by_tax.values(), Decimal(0)) != total:
			by_tax = {"Tax": total}
		return [TradeTax(tax, value_ * sign) for tax, value_ in sorted(by_tax.items()) if value_]

	def currency(self, node: Node) -> str:
		"""The document's currency; the Default endpoint's invoice and cash sale name none, so they are in
		their customer's, unless the customer may bill in others (then it can't be known)."""
		currency = text(node, "CurrencyID")
		if currency:
			return currency
		if node.optional_flag("currency_override") is True:
			raise Unsupported(
				f"the customer bills in several currencies, and the Default endpoint names no invoice currency ({PLANNED})"
			)
		found = node.optional_string("customer_currency")
		if not found:
			raise SourceError(f"Acumatica document {node.string('source_key')} has no currency.")
		return found

	def party(self, node: Node, type_: str, *fields: str) -> str:
		for field in fields:
			found = self.tenant.of(type_, text(node, field))
			if found:
				return found
		raise SourceError(f"Acumatica document {node.string('source_key')} has no {type_.lower()}.")


def rate_of(node: Node) -> Decimal | None:
	rate = optional_amount(node, "CurrencyRate")
	return None if rate is None or rate == 1 else rate


class SalesOrderMapper(Documents):
	"""A sales order (a quotation, of order type QT, as a draft Quotation): details, document discounts,
	taxes and freight; one on hold or pending approval is a draft, a cancelled one is gone."""

	def __init__(self, kind: Kind, tenant: Tenant) -> None:
		super().__init__(tenant)
		self.kind = kind
		self.version = 1

	def map(self, node: Node) -> Desired | None:
		status = text(node, "Status") or ""
		if status in GONE:
			return None
		currency = self.currency(node)
		totals = node.child("Totals")
		lines = self.lines(node, currency, Decimal(1), "OrderQty", ("LineDescription",))
		taxes = self.taxes(node, Decimal(1))
		freight = amount(totals, "Freight") + amount(totals, "PremiumFreight")
		if freight:
			taxes.append(TradeTax("Freight", freight, role=SHIPPING))
		discount = amount(totals, "DiscountTotal")
		document = TradeDocument(
			number=f"{text(node, 'OrderType')} {text(node, 'OrderNbr')}",
			date=required_day(text(node, "Date"), node.path),
			party=self.party(node, "Customer", "CustomerID"),
			currency=currency,
			lines=lines,
			taxes=taxes,
			total=amount(node, "OrderTotal"),
			submitted=status not in DRAFT_ORDERS,
			due_date=day(text(node, "RequestedOn")),
			exchange_rate=rate_of(node),
			reference=text(node, "CustomerOrder"),
			remarks=text(node, "Description"),
			discount=discount or None,
		)
		fields = trade_fields(document)
		if self.kind == Kind.SALES_ORDER:
			return Desired(self.kind, node.id(), fields)
		quotation: Fields = {
			"number": document.number,
			"currency": currency,
			"created_at": midday(document.date),
			"customer": document.party,
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": list[Field](line_fields(trade.line) for trade in lines),
			"discount": discount,
			"charges": [charge_fields(Charge(tax.role, tax.description, tax.amount)) for tax in taxes],
			"total": document.total,
			"remarks": document.remarks,
		}
		if document.exchange_rate is not None:
			quotation["exchange_rate"] = document.exchange_rate
		return Desired(self.kind, node.id(), quotation)


class PurchaseOrderMapper(Documents):
	kind = Kind.PURCHASE_ORDER
	version = 1

	def map(self, node: Node) -> Desired | None:
		status = text(node, "Status") or ""
		if status in GONE:
			return None
		currency = self.currency(node)
		document = TradeDocument(
			number=f"{text(node, 'Type')} {text(node, 'OrderNbr')}",
			date=required_day(text(node, "Date"), node.path),
			party=self.party(node, "Vendor", "VendorID"),
			currency=currency,
			lines=self.lines(node, currency, Decimal(1), "OrderQty", ("LineDescription", "Description")),
			taxes=self.taxes(node, Decimal(1)),
			total=amount(node, "OrderTotal"),
			submitted=status not in DRAFT_ORDERS,
			due_date=day(text(node, "PromisedOn")),
			exchange_rate=rate_of(node),
			reference=text(node, "VendorRef"),
			remarks=text(node, "Description"),
		)
		return Desired(self.kind, node.id(), trade_fields(document))


class SalesDocumentMapper(Documents):
	"""An A/R invoice, debit memo or credit memo (a return, against the one invoice it is applied to), or a
	cash sale or cash return (paid at once: its own payment)."""

	kind = Kind.SALES_INVOICE
	version = 1

	def map(self, node: Node) -> Desired | None:
		booked = status_of(node)
		if booked is None:
			return None
		type_ = text(node, "Type") or ""
		cash = node.string("entity") == "CashSale"
		is_return = type_ in (CREDIT_MEMO, CASH_RETURN)
		sign = Decimal(-1) if is_return else Decimal(1)
		currency = self.currency(node)
		applied = [entry for entry in node.children("ApplicationsCreditMemo") if amount(entry, "AmountPaid")]
		against = (
			self.tenant.of("Invoice", text(applied[0], "DocType"), text(applied[0], "ReferenceNbr"))
			if is_return and len(applied) == 1
			else None
		)
		discount = sum((amount(d, "DiscountAmount") for d in node.children("DiscountDetails")), Decimal(0))
		document = TradeDocument(
			number=f"{type_} {text(node, 'ReferenceNbr')}",
			date=required_day(text(node, "Date"), node.path),
			party=self.party(node, "Customer", "Customer", "CustomerID"),
			currency=currency,
			lines=self.lines(node, currency, sign, "Qty", ("TransactionDescription",)),
			taxes=self.taxes(node, sign),
			total=amount(node, "Amount") * sign,
			submitted=booked,
			due_date=day(text(node, "DueDate")),
			is_return=is_return,
			return_against=against,
			reference=text(node, "CustomerOrder") or text(node, "PaymentRef"),
			remarks=text(node, "Description"),
			return_settles=not cash,
			discount=(discount * sign) or None,
		)
		return Desired(self.kind, node.id(), trade_fields(document))


class BillMapper(Documents):
	"""An A/P bill or credit adjustment as a Purchase Invoice, a debit adjustment as a return (debit note);
	a prepayment is planned."""

	kind = Kind.PURCHASE_INVOICE
	version = 1

	def map(self, node: Node) -> Desired | None:
		booked = status_of(node)
		if booked is None:
			return None
		type_ = text(node, "Type") or ""
		if type_ not in BILLS and type_ != DEBIT_ADJUSTMENT:
			raise Unsupported(f"an A/P {type_ or 'document'} ({PLANNED})")
		sign = Decimal(-1) if type_ == DEBIT_ADJUSTMENT else Decimal(1)
		currency = self.currency(node)
		document = TradeDocument(
			number=f"{type_} {text(node, 'ReferenceNbr')}",
			date=required_day(text(node, "Date"), node.path),
			party=self.party(node, "Vendor", "Vendor"),
			currency=currency,
			lines=self.lines(node, currency, sign, "Qty", ("TransactionDescription", "Description")),
			taxes=self.taxes(node, sign),
			total=amount(node, "Amount") * sign,
			submitted=booked,
			due_date=day(text(node, "DueDate")),
			is_return=sign < 0,
			reference=text(node, "VendorRef"),
			remarks=text(node, "Description"),
		)
		return Desired(self.kind, node.id(), trade_fields(document))


class PaymentMapper:
	"""A customer payment, prepayment or refund, a vendor check (payment, prepayment, quick check, refund),
	or a cash sale's or cash return's own money, allocated to the documents it was applied to. Its cash
	account is Acumatica's own record (the Default endpoint has no cash account entity to mirror), so it
	books to the connection's deposit account. A voided one is gone; one on hold is a draft."""

	kind = Kind.PAYMENT_ENTRY
	version = 1

	def __init__(self, tenant: Tenant) -> None:
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		booked = status_of(node)
		if booked is None:
			return None
		entity = node.string("entity")
		type_ = text(node, "Type") or ""
		if entity == "CashSale":
			payment = self._cash_sale(node, type_, booked)
		else:
			payment = self._payment(node, entity, type_, booked)
		return Desired(self.kind, node.id(), payment_fields(payment))

	def _payment(self, node: Node, entity: str, type_: str, booked: bool) -> Payment:
		customer = entity == "Payment"
		direction = (CUSTOMER_PAYMENTS if customer else VENDOR_PAYMENTS).get(type_)
		if direction is None:
			raise Unsupported(f"a {type_ or 'payment'} ({PLANNED})")
		settled = SETTLED_BY_CUSTOMERS if customer else SETTLED_BY_VENDORS
		rows = node.children("ApplicationHistory") if customer else node.children("Details")
		allocations: list[Allocation] = []
		for row in rows:
			doc_type = text(row, "AdjustedDocType") or text(row, "DocType") or ""
			reference = text(row, "AdjustedRefNbr") or text(row, "ReferenceNbr")
			paid = amount(row, "AmountPaid")
			if not paid:
				continue
			target = settled.get(doc_type)
			if target is None or direction != (RECEIVE if customer else PAY):
				raise Unsupported(f"the payment is applied to a {doc_type or 'document'} ({PLANNED})")
			document = self.tenant.of(target[1], doc_type, reference)
			if document is not None:
				allocations.append(Allocation(target[0], document, paid))
		party_type = CUSTOMER_PARTY if customer else SUPPLIER_PARTY
		party = self.tenant.of(
			"Customer" if customer else "Vendor", text(node, "CustomerID") or text(node, "Vendor")
		)
		if party is None:
			raise SourceError(f"Acumatica payment {node.string('source_key')} has no {party_type.lower()}.")
		return Payment(
			payment_type=direction,
			party_type=party_type,
			party=party,
			date=required_day(text(node, "ApplicationDate"), node.path),
			amount=amount(node, "PaymentAmount"),
			currency=self._currency(node),
			allocations=allocations,
			account=None,
			reference=text(node, "PaymentRef") or node.string("source_key"),
			remarks=text(node, "Description"),
			submitted=booked,
		)

	def _cash_sale(self, node: Node, type_: str, booked: bool) -> Payment:
		returned = type_ == CASH_RETURN
		party = self.tenant.of("Customer", text(node, "CustomerID"))
		if party is None:
			raise SourceError(f"Acumatica cash sale {node.string('source_key')} has no customer.")
		total = amount(node, "Amount")
		return Payment(
			payment_type=PAY if returned else RECEIVE,
			party_type=CUSTOMER_PARTY,
			party=party,
			date=required_day(text(node, "Date"), node.path),
			amount=total,
			currency=self._currency(node),
			allocations=[Allocation(Kind.SALES_INVOICE, node.id(), total)],
			account=None,
			reference=text(node, "PaymentRef") or node.string("source_key"),
			submitted=booked,
		)

	@staticmethod
	def _currency(node: Node) -> str:
		currency = text(node, "CurrencyID") or node.optional_string("customer_currency")
		if not currency:
			raise SourceError(f"Acumatica payment {node.string('source_key')} has no currency.")
		return currency


class JournalMapper:
	"""A general ledger batch (module GL) in its ledger's currency, line by line: account, subaccount (as cost
	center), project, debit and credit. A batch in another currency is planned; a voided one is gone."""

	kind = Kind.JOURNAL_ENTRY
	version = 1

	def __init__(self, tenant: Tenant) -> None:
		self.tenant = tenant

	def map(self, node: Node) -> Desired | None:
		booked = status_of(node)
		if booked is None:
			return None
		currency, ledger = text(node, "CurrencyID"), node.optional_string("ledger_currency")
		if currency and ledger and currency != ledger:
			raise Unsupported(f"a batch in {currency}, not its ledger's {ledger} ({PLANNED})")
		lines: list[JournalLine] = []
		for line in node.children("Details"):
			account = self.tenant.of("Account", text(line, "Account"))
			if account is None:
				raise SourceError(f"A line of Acumatica batch {node.string('source_key')} has no account.")
			lines.append(
				JournalLine(
					account=account,
					debit=amount(line, "DebitAmount"),
					credit=amount(line, "CreditAmount"),
					cost_center=self.tenant.of("Subaccount", text(line, "Subaccount")),
					project=self.tenant.of("Project", text(line, "Project")),
				)
			)
		fields = journal_fields(
			f"{text(node, 'Module')} {text(node, 'BatchNbr')}",
			required_day(text(node, "TransactionDate"), node.path),
			lines,
			booked,
			text(node, "Description"),
		)
		return Desired(self.kind, node.id(), fields)
