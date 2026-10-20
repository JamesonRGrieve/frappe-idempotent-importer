# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure SAP Business One → desired ERPNext state, one mapper per kind, in the shared field contract. The
Service Layer's entities (shaped as its OData v3 ``$metadata`` defines them) are translated here: codes and
document entries become identities, ``tYES``/``tNO`` flags booleans, a document's lines, discount, tax,
freight and rounding its trade document (in its own currency: the ``FC`` amounts of a document in another
currency than the company's), and a payment's means and the invoices it settles a payment entry.

The chart's root types: SAP Business One types an account only as revenue, expense or other; an "other"
account's root type (asset, liability or equity) is its drawer's, which the Service Layer doesn't expose.
The connection's Field Map names it for each drawer (top-level account) code:
``{"root_types": {"1": "Asset", "2": "Liability", "3": "Equity"}}``; an account whose drawer it doesn't name
isn't mirrored (Unsupported, saying so), never guessed. The map may also name the price list items are
priced from (``{"price_list": 2}``; SAP Business One's first, ``1``, otherwise)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from idempotent_importer.contract import (
	CUSTOMER_PARTY,
	FEE,
	PAY,
	RECEIVE,
	ROOT_TYPES,
	SHIPPING,
	SUPPLIER_PARTY,
	TAX,
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
from idempotent_importer.sources.ledger import book_item, country_code, day, midday, required_day
from idempotent_importer.sources.sap_business_one.reader import key_text

YES, NO = "tYES", "tNO"
PLANNED = "planned: card 386"
DEFAULT_PRICE_LIST = 1
# The cost accounting dimension ERPNext's Cost Center stands for; the others are planned as Accounting
# Dimensions.
COST_CENTER_DIMENSION = 1
REVENUE, EXPENSE = "at_Revenues", "at_Expenses"
ACCOUNT_ROOTS = {REVENUE: ("Income", "Income Account"), EXPENSE: ("Expense", "Expense Account")}
# A Business Partner's currency "##" is "all currencies": none of its own.
ALL_CURRENCIES = "##"
ADDRESS_TYPES = {"bo_BillTo": "Billing", "bo_ShipTo": "Shipping"}
GENDERS = {"gt_Male": "Male", "gt_Female": "Female"}
OPPORTUNITY_STATUSES = {"sos_Open": "Open", "sos_Sold": "Converted", "sos_Missed": "Lost"}
CANCELLATION = "csCancellation"
# The documents that return goods (negative quantities), and the object type of the one their lines are
# based on (BaseType: 13 an A/R invoice, 18 an A/P invoice).
RETURNS = {"CreditNote": (13, "Invoice"), "PurchaseCreditNote": (18, "PurchaseInvoice")}
SUPPLIER_DOCUMENTS = frozenset({"PurchaseOrder", "PurchaseInvoice", "PurchaseCreditNote"})
# What a payment line settles, by its invoice type: the kind and identity type of the document.
SETTLED = {
	"it_Invoice": (Kind.SALES_INVOICE, "Invoice"),
	"it_PurchaseInvoice": (Kind.PURCHASE_INVOICE, "PurchaseInvoice"),
}
CREDITS = frozenset({"it_CredItnote", "it_PurchaseCreditNote"})
PAYMENT_PARTIES = {"rCustomer": CUSTOMER_PARTY, "rSupplier": SUPPLIER_PARTY}
CARD_PARTIES = {"cCustomer": CUSTOMER_PARTY, "cSupplier": SUPPLIER_PARTY}


@dataclass(frozen=True)
class FieldMap:
	"""What the connection's Field Map says of this company: each drawer's root type, and the price list."""

	root_types: dict[str, str] = field(default_factory=dict)
	price_list: int = DEFAULT_PRICE_LIST

	@classmethod
	def of(cls, text: str) -> FieldMap:
		if not text.strip():
			return cls()
		try:
			raw = json.loads(text)
		except ValueError:
			raise SourceError("The SAP Business One Field Map isn't JSON.") from None
		roots = raw.get("root_types", {}) if isinstance(raw, dict) else None
		price_list = raw.get("price_list", DEFAULT_PRICE_LIST) if isinstance(raw, dict) else None
		if not isinstance(roots, dict) or not isinstance(price_list, int) or isinstance(price_list, bool):
			raise SourceError(
				'The SAP Business One Field Map must be {"root_types": {"<drawer code>": "<root type>"}, '
				'"price_list": <number>}.'
			)
		for code, root in roots.items():
			if root not in ROOT_TYPES:
				raise SourceError(f"The Field Map's root type for {code} isn't one of {sorted(ROOT_TYPES)}.")
		return cls({str(code): str(root) for code, root in roots.items()}, price_list)


class Company:
	"""The connection's identities of the Service Layer's keys."""

	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config
		self.map = FieldMap.of(config.field_map)

	def of(self, type_: str, key: object) -> str | None:
		"""The identity of a key (a code or a number), or None when it is absent (empty, or 0/-1 for none)."""
		if key is None or key == "" or (isinstance(key, int) and key <= 0):
			return None
		return self.config.identity(type_, key_text(key if isinstance(key, int) else str(key)))


def flag(node: Node, name: str) -> bool:
	return node.optional_string(name) == YES


def amount(node: Node, name: str) -> Decimal:
	return node.optional_decimal(name) or Decimal(0)


def mappers(config: ConnectionConfig) -> list[Mapper]:
	company = Company(config)
	return [
		AccountMapper(company),
		CostCenterMapper(company),
		WarehouseMapper(company),
		ItemGroupMapper(company),
		ItemMapper(company),
		PartyMapper(Kind.CUSTOMER, company),
		PartyMapper(Kind.SUPPLIER, company),
		DepartmentMapper(company),
		PositionMapper(company),
		EmployeeMapper(company),
		ProjectMapper(company),
		LeadMapper(company),
		OpportunityMapper(company),
		QuotationMapper(company),
		DocumentMapper(Kind.SALES_ORDER, company),
		DocumentMapper(Kind.PURCHASE_ORDER, company),
		DocumentMapper(Kind.SALES_INVOICE, company),
		DocumentMapper(Kind.PURCHASE_INVOICE, company),
		PaymentMapper(company),
		JournalMapper(company),
	]


# -- the books -------------------------------------------------------------------------------------------


class AccountMapper:
	"""An account of the chart under its father: a title account (not active for postings) is a group; a
	frozen or invalid one disabled (it stays, for the history booked to it)."""

	kind = Kind.ACCOUNT
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		account_type = node.optional_string("AccountType") or ""
		drawer = node.string("drawer")
		root = self.company.map.root_types.get(drawer)
		erp_type = None
		if account_type in ACCOUNT_ROOTS:
			typed_root, erp_type = ACCOUNT_ROOTS[account_type]
			root = root or typed_root
		if root is None:
			raise Unsupported(
				f"its drawer {drawer}'s root type isn't in the Field Map (root_types), and SAP Business One "
				"doesn't say whether it is an asset, a liability or equity"
			)
		group = node.optional_string("ActiveAccount") == NO
		if flag(node, "CashAccount") and not group:
			erp_type = "Cash"
		account = LedgerAccount(
			number=node.optional_string("FormatCode") or node.string("Code"),
			name=node.string("Name"),
			root_type=root,
			account_type=erp_type,
			parent=self.company.of("Account", node.optional_string("FatherAccountKey")),
			is_group=group,
			currency=None
			if node.optional_string("AcctCurrency") in (None, ALL_CURRENCIES)
			else node.optional_string("AcctCurrency"),
		)
		fields = account_fields(account)
		if flag(node, "FrozenFor") or node.optional_string("ValidFor") == NO:
			fields |= {"disabled": True}
		return Desired(self.kind, node.id(), fields)


class CostCenterMapper:
	"""A profit center of the first dimension as a Cost Center; an inactive one disabled."""

	kind = Kind.COST_CENTER
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		dimension = node.optional_integer("InWhichDimension") or COST_CENTER_DIMENSION
		if dimension != COST_CENTER_DIMENSION:
			raise Unsupported(f"a dimension {dimension} distribution is an Accounting Dimension ({PLANNED})")
		name = node.optional_string("CenterName") or node.string("CenterCode")
		disabled = node.optional_string("Active") == NO
		return Desired(self.kind, node.id(), tree_fields(name, None, disabled=disabled))


def address_of(node: Node, kind: str, name: str | None, street: str = "Street") -> Fields | None:
	line1 = " ".join(p for p in (node.text("StreetNo"), node.text(street)) if p)
	if not line1 or not node.text("City"):
		return None
	return address_fields(
		Address(
			address_type=kind,
			name=name,
			company=None,
			line1=line1,
			line2=" ".join(p for p in (node.text("Block"), node.text("BuildingFloorRoom")) if p) or None,
			city=node.string("City"),
			state=node.optional_string("State"),
			postcode=node.optional_string("ZipCode"),
			country_code=country_code(node.optional_string("Country")),
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
		name = node.optional_string("WarehouseName") or node.string("WarehouseCode")
		fields = warehouse_fields(name, None, False, address_of(node, "Warehouse", name))
		return Desired(self.kind, node.id(), fields | ({"disabled": True} if flag(node, "Inactive") else {}))


class ItemGroupMapper:
	kind = Kind.COLLECTION
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		name = node.string("GroupName")
		fields: Fields = {
			"name": name,
			"slug": slugify(name),
			"parent": None,
			"description": None,
			"image": None,
		}
		return Desired(self.kind, node.id(), fields)


class ItemMapper:
	"""An item in its group: name, code, remarks, bar code, the price list's price, and the stock on hand
	across warehouses for an inventory item. An invalid or frozen item is retired (disabled)."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		if node.optional_string("Valid") == NO or flag(node, "Frozen"):
			return None
		stock = flag(node, "InventoryItem")
		prices = [
			price
			for price in node.children("ItemPrices")
			if price.optional_integer("PriceList") == self.company.map.price_list
		]
		group = self.company.of("ItemGroup", node.optional_integer("ItemsGroupCode"))
		fields = book_item(
			name=node.optional_string("ItemName") or node.string("ItemCode"),
			sku=node.string("ItemCode"),
			description=node.optional_string("User_Text"),
			price=prices[0].optional_decimal("Price") if prices else None,
			is_stock_item=stock,
			stock_qty=amount(node, "QuantityOnStock") if stock else None,
			collections=[group] if group else [],
		)
		fields["barcode"] = node.optional_string("BarCode")
		return Desired(self.kind, node.id(), fields)


# -- parties ---------------------------------------------------------------------------------------------


def addresses(node: Node) -> list[Fields]:
	found: list[Fields] = []
	for entry in node.children("BPAddresses"):
		kind = ADDRESS_TYPES.get(entry.optional_string("AddressType") or "", "Other")
		address = address_of(
			entry, kind, entry.optional_string("AddressName2") or entry.optional_string("AddressName")
		)
		if address is not None:
			found.append(address)
	return found


def contacts(node: Node, company: Company) -> list[ContactPerson]:
	found: list[ContactPerson] = []
	for person in node.children("ContactEmployees"):
		name = " ".join(
			p for p in (person.text("FirstName"), person.text("LastName")) if p
		) or person.optional_string("Name")
		code = person.optional_integer("InternalCode")
		if not name or code is None or person.optional_string("Active") == NO:
			continue
		found.append(
			ContactPerson(
				gid=company.config.identity("ContactEmployee", code),
				name=name,
				email=person.optional_string("E_Mail"),
				phone=person.optional_string("Phone1") or person.optional_string("MobilePhone"),
				job_title=person.optional_string("Position"),
			)
		)
	return found


def blocked(node: Node) -> bool:
	return node.optional_string("Valid") == NO or flag(node, "Frozen")


class PartyMapper:
	"""A business partner as a Customer or a Supplier: its name, company or private, email and phone,
	bill-to and ship-to addresses, federal tax id, currency and contact persons; an invalid or frozen one
	stays, disabled, for the documents that name it."""

	def __init__(self, kind: Kind, company: Company) -> None:
		self.kind = kind
		self.version = 1
		self.company = company

	def map(self, node: Node) -> Desired | None:
		currency = node.optional_string("Currency")
		party = Party(
			name=node.string("CardName"),
			type="Individual" if node.optional_string("CompanyPrivate") == "cPrivate" else "Company",
			company=node.string("CardName") if node.optional_string("CompanyPrivate") != "cPrivate" else None,
			email=node.optional_string("EmailAddress"),
			phone=node.optional_string("Phone1") or node.optional_string("Cellular"),
			addresses=addresses(node),
			tax_id=node.optional_string("FederalTaxID"),
			currency=None if currency in (None, ALL_CURRENCIES) else currency,
			contacts=contacts(node, self.company),
			disabled=blocked(node),
		)
		return Desired(self.kind, node.id(), party_fields(party))


class LeadMapper:
	"""A business partner of type lead as a Lead."""

	kind = Kind.LEAD
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		private = node.optional_string("CompanyPrivate") == "cPrivate"
		name = node.string("CardName")
		first, _, last = name.partition(" ")
		lead = Lead(
			status="Lead",
			first_name=first if private else None,
			last_name=(last or None) if private else None,
			company=None if private else name,
			email=node.optional_string("EmailAddress"),
			phone=node.optional_string("Phone1"),
			mobile=node.optional_string("Cellular"),
			website=node.optional_string("Website"),
			notes=node.optional_string("Notes") or node.optional_string("FreeText"),
			disabled=blocked(node),
		)
		return Desired(self.kind, node.id(), lead_fields(lead))


# -- people ----------------------------------------------------------------------------------------------


class DepartmentMapper:
	kind = Kind.DEPARTMENT
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), tree_fields(node.string("Name"), None))


class PositionMapper:
	kind = Kind.DESIGNATION
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), {"name": node.string("Name")})


class EmployeeMapper:
	"""An employee in their department and position, with their manager: Left when inactive with a
	termination date, Inactive otherwise."""

	kind = Kind.EMPLOYEE
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		terminated = day(node.optional_string("TerminationDate"))
		active = node.optional_string("Active") != NO
		employee = Employee(
			first_name=node.optional_string("FirstName") or node.string("LastName"),
			middle_name=node.optional_string("MiddleName"),
			last_name=node.optional_string("LastName") if node.optional_string("FirstName") else None,
			gender=GENDERS.get(node.optional_string("Gender") or ""),
			date_of_birth=day(node.optional_string("DateOfBirth")),
			date_of_joining=day(node.optional_string("StartDate")),
			relieving_date=terminated,
			status="Active" if active else ("Left" if terminated else "Inactive"),
			employee_number=node.optional_string("ExternalEmployeeNumber")
			or node.optional_string("EmployeeCode"),
			department=self.company.of("Department", node.optional_integer("Department")),
			designation=self.company.of("Position", node.optional_integer("Position")),
			reports_to=self.company.of("Employee", node.optional_integer("Manager")),
			company_email=node.optional_string("eMail"),
			phone=node.optional_string("MobilePhone") or node.optional_string("OfficePhone"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class ProjectMapper:
	kind = Kind.PROJECT
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		fields = project_fields(
			name=node.optional_string("Name") or node.string("Code"),
			status="Open" if node.optional_string("Active") != NO else "Completed",
			start=day(node.optional_string("ValidFrom")),
			end=day(node.optional_string("ValidTo")),
		)
		return Desired(self.kind, node.id(), fields)


class OpportunityMapper:
	"""A sales opportunity with its lead or customer (``card_type``, from the reader), in the company's
	currency."""

	kind = Kind.OPPORTUNITY
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		card_type = node.optional_string("card_type")
		if card_type not in ("cLid", "cCustomer"):
			raise Unsupported(f"an opportunity with a {card_type or 'missing'} business partner")
		party = self.company.of("BusinessPartner", node.optional_string("CardCode"))
		status = OPPORTUNITY_STATUSES.get(node.optional_string("Status") or "")
		if party is None or status is None:
			raise Unsupported(f"an opportunity without a partner or a status ERPNext knows ({PLANNED})")
		opportunity = Opportunity(
			party_kind=Kind.LEAD if card_type == "cLid" else Kind.CUSTOMER,
			party=party,
			title=node.optional_string("OpportunityName") or f"Opportunity {node.string('source_key')}",
			status=status,
			date=required_day(node.optional_string("StartDate"), node.path),
			amount=node.optional_decimal("MaxLocalTotal"),
			probability=node.optional_decimal("ClosingPercentage"),
			expected_closing=day(node.optional_string("PredictedClosingDate")),
			notes=node.optional_string("Remarks"),
		)
		return Desired(self.kind, node.id(), opportunity_fields(opportunity))


# -- documents -------------------------------------------------------------------------------------------


def identity_type(node: Node) -> str:
	return node.id().rsplit("/", 2)[-2]


@dataclass(frozen=True)
class Amounts:
	"""Which of a document's amounts are in its own currency: the ``FC`` ones for a foreign document."""

	foreign: bool

	def of(self, node: Node, local: str, foreign: str) -> Decimal:
		return amount(node, foreign if self.foreign else local)


def foreign(node: Node) -> bool:
	rate = node.optional_decimal("DocRate")
	return rate is not None and rate != 1


def gone(node: Node) -> bool:
	"""A cancelled document, or the cancellation that reverses one: neither is booked."""
	return flag(node, "Cancelled") or node.optional_string("CancelStatus") == CANCELLATION


class Documents:
	"""What every document mapper shares: lines, taxes, freight and rounding, in the document's currency."""

	def __init__(self, company: Company) -> None:
		self.company = company

	def lines(self, node: Node, sign: Decimal) -> list[TradeLine]:
		money = Amounts(foreign(node))
		currency = node.string("DocCurrency")
		found: list[TradeLine] = []
		for line in node.children("DocumentLines"):
			total = money.of(line, "LineTotal", "RowTotalFC")
			item = line.optional_string("ItemCode")
			qty = (line.optional_decimal("Quantity") or Decimal(1)) if item else Decimal(1)
			name = line.optional_string("ItemDescription") or item or "Line"
			rate = exact_quotient(total, qty, minor_units(currency)) if qty else None
			if rate is None:
				raise Unsupported(f'line "{name}" ({total} for {qty}) has no exact unit price in {currency}')
			found.append(
				TradeLine(
					OrderLine(
						f"line:{line.optional_integer('LineNum') or 0}",
						self.company.of("Item", item),
						None,
						name,
						qty * sign,
						rate,
					),
					account=self.company.of("Account", line.optional_string("AccountCode")),
					cost_center=self.company.of("ProfitCenter", line.optional_string("CostingCode")),
					project=self.company.of(
						"Project", line.optional_string("ProjectCode") or node.optional_string("Project")
					),
				)
			)
		return found

	def taxes(self, node: Node, sign: Decimal, sale: bool) -> list[TradeTax]:
		"""Each tax code's tax (the lines' and freight's, when they make the document's VAT), else the VAT
		total; freight as shipping, and a sale's rounding as a fee."""
		money = Amounts(foreign(node))
		by_code: dict[str, Decimal] = {}
		for line in node.children("DocumentLines"):
			code = line.optional_string("TaxCode") or line.optional_string("VatGroup") or "Tax"
			by_code[code] = by_code.get(code, Decimal(0)) + money.of(line, "TaxTotal", "NetTaxAmountFC")
		expenses = node.children("DocumentAdditionalExpenses")
		for expense in expenses:
			code = expense.optional_string("TaxCode") or expense.optional_string("VatGroup") or "Tax"
			by_code[code] = by_code.get(code, Decimal(0)) + money.of(expense, "TaxSum", "TaxSumFC")
		vat = money.of(node, "VatSum", "VatSumFc")
		if sum(by_code.values(), Decimal(0)) != vat:
			by_code = {"Tax": vat}
		found = [TradeTax(code, value * sign) for code, value in sorted(by_code.items()) if value]
		for expense in expenses:
			value = money.of(expense, "LineTotal", "LineTotalFC")
			if value:
				label = (
					expense.optional_string("Remarks") or f"Freight {expense.optional_integer('ExpenseCode')}"
				)
				found.append(TradeTax(label, value * sign, role=SHIPPING if sale else TAX))
		rounding = money.of(node, "RoundingDiffAmount", "RoundingDiffAmountFC")
		if rounding:
			if not sale:
				raise Unsupported(f"a purchase document's rounding ({PLANNED})")
			found.append(TradeTax("Rounding", rounding * sign, role=FEE))
		return found

	def document(self, node: Node) -> TradeDocument:
		type_ = identity_type(node)
		returned = RETURNS.get(type_)
		sign = Decimal(-1) if returned else Decimal(1)
		money = Amounts(foreign(node))
		if money.of(node, "DownPaymentAmount", "DownPaymentAmountFC"):
			raise Unsupported(f"a document drawing a down payment ({PLANNED})")
		against = None
		if returned is not None:
			bases = {
				line.optional_integer("BaseEntry")
				for line in node.children("DocumentLines")
				if line.optional_integer("BaseType") == returned[0]
			}
			if len(bases) == 1 and None not in bases:
				against = self.company.of(returned[1], bases.pop())
		supplier = type_ in SUPPLIER_DOCUMENTS
		party = self.company.of("BusinessPartner", node.optional_string("CardCode"))
		if party is None:
			raise SourceError(
				f"SAP Business One document {node.string('source_key')} has no business partner."
			)
		return TradeDocument(
			number=str(node.integer("DocNum")),
			date=required_day(node.optional_string("DocDate"), node.path),
			party=party,
			currency=node.string("DocCurrency"),
			lines=self.lines(node, sign),
			taxes=self.taxes(node, sign, not supplier),
			total=money.of(node, "DocTotal", "DocTotalFc") * sign,
			submitted=True,
			due_date=day(node.optional_string("DocDueDate")),
			exchange_rate=node.optional_decimal("DocRate") if money.foreign else None,
			is_return=returned is not None,
			return_against=against,
			reference=node.optional_string("NumAtCard"),
			remarks=node.optional_string("Comments"),
			base_total=amount(node, "DocTotal") * sign if money.foreign else None,
			discount=(money.of(node, "TotalDiscount", "TotalDiscountFC") * sign) or None,
		)


class DocumentMapper(Documents):
	"""An order, a purchase order, an A/R or A/P invoice or credit note (credit notes as returns, against
	the one invoice their lines are based on); a cancelled one, and its cancellation, are gone."""

	def __init__(self, kind: Kind, company: Company) -> None:
		super().__init__(company)
		self.kind = kind
		self.version = 1

	def map(self, node: Node) -> Desired | None:
		if gone(node):
			return None
		return Desired(self.kind, node.id(), trade_fields(self.document(node)))


class QuotationMapper(Documents):
	"""A sales quotation as a draft Quotation, dated its document date; a cancelled one is gone."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def map(self, node: Node) -> Desired | None:
		if gone(node):
			return None
		document = self.document(node)
		trade_fields(document)
		fields: Fields = {
			"number": document.number,
			"currency": document.currency,
			"created_at": midday(document.date),
			"customer": document.party,
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": list[Field](line_fields(trade.line) for trade in document.lines),
			"discount": document.discount or Decimal(0),
			"charges": [
				charge_fields(Charge(tax.role, tax.description, tax.amount)) for tax in document.taxes
			],
			"total": document.total,
			"remarks": document.remarks,
		}
		if document.exchange_rate is not None:
			fields["exchange_rate"] = document.exchange_rate
		return Desired(self.kind, node.id(), fields)


class PaymentMapper:
	"""An incoming or outgoing payment to or from a customer or supplier, by one means (cash, a transfer,
	cheques or a card), allocated to the invoices it settles."""

	kind = Kind.PAYMENT_ENTRY
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		if gone(node):
			return None
		party_type = PAYMENT_PARTIES.get(node.optional_string("DocType") or "")
		if party_type is None:
			raise Unsupported(f"a payment on account, to no business partner ({PLANNED})")
		if foreign(node):
			raise Unsupported(f"a payment in another currency than the company's ({PLANNED})")
		incoming = identity_type(node) == "IncomingPayment"
		means = {
			"CashAccount": amount(node, "CashSum"),
			"TransferAccount": amount(node, "TransferSum"),
			"CheckAccount": sum((amount(c, "CheckSum") for c in node.children("PaymentChecks")), Decimal(0)),
			"CreditAcct": sum(
				(amount(c, "CreditSum") for c in node.children("PaymentCreditCards")), Decimal(0)
			),
		}
		used = [name for name, value in means.items() if value]
		if len(used) != 1:
			raise Unsupported(f"a payment by {len(used)} means at once ({PLANNED})")
		account = self._account(node, used[0])
		allocations: list[Allocation] = []
		for line in node.children("PaymentInvoices"):
			invoice_type = line.optional_string("InvoiceType") or ""
			if invoice_type in CREDITS:
				raise Unsupported(f"the payment applies a credit note ({PLANNED})")
			settled = SETTLED.get(invoice_type)
			if settled is None:
				raise Unsupported(f"the payment settles a {invoice_type} ({PLANNED})")
			document = self.company.of(settled[1], line.optional_integer("DocEntry"))
			if document is not None:
				allocations.append(Allocation(settled[0], document, amount(line, "SumApplied")))
		party = self.company.of("BusinessPartner", node.optional_string("CardCode"))
		if party is None:
			raise SourceError(
				f"SAP Business One payment {node.string('source_key')} has no business partner."
			)
		payment = Payment(
			payment_type=RECEIVE if incoming else PAY,
			party_type=party_type,
			party=party,
			date=required_day(node.optional_string("DocDate"), node.path),
			amount=means[used[0]],
			currency=node.string("DocCurrency"),
			allocations=allocations,
			account=account,
			reference=node.optional_string("CounterReference") or str(node.integer("DocNum")),
			remarks=node.optional_string("Remarks"),
		)
		return Desired(self.kind, node.id(), payment_fields(payment))

	def _account(self, node: Node, means: str) -> str | None:
		if means == "CheckAccount":
			accounts = {c.optional_string("CheckAccount") for c in node.children("PaymentChecks")}
			return self.company.of("Account", accounts.pop()) if len(accounts) == 1 else None
		if means == "CreditAcct":
			accounts = {c.optional_string("CreditAcct") for c in node.children("PaymentCreditCards")}
			return self.company.of("Account", accounts.pop()) if len(accounts) == 1 else None
		return self.company.of("Account", node.optional_string(means))


class JournalMapper:
	"""A manual journal entry (the reader leaves out every document's own posting), line by line in the
	company's currency, each business partner line's customer or supplier (``card_type``, from the
	reader), its first-dimension distribution and project."""

	kind = Kind.JOURNAL_ENTRY
	version = 1

	def __init__(self, company: Company) -> None:
		self.company = company

	def map(self, node: Node) -> Desired | None:
		lines: list[JournalLine] = []
		for line in node.children("JournalEntryLines"):
			account = self.company.of("Account", line.optional_string("AccountCode"))
			if account is None:
				raise SourceError(
					f"A SAP Business One journal line of {node.string('source_key')} has no account."
				)
			card_type = line.optional_string("card_type")
			party_type = CARD_PARTIES.get(card_type or "")
			if card_type is not None and party_type is None:
				raise Unsupported(f"a journal line for a {card_type} business partner")
			lines.append(
				JournalLine(
					account=account,
					debit=amount(line, "Debit"),
					credit=amount(line, "Credit"),
					party_type=party_type,
					party=self.company.of("BusinessPartner", line.optional_string("ShortName"))
					if party_type
					else None,
					cost_center=self.company.of("ProfitCenter", line.optional_string("CostingCode")),
					project=self.company.of("Project", line.optional_string("ProjectCode")),
				)
			)
		number = str(node.optional_integer("Number") or node.string("source_key"))
		fields = journal_fields(
			number,
			required_day(node.optional_string("ReferenceDate"), node.path),
			lines,
			True,
			node.optional_string("Memo"),
		)
		return Desired(self.kind, node.id(), fields)
