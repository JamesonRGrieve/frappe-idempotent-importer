# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Salesforce → desired ERPNext state: users as Employees, products as Items at their standard price,
accounts as Prospects with their contacts and notes (customers, and the accounts orders are for, as
Customers too), leads as Leads with their notes, the opportunity types and opportunities at their stage,
logged calls and emails (tasks and email messages) as Communications, other tasks as ToDos, events as
Events, quotes as draft Quotations and orders as Sales Orders. Every record keeps its custom fields' values,
by label.

Field meanings are the Object Reference's (developer.salesforce.com/docs/atlas.en-us.object_reference.meta/
object_reference: User, Product2, PricebookEntry, Account, Contact, Lead, Opportunity, Task, Event,
EmailMessage, Quote, QuoteLineItem, Order, OrderItem). Who an activity is about follows its ids' key
prefixes (00Q a Lead, 006 an Opportunity, 001 an Account). A quote's lines are its subtotal (quantity x
list unit price), its line discounts its discount, then its tax and shipping and handling, which must make
its grand total exactly; an order's lines its total amount. What ERPNext can't hold exactly is Unsupported,
naming the card that plans it."""

from __future__ import annotations

from decimal import Decimal

from idempotent_importer.contract import (
	SHIPPING,
	TAX,
	Address,
	Charge,
	Communication,
	ContactPerson,
	Employee,
	Event,
	Lead,
	Note,
	Opportunity,
	OrderLine,
	Party,
	Prospect,
	ToDo,
	TradeDocument,
	TradeLine,
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
from idempotent_importer.sources.crm import custom_of
from idempotent_importer.sources.ledger import book_item, country_code, day, midday, required_day
from idempotent_importer.sources.salesforce.reader import type_identity

PLANNED = "planned: card 386"
LEAD_PREFIX, OPPORTUNITY_PREFIX, ACCOUNT_PREFIX = "00Q", "006", "001"
CALL, EMAIL = "Call", "Email"
CALL_SUBTYPES = frozenset({CALL})
EMAIL_SUBTYPES = frozenset({EMAIL, "ListEmail"})
OUTBOUND = "Outbound"
REJECTED_QUOTES = frozenset({"Rejected", "Denied"})
DRAFT, CANCELLED = "Draft", "Cancelled"
SUBJECT_LENGTH = 140


class Context:
	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config

	def gid(self, type_: str, id_: str) -> str:
		return self.config.identity(type_, id_)

	def notes(self, node: Node) -> list[Note]:
		"""A record's notes (its ``Notes`` child relationship)."""
		found = []
		for note in children(node, "Notes"):
			text = "\n\n".join(
				part for part in (note.optional_string("Title"), note.optional_string("Body")) if part
			)
			if text:
				found.append(
					Note(self.gid("Note", note.string("Id")), note.optional_time("CreatedDate"), text)
				)
		return found

	def parties(self, node: Node) -> list[tuple[Kind, str]]:
		"""Who an activity is about: its lead (WhoId), its opportunity (WhatId), its account (a prospect)."""
		found: list[tuple[Kind, str]] = []
		who, what, account = (node.optional_string(f) for f in ("WhoId", "WhatId", "AccountId"))
		if who and who.startswith(LEAD_PREFIX):
			found.append((Kind.LEAD, self.gid("Lead", who)))
		if what and what.startswith(OPPORTUNITY_PREFIX):
			found.append((Kind.OPPORTUNITY, self.gid("Opportunity", what)))
		if what and what.startswith(ACCOUNT_PREFIX) and what != account:
			found.append((Kind.PROSPECT, self.gid("Account", what)))
		if account:
			found.append((Kind.PROSPECT, self.gid("Account", account)))
		return found

	def about(self, node: Node, what: str) -> tuple[Kind, str]:
		found = self.parties(node)
		if not found:
			raise Unsupported(f"{what} is about no lead, opportunity or account")
		return found[0]

	def related(self, record: str | None, what: str) -> tuple[Kind, str]:
		"""What an email message is related to, by its id's prefix."""
		prefixes = {
			LEAD_PREFIX: (Kind.LEAD, "Lead"),
			OPPORTUNITY_PREFIX: (Kind.OPPORTUNITY, "Opportunity"),
			ACCOUNT_PREFIX: (Kind.PROSPECT, "Account"),
		}
		kind = prefixes.get((record or "")[:3])
		if record is None or kind is None:
			raise Unsupported(f"{what} is related to no lead, opportunity or account")
		return kind[0], self.gid(kind[1], record)


def children(node: Node, relationship: str) -> list[Node]:
	"""A child relationship's records (the reader gathers them all)."""
	related = node.child(relationship)
	return related.children("records") if related else []


def currency_of(node: Node) -> str:
	return node.optional_string("CurrencyIsoCode") or node.string("org_currency")


def label_of(node: Node, field: str) -> str | None:
	"""A picklist's value as its label (the reader gathers them from the describe)."""
	labels = node.child("labels")
	return (labels.optional_string(field) if labels else None) or node.optional_string(field)


def address_of(node: Node, prefix: str, name: str, company: str | None) -> Fields | None:
	street, city = node.optional_string(f"{prefix}Street"), node.optional_string(f"{prefix}City")
	if not (street and city):
		return None
	line1, _, line2 = street.partition("\n")
	return address_fields(
		Address(
			"Billing",
			name,
			company,
			line1,
			line2 or None,
			city,
			node.optional_string(f"{prefix}State"),
			node.optional_string(f"{prefix}PostalCode"),
			country_code(node.optional_string(f"{prefix}Country")),
			None,
			None,
		)
	)


def person(context: Context, contact: Node) -> ContactPerson:
	first, last = contact.optional_string("FirstName"), contact.optional_string("LastName")
	return ContactPerson(
		gid=context.gid("Contact", contact.string("Id")),
		name=" ".join(part for part in (first, last) if part)
		or contact.optional_string("Email")
		or contact.string("Id"),
		email=contact.optional_string("Email"),
		phone=contact.optional_string("Phone") or contact.optional_string("MobilePhone"),
		job_title=contact.optional_string("Title"),
	)


class UserMapper:
	"""A standard user: names, email, phone, employee number and manager; an inactive user is inactive."""

	kind = Kind.EMPLOYEE
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		first = node.optional_string("FirstName") or node.optional_string("LastName")
		if first is None:
			raise Unsupported("a user with no name")
		manager = node.optional_string("ManagerId")
		employee = Employee(
			first_name=first,
			status="Active" if node.optional_flag("IsActive") else "Inactive",
			last_name=node.optional_string("LastName") if node.optional_string("FirstName") else None,
			employee_number=node.optional_string("EmployeeNumber"),
			reports_to=self.context.gid("User", manager) if manager else None,
			company_email=node.optional_string("Email"),
			phone=node.optional_string("Phone") or node.optional_string("MobilePhone"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class ProductMapper:
	"""An active product at its standard price book price; an inactive one isn't kept. Prices in several
	currencies are several price lists (planned)."""

	kind = Kind.PRODUCT
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("IsActive") is False:
			return None
		prices = children(node, "PricebookEntries")
		if len(prices) > 1:
			raise Unsupported(f"standard prices in {len(prices)} currencies ({PLANNED}: price lists)")
		fields = book_item(
			node.string("Name"),
			node.optional_string("StockKeepingUnit") or node.optional_string("ProductCode"),
			node.optional_string("Description"),
			prices[0].decimal("UnitPrice") if prices else None,
			False,
		)
		return Desired(self.kind, node.id(), with_custom(fields, custom_of(node)))


class CustomerMapper:
	"""A customer account (or one an order is for): a company with its contacts, or a person account."""

	kind = Kind.CUSTOMER
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		name = node.string("Name")
		individual = node.optional_flag("IsPersonAccount") is True
		address = address_of(node, "Billing", name, None if individual else name)
		party = Party(
			name=name,
			type="Individual" if individual else "Company",
			company=None if individual else name,
			email=node.optional_string("PersonEmail"),
			phone=node.optional_string("Phone"),
			addresses=[address] if address else [],
			contacts=[person(self.context, contact) for contact in children(node, "Contacts")],
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), party_fields(party))


class ProspectMapper:
	"""An account as an organisation of the pipeline, with its contacts, address, description and notes."""

	kind = Kind.PROSPECT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		name = node.string("Name")
		prospect = Prospect(
			name=name,
			website=node.optional_string("Website"),
			email=node.optional_string("PersonEmail"),
			phone=node.optional_string("Phone"),
			address=address_of(node, "Billing", name, name),
			notes=node.optional_string("Description"),
			contacts=[person(self.context, contact) for contact in children(node, "Contacts")],
			custom=custom_of(node),
			crm_notes=self.context.notes(node),
		)
		return Desired(self.kind, node.id(), prospect_fields(prospect))


class LeadMapper:
	"""A lead: names, company, how to reach them, title, website, description and notes. Converted is
	Converted; a closed status closes it (disabled, its status kept); a working status is Replied; any other
	is Open."""

	kind = Kind.LEAD
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		status = label_of(node, "Status") or ""
		converted = node.optional_flag("IsConverted") is True
		if converted:
			erpnext_status = "Converted"
		elif status.startswith("Working"):
			erpnext_status = "Replied"
		elif status.startswith("Closed"):
			erpnext_status = "Lead"
		else:
			erpnext_status = "Open"
		lead = Lead(
			status=erpnext_status,
			first_name=node.optional_string("FirstName"),
			last_name=node.optional_string("LastName"),
			company=node.optional_string("Company"),
			email=node.optional_string("Email"),
			phone=node.optional_string("Phone"),
			mobile=node.optional_string("MobilePhone"),
			job_title=node.optional_string("Title"),
			website=node.optional_string("Website"),
			notes=node.optional_string("Description"),
			disabled=status.startswith("Closed") and not converted,
			crm_notes=self.context.notes(node),
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), lead_fields(lead))


class OpportunityTypeMapper:
	"""An opportunity type (a value of the Type picklist), by its label."""

	kind = Kind.OPPORTUNITY_TYPE
	version = 1

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), opportunity_type_fields(node.string("label")))


class OpportunityMapper:
	"""An opportunity with its account (a prospect), at its stage: won is Converted, closed otherwise Lost;
	its type, amount, probability, close date, description and notes."""

	kind = Kind.OPPORTUNITY
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		account = node.optional_string("AccountId")
		if account is None:
			raise Unsupported("an opportunity with no account")
		won, closed = node.optional_flag("IsWon") is True, node.optional_flag("IsClosed") is True
		kind_of = node.optional_string("Type")
		opportunity = Opportunity(
			party_kind=Kind.PROSPECT,
			party=self.context.gid("Account", account),
			title=node.string("Name"),
			status="Converted" if won else ("Lost" if closed else "Open"),
			date=required_day(node.string("CreatedDate"), node.path),
			currency=currency_of(node),
			amount=node.optional_decimal("Amount"),
			probability=node.optional_decimal("Probability"),
			expected_closing=day(node.optional_string("CloseDate")),
			notes=node.optional_string("Description"),
			opportunity_type=type_identity(self.context.config, kind_of) if kind_of else None,
			sales_stage=label_of(node, "StageName"),
			custom=custom_of(node),
			crm_notes=self.context.notes(node),
		)
		return Desired(self.kind, node.id(), opportunity_fields(opportunity))


def subtype(node: Node) -> str:
	return node.optional_string("TaskSubtype") or "Task"


class ActivityMapper:
	"""A logged call or email (a task of that subtype) and an email message, as a Communication on its lead,
	else its opportunity, else its account."""

	kind = Kind.COMMUNICATION
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if Identity.parse(node.id()).type == "EmailMessage":
			return self._email_message(node)
		kind = subtype(node)
		if kind not in CALL_SUBTYPES | EMAIL_SUBTYPES:
			return None
		party_kind, party = self.context.about(node, "a logged activity")
		subject = node.optional_string("Subject") or kind
		if kind in CALL_SUBTYPES:
			seconds = node.optional_integer("CallDurationInSeconds")
			details = ", ".join(
				part
				for part in (
					f"{seconds} seconds" if seconds is not None else None,
					node.optional_string("CallDisposition"),
				)
				if part
			)
			content = "\n".join(part for part in (node.optional_string("Description"), details) if part)
			message = Communication(
				party_kind=party_kind,
				party=party,
				medium="Phone",
				sent=node.optional_string("CallType") == OUTBOUND,
				date=node.time("CreatedDate"),
				subject=subject[:SUBJECT_LENGTH],
				content=content or subject,
				custom=custom_of(node),
			)
		else:
			message = Communication(
				party_kind=party_kind,
				party=party,
				medium="Email",
				sent=True,
				date=node.time("CreatedDate"),
				subject=subject[:SUBJECT_LENGTH],
				content=node.optional_string("Description") or subject,
				custom=custom_of(node),
			)
		return Desired(self.kind, node.id(), communication_fields(message))

	def _email_message(self, node: Node) -> Desired:
		party_kind, party = self.context.related(node.optional_string("RelatedToId"), "an email message")
		incoming = node.optional_flag("Incoming") is True
		subject = node.optional_string("Subject") or "Email"
		message = Communication(
			party_kind=party_kind,
			party=party,
			medium="Email",
			sent=not incoming,
			date=node.time("MessageDate"),
			subject=subject[:SUBJECT_LENGTH],
			content=node.optional_string("TextBody") or subject,
			email=node.optional_string("FromAddress" if incoming else "ToAddress"),
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), communication_fields(message))


class TaskMapper:
	"""A task that isn't a logged call or email, about its lead, opportunity or account; closed or open."""

	kind = Kind.TODO
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if subtype(node) in CALL_SUBTYPES | EMAIL_SUBTYPES:
			return None
		party_kind, party = self.context.about(node, "a task")
		description = "\n\n".join(
			part for part in (node.optional_string("Subject"), node.optional_string("Description")) if part
		)
		todo = ToDo(
			description=description or "Task",
			status="Closed" if node.optional_flag("IsClosed") else "Open",
			party_kind=party_kind,
			party=party,
			date=day(node.optional_string("ActivityDate")),
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), todo_fields(todo))


class EventMapper:
	"""An event at its start and end (UTC; an all-day event on its day), its description and location, with
	its lead, opportunity and account. Salesforce keeps no event status: every event is Open."""

	kind = Kind.EVENT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("IsAllDayEvent"):
			starts, ends = f"{required_day(node.optional_string('ActivityDate'), node.path)} 00:00:00", None
		else:
			starts, ends = node.time("StartDateTime"), node.optional_time("EndDateTime")
		description = "\n\n".join(
			part for part in (node.optional_string("Description"), node.optional_string("Location")) if part
		)
		event = Event(
			subject=(node.optional_string("Subject") or "Event")[:SUBJECT_LENGTH],
			starts_on=starts,
			ends_on=ends,
			status="Open",
			description=description or None,
			participants=self.context.parties(node),
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), event_fields(event))


def lines_of(context: Context, node: Node, relationship: str, currency: str) -> list[OrderLine]:
	"""A quote's or order's products at their unit prices."""
	lines = []
	for item in children(node, relationship):
		product = item.optional_string("Product2Id")
		named = item.child("Product2")
		qty = item.decimal("Quantity")
		lines.append(
			order_line(
				item.string("Id"),
				context.gid("Product2", product) if product else None,
				None,
				(named.optional_string("Name") if named else None)
				or item.optional_string("Description")
				or "Product",
				qty,
				qty * item.decimal("UnitPrice"),
				currency,
			)
		)
	return lines


class QuoteMapper:
	"""A quote as a draft Quotation to its account (a prospect): its lines at list price, its line discounts
	as its discount, its tax and shipping and handling, which must make its grand total exactly. A rejected
	or denied quote isn't kept."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_string("Status") in REJECTED_QUOTES:
			return None
		account = node.optional_string("AccountId")
		if account is None:
			raise Unsupported("a quote with no account")
		currency = currency_of(node)
		lines = lines_of(self.context, node, "QuoteLineItems", currency)
		subtotal = sum((line.qty * line.rate for line in lines), Decimal(0))
		stated = node.optional_decimal("Subtotal")
		if stated is not None and stated != subtotal:
			raise Unsupported(
				f"the quote's lines come to {decimal_text(subtotal)}, not its subtotal {decimal_text(stated)}"
			)
		discount = subtotal - (node.optional_decimal("TotalPrice") or subtotal)
		charges = [
			Charge(TAX, "Tax", node.optional_decimal("Tax") or Decimal(0)),
			Charge(
				SHIPPING, "Shipping and handling", node.optional_decimal("ShippingHandling") or Decimal(0)
			),
		]
		total = node.decimal("GrandTotal")
		made = subtotal - discount + sum((charge.amount for charge in charges), Decimal(0))
		if made != total:
			raise Unsupported(
				f"the quote comes to {decimal_text(made)}, not its grand total {decimal_text(total)}"
			)
		fields: Fields = {
			"number": node.optional_string("QuoteNumber") or node.string("source_id"),
			"currency": currency,
			"created_at": midday(required_day(node.string("CreatedDate"), node.path)),
			"customer": None,
			"quoted": quoted_party(Kind.PROSPECT, self.context.gid("Account", account)),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": list[Field]([line_fields(line) for line in lines]),
			"discount": discount,
			"charges": list[Field]([charge_fields(charge) for charge in charges if charge.amount]),
			"total": total,
			"remarks": node.optional_string("Name"),
		}
		return Desired(self.kind, node.id(), fields)


class OrderMapper:
	"""An order as a Sales Order to its account (a customer): a draft while Salesforce's is, activated
	submitted; a cancelled order isn't kept."""

	kind = Kind.SALES_ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		status = node.optional_string("StatusCode") or DRAFT
		if status == CANCELLED:
			return None
		currency = currency_of(node)
		po = node.optional_string("PoNumber")
		remarks = "\n".join(
			part for part in (f"PO {po}" if po else None, node.optional_string("Description")) if part
		)
		document = TradeDocument(
			number=node.optional_string("OrderNumber") or node.string("source_id"),
			date=required_day(node.string("EffectiveDate"), node.path),
			party=self.context.gid("Account", node.string("AccountId")),
			currency=currency,
			lines=[TradeLine(line) for line in lines_of(self.context, node, "OrderItems", currency)],
			taxes=[],
			total=node.decimal("TotalAmount"),
			submitted=status != DRAFT,
			remarks=remarks or None,
		)
		return Desired(self.kind, node.id(), trade_fields(document))


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		UserMapper(context),
		ProductMapper(),
		CustomerMapper(context),
		LeadMapper(context),
		ProspectMapper(context),
		OpportunityTypeMapper(),
		OpportunityMapper(context),
		ActivityMapper(context),
		EventMapper(context),
		TaskMapper(context),
		QuoteMapper(context),
		OrderMapper(context),
	]
