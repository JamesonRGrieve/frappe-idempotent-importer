# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Zoho Books / Zoho Inventory → desired ERPNext state, in the shared field contract. The two products
share Zoho's data model (www.zoho.com/books/api/v3, www.zoho.com/inventory/api/v1), so one set of mappers
serves both; each product's adapter lists the ones it reads. Translated here: a document's line items
(``rate`` times ``quantity`` is ``item_total``), its taxes and entity-level discount, shipping charge and
adjustment as lines, statuses (a void document has no copy; a draft is a draft), and journals and expenses
as balanced journal entries."""

from __future__ import annotations

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
	variant_attributes,
	warehouse_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.ledger import book_item, country_code, midday, required_day
from idempotent_importer.sources.zoho.client import every

PLANNED = "planned: card 386"
ROOT_TYPES = {
	"cash": "Asset",
	"bank": "Asset",
	"accounts_receivable": "Asset",
	"other_current_asset": "Asset",
	"fixed_asset": "Asset",
	"other_asset": "Asset",
	"stock": "Asset",
	"input_tax": "Asset",
	"payment_clearing": "Asset",
	"intangible_asset": "Asset",
	"non_current_asset": "Asset",
	"accounts_payable": "Liability",
	"other_current_liability": "Liability",
	"credit_card": "Liability",
	"long_term_liability": "Liability",
	"other_liability": "Liability",
	"output_tax": "Liability",
	"overseas_tax_payable": "Liability",
	"non_current_liability": "Liability",
	"equity": "Equity",
	"income": "Income",
	"other_income": "Income",
	"expense": "Expense",
	"cost_of_goods_sold": "Expense",
	"other_expense": "Expense",
}
ACCOUNT_TYPES = {
	"cash": "Cash",
	"bank": "Bank",
	"accounts_receivable": "Receivable",
	"accounts_payable": "Payable",
	"fixed_asset": "Fixed Asset",
	"stock": "Stock",
	"input_tax": "Tax",
	"output_tax": "Tax",
	"equity": "Equity",
	"income": "Income Account",
	"other_income": "Income Account",
	"expense": "Expense Account",
	"other_expense": "Expense Account",
	"cost_of_goods_sold": "Cost of Goods Sold",
}
VOID = frozenset({"void", "cancelled", "voided"})
DRAFT = frozenset({"draft", "pending_approval"})
INACTIVE = "inactive"
RETURN_TYPES = frozenset({"CreditNote", "VendorCredit"})
SALES_TYPES = frozenset({"Invoice", "CreditNote", "SalesReceipt", "SalesOrder", "Estimate"})
DOCUMENT_NUMBERS = (
	"invoice_number",
	"creditnote_number",
	"receipt_number",
	"salesorder_number",
	"purchaseorder_number",
	"bill_number",
	"vendor_credit_number",
	"estimate_number",
)


def text_decimal(node: Node, field: str) -> Decimal:
	"""An amount Zoho sends as a number or as text ("693.00"), exactly."""
	return node.optional_decimal(field) or Decimal(0)


def type_of(node: Node) -> str:
	return Identity.parse(node.id()).type


class Zoho:
	"""The connection's identities of Zoho's references."""

	def __init__(self, config: ConnectionConfig, ledger: bool = True) -> None:
		self.config = config
		# Whether the product reads the chart of accounts (Zoho Inventory has none to read: its documents'
		# accounts are left to the items' and the connection's defaults).
		self.ledger = ledger

	def of(self, type_: str, node: Node | None, field: str) -> str | None:
		if type_ == "Account" and not self.ledger:
			return None
		value = node.optional_string(field) if node is not None else None
		return self.config.identity(type_, value) if value else None

	def required(self, type_: str, node: Node, field: str) -> str:
		found = self.of(type_, node, field)
		if found is None:
			raise Unsupported(f"{node.path} has no {field}")
		return found

	def item(self, line: Node) -> tuple[str | None, str | None]:
		"""A line's item as (product, variant) identities: an item in an item group is a variant of the
		group (Zoho Inventory's reader names the group on the line)."""
		item = self.of("Item", line, "item_id")
		group = self.of("ItemGroup", line, "group_id")
		return (group, item) if group and item else (item, None)


def address_of(
	node: Node | None, kind: str, email: str | None, phone: str | None
) -> tuple[Fields | None, str | None]:
	street = (node.optional_string("address") or node.optional_string("street_address1")) if node else None
	if node is None or not street or not node.optional_string("city"):
		return None, None
	code = country_code(node.optional_string("country"))
	if code is None:
		return None, f"its {kind.lower()} address names no country ERPNext knows ({node.text('country')})"
	return (
		address_fields(
			Address(
				address_type=kind,
				name=node.optional_string("attention"),
				company=None,
				line1=street,
				line2=node.optional_string("street2") or node.optional_string("street_address2"),
				city=node.string("city"),
				state=node.optional_string("state_code") or node.optional_string("state"),
				postcode=node.optional_string("zip"),
				country_code=code,
				email=email,
				phone=phone,
			)
		),
		None,
	)


class AccountMapper:
	"""An account of the chart, top-level in the company's tree (Zoho posts to accounts with sub-accounts,
	which ERPNext allows only to a leaf)."""

	kind = Kind.ACCOUNT
	version = 1

	def __init__(self, zoho: Zoho) -> None:
		self.zoho = zoho

	def map(self, node: Node) -> Desired:
		type_ = node.string("account_type")
		root = ROOT_TYPES.get(type_)
		if root is None:
			raise Unsupported(f"the account type {type_} has no root type ({PLANNED})")
		account = LedgerAccount(
			number=node.optional_string("account_code"),
			name=node.string("account_name"),
			root_type=root,
			account_type=ACCOUNT_TYPES.get(type_),
			currency=node.optional_string("currency_code"),
		)
		return Desired(self.kind, node.id(), account_fields(account))


class LocationMapper:
	kind = Kind.WAREHOUSE
	version = 1

	def __init__(self, zoho: Zoho) -> None:
		self.zoho = zoho

	def map(self, node: Node) -> Desired:
		address, warning = address_of(
			node.child("address"), "Shipping", node.optional_string("email"), node.optional_string("phone")
		)
		fields = warehouse_fields(
			node.string("location_name"), self.zoho.of("Location", node, "parent_location_id"), False, address
		)
		return Desired(self.kind, node.id(), fields, warnings=(warning,) if warning else ())


class CategoryMapper:
	kind = Kind.COLLECTION
	version = 1

	def __init__(self, zoho: Zoho) -> None:
		self.zoho = zoho

	def map(self, node: Node) -> Desired:
		name = node.string("name")
		fields: Fields = {
			"name": name,
			"slug": slugify(name),
			"parent": self.zoho.of("Category", node, "parent_category_id"),
			"description": node.optional_string("description"),
			"image": None,
		}
		return Desired(self.kind, node.id(), fields)


class ItemMapper:
	"""An item (goods, a service, an inventory item) in its category; an item group (Zoho Inventory) as a
	template whose items are its variants. An inactive one is retired."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, zoho: Zoho) -> None:
		self.zoho = zoho

	def map(self, node: Node) -> Desired | None:
		if node.optional_string("status") == INACTIVE:
			return None
		category = self.zoho.of("Category", node, "category_id")
		if type_of(node) == "ItemGroup":
			return self._group(node, [category] if category else [])
		stock = node.optional_string("item_type") in ("inventory", "sales_and_purchases_inventory")
		fields = book_item(
			name=node.string("name"),
			sku=node.optional_string("sku"),
			description=node.optional_string("description"),
			price=node.optional_decimal("rate"),
			is_stock_item=stock,
			stock_qty=node.optional_decimal("stock_on_hand") if stock else None,
			collections=[category] if category else [],
		)
		return Desired(self.kind, node.id(), fields)

	def _group(self, node: Node, collections: list[str]) -> Desired:
		attribute = node.optional_string("attribute_name1") or "Variant"
		options = {
			option.string("id"): option.string("name")
			for holder in (node, *every(node, "attributes"))
			for option in every(holder, "options")
		}
		variants: list[Fields] = []
		for item in every(node, "items"):
			value = options.get(item.text("attribute_option_id1")) or item.optional_string(
				"attribute_option_name1"
			)
			if value is None:
				raise Unsupported(f"{item.text('name')} names no {attribute} option")
			variants.append(
				{
					"gid": self.zoho.required("Item", item, "item_id"),
					"sku": item.optional_string("sku"),
					"barcode": None,
					"attributes": {attribute: value},
					"price": item.optional_decimal("rate"),
					"sale": None,
					"stock_qty": item.optional_decimal("stock_on_hand"),
					"weight": None,
					"image": None,
					"enabled": item.optional_string("status") != INACTIVE,
				}
			)
		fields = book_item(
			name=node.string("group_name"),
			sku=None,
			description=node.optional_string("description"),
			price=None,
			is_stock_item=True,
			collections=collections,
			variants=variants,
			attributes=variant_attributes(variants),
		)
		return Desired(self.kind, node.id(), fields)


class ContactMapper:
	"""A contact as a customer or a vendor (supplier), its primary contact person and its addresses."""

	version = 1

	def __init__(self, zoho: Zoho, kind: Kind) -> None:
		self.zoho = zoho
		self.kind = kind

	def map(self, node: Node) -> Desired:
		persons = node.children("contact_persons")
		person = next(
			(p for p in persons if p.optional_flag("is_primary_contact")), persons[0] if persons else None
		)
		email = person.optional_string("email") if person else None
		phone = (person.optional_string("phone") or person.optional_string("mobile")) if person else None
		addresses: list[Fields] = []
		warnings: list[str] = []
		for field, kind in (("billing_address", "Billing"), ("shipping_address", "Shipping")):
			address, warning = address_of(node.child(field), kind, email, phone)
			if warning:
				warnings.append(warning)
			if address is not None and address not in addresses:
				addresses.append(address)
		company = node.optional_string("company_name")
		party = Party(
			name=node.string("contact_name"),
			type="Company" if company else "Individual",
			first_name=person.optional_string("first_name") if person else None,
			last_name=person.optional_string("last_name") if person else None,
			company=company,
			email=email,
			phone=phone,
			addresses=addresses,
			tax_id=node.optional_string("gst_no") or node.optional_string("tax_reg_no"),
			currency=node.optional_string("currency_code"),
		)
		return Desired(self.kind, node.id(), party_fields(party), warnings=tuple(warnings))


class ProjectMapper:
	kind = Kind.PROJECT
	version = 1

	def __init__(self, zoho: Zoho) -> None:
		self.zoho = zoho

	def map(self, node: Node) -> Desired:
		fields = project_fields(
			name=node.string("project_name"),
			status="Open" if node.optional_string("status") == "active" else "Completed",
			customer=self.zoho.of("Contact", node, "customer_id"),
			notes=node.optional_string("description"),
		)
		return Desired(self.kind, node.id(), fields)


class Document:
	"""A Zoho document's lines, taxes and charges in ERPNext's terms."""

	def __init__(self, zoho: Zoho, node: Node) -> None:
		self.zoho = zoho
		self.node = node
		self.type = type_of(node)
		self.sign = Decimal(-1) if self.type in RETURN_TYPES else Decimal(1)
		if node.optional_flag("is_inclusive_tax") is True:
			raise Unsupported(f"a document with tax-inclusive rates ({PLANNED})")

	def lines(self) -> list[TradeLine]:
		lines: list[TradeLine] = []
		for index, line in enumerate(every(self.node, "line_items")):
			rate = line.optional_decimal("rate")
			if rate is None:
				rate = line.optional_decimal("purchase_rate")
			qty = line.optional_decimal("quantity") or Decimal(1)
			name = line.optional_string("name") or line.optional_string("description") or "Line"
			if rate is None:
				raise Unsupported(f'line "{name}" has no rate')
			total = line.optional_decimal("item_total")
			if total is not None and total != rate * qty:
				raise Unsupported(
					f'line "{name}" ({total}) isn\'t its rate times its quantity (a line discount)'
				)
			product, variant = self.zoho.item(line)
			lines.append(
				TradeLine(
					OrderLine(
						line.optional_string("line_item_id") or str(index),
						product,
						variant,
						name,
						self.sign * qty,
						rate,
					),
					account=self.zoho.of("Account", line, "account_id"),
					project=self.zoho.of("Project", line, "project_id"),
				)
			)
		for field, label in (("shipping_charge", "Shipping"), ("adjustment", "Adjustment")):
			value = text_decimal(self.node, field)
			if value:
				lines.append(TradeLine(OrderLine(field, None, None, label, self.sign, value)))
		discount = self.discount(lines)
		if discount:
			lines.append(TradeLine(OrderLine("discount", None, None, "Discount", self.sign, -discount)))
		return lines

	def taxes(self) -> list[TradeTax]:
		"""The document's taxes by name; a document that itemises none (a sales receipt) its tax total."""
		taxes = [
			TradeTax(tax.string("tax_name"), self.sign * text_decimal(tax, "tax_amount"))
			for tax in every(self.node, "taxes")
			if text_decimal(tax, "tax_amount")
		]
		total = text_decimal(self.node, "tax_total")
		if not taxes and total:
			taxes.append(TradeTax("Tax", self.sign * total))
		return taxes

	def total(self) -> Decimal:
		"""The document's total (Zoho Inventory documents a bill without one: its sub-total and tax)."""
		if self.node.has("total"):
			return text_decimal(self.node, "total")
		return sum(
			(
				text_decimal(self.node, field)
				for field in ("sub_total", "tax_total", "shipping_charge", "adjustment")
			),
			Decimal(0),
		)

	def discount(self, lines: list[TradeLine]) -> Decimal:
		"""The document's entity-level discount, as the amount its lines, taxes and charges come to beyond
		its total (Zoho gives the discount as a percentage or an amount, and its total after it)."""
		if not text_decimal(self.node, "discount") and not text_decimal(self.node, "discount_amount"):
			return Decimal(0)
		made = sum((line.line.qty * line.line.rate for line in lines), Decimal(0)) * self.sign
		taxes = sum((text_decimal(tax, "tax_amount") for tax in every(self.node, "taxes")), Decimal(0))
		return made + taxes - self.total()

	def number(self) -> str:
		for field in DOCUMENT_NUMBERS:
			if self.node.optional_string(field):
				return self.node.string(field)
		return f"{self.type} {self.node.id().rsplit('/', 1)[-1]}"

	def rate(self) -> Decimal | None:
		rate = self.node.optional_decimal("exchange_rate")
		return None if rate is None or rate == 1 else rate

	def currency(self) -> str:
		code = self.node.optional_string("currency_code")
		if code is None:
			raise Unsupported(f"{self.node.path} names no currency")
		return code


class TradeMapper:
	"""Sales and purchase orders, invoices, credit notes, sales receipts, bills and vendor credits as
	ERPNext's orders and invoices (credit notes and vendor credits as returns). A void one has no copy; a
	draft is a draft."""

	version = 1

	def __init__(self, zoho: Zoho, kind: Kind) -> None:
		self.zoho = zoho
		self.kind = kind

	def map(self, node: Node) -> Desired | None:
		status = node.text("status")
		if status in VOID:
			return None
		document = Document(self.zoho, node)
		sales = document.type in SALES_TYPES
		trade = TradeDocument(
			number=document.number(),
			date=required_day(node.optional_string("date"), node.path),
			party=self.zoho.required("Contact", node, "customer_id" if sales else "vendor_id"),
			currency=document.currency(),
			lines=document.lines(),
			taxes=document.taxes(),
			total=document.sign * document.total(),
			submitted=status not in DRAFT,
			due_date=node.optional_string("due_date")
			or node.optional_string("shipment_date")
			or node.optional_string("delivery_date")
			or node.optional_string("expected_delivery_date"),
			exchange_rate=document.rate(),
			is_return=document.type in RETURN_TYPES,
			reference=node.optional_string("reference_number"),
			remarks=node.optional_string("notes"),
		)
		return Desired(self.kind, node.id(), trade_fields(trade))


class EstimateMapper:
	"""An estimate as a draft Quotation (the shared ``draft_order``); a declined or expired one has none."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def __init__(self, zoho: Zoho) -> None:
		self.zoho = zoho

	def map(self, node: Node) -> Desired | None:
		if node.text("status") in ("declined", "expired", *VOID):
			return None
		document = Document(self.zoho, node)
		lines = document.lines()
		fields: Fields = {
			"number": document.number(),
			"currency": document.currency(),
			"created_at": midday(required_day(node.optional_string("date"), node.path)),
			"customer": self.zoho.required("Contact", node, "customer_id"),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": list[Field](line_fields(line.line) for line in lines),
			"discount": Decimal(0),
			"charges": [charge_fields(Charge(TAX, tax.description, tax.amount)) for tax in document.taxes()],
			"total": document.total(),
			"remarks": node.optional_string("notes"),
		}
		if document.rate() is not None:
			fields["exchange_rate"] = document.rate()
		return Desired(self.kind, node.id(), fields)


class PaymentMapper:
	"""Customer payments (against invoices), vendor payments (against bills) and the payment a sales
	receipt took, each with what it was applied to; what it wasn't applied to is an advance."""

	kind = Kind.PAYMENT_ENTRY
	version = 1

	def __init__(self, zoho: Zoho) -> None:
		self.zoho = zoho

	def map(self, node: Node) -> Desired | None:
		if node.text("status") in VOID:
			return None
		match type_of(node):
			case "CustomerPayment":
				payment = self._payment(
					node,
					RECEIVE,
					CUSTOMER_PARTY,
					"customer_id",
					"invoices",
					"invoice_id",
					"Invoice",
					Kind.SALES_INVOICE,
					"account_id",
				)
			case "VendorPayment":
				payment = self._payment(
					node,
					PAY,
					SUPPLIER_PARTY,
					"vendor_id",
					"bills",
					"bill_id",
					"Bill",
					Kind.PURCHASE_INVOICE,
					"paid_through_account_id",
				)
			case "SalesReceiptPayment":
				payment = self._receipt(node)
			case other:
				raise Unsupported(f"a {other} is not a payment")
		return Desired(self.kind, node.id(), payment_fields(payment))

	def _payment(
		self,
		node: Node,
		direction: str,
		party_type: str,
		party_field: str,
		applied: str,
		document_field: str,
		document_type: str,
		kind: Kind,
		account_field: str,
	) -> Payment:
		allocations = [
			Allocation(
				kind,
				self.zoho.required(document_type, entry, document_field),
				text_decimal(entry, "amount_applied"),
			)
			for entry in node.children(applied)
			if text_decimal(entry, "amount_applied")
		]
		currency = node.optional_string("currency_code")
		if currency is None:
			raise Unsupported(f"{node.path} names no currency")
		rate = node.optional_decimal("exchange_rate")
		return Payment(
			payment_type=direction,
			party_type=party_type,
			party=self.zoho.required("Contact", node, party_field),
			date=required_day(node.optional_string("date"), node.path),
			amount=text_decimal(node, "amount"),
			currency=currency,
			allocations=allocations,
			exchange_rate=None if rate is None or rate == 1 else rate,
			account=self.zoho.of("Account", node, account_field),
			reference=node.optional_string("reference_number"),
			remarks=node.optional_string("description"),
		)

	def _receipt(self, node: Node) -> Payment:
		document = Document(self.zoho, node)
		receipt = self.zoho.config.identity("SalesReceipt", node.id().rsplit("/", 1)[-1])
		return Payment(
			payment_type=RECEIVE,
			party_type=CUSTOMER_PARTY,
			party=self.zoho.required("Contact", node, "customer_id"),
			date=required_day(node.optional_string("date"), node.path),
			amount=document.total(),
			currency=document.currency(),
			allocations=[Allocation(Kind.SALES_INVOICE, receipt, document.total())],
			exchange_rate=document.rate(),
			account=self.zoho.of("Account", node, "deposit_to_account_id"),
			reference=document.number(),
		)


class JournalMapper:
	"""Manual journals (in the organisation's currency: each line's base amount), and expenses as the journal
	they post (the expense account debited, the paid-through account credited). Tax on an expense books to
	a tax account Zoho names per tax, and is planned."""

	kind = Kind.JOURNAL_ENTRY
	version = 1

	def __init__(self, zoho: Zoho) -> None:
		self.zoho = zoho

	def map(self, node: Node) -> Desired | None:
		if node.text("status") in VOID:
			return None
		if type_of(node) == "Expense":
			return self._expense(node)
		lines: list[JournalLine] = []
		for line in every(node, "line_items"):
			value = line.optional_decimal("bcy_amount")
			if value is None:
				value = text_decimal(line, "amount")
			debit = line.string("debit_or_credit") == "debit"
			customer = self.zoho.of("Contact", line, "customer_id")
			lines.append(
				JournalLine(
					self.zoho.required("Account", line, "account_id"),
					value if debit else Decimal(0),
					Decimal(0) if debit else value,
					party_type=CUSTOMER_PARTY if customer else None,
					party=customer,
					project=self.zoho.of("Project", line, "project_id"),
				)
			)
		fields = journal_fields(
			node.optional_string("entry_number") or node.id().rsplit("/", 1)[-1],
			required_day(node.optional_string("journal_date"), node.path),
			lines,
			node.text("status") not in DRAFT,
			node.optional_string("notes"),
		)
		return Desired(self.kind, node.id(), fields)

	def _expense(self, node: Node) -> Desired:
		if text_decimal(node, "tax_amount"):
			raise Unsupported(f"an expense with tax ({PLANNED})")
		value = node.optional_decimal("bcy_total")
		if value is None:
			value = text_decimal(node, "total")
		lines = [
			JournalLine(
				self.zoho.required("Account", node, "account_id"),
				value,
				Decimal(0),
				project=self.zoho.of("Project", node, "project_id"),
			),
			JournalLine(self.zoho.required("Account", node, "paid_through_account_id"), Decimal(0), value),
		]
		fields = journal_fields(
			node.optional_string("reference_number") or f"Expense {node.id().rsplit('/', 1)[-1]}",
			required_day(node.optional_string("date"), node.path),
			lines,
			True,
			node.optional_string("description"),
		)
		return Desired(self.kind, node.id(), fields)


def books_mappers(config: ConnectionConfig) -> list[Mapper]:
	zoho = Zoho(config)
	return [
		AccountMapper(zoho),
		LocationMapper(zoho),
		ItemMapper(zoho),
		ContactMapper(zoho, Kind.CUSTOMER),
		ContactMapper(zoho, Kind.SUPPLIER),
		ProjectMapper(zoho),
		EstimateMapper(zoho),
		TradeMapper(zoho, Kind.SALES_ORDER),
		TradeMapper(zoho, Kind.SALES_INVOICE),
		TradeMapper(zoho, Kind.PURCHASE_ORDER),
		TradeMapper(zoho, Kind.PURCHASE_INVOICE),
		PaymentMapper(zoho),
		JournalMapper(zoho),
	]


def inventory_mappers(config: ConnectionConfig) -> list[Mapper]:
	zoho = Zoho(config, ledger=False)
	return [
		LocationMapper(zoho),
		CategoryMapper(zoho),
		ItemMapper(zoho),
		ContactMapper(zoho, Kind.CUSTOMER),
		ContactMapper(zoho, Kind.SUPPLIER),
		TradeMapper(zoho, Kind.SALES_ORDER),
		TradeMapper(zoho, Kind.SALES_INVOICE),
		TradeMapper(zoho, Kind.PURCHASE_ORDER),
		TradeMapper(zoho, Kind.PURCHASE_INVOICE),
		PaymentMapper(zoho),
	]
