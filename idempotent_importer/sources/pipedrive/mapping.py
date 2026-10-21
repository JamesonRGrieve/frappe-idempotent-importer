# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Pipedrive → desired ERPNext state: users as Employees, products with their variations as Items,
persons as Leads, organizations as Prospects with their persons, the organizations and persons of won deals
as Customers, pipelines as Opportunity Types, deals (and leads, Pipedrive's deals in waiting) as
Opportunities at their stage, done calls and emails, notes and call logs as Communications, meetings and
lunches as Events, every other activity as a ToDo. Every record keeps its custom fields' values, by name.

Field meanings are Pipedrive's OpenAPI documents' (developers.pipedrive.com, v1 and v2). An activity is about
its person (a lead), else its deal or lead (an opportunity), else its organization (a prospect). Times
without a zone (``YYYY-MM-DD HH:MM:SS``, activities' due times) are UTC, as Pipedrive keeps them. What
ERPNext can't hold exactly is Unsupported, naming the card that plans it."""

from __future__ import annotations

from datetime import timedelta

from idempotent_importer.contract import (
	Address,
	Communication,
	ContactPerson,
	Employee,
	Event,
	Lead,
	Opportunity,
	Party,
	Prospect,
	ToDo,
	address_fields,
	communication_fields,
	employee_fields,
	event_fields,
	lead_fields,
	opportunity_fields,
	opportunity_type_fields,
	party_fields,
	prospect_fields,
	todo_fields,
	variant_attributes,
	with_custom,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Fields
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.core.times import format_utc, iso_utc, parse_utc
from idempotent_importer.sources.crm import custom_of, plain_text
from idempotent_importer.sources.ledger import book_item, country_code, day, required_day

PLANNED = "planned: card 386"
ONE_TIME = "one-time"
OPPORTUNITY_STATUSES = {"open": "Open", "won": "Converted", "lost": "Lost"}
MESSAGES = {"call": "Phone", "email": "Email"}
MEETINGS = frozenset({"meeting", "lunch"})
SUBJECT_LENGTH = 140
VARIATION = "Variation"


class Context:
	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config

	def gid(self, type_: str, id_: int | str) -> str:
		return self.config.identity(type_, id_)

	def parties(self, node: Node, organisation: str = "org_id") -> list[tuple[Kind, str]]:
		"""Who a record is about: its person (a lead), its deal or lead (opportunities), its organization."""
		found: list[tuple[Kind, str]] = []
		for key, kind, type_ in (
			("person_id", Kind.LEAD, "Person"),
			("deal_id", Kind.OPPORTUNITY, "Deal"),
			("lead_id", Kind.OPPORTUNITY, "Lead"),
			(organisation, Kind.PROSPECT, "Organization"),
		):
			value = node.get(key)
			if value is not None and value not in ("", 0):
				found.append((kind, self.gid(type_, str(value))))
		return found

	def about(self, node: Node, what: str) -> tuple[Kind, str]:
		found = self.parties(node)
		if not found:
			raise Unsupported(f"{what} is about no person, deal, lead or organization")
		return found[0]


def address_of(address: Node | None, name: str, company: str | None) -> Fields | None:
	"""A Pipedrive address (its parts as Google's geocoder names them), when it has a street and a city."""
	if address is None:
		return None
	street = " ".join(
		part for part in (address.optional_string("street_number"), address.optional_string("route")) if part
	)
	line1 = street or address.optional_string("value")
	city = address.optional_string("locality") or address.optional_string("admin_area_level_2")
	if not (line1 and city):
		return None
	return address_fields(
		Address(
			"Billing",
			name,
			company,
			line1,
			address.optional_string("subpremise"),
			city,
			address.optional_string("admin_area_level_1"),
			address.optional_string("postal_code"),
			country_code(address.optional_string("country")),
			None,
			None,
		)
	)


def primary(node: Node, field: str) -> str | None:
	"""A person's primary email or phone (else the first)."""
	values = node.children(field)
	chosen = next(
		(value for value in values if value.optional_flag("primary")), values[0] if values else None
	)
	return chosen.optional_string("value") if chosen else None


def person_of(context: Context, person: Node) -> ContactPerson:
	return ContactPerson(
		gid=context.gid("Person", person.integer("id")),
		name=person.optional_string("name") or primary(person, "emails") or str(person.integer("id")),
		email=primary(person, "emails"),
		phone=primary(person, "phones"),
		job_title=person.optional_string("job_title"),
	)


class UserMapper:
	"""A user: name, email and phone; a deactivated or deleted user is inactive."""

	kind = Kind.EMPLOYEE
	version = 1

	def map(self, node: Node) -> Desired | None:
		name = node.optional_string("name") or node.optional_string("email")
		if name is None:
			raise Unsupported("a user with no name or email")
		first, _, last = name.partition(" ")
		active = (
			node.optional_flag("active_flag") is not False and node.optional_flag("is_deleted") is not True
		)
		employee = Employee(
			first_name=first,
			status="Active" if active else "Inactive",
			last_name=last or None,
			company_email=node.optional_string("email"),
			phone=node.optional_string("phone"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


def one_price(prices: list[Node], what: str) -> Node | None:
	if len(prices) > 1:
		raise Unsupported(f"{what} has prices in {len(prices)} currencies ({PLANNED}: price lists)")
	return prices[0] if prices else None


class ProductMapper:
	"""A product at its price, with a variant per variation (``Variation``: its name) at its own. A product
	billed on a schedule is a subscription plan (planned); a deleted product isn't kept."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("is_deleted"):
			return None
		frequency = node.optional_string("billing_frequency") or ONE_TIME
		if frequency != ONE_TIME:
			raise Unsupported(f"a product billed {frequency} is a subscription plan ({PLANNED})")
		name = node.string("name")
		variations = node.children("variations")
		if variations:
			variants: list[Fields] = []
			for variation in variations:
				price = one_price(variation.children("prices"), f"variation {variation.string('name')}")
				variants.append(
					{
						"gid": self.context.gid("ProductVariation", variation.integer("id")),
						"sku": None,
						"barcode": None,
						"attributes": {VARIATION: variation.string("name")},
						"price": price.optional_decimal("price") if price else None,
						"sale": None,
						"stock_qty": None,
						"weight": None,
						"image": None,
						"enabled": True,
					}
				)
			fields = book_item(
				name,
				None,
				node.optional_string("description"),
				None,
				False,
				variants=variants,
				attributes=variant_attributes(variants),
			)
		else:
			price = one_price(node.children("prices"), name)
			fields = book_item(
				name,
				node.optional_string("code"),
				node.optional_string("description"),
				price.optional_decimal("price") if price else None,
				False,
			)
		return Desired(self.kind, node.id(), with_custom(fields, custom_of(node)))


class CustomerMapper:
	"""An organization a won deal is with (a company, its persons as its contacts), or the person of one
	without an organization."""

	kind = Kind.CUSTOMER
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		name = node.string("name")
		if Identity.parse(node.id()).type == "Person":
			address = address_of(node.child("postal_address"), name, None)
			party = Party(
				name=name,
				type="Individual",
				first_name=node.optional_string("first_name"),
				last_name=node.optional_string("last_name"),
				email=primary(node, "emails"),
				phone=primary(node, "phones"),
				addresses=[address] if address else [],
				custom=custom_of(node),
			)
		else:
			address = address_of(node.child("address"), name, name)
			party = Party(
				name=name,
				type="Company",
				company=name,
				addresses=[address] if address else [],
				contacts=[person_of(self.context, person) for person in node.children("persons")],
				custom=custom_of(node),
			)
		return Desired(self.kind, node.id(), party_fields(party))


class PersonMapper:
	"""A person as a lead: names, organization, email, phone, job title and notes."""

	kind = Kind.LEAD
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("is_deleted"):
			return None
		lead = Lead(
			status="Lead",
			first_name=node.optional_string("first_name") or node.optional_string("name"),
			last_name=node.optional_string("last_name"),
			email=primary(node, "emails"),
			phone=primary(node, "phones"),
			job_title=node.optional_string("job_title"),
			notes=node.optional_string("notes"),
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), lead_fields(lead))


class OrganizationMapper:
	"""An organization of the pipeline, with its persons as its leads, its address and website."""

	kind = Kind.PROSPECT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("is_deleted"):
			return None
		name = node.string("name")
		prospect = Prospect(
			name=name,
			website=node.optional_string("website"),
			address=address_of(node.child("address"), name, name),
			leads=[self.context.gid("Person", person.integer("id")) for person in node.children("persons")],
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), prospect_fields(prospect))


class PipelineMapper:
	kind = Kind.OPPORTUNITY_TYPE
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("is_deleted"):
			return None
		return Desired(self.kind, node.id(), opportunity_type_fields(node.string("name")))


class DealMapper:
	"""A deal with its organization (a prospect) or person (a lead), in its pipeline at its stage: open, won
	(Converted) or lost; archived and open, Closed; its value, probability (else its stage's) and expected
	close date. A lead (Pipedrive's deal in waiting) is an open opportunity at no stage, archived Closed."""

	kind = Kind.OPPORTUNITY
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("is_deleted"):
			return None
		lead = Identity.parse(node.id()).type == "Lead"
		organisation = "organization_id" if lead else "org_id"
		parties = [
			(kind, gid)
			for kind, gid in self.context.parties(node, organisation)
			if kind in (Kind.PROSPECT, Kind.LEAD)
		]
		party = next((p for p in parties if p[0] is Kind.PROSPECT), parties[0] if parties else None)
		if party is None:
			raise Unsupported("a deal with no organization or person")
		archived = node.optional_flag("is_archived") is True
		if lead:
			value = node.child("value")
			opportunity = Opportunity(
				party_kind=party[0],
				party=party[1],
				title=node.string("title"),
				status="Closed" if archived else "Open",
				date=required_day(node.string("add_time"), node.path),
				currency=value.optional_string("currency") if value else None,
				amount=value.optional_decimal("amount") if value else None,
				expected_closing=day(node.optional_string("expected_close_date")),
				custom=custom_of(node),
			)
			return Desired(self.kind, node.id(), opportunity_fields(opportunity))
		status = node.string("status")
		if status not in OPPORTUNITY_STATUSES:
			return None
		stage = node.child("stage")
		probability = node.optional_decimal("probability")
		if probability is None and stage is not None:
			probability = stage.optional_decimal("deal_probability")
		opportunity = Opportunity(
			party_kind=party[0],
			party=party[1],
			title=node.string("title"),
			status="Closed" if archived and status == "open" else OPPORTUNITY_STATUSES[status],
			date=required_day(node.string("add_time"), node.path),
			currency=node.optional_string("currency"),
			amount=node.optional_decimal("value"),
			probability=probability,
			expected_closing=day(node.optional_string("expected_close_date")),
			notes=node.optional_string("lost_reason"),
			opportunity_type=self.context.gid("Pipeline", node.integer("pipeline_id")),
			sales_stage=stage.string("name") if stage else None,
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), opportunity_fields(opportunity))


def activity_start(node: Node) -> str:
	"""An activity's due date and time (UTC, as Pipedrive keeps them): a UTC time, or its day."""
	due, at = node.string("due_date"), node.optional_string("due_time")
	if at:
		with_seconds = at if len(at) == len("HH:MM:SS") else f"{at}:00"
		time_ = iso_utc(f"{due}T{with_seconds}Z")
		if time_ is None:
			raise Unsupported(f"an activity due at {due} {at}")
		return time_
	return f"{required_day(due, node.path)} 00:00:00"


class MessageMapper:
	"""A done call or email activity, a note or a call log, on the person (else deal, lead, organization) it
	is about."""

	kind = Kind.COMMUNICATION
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		match Identity.parse(node.id()).type:
			case "Note":
				return self._note(node)
			case "CallLog":
				return self._call_log(node)
		medium = MESSAGES.get(node.optional_string("type") or "")
		if medium is None or not node.optional_flag("done") or node.optional_flag("is_deleted"):
			return None
		party_kind, party = self.context.about(node, "an activity")
		subject = node.optional_string("subject") or medium
		content = "\n\n".join(
			part
			for part in (plain_text(node.optional_string("note")), node.optional_string("public_description"))
			if part
		)
		message = Communication(
			party_kind=party_kind,
			party=party,
			medium=medium,
			sent=True,
			date=node.optional_time("marked_as_done_time") or self._due(node),
			subject=subject[:SUBJECT_LENGTH],
			content=content or subject,
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), communication_fields(message))

	@staticmethod
	def _due(node: Node) -> str:
		start = activity_start(node)
		return start if start.endswith("Z") else f"{start[:10]}T00:00:00Z"

	def _note(self, node: Node) -> Desired | None:
		if node.optional_flag("active_flag") is False:
			return None
		party_kind, party = self.context.about(node, "a note")
		text = plain_text(node.optional_string("content")) or "Note"
		message = Communication(
			party_kind=party_kind,
			party=party,
			medium="Other",
			sent=True,
			date=node.time("add_time"),
			subject=text.splitlines()[0][:SUBJECT_LENGTH],
			content=text,
		)
		return Desired(self.kind, node.id(), communication_fields(message))

	def _call_log(self, node: Node) -> Desired:
		party_kind, party = self.context.about(node, "a call log")
		subject = node.optional_string("subject") or "Call"
		details = ", ".join(
			part
			for part in (
				f"{node.optional_string('duration')} seconds" if node.optional_string("duration") else None,
				node.optional_string("outcome"),
			)
			if part
		)
		message = Communication(
			party_kind=party_kind,
			party=party,
			medium="Phone",
			sent=True,
			date=node.time("start_time"),
			subject=subject[:SUBJECT_LENGTH],
			content="\n".join(part for part in (node.optional_string("note"), details) if part) or subject,
			phone=node.optional_string("to_phone_number"),
		)
		return Desired(self.kind, node.id(), communication_fields(message))


class MeetingMapper:
	"""A meeting or lunch, at its due time (UTC) for its duration, done Completed, with its persons, deal,
	lead and organization."""

	kind = Kind.EVENT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_string("type") not in MEETINGS or node.optional_flag("is_deleted"):
			return None
		start = activity_start(node)
		participants = self.context.parties(node)
		for attendee in node.children("participants"):
			person = attendee.optional_integer("person_id")
			if person is not None and (Kind.LEAD, self.context.gid("Person", person)) not in participants:
				participants.append((Kind.LEAD, self.context.gid("Person", person)))
		location = node.child("location")
		description = "\n\n".join(
			part
			for part in (
				plain_text(node.optional_string("note")),
				node.optional_string("public_description"),
				location.optional_string("value") if location else None,
				node.optional_string("conference_meeting_url"),
			)
			if part
		)
		event = Event(
			subject=(node.optional_string("subject") or "Meeting")[:SUBJECT_LENGTH],
			starts_on=start,
			ends_on=ends(start, node.optional_string("duration")),
			status="Completed" if node.optional_flag("done") else "Open",
			description=description or None,
			participants=participants,
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), event_fields(event))


def ends(start: str, duration: str | None) -> str | None:
	"""An event's end: its UTC start plus its ``HH:MM`` duration."""
	if not duration or not start.endswith("Z"):
		return None
	hours, _, minutes = duration.partition(":")
	return format_utc(parse_utc(start) + timedelta(hours=int(hours), minutes=int(minutes or 0)))


class TaskMapper:
	"""Any other activity (a task, a deadline, a call or email not yet done, the account's own types) about
	its person, deal, lead or organization, due on its day; done is Closed."""

	kind = Kind.TODO
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		kind = node.optional_string("type") or ""
		if (
			node.optional_flag("is_deleted")
			or kind in MEETINGS
			or (kind in MESSAGES and node.optional_flag("done"))
		):
			return None
		party_kind, party = self.context.about(node, "an activity")
		category = node.optional_string("category")
		description = "\n\n".join(
			part
			for part in (
				f"{category}: {node.optional_string('subject') or ''}".strip()
				if category
				else node.optional_string("subject"),
				plain_text(node.optional_string("note")),
			)
			if part
		)
		todo = ToDo(
			description=description or "Activity",
			status="Closed" if node.optional_flag("done") else "Open",
			party_kind=party_kind,
			party=party,
			date=day(node.optional_string("due_date")),
			custom=custom_of(node),
		)
		return Desired(self.kind, node.id(), todo_fields(todo))


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		UserMapper(),
		ProductMapper(context),
		CustomerMapper(context),
		PersonMapper(),
		OrganizationMapper(context),
		PipelineMapper(),
		DealMapper(context),
		MessageMapper(context),
		MeetingMapper(context),
		TaskMapper(context),
	]
