# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Zoho CRM → desired ERPNext state: users as Employees, products as Items, vendors as Suppliers, leads as
Leads, accounts as Prospects with their contacts (the customer accounts, and those sales orders and invoices
are for, as Customers too), deal pipelines as Opportunity Types and deals as Opportunities at their stage,
calls and notes as Communications, events as Events, tasks as ToDos, quotes as draft Quotations, sales orders
and purchase orders as Sales and Purchase Orders, invoices as Sales Invoices. Every record keeps its custom
fields' values, by label.

Field meanings are Zoho's (www.zoho.com/crm/developer/docs/api/v8, and the v8 OpenAPI documents in
zoho/crm-oas). An activity is about the record it is on (``What_Id`` and ``$se_module``: a deal, an account or
a lead), else its contact's account. A quote's or order's lines are at list price, quantity times list price;
its discounts are its lines' and its own, its taxes its lines' and its own, its adjustment a charge (a
negative one a further discount), which must make its grand total exactly. A deal is won at a stage named
"Closed Won" or at 100% probability, lost at any other "Closed" stage. What ERPNext can't hold exactly is
Unsupported, naming the card that plans it."""

from __future__ import annotations

from decimal import Decimal

from idempotent_importer.contract import (
	FEE,
	TAX,
	Address,
	Charge,
	Communication,
	ContactPerson,
	Employee,
	Event,
	Lead,
	Opportunity,
	OrderLine,
	Party,
	Prospect,
	ToDo,
	TradeDocument,
	TradeLine,
	TradeTax,
	address_fields,
	charge_fields,
	communication_fields,
	employee_fields,
	event_fields,
	lead_fields,
	line_fields,
	opportunity_fields,
	opportunity_type_fields,
	order_line,
	party_fields,
	prospect_fields,
	quoted_party,
	todo_fields,
	trade_fields,
	with_custom,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_text
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.crm import custom_of, plain_text
from idempotent_importer.sources.ledger import book_item, country_code, day, midday, required_day
from idempotent_importer.sources.zoho_crm.reader import lookup_id

PLANNED = "planned: card 386"
HUNDRED = Decimal(100)
MODULE_KINDS = {
	"Deals": (Kind.OPPORTUNITY, "Deals"),
	"Accounts": (Kind.PROSPECT, "Accounts"),
	"Leads": (Kind.LEAD, "Leads"),
}
OUTBOUND = "Outbound"
CLOSED_TASKS = frozenset({"Completed"})
CANCELLED = frozenset({"Cancelled"})
LOST_QUOTES = frozenset({"Closed Lost"})
DRAFT_INVOICES = frozenset({"Created"})
DRAFT_ORDERS = frozenset({"Created"})
SUBJECT_LENGTH = 140
LEAD_STATUSES = {
	"Not Contacted": "Open",
	"Attempted to Contact": "Open",
	"Contact in Future": "Interested",
	"Contacted": "Replied",
	"Pre-Qualified": "Opportunity",
	"Not Qualified": "Lead",
	"Junk Lead": "Lead",
	"Lost Lead": "Lead",
}
CLOSED_LEADS = frozenset({"Not Qualified", "Junk Lead", "Lost Lead"})


class Context:
	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config

	def gid(self, type_: str, id_: str) -> str:
		return self.config.identity(type_, id_)

	def about(self, node: Node, what: str) -> tuple[Kind, str]:
		"""The record an activity is on, else its contact's account (a prospect)."""
		found = self.parties(node)
		if not found:
			raise Unsupported(f"{what} is on no deal, account, lead or contact with an account")
		return found[0]

	def parties(self, node: Node) -> list[tuple[Kind, str]]:
		found: list[tuple[Kind, str]] = []
		what = lookup_id(node, "What_Id")
		module = node.optional_string("$se_module")
		parent = node.child("Parent_Id")
		if parent is not None:
			of = parent.child("module")
			module, what = (of.optional_string("api_name") if of else None), parent.optional_string("id")
		if what and module in MODULE_KINDS:
			kind, type_ = MODULE_KINDS[module]
			found.append((kind, self.gid(type_, what)))
		account = node.optional_string("contact_account")
		if account and (Kind.PROSPECT, self.gid("Accounts", account)) not in found:
			found.append((Kind.PROSPECT, self.gid("Accounts", account)))
		return found


def label_of(node: Node, field: str) -> str | None:
	labels = node.child("labels")
	return (labels.optional_string(field) if labels else None) or node.optional_string(field)


def currency_of(node: Node) -> str:
	return node.optional_string("Currency") or node.string("org_currency")


def address_of(
	node: Node, fields: tuple[str, str, str, str, str], name: str, company: str | None
) -> Fields | None:
	street, city, state, code, country = (node.optional_string(field) for field in fields)
	if not (street and city):
		return None
	return address_fields(
		Address("Billing", name, company, street, None, city, state, code, country_code(country), None, None)
	)


ADDRESS = ("Street", "City", "State", "Zip_Code", "Country")
BILLING = ("Billing_Street", "Billing_City", "Billing_State", "Billing_Code", "Billing_Country")


def person_of(context: Context, contact: Node) -> ContactPerson:
	first, last = contact.optional_string("First_Name"), contact.optional_string("Last_Name")
	return ContactPerson(
		gid=context.gid("Contacts", contact.string("id")),
		name=" ".join(part for part in (first, last) if part)
		or contact.optional_string("Email")
		or contact.string("id"),
		email=contact.optional_string("Email"),
		phone=contact.optional_string("Phone") or contact.optional_string("Mobile"),
		job_title=contact.optional_string("Title"),
	)


class UserMapper:
	"""A user: names, email, phone and manager; an active user is active."""

	kind = Kind.EMPLOYEE
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		first = node.optional_string("first_name") or node.optional_string("full_name")
		if first is None:
			raise Unsupported("a user with no name")
		manager = lookup_id(node, "Reporting_To")
		employee = Employee(
			first_name=first,
			status="Active" if node.optional_string("status") == "active" else "Inactive",
			last_name=node.optional_string("last_name") if node.optional_string("first_name") else None,
			reports_to=self.context.gid("User", manager) if manager else None,
			company_email=node.optional_string("email"),
			phone=node.optional_string("phone") or node.optional_string("mobile"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class ProductMapper:
	"""An active product at its unit price; an inactive one isn't kept."""

	kind = Kind.PRODUCT
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("Product_Active") is False:
			return None
		fields = book_item(
			node.string("Product_Name"),
			node.optional_string("Product_Code"),
			node.optional_string("Description"),
			node.optional_decimal("Unit_Price"),
			node.has("Qty_in_Stock"),
			node.optional_decimal("Qty_in_Stock"),
		)
		return Desired(self.kind, node.id(), with_custom(fields, custom_of(node)))


class VendorMapper:
	kind = Kind.SUPPLIER
	version = 1

	def map(self, node: Node) -> Desired | None:
		name = node.string("Vendor_Name")
		address = address_of(node, ADDRESS, name, name)
		party = Party(
			name=name,
			type="Company",
			company=name,
			email=node.optional_string("Email"),
			phone=node.optional_string("Phone"),
			addresses=[address] if address else [],
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), party_fields(party))


class CustomerMapper:
	"""A customer account (or one a sales order or invoice is for) as a company with its contacts."""

	kind = Kind.CUSTOMER
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		name = node.string("Account_Name")
		address = address_of(node, BILLING, name, name)
		party = Party(
			name=name,
			type="Company",
			company=name,
			phone=node.optional_string("Phone"),
			addresses=[address] if address else [],
			contacts=[person_of(self.context, contact) for contact in node.children("contacts")],
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), party_fields(party))


class LeadMapper:
	"""A lead: names, company, how to reach them, designation, website, description; its status as ERPNext's
	(not contacted and attempted Open, contacted Replied, pre-qualified Opportunity, contact in future
	Interested); a junk, lost or unqualified lead is closed (disabled)."""

	kind = Kind.LEAD
	version = 1

	def map(self, node: Node) -> Desired | None:
		status = node.optional_string("Lead_Status")
		lead = Lead(
			status=LEAD_STATUSES.get(status or "", "Lead"),
			first_name=node.optional_string("First_Name"),
			last_name=node.optional_string("Last_Name"),
			company=node.optional_string("Company"),
			email=node.optional_string("Email"),
			phone=node.optional_string("Phone"),
			mobile=node.optional_string("Mobile"),
			job_title=node.optional_string("Designation"),
			website=node.optional_string("Website"),
			notes=node.optional_string("Description"),
			disabled=status in CLOSED_LEADS,
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), lead_fields(lead))


class AccountMapper:
	"""An account as an organisation of the pipeline with its contacts, address, website and description."""

	kind = Kind.PROSPECT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		name = node.string("Account_Name")
		prospect = Prospect(
			name=name,
			website=node.optional_string("Website"),
			phone=node.optional_string("Phone"),
			address=address_of(node, BILLING, name, name),
			notes=node.optional_string("Description"),
			contacts=[person_of(self.context, contact) for contact in node.children("contacts")],
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), prospect_fields(prospect))


class PipelineMapper:
	kind = Kind.OPPORTUNITY_TYPE
	version = 1

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), opportunity_type_fields(node.string("label")))


class DealMapper:
	"""A deal with its account (a prospect), in its pipeline at its stage, with its amount, probability,
	closing date and description."""

	kind = Kind.OPPORTUNITY
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		account = lookup_id(node, "Account_Name")
		if account is None:
			raise Unsupported("a deal with no account")
		stage = label_of(node, "Stage") or ""
		probability = node.optional_decimal("Probability")
		if stage.startswith("Closed Won") or probability == HUNDRED:
			status = "Converted"
		elif stage.startswith("Closed"):
			status = "Lost"
		else:
			status = "Open"
		pipeline = node.optional_string("Pipeline")
		opportunity = Opportunity(
			party_kind=Kind.PROSPECT,
			party=self.context.gid("Accounts", account),
			title=node.string("Deal_Name"),
			status=status,
			date=required_day(node.string("Created_Time"), node.path),
			currency=currency_of(node),
			amount=node.optional_decimal("Amount"),
			probability=probability,
			expected_closing=day(node.optional_string("Closing_Date")),
			notes=node.optional_string("Description"),
			opportunity_type=self.context.gid("Pipeline", pipeline) if pipeline else None,
			sales_stage=stage or None,
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), opportunity_fields(opportunity))


class CallMapper:
	"""A call or a note, on the deal, account or lead it is on (else its contact's account)."""

	kind = Kind.COMMUNICATION
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		party_kind, party = self.context.about(node, "an activity")
		if Identity.parse(node.id()).type == "Notes":
			text = (
				plain_text(node.optional_string("Note_Content"))
				or node.optional_string("Note_Title")
				or "Note"
			)
			subject = node.optional_string("Note_Title") or text.splitlines()[0]
			message = Communication(
				party_kind=party_kind,
				party=party,
				medium="Other",
				sent=True,
				date=node.time("Created_Time"),
				subject=subject[:SUBJECT_LENGTH],
				content=text,
				custom=custom_of(node),
			)
			return Desired(self.kind, node.id(), communication_fields(message))
		subject = node.optional_string("Subject") or "Call"
		seconds = node.optional_decimal("Call_Duration_in_seconds")
		details = ", ".join(
			part
			for part in (
				f"{decimal_text(seconds)} seconds" if seconds is not None else None,
				node.optional_string("Call_Result"),
			)
			if part
		)
		message = Communication(
			party_kind=party_kind,
			party=party,
			medium="Phone",
			sent=node.optional_string("Call_Type") == OUTBOUND,
			date=node.optional_time("Call_Start_Time") or node.time("Created_Time"),
			subject=subject[:SUBJECT_LENGTH],
			content="\n".join(part for part in (node.optional_string("Description"), details) if part)
			or subject,
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), communication_fields(message))


class EventMapper:
	"""An event at its start and end (UTC; all day on its day), its venue and description, with the record it
	is on and its lead participants."""

	kind = Kind.EVENT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("All_day"):
			starts, ends = f"{required_day(node.string('Start_DateTime'), node.path)} 00:00:00", None
		else:
			starts, ends = node.time("Start_DateTime"), node.optional_time("End_DateTime")
		participants = self.context.parties(node)
		for participant in node.children("Participants"):
			lead = participant.optional_string("participant")
			if participant.optional_string("type") == "lead" and lead:
				entry = (Kind.LEAD, self.context.gid("Leads", lead))
				if entry not in participants:
					participants.append(entry)
		description = "\n\n".join(
			part for part in (node.optional_string("Description"), node.optional_string("Venue")) if part
		)
		event = Event(
			subject=(node.optional_string("Event_Title") or "Event")[:SUBJECT_LENGTH],
			starts_on=starts,
			ends_on=ends,
			status="Open",
			description=description or None,
			participants=participants,
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), event_fields(event))


class TaskMapper:
	"""A task on the record it is on (else its contact's account), due on its date; completed is Closed."""

	kind = Kind.TODO
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		party_kind, party = self.context.about(node, "a task")
		description = "\n\n".join(
			part for part in (node.optional_string("Subject"), node.optional_string("Description")) if part
		)
		todo = ToDo(
			description=description or "Task",
			status="Closed" if node.optional_string("Status") in CLOSED_TASKS else "Open",
			party_kind=party_kind,
			party=party,
			date=day(node.optional_string("Due_Date")),
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), todo_fields(todo))


class Amounts:
	"""A quote's or order's lines at list price, its discounts (its lines' and its own), taxes (its lines'
	and its own) and adjustment, which must make its grand total exactly."""

	def __init__(self, context: Context, node: Node, items: str, currency: str) -> None:
		self.lines: list[OrderLine] = []
		discount = node.optional_decimal("Discount") or Decimal(0)
		tax = node.optional_decimal("Tax") or Decimal(0)
		for item in node.children(items):
			product = item.child("Product_Name")
			qty = item.decimal("Quantity")
			self.lines.append(
				order_line(
					item.string("id"),
					context.gid("Products", product.string("id")) if product and product.has("id") else None,
					None,
					(product.optional_string("name") if product else None) or "Product",
					qty,
					qty * item.decimal("List_Price"),
					currency,
				)
			)
			discount += item.optional_decimal("Discount") or Decimal(0)
			tax += item.optional_decimal("Tax") or Decimal(0)
		adjustment = node.optional_decimal("Adjustment") or Decimal(0)
		self.fee = max(adjustment, Decimal(0))
		self.discount = discount - min(adjustment, Decimal(0))
		self.tax = tax
		self.total = node.decimal("Grand_Total")
		made = (
			sum((line.qty * line.rate for line in self.lines), Decimal(0))
			- self.discount
			+ self.tax
			+ self.fee
		)
		if made != self.total:
			raise Unsupported(
				f"the lines, discounts, taxes and adjustment come to {decimal_text(made)}, not the grand total {decimal_text(self.total)}"
			)

	def charges(self) -> list[Field]:
		charges = [Charge(TAX, "Tax", self.tax), Charge(FEE, "Adjustment", self.fee)]
		return [charge_fields(charge) for charge in charges if charge.amount]

	def taxes(self) -> list[TradeTax]:
		taxes = [TradeTax("Tax", self.tax), TradeTax("Adjustment", self.fee, role=FEE)]
		return [tax for tax in taxes if tax.amount]


class QuoteMapper:
	"""A quote as a draft Quotation to its account (a prospect); a lost quote isn't kept."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_string("Quote_Stage") in LOST_QUOTES:
			return None
		account = lookup_id(node, "Account_Name")
		if account is None:
			raise Unsupported("a quote with no account")
		currency = currency_of(node)
		amounts = Amounts(self.context, node, "Quoted_Items", currency)
		fields: Fields = {
			"number": node.string("source_id"),
			"currency": currency,
			"created_at": midday(required_day(node.string("Created_Time"), node.path)),
			"customer": None,
			"quoted": quoted_party(Kind.PROSPECT, self.context.gid("Accounts", account)),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": [line_fields(line) for line in amounts.lines],
			"discount": amounts.discount,
			"charges": amounts.charges(),
			"total": amounts.total,
			"remarks": node.optional_string("Subject"),
		}
		return Desired(self.kind, node.id(), fields)


class DocumentMapper:
	"""A sales order, invoice or purchase order to its account (a customer) or vendor (a supplier): a draft
	while Zoho's is created, submitted once approved; cancelled isn't kept."""

	version = 1

	def __init__(self, kind: Kind, context: Context, number: str, items: str, date: str, party: str) -> None:
		self.kind = kind
		self.context = context
		self._number = number
		self._items = items
		self._date = date
		self._party = party

	def map(self, node: Node) -> Desired | None:
		status = node.optional_string("Status")
		if status in CANCELLED:
			return None
		party = lookup_id(node, self._party)
		if party is None:
			raise Unsupported(f"a {self.kind.value} with no {self._party}")
		currency = currency_of(node)
		amounts = Amounts(self.context, node, self._items, currency)
		document = TradeDocument(
			number=node.optional_string(self._number) or node.string("source_id"),
			date=required_day(node.optional_string(self._date) or node.string("Created_Time"), node.path),
			party=self.context.gid("Vendors" if self._party == "Vendor_Name" else "Accounts", party),
			currency=currency,
			lines=[TradeLine(line) for line in amounts.lines],
			taxes=amounts.taxes(),
			total=amounts.total,
			submitted=status not in DRAFT_ORDERS,
			due_date=day(node.optional_string("Due_Date")),
			remarks=node.optional_string("Subject"),
			discount=amounts.discount or None,
		)
		return Desired(self.kind, node.id(), trade_fields(document))


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		UserMapper(context),
		ProductMapper(),
		VendorMapper(),
		CustomerMapper(context),
		LeadMapper(),
		AccountMapper(context),
		PipelineMapper(),
		DealMapper(context),
		CallMapper(context),
		EventMapper(context),
		TaskMapper(context),
		QuoteMapper(context),
		DocumentMapper(
			Kind.SALES_ORDER, context, "SO_Number", "Ordered_Items", "Created_Time", "Account_Name"
		),
		DocumentMapper(
			Kind.SALES_INVOICE, context, "Invoice_Number", "Invoiced_Items", "Invoice_Date", "Account_Name"
		),
		DocumentMapper(
			Kind.PURCHASE_ORDER, context, "PO_Number", "Purchased_Items", "PO_Date", "Vendor_Name"
		),
	]
