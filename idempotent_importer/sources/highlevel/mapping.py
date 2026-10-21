# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure HighLevel → desired ERPNext state: users as Employees, products as Items at their one-time
prices (a price per variant option set as an item variant), contacts as Leads with their notes (and the
billed ones as Customers), businesses as Prospects with their people, pipelines as Opportunity Types,
opportunities, conversation messages as Communications, appointments as Events, tasks as ToDos, invoices
as Sales Invoices and payment transactions (and their refunds) as Payment Entries. Field meanings are
HighLevel's OpenAPI documents' (GoHighLevel/highlevel-api-docs, apps/*.json). Amounts are as HighLevel
states them, in the record's currency. What ERPNext can't hold exactly is Unsupported, naming the card that
plans it."""

from __future__ import annotations

from decimal import Decimal

from idempotent_importer.contract import (
	PAY,
	RECEIVE,
	Address,
	Allocation,
	Communication,
	Employee,
	Event,
	Lead,
	Note,
	Opportunity,
	Party,
	Payment,
	Prospect,
	ToDo,
	TradeDocument,
	TradeLine,
	TradeTax,
	address_fields,
	communication_fields,
	employee_fields,
	event_fields,
	lead_fields,
	opportunity_fields,
	opportunity_type_fields,
	order_line,
	party_fields,
	payment_fields,
	prospect_fields,
	todo_fields,
	trade_fields,
	variant_attributes,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.highlevel.reader import CONTACT
from idempotent_importer.sources.ledger import book_item, country_code, day, required_day

PLANNED = "planned: card 386"
ONE_TIME, RECURRING = "one_time", "recurring"
PHYSICAL = "PHYSICAL"
SUCCEEDED = "succeeded"
INVOICE_ENTITY = "invoice"
DRAFT, VOID = "draft", "void"
OPPORTUNITY_STATUSES = {"open": "Open", "won": "Converted", "lost": "Lost", "abandoned": "Closed"}
APPOINTMENT_STATUSES = {
	"new": "Open",
	"booked": "Open",
	"confirmed": "Open",
	"showed": "Completed",
	"noshow": "Closed",
	"cancelled": "Cancelled",
	"invalid": "Cancelled",
}
MEDIUMS = {
	"TYPE_EMAIL": "Email",
	"TYPE_CAMPAIGN_EMAIL": "Email",
	"TYPE_CUSTOM_EMAIL": "Email",
	"TYPE_CUSTOM_PROVIDER_EMAIL": "Email",
	"TYPE_SMS": "SMS",
	"TYPE_RCS": "SMS",
	"TYPE_CAMPAIGN_SMS": "SMS",
	"TYPE_CAMPAIGN_MANUAL_SMS": "SMS",
	"TYPE_CUSTOM_SMS": "SMS",
	"TYPE_CUSTOM_PROVIDER_SMS": "SMS",
	"TYPE_SMS_REVIEW_REQUEST": "SMS",
	"TYPE_SMS_NO_SHOW_REQUEST": "SMS",
	"TYPE_SMS_REACTION": "SMS",
	"TYPE_CALL": "Phone",
	"TYPE_CAMPAIGN_CALL": "Phone",
	"TYPE_CAMPAIGN_MANUAL_CALL": "Phone",
	"TYPE_CAMPAIGN_VOICEMAIL": "Phone",
	"TYPE_IVR_CALL": "Phone",
	"TYPE_CUSTOM_CALL": "Phone",
	"TYPE_WEBCHAT": "Chat",
	"TYPE_LIVE_CHAT": "Chat",
	"TYPE_FACEBOOK": "Chat",
	"TYPE_INSTAGRAM": "Chat",
	"TYPE_WHATSAPP": "Chat",
	"TYPE_GMB": "Chat",
	"TYPE_TIKTOK": "Chat",
}
SUBJECT_LENGTH = 140


class Context:
	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config

	def gid(self, type_: str, id_: str) -> str:
		return self.config.identity(type_, id_)

	def contact(self, contact_id: str | None, what: str) -> str:
		if not contact_id:
			raise Unsupported(f"{what} names no contact")
		return self.config.identity(CONTACT, contact_id)


def name_of(node: Node) -> tuple[str | None, str | None]:
	return node.optional_string("firstName"), node.optional_string("lastName")


class UserMapper:
	"""A user of the sub-account: names, email and phone; a deleted user is inactive (HighLevel keeps no
	date, gender or birth date of a user, and ERPNext's Left needs the day they left)."""

	kind = Kind.EMPLOYEE
	version = 1

	def map(self, node: Node) -> Desired | None:
		first, last = name_of(node)
		first = first or node.optional_string("name")
		if first is None:
			raise Unsupported("a user with no name")
		employee = Employee(
			first_name=first,
			status="Inactive" if node.optional_flag("deleted") else "Active",
			last_name=last,
			company_email=node.optional_string("email"),
			phone=node.optional_string("phone"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class ProductMapper:
	"""A product at its one-time price, or with a variant per one-time price of a set of its variant
	options. A recurring price is a subscription plan (planned); several one-time prices of a product
	without variants are several price lists (planned)."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		prices = node.children("prices")
		one_time = [price for price in prices if price.optional_string("type") == ONE_TIME]
		warnings = (
			(f"its recurring prices are subscription plans ({PLANNED})",)
			if any(price.optional_string("type") == RECURRING for price in prices)
			else ()
		)
		options = {
			option.string("id"): (variant.string("name"), option.string("name"))
			for variant in node.children("variants")
			for option in variant.children("options")
		}
		physical = node.optional_string("productType") == PHYSICAL
		name = node.string("name")
		if options:
			variants = [self._variant(price, options, physical) for price in one_time]
			fields = book_item(
				name,
				None,
				node.optional_string("description"),
				None,
				physical,
				None,
				[],
				variants,
				variant_attributes(variants),
			)
		else:
			if len(one_time) > 1:
				raise Unsupported(
					f"{len(one_time)} one-time prices without variants ({PLANNED}: price lists)"
				)
			price = one_time[0] if one_time else None
			fields = book_item(
				name,
				None,
				node.optional_string("description"),
				price.decimal("amount") if price else None,
				physical,
				stock(price) if price and physical else None,
			)
		image = node.optional_string("image")
		fields |= {"images": [{"url": image, "alt": name}] if image else [], "published": False}
		return Desired(self.kind, node.id(), fields, warnings=warnings)

	def _variant(self, price: Node, options: dict[str, tuple[str, str]], physical: bool) -> Fields:
		chosen = price.strings("variantOptionIds")
		unknown = [option for option in chosen if option not in options]
		if unknown:
			raise SourceError(f"{price.path} names variant options its product doesn't have: {unknown}")
		attributes: dict[str, Field] = {options[option][0]: options[option][1] for option in chosen}
		return {
			"gid": self.context.gid("Price", price.string("_id")),
			"sku": None,
			"barcode": None,
			"attributes": attributes,
			"price": price.decimal("amount"),
			"sale": None,
			"stock_qty": stock(price) if physical else None,
			"weight": None,
			"image": None,
			"enabled": True,
		}


def stock(price: Node) -> Decimal | None:
	"""A price's available quantity, when HighLevel tracks its inventory."""
	return price.optional_decimal("availableQuantity") if price.optional_flag("trackInventory") else None


def contact_address(
	node: Node, name: str, street: str = "address1", postcode: str = "postalCode"
) -> Fields | None:
	line1, city = node.optional_string(street), node.optional_string("city")
	if not (line1 and city):
		return None
	return address_fields(
		Address(
			"Billing",
			name,
			node.optional_string("companyName"),
			line1,
			None,
			city,
			node.optional_string("state"),
			node.optional_string(postcode),
			country_code(node.optional_string("country")),
			None,
			None,
		)
	)


class LeadMapper:
	"""A contact as a lead: names, organisation, email, phone, website and their notes; one asking not to
	be disturbed (``dnd``) is Do Not Contact."""

	kind = Kind.LEAD
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		first, last = name_of(node)
		lead = Lead(
			status="Do Not Contact" if node.optional_flag("dnd") else "Lead",
			first_name=first,
			last_name=last,
			company=node.optional_string("companyName"),
			email=node.optional_string("email"),
			phone=node.optional_string("phone"),
			website=node.optional_string("website"),
			crm_notes=[
				Note(
					self.context.gid("Note", note.string("id")),
					note.optional_time("dateAdded"),
					"\n\n".join(
						part for part in (note.optional_string("title"), note.optional_string("body")) if part
					),
				)
				for note in node.children("notes")
			],
		)
		return Desired(self.kind, node.id(), lead_fields(lead))


class CustomerMapper:
	"""A contact HighLevel bills, as a customer: a person (or, without names, their company) with their
	email, phone and address."""

	kind = Kind.CUSTOMER
	version = 1

	def map(self, node: Node) -> Desired | None:
		first, last = name_of(node)
		company = node.optional_string("companyName")
		person = " ".join(part for part in (first, last) if part) or node.optional_string("name")
		name = person or company or node.optional_string("email")
		if name is None:
			raise Unsupported("a contact with no name, company or email")
		address = contact_address(node, name)
		party = Party(
			name=name,
			type="Individual" if person else "Company",
			first_name=first,
			last_name=last,
			company=company,
			email=node.optional_string("email"),
			phone=node.optional_string("phone"),
			addresses=[address] if address else [],
		)
		return Desired(self.kind, node.id(), party_fields(party))


class ProspectMapper:
	"""A business, with how to reach it, its address and description, and its contacts as its leads."""

	kind = Kind.PROSPECT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		name = node.string("name")
		prospect = Prospect(
			name=name,
			website=node.optional_string("website"),
			email=node.optional_string("email"),
			phone=node.optional_string("phone"),
			address=contact_address(node, name, street="address"),
			notes=node.optional_string("description"),
			leads=[self.context.gid(CONTACT, contact) for contact in node.strings("contact_ids")],
		)
		return Desired(self.kind, node.id(), prospect_fields(prospect))


class PipelineMapper:
	"""A pipeline, as the type of its opportunities. Its stages' shape is undocumented (planned)."""

	kind = Kind.OPPORTUNITY_TYPE
	version = 1

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), opportunity_type_fields(node.string("name")))


class OpportunityMapper:
	"""An opportunity with its contact (a lead), in its pipeline, at its monetary value."""

	kind = Kind.OPPORTUNITY
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		status = node.string("status")
		if status not in OPPORTUNITY_STATUSES:
			raise Unsupported(f'an opportunity "{status}"')
		pipeline = node.optional_string("pipelineId")
		opportunity = Opportunity(
			party_kind=Kind.LEAD,
			party=self.context.contact(node.optional_string("contactId"), "the opportunity"),
			title=node.optional_string("name") or node.string("source_id"),
			status=OPPORTUNITY_STATUSES[status],
			date=required_day(node.optional_string("createdAt"), node.path),
			amount=node.optional_decimal("monetaryValue"),
			opportunity_type=self.context.gid("Pipeline", pipeline) if pipeline else None,
		)
		return Desired(self.kind, node.id(), opportunity_fields(opportunity))


class MessageMapper:
	"""A message of a conversation with a contact: its medium, direction and time, its body (a call's
	duration and status; its attachments' addresses)."""

	kind = Kind.COMMUNICATION
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		message_type = node.string("messageType")
		medium = MEDIUMS.get(message_type, "Other")
		body = node.optional_string("body")
		meta = node.child("meta")
		call = (
			", ".join(
				part
				for part in (
					f"{meta.optional_string('callDuration')} seconds" if meta.has("callDuration") else None,
					meta.optional_string("callStatus"),
				)
				if part
			)
			if meta
			else ""
		)
		attachments = node.strings("attachments")
		content = "\n".join(part for part in (body, call, *attachments) if part) or message_type
		subject = (
			(body or "").strip().splitlines()[0] if body and body.strip() else f"{medium} {message_type}"
		)
		message = Communication(
			party_kind=Kind.LEAD,
			party=self.context.contact(node.optional_string("contactId"), "the message"),
			medium=medium,
			sent=node.optional_string("direction") == "outbound",
			date=node.time("dateAdded"),
			subject=subject[:SUBJECT_LENGTH],
			content=content,
			phone=node.optional_string("conversationPhone") if medium in ("SMS", "Phone") else None,
			email=node.optional_string("conversationEmail") if medium == "Email" else None,
		)
		return Desired(self.kind, node.id(), communication_fields(message))


class AppointmentMapper:
	"""A contact's appointment, at its times as HighLevel's calendar shows them, with its status and
	notes."""

	kind = Kind.EVENT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		status = node.optional_string("appointmentStatus") or node.optional_string("status") or "booked"
		if status not in APPOINTMENT_STATUSES:
			raise Unsupported(f'an appointment "{status}"')
		event = Event(
			subject=node.optional_string("title") or "Appointment",
			starts_on=node.string("startTime"),
			ends_on=node.optional_string("endTime"),
			status=APPOINTMENT_STATUSES[status],
			description="\n".join(
				part for part in (node.optional_string("notes"), node.optional_string("address")) if part
			)
			or None,
			participants=[
				(Kind.LEAD, self.context.contact(node.optional_string("contactId"), "the appointment"))
			],
		)
		return Desired(self.kind, node.id(), event_fields(event))


class TaskMapper:
	kind = Kind.TODO
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		description = "\n\n".join(
			part for part in (node.optional_string("title"), node.optional_string("body")) if part
		)
		todo = ToDo(
			description=description or "Task",
			status="Closed" if node.optional_flag("completed") else "Open",
			party_kind=Kind.LEAD,
			party=self.context.contact(node.optional_string("contactId"), "the task"),
			date=day(node.optional_string("dueDate")),
		)
		return Desired(self.kind, node.id(), todo_fields(todo))


class InvoiceMapper:
	"""An invoice to its contact (a customer): its items at their amounts, its discount and tax as its total
	summary states them, which must make its total exactly. A draft stays a draft; a void or test-mode
	invoice isn't kept."""

	kind = Kind.SALES_INVOICE
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		status = node.string("status")
		if status == VOID or node.optional_flag("liveMode") is False:
			return None
		currency = node.string("currency")
		lines = [
			TradeLine(
				order_line(
					item.optional_string("_id") or str(index),
					self._product(item.optional_string("productId")),
					self._price(item.optional_string("priceId")),
					item.string("name"),
					item.decimal("qty"),
					item.decimal("amount") * item.decimal("qty"),
					currency,
				)
			)
			for index, item in enumerate(node.children("invoiceItems"))
		]
		summary = node.required_child("totalSummary")
		tax = summary.decimal("tax")
		details = node.required_child("contactDetails")
		document = TradeDocument(
			number=str(node.integer("invoiceNumber"))
			if node.has("invoiceNumber")
			else node.string("source_id"),
			date=required_day(node.string("issueDate"), node.path),
			due_date=day(node.optional_string("dueDate")),
			party=self.context.contact(details.optional_string("id"), "the invoice"),
			currency=currency,
			lines=lines,
			taxes=[TradeTax("Tax", tax)] if tax else [],
			total=node.decimal("total"),
			submitted=status != DRAFT,
			remarks=node.optional_string("title") or node.optional_string("name"),
			discount=summary.optional_decimal("discount") or None,
		)
		return Desired(self.kind, node.id(), trade_fields(document))

	def _product(self, product: str | None) -> str | None:
		return self.context.gid("Product", product) if product else None

	def _price(self, price: str | None) -> str | None:
		return self.context.gid("Price", price) if price else None


class TransactionMapper:
	"""A succeeded live payment from a contact (a customer), allocated to the invoice it pays when its
	entity is one; what was refunded of it is a payment out of its own. Pending, failed and test-mode
	transactions move no money."""

	kind = Kind.PAYMENT_ENTRY
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_string("status") != SUCCEEDED or node.optional_flag("liveMode") is False:
			return None
		refund = node.optional_flag("refund") is True
		amount = node.decimal("amountRefunded") if refund else node.decimal("amount")
		invoice = (
			node.optional_string("entityId") if node.optional_string("entityType") == INVOICE_ENTITY else None
		)
		reference = node.optional_string("chargeId") or node.string("source_id")
		payment = Payment(
			payment_type=PAY if refund else RECEIVE,
			party_type="Customer",
			party=self.context.contact(node.optional_string("contactId"), "the transaction"),
			date=required_day(
				node.optional_string("fulfilledAt") or node.optional_string("createdAt"), node.path
			),
			amount=amount,
			currency=node.string("currency"),
			allocations=[Allocation(Kind.SALES_INVOICE, self.context.gid("Invoice", invoice), amount)]
			if invoice and not refund
			else [],
			reference=f"Refund of {reference}" if refund else reference,
			remarks=node.optional_string("entitySourceName"),
		)
		return Desired(self.kind, node.id(), payment_fields(payment))


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		UserMapper(),
		ProductMapper(context),
		CustomerMapper(),
		LeadMapper(context),
		ProspectMapper(context),
		PipelineMapper(),
		OpportunityMapper(context),
		MessageMapper(context),
		AppointmentMapper(context),
		TaskMapper(context),
		InvoiceMapper(context),
		TransactionMapper(context),
	]
