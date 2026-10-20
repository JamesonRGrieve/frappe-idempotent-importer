# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure NetSuite → desired ERPNext state, one mapper per kind, in the shared field contract. NetSuite's
record shapes are those of its REST API Browser (2026.1); its account types, transaction statuses and
item types are translated here. Identities arrive scoped to the connection, with NetSuite's internal id
as ``source_id``; the reader adds ``record_type`` (the REST record), ``currency_code`` (the ISO code of a
record's currency), and for transactions ``posting`` and ``voided`` (SuiteQL's flags) and each line's
``item_type``.

A record a transaction refers to (``{"id", "refName", "links"}``) is identified by the record type in its
``self`` link, so a journal line's entity is known to be a customer, a vendor or an employee."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from idempotent_importer.contract import (
	CUSTOMER_PARTY,
	EMPLOYEE_PARTY,
	FEE,
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
	warehouse_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.ledger import midday

TRUE = "T"
DESIGNATION_KEY_LENGTH = 32
# NetSuite's account types (acctType) by the root type ERPNext files them under, and the ERPNext account
# type the ones with a special role take. Non-posting and statistical accounts hold no money.
ROOT_TYPES: dict[str, str] = {
	"Bank": "Asset",
	"AcctRec": "Asset",
	"OthCurrAsset": "Asset",
	"FixedAsset": "Asset",
	"OthAsset": "Asset",
	"DeferExpense": "Asset",
	"UnbilledRec": "Asset",
	"AcctPay": "Liability",
	"CredCard": "Liability",
	"OthCurrLiab": "Liability",
	"LongTermLiab": "Liability",
	"DeferRevenue": "Liability",
	"Equity": "Equity",
	"Income": "Income",
	"OthIncome": "Income",
	"COGS": "Expense",
	"Expense": "Expense",
	"OthExpense": "Expense",
}
ACCOUNT_TYPES: dict[str, str] = {
	"Bank": "Bank",
	"AcctRec": "Receivable",
	"AcctPay": "Payable",
	"FixedAsset": "Fixed Asset",
	"COGS": "Cost of Goods Sold",
	"Equity": "Equity",
	"Income": "Income Account",
	"Expense": "Expense Account",
}
# A tax account (sSpecAcct) is a Tax account whatever its type.
TAX_SPECIAL_ACCOUNTS = frozenset({"SalesTaxPay", "Tax", "PSTPay", "PSTExp"})
WEIGHT_UNITS = {"lb": "lbs", "oz": "oz", "kg": "kg", "g": "g"}
STOCK_ITEMS = frozenset(
	{
		"inventoryItem",
		"lotNumberedInventoryItem",
		"serializedInventoryItem",
		"assemblyItem",
		"lotNumberedAssemblyItem",
		"serializedAssemblyItem",
	}
)
BASE_PRICE_LEVEL = "1"
# Lines that carry no amount of their own: a subtotal (of the lines above it), a description, and an
# item group's header and end (its members are lines of their own).
UNBOOKED_LINES = frozenset({"Subtotal", "Description", "Group", "EndGroup"})
# A discount line's (negative) amount comes off the document's net total.
DISCOUNT_LINE = "Discount"
# Lines whose item is a transaction construct, not something sold: booked as a line of no item.
CONSTRUCT_LINES = frozenset({"Markup", "Payment", "ShipItem", "TaxItem"})
# Transaction statuses (``status.id``): a sales order pending approval is a draft, a cancelled one gone; a
# purchase order pending supervisor approval or planned is a draft, a rejected one gone.
SALES_ORDER_DRAFT, SALES_ORDER_CANCELLED = "A", "C"
PURCHASE_ORDER_DRAFTS, PURCHASE_ORDER_REJECTED = frozenset({"A", "P"}), "C"
ESTIMATE_VOIDED = "V"
OPPORTUNITY_STATUSES = {"A": "Open", "B": "Quotation", "C": "Converted", "D": "Lost"}
PROJECT_STATUSES = {"closed": "Completed", "not awarded": "Cancelled"}
PROJECT_OPEN = "Open"
GENDERS = {"m": "Male", "f": "Female", "ns": "Prefer not to say"}
RETURNS = frozenset({"creditMemo", "cashRefund", "vendorCredit"})
CASH_SALES = frozenset({"cashSale", "cashRefund"})
# The documents whose ``account`` is the receivable or payable account they book the party's balance to (a
# cash sale's and a cash refund's is where the money went).
PARTY_ACCOUNT_RECORDS = frozenset({"invoice", "creditMemo", "vendorBill", "vendorCredit"})
PURCHASES = frozenset({"vendorBill", "vendorCredit", "purchaseOrder"})
# The charges on top of a document's lines, and the role each books as.
CHARGES = (("shippingCost", "Shipping", SHIPPING), ("handlingCost", "Handling", FEE))
# The party type of each entity record a reference names (its self link's record type).
ENTITY_PARTIES = {"customer": CUSTOMER_PARTY, "vendor": SUPPLIER_PARTY, "employee": EMPLOYEE_PARTY}
RECORD_LINK = "/record/v1/"


def designation_key(title: str) -> str:
	"""A job title's identity (titles have no record of their own)."""
	return hashlib.sha256(title.encode()).hexdigest()[:DESIGNATION_KEY_LENGTH]


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, type_: str, id_: str) -> str:
		return self.config.identity(type_, id_)

	def ref(self, node: Node, field: str, type_: str) -> str | None:
		"""The identity of the record ``field`` refers to, or None."""
		found = ref_id(node, field)
		return self.gid(type_, found) if found else None


def ref_id(node: Node, field: str) -> str | None:
	ref = node.child(field)
	return ref.optional_string("id") if ref is not None else None


def ref_name(node: Node, field: str) -> str | None:
	ref = node.child(field)
	return ref.optional_string("refName") if ref is not None else None


def enum_id(node: Node, field: str) -> str | None:
	"""An enumerated field's value (``{"id", "refName"}``)."""
	return ref_id(node, field)


def ref_record(node: Node, field: str) -> str | None:
	"""The REST record type a reference's self link names ("…/record/v1/vendor/12" → "vendor")."""
	ref = node.child(field)
	for link in ref.children("links") if ref is not None else []:
		href = link.optional_string("href") or ""
		if RECORD_LINK in href:
			return href.split(RECORD_LINK, 1)[1].split("/", 1)[0]
	return None


def is_true(node: Node, field: str) -> bool:
	return node.optional_string(field) == TRUE


def items_of(node: Node, sublist: str) -> list[Node]:
	collection = node.child(sublist)
	return collection.children("items") if collection is not None else []


def money(node: Node, field: str) -> Decimal:
	return node.optional_decimal(field) or Decimal(0)


def rate_of(node: Node) -> Decimal | None:
	"""The exchange rate a transaction recorded, when it isn't 1 (a document in the company's currency)."""
	rate = node.optional_decimal("exchangeRate")
	return rate if rate is not None and rate != 1 else None


def currency_of(node: Node) -> str:
	code = node.optional_string("currency_code")
	if code is None:
		raise SourceError(f"NetSuite record {node.string('source_id')} has no currency.")
	return code


def person(node: Node) -> str | None:
	return " ".join(p for p in (node.text("firstName"), node.text("lastName")) if p) or None


def addresses(node: Node) -> list[Fields]:
	"""An entity's address book: the default billing address as Billing, the default shipping one as
	Shipping, the rest as Other; an address without a street and city can't be an ERPNext Address."""
	found: list[Fields] = []
	for entry in items_of(node, "addressBook"):
		address = entry.child("addressBookAddress")
		if address is None or not address.text("addr1") or not address.text("city"):
			continue
		kind = (
			"Billing"
			if entry.optional_flag("defaultBilling")
			else "Shipping"
			if entry.optional_flag("defaultShipping")
			else "Other"
		)
		found.append(
			address_fields(
				Address(
					address_type=kind,
					name=address.optional_string("addressee") or entry.optional_string("label"),
					company=None,
					line1=address.string("addr1"),
					line2=" ".join(a for a in (address.text("addr2"), address.text("addr3")) if a) or None,
					city=address.string("city"),
					state=address.optional_string("state"),
					postcode=address.optional_string("zip"),
					country_code=enum_id(address, "country"),
					email=None,
					phone=address.optional_string("addrPhone"),
				)
			)
		)
	return found


def contacts(node: Node, context: Context) -> list[ContactPerson]:
	"""The entity's contacts (``contactList``), each with its own identity."""
	found: list[ContactPerson] = []
	for contact in items_of(node, "contactList"):
		name = person(contact) or contact.optional_string("entityId") or contact.optional_string("refName")
		if name is None or not contact.has("id"):
			continue
		found.append(
			ContactPerson(
				gid=context.gid("Contact", contact.string("id")),
				name=name,
				email=contact.optional_string("email"),
				phone=contact.optional_string("phone")
				or contact.optional_string("officePhone")
				or contact.optional_string("mobilePhone"),
				job_title=contact.optional_string("title"),
			)
		)
	return found


def party(node: Node, context: Context) -> Party:
	"""A customer or vendor; an inactive one stays, disabled, since its transactions are booked to it."""
	individual = node.optional_flag("isPerson") is True
	company = node.optional_string("companyName")
	name = (person(node) if individual else company) or company or person(node) or node.string("entityId")
	return Party(
		name=name,
		type="Individual" if individual else "Company",
		first_name=node.optional_string("firstName"),
		last_name=node.optional_string("lastName"),
		company=company,
		email=node.optional_string("email"),
		phone=node.optional_string("phone") or node.optional_string("mobilePhone"),
		addresses=addresses(node),
		tax_id=node.optional_string("vatRegNumber") or node.optional_string("defaultTaxReg"),
		currency=node.optional_string("currency_code"),
		contacts=contacts(node, context),
		disabled=node.optional_flag("isInactive") is True,
	)


def disabled_if_inactive(node: Node) -> Fields:
	return {"disabled": True} if node.optional_flag("isInactive") is True else {}


class AccountMapper:
	"""An account → an Account of the company's chart, under its parent when that is a summary account
	(ERPNext posts only to accounts without children, so a posting parent can't hold them: its children go
	under the root of their type). An inactive account stays, disabled, for the history booked to it.
	Non-posting and statistical accounts hold no money: no home."""

	kind = Kind.ACCOUNT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		account_type = enum_id(node, "acctType") or ""
		root_type = ROOT_TYPES.get(account_type)
		if root_type is None:
			raise Unsupported(f"a {account_type or 'typeless'} account holds no money ERPNext can book")
		special = enum_id(node, "sSpecAcct")
		parent = self._context.ref(node, "parent", "Account")
		warnings: tuple[str, ...] = ()
		if parent is not None and node.optional_flag("parent_is_summary") is not True:
			warnings = ("its parent account takes postings, so it is filed under the root of its type",)
			parent = None
		account = LedgerAccount(
			number=node.optional_string("acctNumber"),
			name=node.optional_string("acctName") or node.string("fullName"),
			root_type=root_type,
			account_type="Tax" if special in TAX_SPECIAL_ACCOUNTS else ACCOUNT_TYPES.get(account_type),
			parent=parent,
			is_group=node.optional_flag("isSummary") is True,
			currency=node.optional_string("currency_code"),
		)
		fields = account_fields(account) | disabled_if_inactive(node)
		return Desired(self.kind, node.id(), fields, warnings=warnings)


class TreeMapper:
	"""A department (as a Cost Center, or as an HR Department) or a location (as a Warehouse): its name and
	parent, a group when it has children (``has_children``, from the reader); an inactive one disabled. As a
	Cost Center a department is top-level and never a group: NetSuite books to a department that has
	sub-departments, ERPNext only to a cost center without children (the HR tree keeps the hierarchy)."""

	def __init__(self, kind: Kind, context: Context, identity: str) -> None:
		self.kind = kind
		self.version = 1
		self._context = context
		self._identity = identity

	def map(self, node: Node) -> Desired | None:
		name = node.string("name")
		parent = self._context.ref(node, "parent", self._identity)
		group = node.optional_flag("has_children") is True
		if self.kind == Kind.WAREHOUSE:
			fields = warehouse_fields(name, parent, group, location_address(node)) | disabled_if_inactive(
				node
			)
			return Desired(self.kind, node.id(), fields)
		inactive = node.optional_flag("isInactive") is True
		if self.kind == Kind.COST_CENTER:
			return Desired(self.kind, node.id(), tree_fields(name, None, False, inactive))
		return Desired(self.kind, node.id(), tree_fields(name, parent, group, inactive))


def location_address(node: Node) -> Fields | None:
	address = node.child("mainAddress")
	if address is None or not address.text("addr1") or not address.text("city"):
		return None
	return address_fields(
		Address(
			address_type="Warehouse",
			name=address.optional_string("addressee") or node.string("name"),
			company=None,
			line1=address.string("addr1"),
			line2=" ".join(a for a in (address.text("addr2"), address.text("addr3")) if a) or None,
			city=address.string("city"),
			state=address.optional_string("state"),
			postcode=address.optional_string("zip"),
			country_code=enum_id(address, "country"),
			email=None,
			phone=address.optional_string("addrPhone"),
		)
	)


class PartyMapper:
	"""A customer (at the customer stage) → a Customer, a vendor → a Supplier: contact details, address
	book and contacts."""

	def __init__(self, kind: Kind, context: Context) -> None:
		self.kind = kind
		self.version = 1
		self._context = context

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), party_fields(party(node, self._context)))


class LeadMapper:
	"""A customer at the lead or prospect stage → a Lead (a prospect has an opportunity or estimate); an
	inactive one disabled."""

	kind = Kind.LEAD
	version = 1

	def map(self, node: Node) -> Desired | None:
		stage = node.optional_string("searchStage")
		individual = node.optional_flag("isPerson") is True
		lead = Lead(
			status="Opportunity" if stage == "Prospect" else "Lead",
			first_name=node.optional_string("firstName") if individual else None,
			last_name=node.optional_string("lastName") if individual else None,
			company=node.optional_string("companyName"),
			email=node.optional_string("email"),
			phone=node.optional_string("phone"),
			mobile=node.optional_string("mobilePhone"),
			job_title=node.optional_string("title"),
			website=node.optional_string("url"),
			notes=node.optional_string("comments"),
			disabled=node.optional_flag("isInactive") is True,
		)
		return Desired(self.kind, node.id(), lead_fields(lead))


class EmployeeMapper:
	"""An employee → an Employee in their department, with their job title as designation and their
	supervisor. NetSuite keeps no status beyond inactive: an inactive employee with a release date has
	left."""

	kind = Kind.EMPLOYEE
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		released = node.optional_string("releasedate")
		inactive = node.optional_flag("isInactive") is True
		title = node.optional_string("title")
		employee = Employee(
			first_name=node.optional_string("firstName") or node.string("entityId"),
			middle_name=node.optional_string("middleName"),
			last_name=node.optional_string("lastName"),
			gender=GENDERS.get(enum_id(node, "gender") or ""),
			date_of_birth=node.optional_string("birthdate"),
			date_of_joining=node.optional_string("hireDate"),
			relieving_date=released,
			status=("Left" if released else "Inactive") if inactive else "Active",
			employee_number=node.optional_string("entityId"),
			department=self._context.ref(node, "department", "Department"),
			designation=self._context.gid("Designation", designation_key(title)) if title else None,
			reports_to=self._context.ref(node, "supervisor", "Employee"),
			company_email=node.optional_string("email"),
			personal_email=None,
			phone=node.optional_string("mobilePhone") or node.optional_string("phone"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class DesignationMapper:
	kind = Kind.DESIGNATION
	version = 1

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), {"name": node.string("title")})


class ProjectMapper:
	"""A job (project) → a Project for its customer, with its status, dates and comments."""

	kind = Kind.PROJECT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		status_name = (ref_name(node, "entityStatus") or "").strip().lower()
		status = PROJECT_STATUSES.get(status_name, PROJECT_OPEN)
		if node.optional_flag("isInactive") is True and status == PROJECT_OPEN:
			status = "Cancelled"
		name = node.optional_string("companyName") or node.string("entityId")
		fields = project_fields(
			name=name,
			status=status,
			customer=self._context.ref(node, "parent", "Customer"),
			start=node.optional_string("startDate"),
			end=node.optional_string("scheduledEndDate") or node.optional_string("endDate"),
			notes=node.optional_string("comments"),
		)
		return Desired(self.kind, node.id(), fields)


class ProductMapper:
	"""An item → an Item: its name, sales description, UPC, base price, weight, and (for inventory and
	assembly items) its quantity on hand across locations. Lot-numbered and serialized items are mirrored
	as plain stock items. An inactive item is disabled."""

	kind = Kind.PRODUCT
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("isInactive") is True:
			return None
		record = node.string("record_type")
		name = node.optional_string("displayName") or node.string("itemId")
		stock = record in STOCK_ITEMS
		price, warnings = base_price(node)
		weight = node.optional_decimal("weight")
		unit = WEIGHT_UNITS.get((ref_name(node, "weightUnit") or "").strip().lower())
		on_hand = node.optional_decimal("quantityOnHand")
		fields: Fields = {
			"type": "simple",
			"name": name,
			"slug": slugify(node.string("itemId")),
			"description": node.optional_string("salesDescription") or node.optional_string("description"),
			"short_description": None,
			"sku": node.string("itemId"),
			"barcode": node.optional_string("upcCode"),
			"brand": node.optional_string("manufacturer"),
			"collections": [],
			"images": [],
			"published": node.optional_flag("isOnline") is True,
			"is_stock_item": stock,
			"weight": {"value": weight, "unit": unit} if weight and unit else None,
			"price": price,
			"sale": None,
			"stock_qty": (on_hand or Decimal(0)) if stock else None,
			"attributes": [],
			"specifications": [],
			"variants": [],
			"tags": [],
		}
		return Desired(self.kind, node.id(), fields, warnings=warnings)


def base_price(item: Node) -> tuple[Decimal | None, tuple[str, ...]]:
	"""The item's base price (price level 1, for a quantity of one): None when it has none."""
	prices = [
		p
		for p in items_of(item, "price")
		if ref_id(p, "priceLevel") == BASE_PRICE_LEVEL and (p.optional_integer("priceQty") or 0) <= 1
	]
	if not prices:
		return None, ()
	currencies = {ref_id(p, "currencyPage") for p in prices}
	warnings = (
		("it has base prices in several currencies; the first is mirrored",) if len(currencies) > 1 else ()
	)
	return prices[0].optional_decimal("price"), warnings


# -- transactions ------------------------------------------------------------------------------------


class Transactions:
	"""What every transaction mapper shares: lines, taxes, references."""

	def __init__(self, context: Context) -> None:
		self._context = context

	def gid(self, type_: str, id_: str) -> str:
		return self._context.gid(type_, id_)

	def lines(self, node: Node, sign: Decimal) -> tuple[list[TradeLine], Decimal]:
		"""The item and expense lines that carry an amount, at ``sign`` (-1 for a return), and the discount
		off the net total: the discount lines' and the document's own (``discountTotal``), both negative in
		NetSuite."""
		currency = currency_of(node)
		found: list[TradeLine] = []
		discount = -money(node, "discountTotal")
		for index, line in enumerate(items_of(node, "item")):
			item_type = line.optional_string("item_type") or ""
			if item_type in UNBOOKED_LINES:
				continue
			amount = money(line, "amount")
			if item_type == DISCOUNT_LINE:
				discount -= amount
				continue
			if amount == 0 and item_type in CONSTRUCT_LINES:
				continue
			construct = item_type in CONSTRUCT_LINES
			qty = Decimal(1) if construct else (line.optional_decimal("quantity") or Decimal(1))
			product = None if construct else self._context.ref(line, "item", "Item")
			name = line.optional_string("description") or ref_name(line, "item") or "Line"
			key = f"item:{line.optional_integer('line') or index}"
			found.append(
				TradeLine(
					order_line(key, product, None, name, qty * sign, amount * sign, currency),
					account=self._context.ref(line, "account", "Account"),
					cost_center=self._context.ref(line, "department", "Department"),
					project=self._context.ref(line, "job", "Job"),
				)
			)
		for index, line in enumerate(items_of(node, "expense")):
			amount = money(line, "amount")
			if amount == 0:
				continue
			name = (
				line.optional_string("memo")
				or ref_name(line, "category")
				or ref_name(line, "account")
				or "Expense"
			)
			key = f"expense:{line.optional_integer('line') or index}"
			found.append(
				TradeLine(
					order_line(key, None, None, name, sign, amount * sign, currency),
					account=self._context.ref(line, "account", "Account"),
					cost_center=self._context.ref(line, "department", "Department"),
					project=self._context.ref(line, "customer", "Job")
					if ref_record(line, "customer") == "job"
					else None,
				)
			)
		return found, discount * sign

	def taxes(self, node: Node, sign: Decimal) -> list[TradeTax]:
		"""Each tax code's amount (SuiteTax's tax details), else the legacy tax totals; then shipping (as
		freight) and handling (as a fee)."""
		by_code: dict[str, Decimal] = {}
		for detail in items_of(node, "taxDetails"):
			code = ref_name(detail, "taxCode") or detail.optional_string("taxType") or "Tax"
			by_code[code] = by_code.get(code, Decimal(0)) + money(detail, "taxAmount")
		if not by_code:
			for field, label in (("taxTotal", "Tax"), ("tax2Total", "Tax 2")):
				if money(node, field):
					by_code[label] = money(node, field)
		found = [TradeTax(code, amount * sign) for code, amount in by_code.items() if amount]
		for field, label, role in CHARGES:
			if money(node, field):
				found.append(TradeTax(label, money(node, field) * sign, role=role))
		return found

	def document(self, node: Node, party_type: str, submitted: bool) -> Fields:
		"""A trade document. A return settles the one document it is applied to; a cash refund is paid out
		at once (its own payment), so it keeps its own balance."""
		record = node.string("record_type")
		is_return = record in RETURNS
		sign = Decimal(-1) if is_return else Decimal(1)
		lines, discount = self.lines(node, sign)
		applied = [line for line in items_of(node, "apply") if line.optional_flag("apply") is True]
		against = (
			self.gid("Transaction", doc)
			if is_return and len(applied) == 1 and (doc := ref_id(applied[0], "doc"))
			else None
		)
		number = node.optional_string("tranId") or node.string("source_id")
		document = TradeDocument(
			number=number,
			date=node.string("tranDate"),
			party=self._context.ref(node, "entity", party_type) or self._missing(node, "entity"),
			currency=currency_of(node),
			lines=lines,
			taxes=self.taxes(node, sign),
			total=money(node, "total") * sign,
			submitted=submitted,
			due_date=node.optional_string("dueDate"),
			exchange_rate=rate_of(node),
			is_return=is_return,
			return_against=against,
			# A vendor's bill number is the bill's own (tranId); a sale's is the customer's PO number.
			reference=node.optional_string("tranId")
			if record in PURCHASES
			else node.optional_string("otherRefNum"),
			remarks=node.optional_string("memo"),
			party_account=self._context.ref(node, "account", "Account")
			if record in PARTY_ACCOUNT_RECORDS
			else None,
			return_settles=record != "cashRefund",
			discount=discount or None,
		)
		return trade_fields(document)

	@staticmethod
	def _missing(node: Node, field: str) -> str:
		raise SourceError(f"NetSuite transaction {node.string('source_id')} has no {field}.")


def posted(node: Node) -> bool | None:
	"""Whether a posting transaction is on the books: None when it was voided (it is retired)."""
	if is_true(node, "voided"):
		return None
	return is_true(node, "posting")


class SalesInvoiceMapper(Transactions):
	"""An invoice or cash sale → a Sales Invoice; a credit memo or cash refund → a return Sales Invoice
	(against the one invoice it is applied to, when it is applied to exactly one). Posted when NetSuite
	posted it (not pending approval); a voided one is cancelled."""

	kind = Kind.SALES_INVOICE
	version = 1

	def map(self, node: Node) -> Desired | None:
		on_books = posted(node)
		if on_books is None:
			return None
		return Desired(self.kind, node.id(), self.document(node, "Customer", on_books))


class PurchaseInvoiceMapper(Transactions):
	"""A vendor bill → a Purchase Invoice (the vendor's number as its bill number); a vendor credit → a
	return Purchase Invoice."""

	kind = Kind.PURCHASE_INVOICE
	version = 1

	def map(self, node: Node) -> Desired | None:
		on_books = posted(node)
		if on_books is None:
			return None
		return Desired(self.kind, node.id(), self.document(node, "Vendor", on_books))


class SalesOrderMapper(Transactions):
	"""A sales order → a Sales Order: a draft while pending approval; a cancelled one is gone."""

	kind = Kind.SALES_ORDER
	version = 1

	def map(self, node: Node) -> Desired | None:
		status = enum_id(node, "status")
		if status == SALES_ORDER_CANCELLED:
			return None
		return Desired(self.kind, node.id(), self.document(node, "Customer", status != SALES_ORDER_DRAFT))


class PurchaseOrderMapper(Transactions):
	"""A purchase order → a Purchase Order: a draft while planned or pending supervisor approval; a rejected
	one is gone."""

	kind = Kind.PURCHASE_ORDER
	version = 1

	def map(self, node: Node) -> Desired | None:
		status = enum_id(node, "status")
		if status == PURCHASE_ORDER_REJECTED:
			return None
		return Desired(
			self.kind, node.id(), self.document(node, "Vendor", status not in PURCHASE_ORDER_DRAFTS)
		)


class EstimateMapper(Transactions):
	"""An estimate (quote) → a draft Quotation to its customer; a voided one is gone."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def map(self, node: Node) -> Desired | None:
		if enum_id(node, "status") == ESTIMATE_VOIDED:
			return None
		trade, discount = self.lines(node, Decimal(1))
		charges = [Charge(tax.role, tax.description, tax.amount) for tax in self.taxes(node, Decimal(1))]
		fields: Fields = {
			"number": node.optional_string("tranId") or node.string("source_id"),
			"currency": currency_of(node),
			"created_at": midday(node.string("tranDate")),
			"customer": self._context.ref(node, "entity", "Customer"),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": [line_fields(line.line) for line in trade],
			"discount": discount,
			"charges": [charge_fields(charge) for charge in charges],
			"total": money(node, "total"),
			"remarks": node.optional_string("memo") or node.optional_string("title"),
		}
		if rate_of(node) is not None:
			fields["exchange_rate"] = rate_of(node)
		return Desired(self.kind, node.id(), fields)


class OpportunityMapper:
	"""An opportunity → an Opportunity with its customer or lead (``entity_stage``, from the reader)."""

	kind = Kind.OPPORTUNITY
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		status = OPPORTUNITY_STATUSES.get(enum_id(node, "status") or "")
		if status is None:
			raise Unsupported(f"the opportunity status {enum_id(node, 'status')!r} has no ERPNext status")
		entity = self._context.ref(node, "entity", "Customer")
		if entity is None:
			raise SourceError(f"NetSuite opportunity {node.string('source_id')} has no entity.")
		stage = node.optional_string("entity_stage")
		opportunity = Opportunity(
			party_kind=Kind.CUSTOMER if stage == "Customer" else Kind.LEAD,
			party=entity,
			title=node.optional_string("title") or node.optional_string("tranId") or node.string("source_id"),
			status=status,
			date=node.string("tranDate"),
			currency=node.optional_string("currency_code"),
			amount=node.optional_decimal("projectedTotal"),
			probability=node.optional_decimal("probability"),
			expected_closing=node.optional_string("expectedCloseDate"),
			notes=node.optional_string("memo"),
		)
		return Desired(self.kind, node.id(), opportunity_fields(opportunity))


class PaymentMapper(Transactions):
	"""A customer payment, deposit or refund, or a vendor payment → a Payment Entry allocated to the
	invoices (or, for a refund, the credit memos) it applies to; a cash sale or cash refund is paid at once,
	so it is also its own payment. Credits applied inside a payment (a credit memo settling an invoice) have
	no Payment Entry allocation: such a payment is not mirrored."""

	kind = Kind.PAYMENT_ENTRY
	version = 1

	def map(self, node: Node) -> Desired | None:
		on_books = posted(node)
		if on_books is None:
			return None
		if not on_books:
			raise Unsupported("the payment is pending approval, and a Payment Entry is never a draft")
		record = node.string("record_type")
		if any(line.optional_flag("apply") is True for line in items_of(node, "credit")):
			raise Unsupported("the payment applies credits, which a Payment Entry can't allocate")
		number = node.optional_string("tranId") or node.string("source_id")
		if record in CASH_SALES:
			payment_type, party_type = (PAY if record == "cashRefund" else RECEIVE), CUSTOMER_PARTY
			field, kind = "entity", Kind.SALES_INVOICE
			amount = money(node, "total")
			allocations = [Allocation(kind, node.id(), amount)]
		else:
			vendor = record == "vendorPayment"
			payment_type = PAY if record in ("vendorPayment", "customerRefund") else RECEIVE
			party_type = SUPPLIER_PARTY if vendor else CUSTOMER_PARTY
			field = "entity" if vendor else "customer"
			kind = Kind.PURCHASE_INVOICE if vendor else Kind.SALES_INVOICE
			amount = (
				money(node, "payment")
				if record in ("customerPayment", "customerDeposit")
				else money(node, "total")
			)
			allocations = [
				Allocation(kind, self.gid("Transaction", doc), money(line, "amount"))
				for line in items_of(node, "apply")
				if line.optional_flag("apply") is True and (doc := ref_id(line, "doc"))
			]
		party_identity = self._context.ref(
			node, field, "Vendor" if party_type == SUPPLIER_PARTY else "Customer"
		)
		if party_identity is None:
			raise SourceError(f"NetSuite payment {node.string('source_id')} has no {field}.")
		payment = Payment(
			payment_type=payment_type,
			party_type=party_type,
			party=party_identity,
			date=node.string("tranDate"),
			amount=amount,
			currency=currency_of(node),
			allocations=allocations,
			exchange_rate=rate_of(node),
			account=self._context.ref(node, "account", "Account"),
			# The receivable or payable account it settles (a cash sale's is its own invoice's default).
			party_account=self._context.ref(
				node, "apAcct" if party_type == SUPPLIER_PARTY else "arAcct", "Account"
			),
			reference=number,
			remarks=node.optional_string("memo"),
		)
		return Desired(self.kind, node.id(), payment_fields(payment))


class JournalMapper(Transactions):
	"""A journal entry → a Journal Entry, line by line (each line's entity as its party); a check → a
	Journal Entry paying its expense lines out of its bank account. A journal in another currency than the
	company's, and a check with item lines or tax, have no exact Journal Entry: not mirrored."""

	kind = Kind.JOURNAL_ENTRY
	version = 1

	def map(self, node: Node) -> Desired | None:
		on_books = posted(node)
		if on_books is None:
			return None
		if rate_of(node) is not None:
			raise Unsupported("the journal is in another currency than the company's")
		number = node.optional_string("tranId") or node.string("source_id")
		if node.string("record_type") == "check":
			lines = self._check(node)
		else:
			lines = [self._line(line) for line in items_of(node, "line")]
		fields = journal_fields(
			number, node.string("tranDate"), lines, on_books, node.optional_string("memo")
		)
		return Desired(self.kind, node.id(), fields)

	def _line(self, line: Node) -> JournalLine:
		account = self._context.ref(line, "account", "Account")
		if account is None:
			raise SourceError("A NetSuite journal line has no account.")
		party_type, party_id = self._party(line, "entity")
		return JournalLine(
			account=account,
			debit=money(line, "debit"),
			credit=money(line, "credit"),
			party_type=party_type,
			party=party_id,
			cost_center=self._context.ref(line, "department", "Department"),
		)

	def _party(self, line: Node, field: str) -> tuple[str | None, str | None]:
		record = ref_record(line, field)
		entity = ref_id(line, field)
		if entity is None or record is None:
			return None, None
		party_type = ENTITY_PARTIES.get(record)
		if party_type is None:
			raise Unsupported(f"a journal line is for a {record}, which ERPNext has no party for")
		identity = {CUSTOMER_PARTY: "Customer", SUPPLIER_PARTY: "Vendor", EMPLOYEE_PARTY: "Employee"}[
			party_type
		]
		return party_type, self.gid(identity, entity)

	def _check(self, node: Node) -> list[JournalLine]:
		if items_of(node, "item") or money(node, "taxTotal") or money(node, "tax2Total"):
			raise Unsupported("the check has item lines or tax, which a Journal Entry can't book exactly")
		bank = self._context.ref(node, "account", "Account")
		if bank is None:
			raise SourceError(f"NetSuite check {node.string('source_id')} has no bank account.")
		party_type, party_id = self._party(node, "entity")
		expenses = [
			JournalLine(
				account=self._context.ref(line, "account", "Account") or bank,
				debit=money(line, "amount"),
				credit=Decimal(0),
				cost_center=self._context.ref(line, "department", "Department"),
			)
			for line in items_of(node, "expense")
		]
		total = sum((line.debit for line in expenses), Decimal(0))
		return [
			*expenses,
			JournalLine(bank, Decimal(0), total, party_type=party_type, party=party_id),
		]


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		AccountMapper(context),
		TreeMapper(Kind.COST_CENTER, context, "Department"),
		TreeMapper(Kind.DEPARTMENT, context, "Department"),
		TreeMapper(Kind.WAREHOUSE, context, "Location"),
		ProductMapper(),
		PartyMapper(Kind.CUSTOMER, context),
		PartyMapper(Kind.SUPPLIER, context),
		LeadMapper(),
		DesignationMapper(),
		EmployeeMapper(context),
		ProjectMapper(context),
		OpportunityMapper(context),
		EstimateMapper(context),
		SalesOrderMapper(context),
		PurchaseOrderMapper(context),
		SalesInvoiceMapper(context),
		PurchaseInvoiceMapper(context),
		PaymentMapper(context),
		JournalMapper(context),
	]
