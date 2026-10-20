# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure QuickBooks Online → desired ERPNext state, one mapper per kind, in the shared field contract. The
Accounting API's entities (developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities) are
translated here: references (``{"value": id}``) become identities, a transaction's lines its trade lines
(an item sold or bought, an expense to an account, a discount), its TxnTaxDetail its taxes, and the money
movements (deposits, transfers, expenses) journal entries that balance exactly."""

from __future__ import annotations

from decimal import Decimal

from idempotent_importer.contract import (
	CUSTOMER_PARTY,
	EMPLOYEE_PARTY,
	PAY,
	RECEIVE,
	SHIPPING,
	SUPPLIER_PARTY,
	Address,
	Allocation,
	Charge,
	Employee,
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
	employee_fields,
	journal_fields,
	line_fields,
	party_fields,
	payment_fields,
	project_fields,
	trade_fields,
	tree_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import exact_quotient, minor_units
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.ledger import book_item, country_code, midday, required_day

ROOT_TYPES = {
	"Asset": "Asset",
	"Liability": "Liability",
	"Equity": "Equity",
	"Revenue": "Income",
	"Expense": "Expense",
}
# QuickBooks' account types and sub-types → ERPNext's account type (None: an ordinary account).
ACCOUNT_TYPES = {
	"Bank": "Bank",
	"Accounts Receivable": "Receivable",
	"Accounts Payable": "Payable",
	"Fixed Asset": "Fixed Asset",
	"Cost of Goods Sold": "Cost of Goods Sold",
	"Income": "Income Account",
	"Other Income": "Income Account",
	"Expense": "Expense Account",
	"Other Expense": "Expense Account",
	"Equity": "Equity",
}
ACCOUNT_SUB_TYPES = {
	"Inventory": "Stock",
	"GlobalTaxPayable": "Tax",
	"SalesTaxPayable": "Tax",
	"GlobalTaxSuspense": "Tax",
	"UndepositedFunds": "Cash",
	"CashOnHand": "Cash",
}
INVENTORY = "Inventory"
CATEGORY = "Category"
# Items of a bundle (Group) are its lines; a bundle itself is an ERPNext Product Bundle (planned work).
BUNDLE_TYPES = frozenset({"Group", "Bundle"})
# The shipping line QuickBooks adds when the company charges shipping is this pseudo item.
SHIPPING_ITEM = "SHIPPING_ITEM_ID"
SALES_LINE = "SalesItemLineDetail"
GROUP_LINE = "GroupLineDetail"
DISCOUNT_LINE = "DiscountLineDetail"
ITEM_EXPENSE_LINE = "ItemBasedExpenseLineDetail"
ACCOUNT_EXPENSE_LINE = "AccountBasedExpenseLineDetail"
IGNORED_LINES = frozenset({"SubTotalLineDetail", "DescriptionOnly", "DescriptionLineDetail"})
RETURNS = frozenset({"CreditMemo", "RefundReceipt", "VendorCredit"})
DOCUMENT_KINDS = {
	"Invoice": Kind.SALES_INVOICE,
	"SalesReceipt": Kind.SALES_INVOICE,
	"CreditMemo": Kind.SALES_INVOICE,
	"RefundReceipt": Kind.SALES_INVOICE,
	"Bill": Kind.PURCHASE_INVOICE,
	"VendorCredit": Kind.PURCHASE_INVOICE,
}
PARTY_TYPES = {"Customer": CUSTOMER_PARTY, "Vendor": SUPPLIER_PARTY, "Employee": EMPLOYEE_PARTY}
PARTY_ENTITIES = {CUSTOMER_PARTY: "Customer", SUPPLIER_PARTY: "Vendor", EMPLOYEE_PARTY: "Employee"}
DEBIT, CREDIT = "Debit", "Credit"
PLANNED = "planned: card 386"


class QuickBooks:
	"""The connection's identities of QuickBooks' references."""

	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config

	def of(self, entity: str, reference: Node | None) -> str | None:
		"""The identity of a reference (``{"value": "12"}``), or None when it is absent."""
		if reference is None or not reference.optional_string("value"):
			return None
		return self.config.identity(entity, reference.string("value"))

	def required(self, entity: str, node: Node, field: str) -> str:
		found = self.of(entity, node.child(field))
		if found is None:
			raise Unsupported(f"{node.path} has no {field}")
		return found

	def cost_center(self, *nodes: Node) -> str | None:
		"""The first class, else department, named on these (a line, then its transaction). ERPNext books a
		line to one cost center; QuickBooks' location (department) beside a class is planned as an
		accounting dimension."""
		for node in nodes:
			found = self.of("Class", node.child("ClassRef"))
			if found:
				return found
		for node in nodes:
			found = self.of("Department", node.child("DepartmentRef"))
			if found:
				return found
		return None


def mappers(config: ConnectionConfig) -> list[Mapper]:
	books = QuickBooks(config)
	return [
		AccountMapper(books),
		CostCenterMapper(books),
		CategoryMapper(books),
		ItemMapper(books),
		CustomerMapper(books),
		VendorMapper(books),
		EmployeeMapper(books),
		ProjectMapper(books),
		EstimateMapper(books),
		PurchaseOrderMapper(books),
		SalesDocumentMapper(books),
		PurchaseDocumentMapper(books),
		PaymentMapper(books),
		JournalMapper(books),
	]


def entity(node: Node) -> str:
	return node.string("Entity")


def amount(node: Node, field: str = "Amount") -> Decimal:
	return node.optional_decimal(field) or Decimal(0)


class AccountMapper:
	kind = Kind.ACCOUNT
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		"""An account of the chart, top-level in the company's tree: QuickBooks posts to an account that has
		sub-accounts, which ERPNext allows only to a leaf, so sub-accounts are not nested."""
		account_type = ACCOUNT_SUB_TYPES.get(node.text("AccountSubType")) or ACCOUNT_TYPES.get(
			node.text("AccountType")
		)
		currency = node.child("CurrencyRef")
		account = LedgerAccount(
			number=node.optional_string("AcctNum"),
			name=node.string("Name"),
			root_type=ROOT_TYPES[node.string("Classification")],
			account_type=account_type,
			currency=currency.optional_string("value") if currency else None,
		)
		return Desired(self.kind, node.id(), account_fields(account))


class CostCenterMapper:
	"""A class or a department (location) as a cost center, top-level for the same reason as accounts."""

	kind = Kind.COST_CENTER
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		return Desired(self.kind, node.id(), tree_fields(node.string("Name"), None))


class CategoryMapper:
	kind = Kind.COLLECTION
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		name = node.string("Name")
		fields: Fields = {
			"name": name,
			"slug": slugify(node.optional_string("FullyQualifiedName") or name),
			"parent": self.books.of("Item", node.child("ParentRef")),
			"description": None,
			"image": None,
		}
		return Desired(self.kind, node.id(), fields)


class ItemMapper:
	kind = Kind.PRODUCT
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired | None:
		"""An item (inventory, non-inventory or service) in its category; an inactive one is retired."""
		if node.optional_flag("Active") is False:
			return None
		if node.text("Type") in BUNDLE_TYPES:
			raise Unsupported(f"the bundle {node.string('Name')} is a Product Bundle ({PLANNED})")
		stock = node.text("Type") == INVENTORY
		parent = self.books.of("Item", node.child("ParentRef"))
		fields = book_item(
			name=node.string("Name"),
			sku=node.optional_string("Sku"),
			description=node.optional_string("Description"),
			price=node.optional_decimal("UnitPrice"),
			is_stock_item=stock,
			stock_qty=node.optional_decimal("QtyOnHand") if stock else None,
			collections=[parent] if parent else [],
		)
		return Desired(self.kind, node.id(), fields)


def addresses(node: Node, email: str | None, phone: str | None) -> tuple[list[Fields], list[str]]:
	"""A party's billing and shipping addresses (the same one once), and a warning for one whose country
	isn't known by name."""
	found: list[Fields] = []
	warnings: list[str] = []
	seen: set[tuple[str, ...]] = set()
	for field, kind in (("BillAddr", "Billing"), ("ShipAddr", "Shipping")):
		address = node.child(field)
		if address is None or not address.optional_string("Line1") or not address.optional_string("City"):
			continue
		key = tuple(
			address.text(part) for part in ("Line1", "Line2", "City", "CountrySubDivisionCode", "PostalCode")
		)
		if key in seen:
			continue
		seen.add(key)
		code = country_code(address.optional_string("CountryCode") or address.optional_string("Country"))
		if code is None:
			warnings.append(
				f"its {kind.lower()} address names no country ERPNext knows ({address.text('Country')})"
			)
			continue
		found.append(
			address_fields(
				Address(
					address_type=kind,
					name=None,
					company=None,
					line1=address.string("Line1"),
					line2=address.optional_string("Line2"),
					city=address.string("City"),
					state=address.optional_string("CountrySubDivisionCode"),
					postcode=address.optional_string("PostalCode"),
					country_code=code,
					email=email,
					phone=phone,
				)
			)
		)
	return found, warnings


def contact_of(node: Node, field: str, key: str) -> str | None:
	child = node.child(field)
	return child.optional_string(key) if child is not None else None


def party(node: Node) -> tuple[Party, list[str]]:
	email = contact_of(node, "PrimaryEmailAddr", "Address")
	phone = contact_of(node, "PrimaryPhone", "FreeFormNumber") or contact_of(node, "Mobile", "FreeFormNumber")
	found, warnings = addresses(node, email, phone)
	company = node.optional_string("CompanyName")
	currency = node.child("CurrencyRef")
	return (
		Party(
			name=node.string("DisplayName"),
			type="Company" if company else "Individual",
			first_name=node.optional_string("GivenName"),
			last_name=node.optional_string("FamilyName"),
			company=company,
			email=email,
			phone=phone,
			addresses=found,
			tax_id=node.optional_string("BusinessNumber") or node.optional_string("TaxIdentifier"),
			currency=currency.optional_string("value") if currency else None,
		),
		warnings,
	)


class CustomerMapper:
	kind = Kind.CUSTOMER
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		"""A customer (a sub-customer is a customer of its own; ERPNext has no customer hierarchy). An
		inactive one stays, since its invoices are booked to it."""
		found, warnings = party(node)
		return Desired(self.kind, node.id(), party_fields(found), warnings=tuple(warnings))


class VendorMapper:
	kind = Kind.SUPPLIER
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		found, warnings = party(node)
		return Desired(self.kind, node.id(), party_fields(found), warnings=tuple(warnings))


class EmployeeMapper:
	kind = Kind.EMPLOYEE
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		gender = node.optional_string("Gender")
		employee = Employee(
			first_name=node.optional_string("GivenName") or node.string("DisplayName"),
			last_name=node.optional_string("FamilyName"),
			middle_name=node.optional_string("MiddleName"),
			gender=gender if gender in ("Male", "Female") else None,
			date_of_birth=node.optional_string("BirthDate"),
			date_of_joining=node.optional_string("HiredDate"),
			relieving_date=node.optional_string("ReleasedDate"),
			status="Active" if node.optional_flag("Active") is not False else "Left",
			employee_number=node.optional_string("EmployeeNumber"),
			personal_email=contact_of(node, "PrimaryEmailAddr", "Address"),
			phone=contact_of(node, "PrimaryPhone", "FreeFormNumber")
			or contact_of(node, "Mobile", "FreeFormNumber"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class ProjectMapper:
	kind = Kind.PROJECT
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		"""A project (QuickBooks keeps it as a sub-customer) for the customer it belongs to."""
		fields = project_fields(
			name=node.string("DisplayName"),
			status="Open" if node.optional_flag("Active") is not False else "Completed",
			customer=self.books.of("Customer", node.child("ParentRef")),
			notes=node.optional_string("Notes"),
		)
		return Desired(self.kind, node.id(), fields)


class Lines:
	"""A transaction's lines as trade lines, exact: each line's rate times its quantity is its amount. A
	discount line is the document's discount (``discount``), and a sale's shipping line its freight
	(``freight``)."""

	def __init__(self, books: QuickBooks, node: Node, currency: str, sign: Decimal) -> None:
		self.books = books
		self.node = node
		self.currency = currency
		self.sign = sign
		self.project = books.of("Customer", node.child("ProjectRef"))
		self.discount = Decimal(0)
		self.freight: list[TradeTax] = []

	def trade(self) -> list[TradeLine]:
		lines: list[TradeLine] = []
		for line in self.node.children("Line"):
			lines += self._line(line)
		return lines

	def _line(self, line: Node) -> list[TradeLine]:
		detail_type = line.text("DetailType")
		if detail_type in IGNORED_LINES:
			return []
		detail = line.child(detail_type)
		if detail is None:
			raise Unsupported(f"{line.path} has no {detail_type}")
		key = line.optional_string("Id") or str(line.path)
		match detail_type:
			case "SalesItemLineDetail" if self._shipping(detail):
				self.freight.append(
					TradeTax(self._name(line, "Shipping"), self.sign * amount(line), role=SHIPPING)
				)
				return []
			case "SalesItemLineDetail" | "ItemBasedExpenseLineDetail":
				return [self._item(key, line, detail)]
			case "GroupLineDetail":
				return [entry for inner in detail.children("Line") for entry in self._line(inner)]
			case "AccountBasedExpenseLineDetail":
				return [
					TradeLine(
						OrderLine(key, None, None, self._name(line, "Expense"), self.sign, amount(line)),
						account=self.books.required("Account", detail, "AccountRef"),
						cost_center=self.books.cost_center(detail, self.node),
						project=self.books.of("Customer", detail.child("CustomerRef")) or self.project,
					)
				]
			case "DiscountLineDetail":
				self.discount += self.sign * amount(line)
				return []
			case _:
				raise Unsupported(f"a {detail_type} line ({PLANNED})")

	@staticmethod
	def _shipping(detail: Node) -> bool:
		item = detail.child("ItemRef")
		return item is not None and item.optional_string("value") == SHIPPING_ITEM

	def _item(self, key: str, line: Node, detail: Node) -> TradeLine:
		item = detail.child("ItemRef")
		product = self.books.of("Item", item)
		qty = detail.optional_decimal("Qty")
		total = amount(line)
		if qty is None or qty == 0:
			qty = Decimal(1)
		rate = detail.optional_decimal("UnitPrice")
		if rate is None or rate * qty != total:
			rate = exact_quotient(total, qty, minor_units(self.currency))
		if rate is None:
			raise Unsupported(
				f'line "{self._name(line, "Item")}" ({total} for {qty}) has no exact unit price'
			)
		return TradeLine(
			OrderLine(key, product, None, self._name(line, "Item"), self.sign * qty, rate),
			account=self.books.of("Account", detail.child("ItemAccountRef")),
			cost_center=self.books.cost_center(detail, self.node),
			project=self.books.of("Customer", detail.child("CustomerRef")) or self.project,
		)

	@staticmethod
	def _name(line: Node, fallback: str) -> str:
		description = line.optional_string("Description")
		if description:
			return description
		for detail_type in (SALES_LINE, ITEM_EXPENSE_LINE):
			detail = line.child(detail_type)
			item = detail.child("ItemRef") if detail else None
			if item is not None and item.optional_string("name"):
				return item.string("name")
		return fallback


def taxes(node: Node, sign: Decimal) -> list[TradeTax]:
	"""A transaction's taxes, one per tax rate it charged (the connection's tax account books them)."""
	detail = node.child("TxnTaxDetail")
	if detail is None:
		return []
	rows: list[TradeTax] = []
	for line in detail.children("TaxLine"):
		tax = line.child("TaxLineDetail")
		rate = tax.child("TaxRateRef") if tax else None
		percent = tax.optional_decimal("TaxPercent") if tax else None
		label = (
			rate.optional_string("name") if rate else None
		) or f"Tax rate {rate.text('value') if rate else ''}".strip()
		if percent is not None:
			label = f"{label} ({percent}%)"
		if amount(line):
			rows.append(TradeTax(label, sign * amount(line)))
	if not rows and amount(detail, "TotalTax"):
		rows.append(TradeTax("Tax", sign * amount(detail, "TotalTax")))
	return rows


def currency_of(node: Node) -> str:
	reference = node.child("CurrencyRef")
	code = reference.optional_string("value") if reference else None
	if code is None:
		raise Unsupported(f"{node.path} names no currency")
	return code


def rate_of(node: Node) -> Decimal | None:
	rate = node.optional_decimal("ExchangeRate")
	return None if rate is None or rate == 1 else rate


def document(books: QuickBooks, node: Node, party_entity: str, party_field: str) -> TradeDocument:
	kind = entity(node)
	sign = Decimal(-1) if kind in RETURNS else Decimal(1)
	currency = currency_of(node)
	lines = Lines(books, node, currency, sign)
	trade = lines.trade()
	return TradeDocument(
		number=node.optional_string("DocNumber") or f"{kind} {node.string('Id')}",
		date=required_day(node.optional_string("TxnDate"), node.path),
		party=books.required(party_entity, node, party_field),
		currency=currency,
		lines=trade,
		taxes=[*taxes(node, sign), *lines.freight],
		discount=lines.discount or None,
		total=sign * amount(node, "TotalAmt"),
		submitted=True,
		due_date=node.optional_string("DueDate"),
		exchange_rate=rate_of(node),
		is_return=kind in RETURNS,
		reference=node.optional_string("DocNumber")
		if party_entity == "Vendor"
		else node.optional_string("PONumber"),
		remarks=node.optional_string("PrivateNote"),
	)


class SalesDocumentMapper:
	"""Invoices, sales receipts, credit memos and refund receipts as Sales Invoices (the two returns as
	credit notes); a sales receipt's and a refund receipt's money is their own payment entry."""

	kind = Kind.SALES_INVOICE
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		return Desired(
			self.kind, node.id(), trade_fields(document(self.books, node, "Customer", "CustomerRef"))
		)


class PurchaseDocumentMapper:
	"""Bills and vendor credits as Purchase Invoices (a vendor credit as a debit note)."""

	kind = Kind.PURCHASE_INVOICE
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		return Desired(self.kind, node.id(), trade_fields(document(self.books, node, "Vendor", "VendorRef")))


class EstimateMapper:
	"""An estimate as a draft Quotation (the shared ``draft_order``): its item lines, discount and taxes. Its
	date is the estimate's, at midday UTC, so the site's calendar date is the estimate's own."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		currency = currency_of(node)
		read = Lines(self.books, node, currency, Decimal(1))
		lines = [line_fields(trade.line) for trade in read.trade()]
		charges = [*taxes(node, Decimal(1)), *read.freight]
		fields: Fields = {
			"number": node.optional_string("DocNumber") or f"Estimate {node.string('Id')}",
			"currency": currency,
			"created_at": midday(required_day(node.optional_string("TxnDate"), node.path)),
			"customer": self.books.required("Customer", node, "CustomerRef"),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": list[Field](lines),
			"discount": read.discount,
			"charges": [charge_fields(Charge(tax.role, tax.description, tax.amount)) for tax in charges],
			"total": amount(node, "TotalAmt"),
			"remarks": node.optional_string("PrivateNote"),
		}
		if rate_of(node) is not None:
			fields["exchange_rate"] = rate_of(node)
		return Desired(self.kind, node.id(), fields)


class PurchaseOrderMapper:
	kind = Kind.PURCHASE_ORDER
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		return Desired(self.kind, node.id(), trade_fields(document(self.books, node, "Vendor", "VendorRef")))


class PaymentMapper:
	"""Payments received (against invoices), bill payments (against bills), and the money a sales receipt
	took or a refund receipt paid out. A payment that applies a credit memo or vendor credit is planned."""

	kind = Kind.PAYMENT_ENTRY
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		match entity(node):
			case "Payment":
				account = self.books.of("Account", node.child("DepositToAccountRef"))
				payment = self._applied(node, RECEIVE, CUSTOMER_PARTY, "Customer", "CustomerRef", account)
			case "BillPayment":
				payment = self._applied(
					node, PAY, SUPPLIER_PARTY, "Vendor", "VendorRef", self._paid_from(node)
				)
			case "SalesReceiptPayment" | "RefundReceiptPayment":
				payment = self._receipt(node)
			case other:
				raise Unsupported(f"a {other} is not a payment")
		return Desired(self.kind, node.id(), payment_fields(payment))

	def _paid_from(self, node: Node) -> str | None:
		"""A bill payment's bank account (a cheque) or credit card account."""
		cheque = node.child("CheckPayment")
		if node.text("PayType") == "Check" and cheque is not None:
			return self.books.of("Account", cheque.child("BankAccountRef"))
		card = node.child("CreditCardPayment")
		return self.books.of("Account", card.child("CCAccountRef")) if card is not None else None

	def _applied(
		self,
		node: Node,
		direction: str,
		party_type: str,
		party_entity: str,
		party_field: str,
		account: str | None,
	) -> Payment:
		allocations: list[Allocation] = []
		for line in node.children("Line"):
			for linked in line.children("LinkedTxn"):
				kind = DOCUMENT_KINDS.get(linked.text("TxnType"))
				if kind is None or linked.text("TxnType") in RETURNS:
					raise Unsupported(f"it applies a {linked.text('TxnType')} ({PLANNED})")
				allocations.append(
					Allocation(
						kind,
						self.books.config.identity(linked.text("TxnType"), linked.string("TxnId")),
						amount(line),
					)
				)
		return Payment(
			payment_type=direction,
			party_type=party_type,
			party=self.books.required(party_entity, node, party_field),
			date=required_day(node.optional_string("TxnDate"), node.path),
			amount=amount(node, "TotalAmt"),
			currency=currency_of(node),
			allocations=allocations,
			exchange_rate=rate_of(node),
			account=account,
			reference=node.optional_string("PaymentRefNum") or node.optional_string("DocNumber"),
			remarks=node.optional_string("PrivateNote"),
		)

	def _receipt(self, node: Node) -> Payment:
		source = entity(node).removesuffix("Payment")
		document_gid = self.books.config.identity(source, node.string("Id"))
		return Payment(
			payment_type=RECEIVE if source == "SalesReceipt" else PAY,
			party_type=CUSTOMER_PARTY,
			party=self.books.required("Customer", node, "CustomerRef"),
			date=required_day(node.optional_string("TxnDate"), node.path),
			amount=amount(node, "TotalAmt"),
			currency=currency_of(node),
			allocations=[Allocation(Kind.SALES_INVOICE, document_gid, amount(node, "TotalAmt"))],
			exchange_rate=rate_of(node),
			account=self.books.of("Account", node.child("DepositToAccountRef")),
			reference=node.optional_string("PaymentRefNum") or node.optional_string("DocNumber"),
		)


class JournalMapper:
	"""Journal entries as they are, and the money QuickBooks moves without a document of ERPNext's own
	(deposits, transfers between accounts, expenses paid by cheque, cash or card) as the journal entries
	they post, balanced exactly."""

	kind = Kind.JOURNAL_ENTRY
	version = 1

	def __init__(self, books: QuickBooks) -> None:
		self.books = books

	def map(self, node: Node) -> Desired:
		kind = entity(node)
		match kind:
			case "JournalEntry":
				lines = self._journal(node)
			case "Deposit":
				lines = self._deposit(node)
			case "Transfer":
				lines = self._transfer(node)
			case "Purchase":
				lines = self._purchase(node)
			case other:
				raise Unsupported(f"a {other} is not a journal")
		number = node.optional_string("DocNumber") or f"{kind} {node.string('Id')}"
		fields = journal_fields(
			number,
			required_day(node.optional_string("TxnDate"), node.path),
			lines,
			True,
			node.optional_string("PrivateNote"),
		)
		return Desired(self.kind, node.id(), fields)

	def _party(self, reference: Node | None, type_: str | None) -> tuple[str | None, str | None]:
		party_type = PARTY_TYPES.get(type_ or "")
		if reference is None or party_type is None:
			return None, None
		return party_type, self.books.of(PARTY_ENTITIES[party_type], reference)

	def _journal(self, node: Node) -> list[JournalLine]:
		lines: list[JournalLine] = []
		for line in node.children("Line"):
			detail = line.required_child("JournalEntryLineDetail")
			value = amount(line)
			holder = detail.child("Entity")
			party_type, party = self._party(
				holder.child("EntityRef") if holder else None,
				holder.optional_string("Type") if holder else None,
			)
			debit = detail.string("PostingType") == DEBIT
			lines.append(
				JournalLine(
					account=self.books.required("Account", detail, "AccountRef"),
					debit=value if debit else Decimal(0),
					credit=Decimal(0) if debit else value,
					party_type=party_type,
					party=party,
					cost_center=self.books.cost_center(detail),
				)
			)
		return lines

	def _deposit(self, node: Node) -> list[JournalLine]:
		"""The bank debited the deposit's total (and any cash back's account its amount); each line credits
		its account, or Undeposited Funds for a payment it deposits."""
		lines = [
			JournalLine(
				self.books.required("Account", node, "DepositToAccountRef"),
				amount(node, "TotalAmt"),
				Decimal(0),
			)
		]
		cash_back = node.child("CashBack")
		if cash_back is not None and amount(cash_back):
			lines.append(
				JournalLine(
					self.books.required("Account", cash_back, "AccountRef"), amount(cash_back), Decimal(0)
				)
			)
		for line in node.children("Line"):
			detail = line.child("DepositLineDetail")
			account = self.books.of("Account", detail.child("AccountRef")) if detail else None
			if account is None and line.children("LinkedTxn"):
				account = self.books.of("Account", node.child("UndepositedFundsRef"))
			if account is None:
				raise Unsupported(f"{line.path} deposits to no account")
			entity_ref = detail.child("Entity") if detail else None
			party_type, party = self._party(
				entity_ref, entity_ref.optional_string("type") if entity_ref else None
			)
			lines.append(
				JournalLine(
					account,
					Decimal(0),
					amount(line),
					party_type=party_type,
					party=party,
					cost_center=self.books.cost_center(detail, node) if detail else None,
				)
			)
		return lines

	def _transfer(self, node: Node) -> list[JournalLine]:
		value = amount(node)
		return [
			JournalLine(
				self.books.required("Account", node, "ToAccountRef"),
				value,
				Decimal(0),
				cost_center=self.books.cost_center(node),
			),
			JournalLine(
				self.books.required("Account", node, "FromAccountRef"),
				Decimal(0),
				value,
				cost_center=self.books.cost_center(node),
			),
		]

	def _purchase(self, node: Node) -> list[JournalLine]:
		"""An expense paid from a bank or card account: the account credited the total, each line's expense
		account debited (a refund, ``Credit``, the other way round). Tax on an expense, and expenses of items,
		book to accounts QuickBooks keeps per tax agency and item, and are planned."""
		if amount(node.child("TxnTaxDetail") or Node({}), "TotalTax"):
			raise Unsupported(f"an expense with tax ({PLANNED})")
		refund = node.optional_flag("Credit") is True
		entity_ref = node.child("EntityRef")
		party_type, party = self._party(
			entity_ref, entity_ref.optional_string("type") if entity_ref else None
		)
		total = amount(node, "TotalAmt")
		lines = [
			JournalLine(
				self.books.required("Account", node, "AccountRef"),
				total if refund else Decimal(0),
				Decimal(0) if refund else total,
			)
		]
		for line in node.children("Line"):
			detail = line.child(ACCOUNT_EXPENSE_LINE)
			if detail is None:
				raise Unsupported(f"an expense of a {line.text('DetailType')} ({PLANNED})")
			value = amount(line)
			lines.append(
				JournalLine(
					self.books.required("Account", detail, "AccountRef"),
					Decimal(0) if refund else value,
					value if refund else Decimal(0),
					party_type=party_type,
					party=party,
					cost_center=self.books.cost_center(detail, node),
					project=self.books.of("Customer", detail.child("CustomerRef")),
				)
			)
		return lines
