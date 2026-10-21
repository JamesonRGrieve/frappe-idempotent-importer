# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure HubSpot → desired ERPNext state: owners as Employees, products as Items, contacts as Leads with their
notes, companies as Prospects with their people and notes, the contacts and companies HubSpot counts as
customers (or bills) as Customers, deal pipelines as Opportunity Types and deals as Opportunities at their
stage, calls and emails as Communications, meetings as Events, tasks as ToDos, quotes as draft Quotations,
orders as Sales Orders, invoices as Sales Invoices and commerce payments (and their refunds) as Payment
Entries. Every record keeps the values of the properties the account's users defined, by label.

Property meanings are HubSpot's (its CRM API guides on developers.hubspot.com and the v3 OpenAPI documents
in HubSpot/HubSpot-public-api-spec-collection). A document's total is its line items (quantity x unit
price) less its discounts, plus its fees, plus its taxes, each a fixed amount or a percentage of what
comes before it, in that order (the quotes guide); a percentage that doesn't come to an exact amount in the
currency, or a line item taxed by a tax rate group (whose amount HubSpot doesn't document), is Unsupported.
What ERPNext can't hold exactly is Unsupported, naming the card that plans it."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from idempotent_importer.contract import (
	FEE,
	PAY,
	RECEIVE,
	TAX,
	Address,
	Allocation,
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
	Payment,
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
	payment_fields,
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
from idempotent_importer.core.money import decimal_text, exact_quotient, minor_units
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.crm import custom_of
from idempotent_importer.sources.hubspot.reader import (
	COMPANIES,
	COMPANY,
	CONTACT,
	CONTACTS,
	CUSTOMER_STAGE,
	DEALS,
	DISPUTE_LOST,
	REFUND,
	associated,
	billed_party,
)
from idempotent_importer.sources.ledger import book_item, country_code, day, midday, required_day

PLANNED = "planned: card 386"
HUNDRED = Decimal(100)
FIXED, PERCENT = "FIXED", "PERCENT"
INVENTORY = "inventory"
# hs_lead_status → ERPNext's Lead status; an unqualified lead is closed (disabled), its status kept.
LEAD_STATUSES = {
	"NEW": "Lead",
	"OPEN": "Open",
	"ATTEMPTED_TO_CONTACT": "Open",
	"IN_PROGRESS": "Replied",
	"CONNECTED": "Replied",
	"OPEN_DEAL": "Opportunity",
	"BAD_TIMING": "Interested",
	"UNQUALIFIED": "Lead",
}
UNQUALIFIED = "UNQUALIFIED"
MEETING_STATUSES = {
	"SCHEDULED": "Open",
	"RESCHEDULED": "Open",
	"COMPLETED": "Completed",
	"NO_SHOW": "Closed",
	"CANCELED": "Cancelled",
}
COMPLETED = "COMPLETED"
INCOMING_EMAIL, OUTBOUND = "INCOMING_EMAIL", "OUTBOUND"
REJECTED = "REJECTED"
DRAFT, OPEN, PAID, VOIDED = "draft", "open", "paid", "voided"
INVOICE_STATUSES = frozenset({DRAFT, OPEN, PAID, VOIDED})
# Commerce payment statuses (hs_latest_status) whose money was taken; a lost dispute gave it all back.
RECEIVED = frozenset(
	{
		"succeeded",
		"partially_refunded",
		"refunded",
		"processing_refund",
		"disputed_won",
		"disputed_lost",
		"disputed_action_required",
		"disputed_awaiting_decision",
	}
)
MILLISECONDS = Decimal(1000)
SUBJECT_LENGTH = 140


def props(node: Node) -> Node:
	return node.required_child("properties")


class Context:
	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config

	def gid(self, type_: str, id_: str) -> str:
		return self.config.identity(type_, id_)

	def notes(self, node: Node) -> list[Note]:
		"""The notes the reader gathered on a contact, company or deal."""
		found = []
		for note in node.children("notes"):
			text = props(note).optional_string("hs_note_body")
			if text:
				found.append(
					Note(self.gid("Note", note.string("id")), props(note).optional_time("hs_timestamp"), text)
				)
		return found

	def engaged(self, node: Node) -> list[tuple[Kind, str]]:
		"""Who an engagement is about: its contacts (leads), then its deals, then its companies (prospects)."""
		return [
			*((Kind.LEAD, self.gid(CONTACT.type, c)) for c in associated(node, CONTACTS)),
			*((Kind.OPPORTUNITY, self.gid("Deal", d)) for d in associated(node, DEALS)),
			*((Kind.PROSPECT, self.gid(COMPANY.type, c)) for c in associated(node, COMPANIES)),
		]

	def about(self, node: Node, what: str) -> tuple[Kind, str]:
		engaged = self.engaged(node)
		if not engaged:
			raise Unsupported(f"{what} is associated with no contact, deal or company")
		return engaged[0]

	def customer(self, node: Node, what: str) -> str:
		"""The customer a document is for: its company, else its contact (both mirrored as customers)."""
		party = billed_party(node)
		if party is None:
			raise Unsupported(f"{what} is associated with no company or contact")
		return self.gid(party[0].type, party[1])


def full_name(properties: Node) -> str | None:
	first, last = properties.optional_string("firstname"), properties.optional_string("lastname")
	return " ".join(part for part in (first, last) if part) or None


def address_of(properties: Node, name: str, company: str | None = None) -> Fields | None:
	line1, city = properties.optional_string("address"), properties.optional_string("city")
	if not (line1 and city):
		return None
	return address_fields(
		Address(
			"Billing",
			name,
			company,
			line1,
			properties.optional_string("address2"),
			city,
			properties.optional_string("state"),
			properties.optional_string("zip"),
			country_code(properties.optional_string("country")),
			None,
			None,
		)
	)


class OwnerMapper:
	"""An owner (a user of the account): names and email; an archived owner is inactive. A queue is no
	person."""

	kind = Kind.EMPLOYEE
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.optional_string("type") == "QUEUE":
			return None
		first = node.optional_string("firstName") or node.optional_string("email")
		if first is None:
			raise Unsupported("an owner with no name or email")
		employee = Employee(
			first_name=first,
			status="Inactive" if node.optional_flag("archived") else "Active",
			last_name=node.optional_string("lastName"),
			company_email=node.optional_string("email"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class ProductMapper:
	"""A product at its unit price, a stock item when its type is inventory. A recurring billing period
	makes it a subscription (planned)."""

	kind = Kind.PRODUCT
	version = 1

	def map(self, node: Node) -> Desired | None:
		properties = props(node)
		if properties.optional_string("hs_recurring_billing_period"):
			raise Unsupported(f"a recurring product is a subscription plan ({PLANNED})")
		fields = book_item(
			properties.optional_string("name") or node.string("source_id"),
			properties.optional_string("hs_sku"),
			properties.optional_string("description"),
			properties.optional_decimal("price"),
			properties.optional_string("hs_product_type") == INVENTORY,
		)
		return Desired(self.kind, node.id(), with_custom(fields, custom_of(node)))


class CustomerMapper:
	"""A contact or company HubSpot counts as a customer (or bills): a person, or a company with its people
	as its contacts; with email, phone and address."""

	kind = Kind.CUSTOMER
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		properties = props(node)
		if Identity.parse(node.id()).type == CONTACT.type:
			person = full_name(properties)
			name = person or properties.optional_string("company") or properties.optional_string("email")
			if name is None:
				raise Unsupported("a contact with no name, company or email")
			address = address_of(properties, name, properties.optional_string("company"))
			party = Party(
				name=name,
				type="Individual" if person else "Company",
				first_name=properties.optional_string("firstname"),
				last_name=properties.optional_string("lastname"),
				company=properties.optional_string("company"),
				email=properties.optional_string("email"),
				phone=properties.optional_string("phone") or properties.optional_string("mobilephone"),
				addresses=[address] if address else [],
				custom=custom_of(node),
			)
			return Desired(self.kind, node.id(), party_fields(party))
		name = properties.optional_string("name") or properties.optional_string("domain")
		if name is None:
			raise Unsupported("a company with no name or domain")
		address = address_of(properties, name, name)
		party = Party(
			name=name,
			type="Company",
			company=name,
			phone=properties.optional_string("phone"),
			addresses=[address] if address else [],
			contacts=[self._person(person) for person in node.children("people")],
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), party_fields(party))

	def _person(self, person: Node) -> ContactPerson:
		properties = props(person)
		return ContactPerson(
			gid=self.context.gid(CONTACT.type, person.string("id")),
			name=full_name(properties) or properties.optional_string("email") or person.string("id"),
			email=properties.optional_string("email"),
			phone=properties.optional_string("phone") or properties.optional_string("mobilephone"),
			job_title=properties.optional_string("jobtitle"),
		)


class LeadMapper:
	"""A contact as a lead: names, company, how to reach them, their notes; its status from its lead status
	(Converted once a customer; an unqualified lead closed)."""

	kind = Kind.LEAD
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		properties = props(node)
		lead_status = properties.optional_string("hs_lead_status")
		if properties.optional_string("lifecyclestage") == CUSTOMER_STAGE:
			status = "Converted"
		elif lead_status is None:
			status = "Lead"
		elif lead_status in LEAD_STATUSES:
			status = LEAD_STATUSES[lead_status]
		else:
			raise Unsupported(f'a lead status "{lead_status}"')
		lead = Lead(
			status=status,
			first_name=properties.optional_string("firstname"),
			last_name=properties.optional_string("lastname"),
			company=properties.optional_string("company"),
			email=properties.optional_string("email"),
			phone=properties.optional_string("phone"),
			mobile=properties.optional_string("mobilephone"),
			job_title=properties.optional_string("jobtitle"),
			website=properties.optional_string("website"),
			disabled=lead_status == UNQUALIFIED,
			crm_notes=self.context.notes(node),
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), lead_fields(lead))


class ProspectMapper:
	"""A company as an organisation of the pipeline, with its contacts as its leads, its address, description
	and notes."""

	kind = Kind.PROSPECT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		properties = props(node)
		name = properties.optional_string("name") or properties.optional_string("domain")
		if name is None:
			raise Unsupported("a company with no name or domain")
		domain = properties.optional_string("domain")
		prospect = Prospect(
			name=name,
			website=properties.optional_string("website") or (f"https://{domain}" if domain else None),
			phone=properties.optional_string("phone"),
			address=address_of(properties, name, name),
			notes=properties.optional_string("description"),
			leads=[self.context.gid(CONTACT.type, contact) for contact in associated(node, CONTACTS)],
			custom=custom_of(node),
			crm_notes=self.context.notes(node),
		)
		return Desired(self.kind, node.id(), prospect_fields(prospect))


class PipelineMapper:
	"""A deal pipeline, as the type of its deals."""

	kind = Kind.OPPORTUNITY_TYPE
	version = 1

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), opportunity_type_fields(node.string("label")))


class DealMapper:
	"""A deal with its company (a prospect) or, without one, its contact (a lead): in its pipeline, at its
	stage (won, lost or open as the stage says), with its amount, probability, close date, description and
	notes."""

	kind = Kind.OPPORTUNITY
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		properties = props(node)
		companies, contacts = associated(node, COMPANIES), associated(node, CONTACTS)
		if companies:
			party = (Kind.PROSPECT, self.context.gid(COMPANY.type, companies[0]))
		elif contacts:
			party = (Kind.LEAD, self.context.gid(CONTACT.type, contacts[0]))
		else:
			raise Unsupported("a deal associated with no company or contact")
		stage = node.child("stage")
		if stage is None:
			raise Unsupported(f"a deal at a stage its pipeline doesn't have ({properties.text('dealstage')})")
		metadata = stage.required_child("metadata")
		won = metadata.optional_decimal("probability")
		closed = metadata.optional_string("isClosed") == "true"
		status = ("Converted" if won == 1 else "Lost") if closed else "Open"
		probability = properties.optional_decimal("hs_deal_stage_probability")
		probability = probability if probability is not None else won
		opportunity = Opportunity(
			party_kind=party[0],
			party=party[1],
			title=properties.optional_string("dealname") or node.string("source_id"),
			status=status,
			date=required_day(
				properties.optional_string("createdate") or node.string("createdAt"), node.path
			),
			currency=properties.optional_string("deal_currency_code"),
			amount=properties.optional_decimal("amount"),
			probability=probability * HUNDRED if probability is not None else None,
			expected_closing=day(properties.optional_string("closedate")),
			notes=properties.optional_string("description"),
			opportunity_type=self.context.gid("Pipeline", properties.string("pipeline")),
			sales_stage=stage.string("label"),
			custom=custom_of(node),
			crm_notes=self.context.notes(node),
		)
		return Desired(self.kind, node.id(), opportunity_fields(opportunity))


class EngagementMapper:
	"""A call or an email, recorded on the contact (else the deal, else the company) it is about."""

	kind = Kind.COMMUNICATION
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		properties = props(node)
		party_kind, party = self.context.about(node, "an engagement")
		when = properties.optional_time("hs_timestamp") or node.time("createdAt")
		if Identity.parse(node.id()).type == "Call":
			sent = properties.optional_string("hs_call_direction") == OUTBOUND
			duration = properties.optional_decimal("hs_call_duration")
			call = ", ".join(
				part
				for part in (
					f"{decimal_text(duration / MILLISECONDS)} seconds" if duration is not None else None,
					properties.optional_string("hs_call_status"),
				)
				if part
			)
			body = properties.optional_string("hs_call_body")
			message = Communication(
				party_kind=party_kind,
				party=party,
				medium="Phone",
				sent=sent,
				date=when,
				subject=(properties.optional_string("hs_call_title") or "Call")[:SUBJECT_LENGTH],
				content="\n".join(part for part in (body, call) if part) or "Call",
				phone=properties.optional_string("hs_call_to_number" if sent else "hs_call_from_number"),
				custom=custom_of(node),
			)
		else:
			sent = properties.optional_string("hs_email_direction") != INCOMING_EMAIL
			subject = properties.optional_string("hs_email_subject") or "Email"
			message = Communication(
				party_kind=party_kind,
				party=party,
				medium="Email",
				sent=sent,
				date=when,
				subject=subject[:SUBJECT_LENGTH],
				content=properties.optional_string("hs_email_text") or subject,
				email=properties.optional_string("hs_email_to_email" if sent else "hs_email_from_email"),
				custom=custom_of(node),
			)
		return Desired(self.kind, node.id(), communication_fields(message))


class MeetingMapper:
	"""A meeting, at its start and end (UTC), with its outcome as its status, its body, internal notes and
	location, and the contacts, deals and companies it is with."""

	kind = Kind.EVENT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		properties = props(node)
		outcome = properties.optional_string("hs_meeting_outcome") or "SCHEDULED"
		if outcome not in MEETING_STATUSES:
			raise Unsupported(f'a meeting outcome "{outcome}"')
		start = properties.optional_time("hs_meeting_start_time") or properties.time("hs_timestamp")
		description = "\n\n".join(
			part
			for part in (
				properties.optional_string("hs_meeting_body"),
				properties.optional_string("hs_internal_meeting_notes"),
				properties.optional_string("hs_meeting_location"),
			)
			if part
		)
		event = Event(
			subject=(properties.optional_string("hs_meeting_title") or "Meeting")[:SUBJECT_LENGTH],
			starts_on=start,
			ends_on=properties.optional_time("hs_meeting_end_time"),
			status=MEETING_STATUSES[outcome],
			description=description or None,
			participants=self.context.engaged(node),
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), event_fields(event))


class TaskMapper:
	"""A task about its contact (else deal, else company), due on its date; completed is closed."""

	kind = Kind.TODO
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		properties = props(node)
		party_kind, party = self.context.about(node, "a task")
		description = "\n\n".join(
			part
			for part in (
				properties.optional_string("hs_task_subject"),
				properties.optional_string("hs_task_body"),
			)
			if part
		)
		todo = ToDo(
			description=description or "Task",
			status="Closed" if properties.optional_string("hs_task_status") == COMPLETED else "Open",
			party_kind=party_kind,
			party=party,
			date=day(properties.optional_string("hs_timestamp")),
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), todo_fields(todo))


class Amounts:
	"""A quote's, order's or invoice's lines, discounts, fees and taxes, each fixed or a percentage of what
	comes before it (discounts of the lines, fees of the discounted lines, taxes of both)."""

	def __init__(self, context: Context, node: Node, currency: str) -> None:
		self.currency = currency
		self.places = minor_units(currency)
		self.lines = [self._line(context, item) for item in node.children("line_items")]
		subtotal = sum((line.qty * line.rate for line in self.lines), Decimal(0))
		self.discount = self._applied(node, "discounts", subtotal)
		fees = [(label, amount) for label, amount in self._each(node, "fees", subtotal - self.discount)]
		self.fees = fees
		taxed = subtotal - self.discount + sum((amount for _, amount in fees), Decimal(0))
		self.taxes = list(self._each(node, "taxes", taxed))
		self.total = taxed + sum((amount for _, amount in self.taxes), Decimal(0))

	def _line(self, context: Context, item: Node) -> OrderLine:
		properties = props(item)
		name = properties.optional_string("name") or item.string("id")
		if properties.optional_string("hs_tax_rate_group_id"):
			raise Unsupported(
				f'line "{name}" is taxed by a tax rate group, whose amount isn\'t documented ({PLANNED})'
			)
		if properties.optional_string("hs_recurring_billing_period"):
			raise Unsupported(f'line "{name}" bills on a schedule: a subscription ({PLANNED})')
		qty = properties.decimal("quantity")
		amount = properties.optional_decimal("amount")
		product = properties.optional_string("hs_product_id")
		return order_line(
			item.string("id"),
			context.gid("Product", product) if product else None,
			None,
			name,
			qty,
			amount if amount is not None else qty * properties.decimal("price"),
			self.currency,
		)

	def _each(self, node: Node, name: str, base: Decimal) -> list[tuple[str, Decimal]]:
		parts = sorted(
			node.children(name), key=lambda part: props(part).optional_integer("hs_sort_order") or 0
		)
		return [(self._label(part, name), self._amount(part, base)) for part in parts]

	def _applied(self, node: Node, name: str, base: Decimal) -> Decimal:
		return sum((amount for _, amount in self._each(node, name, base)), Decimal(0))

	def _amount(self, part: Node, base: Decimal) -> Decimal:
		properties = props(part)
		kind, value = properties.string("hs_type"), properties.decimal("hs_value")
		if kind == FIXED:
			return value
		if kind != PERCENT:
			raise Unsupported(f"a {kind} adjustment")
		amount = exact_quotient(base * value, HUNDRED, self.places)
		if amount is None:
			raise Unsupported(
				f"{decimal_text(value)}% of {decimal_text(base)} isn't an exact amount in {self.currency}"
			)
		return amount

	@staticmethod
	def _label(part: Node, name: str) -> str:
		return props(part).optional_string("hs_label") or name.rstrip("s").title()

	def charges(self) -> list[Fields]:
		return [
			*(charge_fields(Charge(FEE, label, amount)) for label, amount in self.fees),
			*(charge_fields(Charge(TAX, label, amount)) for label, amount in self.taxes),
		]

	def trade_taxes(self) -> list[TradeTax]:
		return [
			*(TradeTax(label, amount, role=FEE) for label, amount in self.fees),
			*(TradeTax(label, amount) for label, amount in self.taxes),
		]


class QuoteMapper:
	"""A quote as a draft Quotation to its company (a prospect) or contact (a lead), at its line items,
	discounts, fees and taxes, which must make its amount exactly. A rejected quote isn't kept."""

	kind = Kind.DRAFT_ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		properties = props(node)
		if properties.optional_string("hs_status") == REJECTED:
			return None
		currency = properties.string("hs_currency")
		amounts = Amounts(self.context, node, currency)
		stated = properties.optional_decimal("hs_quote_amount")
		if stated is not None and stated != amounts.total:
			raise Unsupported(
				f"the quote's lines, discounts, fees and taxes come to {decimal_text(amounts.total)}, not its "
				f"amount {decimal_text(stated)}"
			)
		party = billed_party(node)
		if party is None:
			raise Unsupported("a quote associated with no company or contact")
		quoted = (
			Kind.PROSPECT if party[0] is COMPANY else Kind.LEAD,
			self.context.gid(party[0].type, party[1]),
		)
		created = properties.optional_string("hs_createdate") or node.string("createdAt")
		fields: Fields = {
			"number": properties.optional_string("hs_quote_number") or node.string("source_id"),
			"currency": currency,
			"created_at": midday(required_day(created, node.path)),
			"customer": None,
			"quoted": quoted_party(*quoted),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": list[Field]([line_fields(line) for line in amounts.lines]),
			"discount": amounts.discount,
			"charges": list[Field](amounts.charges()),
			"total": amounts.total,
			"remarks": properties.optional_string("hs_title"),
		}
		return Desired(self.kind, node.id(), fields)


class DocumentMapper:
	"""An order (a submitted Sales Order) or an invoice (a Sales Invoice: a draft while HubSpot's is; open or
	paid, submitted; voided, none) to its customer, at its line items, discounts, fees and taxes."""

	version = 1

	def __init__(self, kind: Kind, context: Context, read: Callable[[Node], DocumentTerms | None]) -> None:
		self.kind = kind
		self.context = context
		self._read = read

	def map(self, node: Node) -> Desired | None:
		terms = self._read(node)
		if terms is None:
			return None
		amounts = Amounts(self.context, node, terms.currency)
		document = TradeDocument(
			number=terms.number,
			date=terms.date,
			party=self.context.customer(node, f"{self.kind.value} {terms.number}"),
			currency=terms.currency,
			lines=[TradeLine(line) for line in amounts.lines],
			taxes=amounts.trade_taxes(),
			total=amounts.total,
			submitted=terms.submitted,
			due_date=terms.due_date,
			remarks=terms.remarks,
			discount=amounts.discount or None,
		)
		return Desired(self.kind, node.id(), trade_fields(document))


@dataclass(frozen=True)
class DocumentTerms:
	"""What an order or an invoice says about itself, beyond its amounts."""

	number: str
	date: str
	currency: str
	submitted: bool
	due_date: str | None = None
	remarks: str | None = None


def order_terms(node: Node) -> DocumentTerms | None:
	properties = props(node)
	created = properties.optional_string("hs_createdate") or node.string("createdAt")
	return DocumentTerms(
		number=node.string("source_id"),
		date=required_day(created, node.path),
		currency=properties.string("hs_currency_code"),
		submitted=True,
		remarks=properties.optional_string("hs_order_name"),
	)


def invoice_terms(node: Node) -> DocumentTerms | None:
	properties = props(node)
	status = properties.string("hs_invoice_status")
	if status not in INVOICE_STATUSES:
		raise Unsupported(f'an invoice "{status}"')
	if status == VOIDED:
		return None
	date = properties.optional_string("hs_invoice_date") or node.string("createdAt")
	po = properties.optional_string("hs_purchase_order_number")
	notes = (f"PO {po}" if po else None, properties.optional_string("hs_comments"))
	remarks = "\n".join(part for part in notes if part)
	return DocumentTerms(
		number=properties.optional_string("hs_number") or node.string("source_id"),
		date=required_day(date, node.path),
		currency=properties.string("hs_currency"),
		submitted=status != DRAFT,
		due_date=day(properties.optional_string("hs_due_date")),
		remarks=remarks or None,
	)


class PaymentMapper:
	"""A commerce payment HubSpot collected, from its customer, allocated to the invoice it pays when it pays
	one; what was refunded of it (or lost in a dispute) is a payment out of its own. Processing and failed
	payments move no money."""

	kind = Kind.PAYMENT_ENTRY
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		properties = props(node)
		status = properties.optional_string("hs_latest_status")
		if status not in RECEIVED:
			return None
		refund = Identity.parse(node.id()).type == REFUND
		charged = properties.decimal("hs_initial_amount")
		if refund:
			amount = charged if status == DISPUTE_LOST else properties.optional_decimal("hs_refunds_amount")
			if not amount:
				return None
		else:
			amount = charged
		invoices = associated(node, "invoices")
		reference = properties.optional_string("hs_reference_number") or node.string("source_id")
		payment = Payment(
			payment_type=PAY if refund else RECEIVE,
			party_type="Customer",
			party=self.context.customer(node, "a payment"),
			date=required_day(
				properties.optional_string("hs_initiated_date") or node.string("createdAt"), node.path
			),
			amount=amount,
			currency=properties.string("hs_currency_code"),
			allocations=[Allocation(Kind.SALES_INVOICE, self.context.gid("Invoice", invoices[0]), amount)]
			if len(invoices) == 1 and not refund
			else [],
			reference=f"Refund of {reference}" if refund else reference,
			remarks=properties.optional_string("hs_payment_method_type"),
		)
		return Desired(self.kind, node.id(), payment_fields(payment))


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		OwnerMapper(),
		ProductMapper(),
		CustomerMapper(context),
		LeadMapper(context),
		ProspectMapper(context),
		PipelineMapper(),
		DealMapper(context),
		EngagementMapper(context),
		MeetingMapper(context),
		TaskMapper(context),
		QuoteMapper(context),
		DocumentMapper(Kind.SALES_ORDER, context, order_terms),
		DocumentMapper(Kind.SALES_INVOICE, context, invoice_terms),
		PaymentMapper(context),
	]
