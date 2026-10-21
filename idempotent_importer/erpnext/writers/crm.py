# SPDX-License-Identifier: AGPL-3.0-or-later
"""A source's sales pipeline in ERPNext's CRM: Leads (the person, their organisation, how to reach them and
their notes), Prospects (the organisations, with their Contact, Address and leads), Opportunity Types (a
source's pipelines, named with the connection's abbreviation), Opportunities with a lead, a customer or a
prospect, at their pipeline stage's Sales Stage (named the same way); and the conversations around them as
Communications, appointments as Events and tasks as ToDos, each linked to its lead, customer, prospect or
opportunity. Each keeps the values of the source's own fields in its Source Fields.

Nothing here sends anything: a Communication is recorded, never sent, and mail is muted for every write.
Retiring disables a lead, closes an opportunity, cancels an event or a task and marks a communication; a
prospect and an opportunity type have no disabled state and stay."""

from __future__ import annotations

from typing import Any

import frappe

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import money, system_datetime
from idempotent_importer.erpnext.writers.base import Writer, mark_retired, set_custom
from idempotent_importer.erpnext.writers.parties import ReachableWriter

LEAD, OPPORTUNITY, CUSTOMER, PROSPECT = "Lead", "Opportunity", "Customer", "Prospect"
SALES_STAGE = "Sales Stage"
OPPORTUNITY_TYPE, COMMUNICATION, EVENT, TODO = "Opportunity Type", "Communication", "Event", "ToDo"
CLOSED, CANCELLED = "Closed", "Cancelled"
TITLE_LENGTH = 140
SUBJECT_LENGTH = 140
PARTY_DOCTYPES = {
	Kind.LEAD.value: (Kind.LEAD, LEAD),
	Kind.CUSTOMER.value: (Kind.CUSTOMER, CUSTOMER),
	Kind.PROSPECT.value: (Kind.PROSPECT, PROSPECT),
	Kind.OPPORTUNITY.value: (Kind.OPPORTUNITY, OPPORTUNITY),
}


def crm_notes(values: Values) -> list[dict[str, Any]]:
	"""A record's CRM Note rows: its own note, then each note the source keeps on it, when written."""
	notes = values.optional_string("notes")
	rows: list[dict[str, Any]] = [{"note": notes}] if notes else []
	for note in values.records("crm_notes"):
		date = note.optional_string("date")
		rows.append({"note": note.string("text"), "added_on": system_datetime(date) if date else None})
	return rows


def on_site_clock(time: str | None) -> Any:
	"""An event's time as the site shows it: as the source's clock showed it, or a UTC time converted."""
	if time is None or not time.endswith("Z"):
		return time
	return system_datetime(time)


class PartyLinked(Writer):
	"""A writer whose record is about a lead, a customer, a prospect or an opportunity (``{kind, party}``)."""

	def party(self, party: Values) -> tuple[str, str]:
		kind, doctype = PARTY_DOCTYPES[party.string("kind")]
		return doctype, self.dependency(kind, party.string("party"))


class LeadWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = self.existing(link) or frappe.new_doc(LEAD)
		doc.update(
			{
				"company": self.context.company,
				"status": values.string("status"),
				"first_name": values.optional_string("first_name"),
				"last_name": values.optional_string("last_name"),
				"company_name": values.optional_string("company"),
				"email_id": values.optional_string("email"),
				"phone": values.optional_string("phone"),
				"mobile_no": values.optional_string("mobile"),
				"job_title": values.optional_string("job_title"),
				"website": values.optional_string("website"),
				"disabled": int(bool(values.all().get("disabled"))),
			}
		)
		doc.set("notes", crm_notes(values))
		set_custom(doc, values)
		doc.save(ignore_permissions=True)
		return Target(LEAD, doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value(LEAD, link.target.name, "disabled", 1)


class ProspectWriter(ReachableWriter):
	"""An organisation, named with the connection's abbreviation, with its Contact and Address, its people as
	its leads, and the people a CRM keeps at it as its Contacts."""

	doctype = PROSPECT

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		name = self.context.suffixed(values.string("name"))
		doc = self.existing(link)
		if doc is not None and doc.name != name:
			frappe.rename_doc(PROSPECT, doc.name, name, force=True)
			doc = frappe.get_doc(PROSPECT, name)
		if doc is None:
			doc = frappe.new_doc(PROSPECT)
			doc.company_name = name
		doc.update({"company": self.context.company, "website": values.optional_string("website")})
		doc.set("notes", crm_notes(values))
		doc.set("leads", [{"lead": self.dependency(Kind.LEAD, lead)} for lead in values.strings("leads")])
		set_custom(doc, values)
		doc.save(ignore_permissions=True)
		self._contact(desired.gid, doc.name, values)
		self._people(desired.gid, doc.name, values.records("contacts"))
		self._addresses(desired.gid, doc.name, values)
		return Target(PROSPECT, doc.name)

	def retire(self, link: Link) -> None:
		"""Nothing to switch off: a Prospect has no disabled state."""


class OpportunityTypeWriter(Writer):
	"""A source's pipeline, named with the connection's abbreviation."""

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		name = self.context.suffixed(values.string("name"))
		doc = self.existing(link)
		if doc is not None and doc.name != name:
			frappe.rename_doc(OPPORTUNITY_TYPE, doc.name, name, force=True)
			doc = frappe.get_doc(OPPORTUNITY_TYPE, name)
		doc = doc or frappe.new_doc(OPPORTUNITY_TYPE)
		doc.description = values.optional_string("description")
		self.save(doc, name)
		return Target(OPPORTUNITY_TYPE, name)

	def retire(self, link: Link) -> None:
		"""Nothing to switch off: an Opportunity Type has no disabled state."""


class OpportunityWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		kind, doctype = PARTY_DOCTYPES[values.string("party_kind")]
		doc = self.existing(link) or frappe.new_doc(OPPORTUNITY)
		amount = values.optional_decimal("amount")
		probability = values.optional_decimal("probability")
		opportunity_type = values.optional_string("opportunity_type")
		doc.update(
			{
				"company": self.context.company,
				"opportunity_from": doctype,
				"party_name": self.dependency(kind, values.string("party")),
				"title": values.string("title")[:TITLE_LENGTH],
				"status": values.string("status"),
				"transaction_date": values.string("date"),
				"expected_closing": values.optional_string("expected_closing"),
				"currency": values.optional_string("currency") or self.context.currency,
				"opportunity_amount": money(amount) if amount is not None else 0,
				"probability": float(probability) if probability is not None else 0,
			}
		)
		if opportunity_type:
			doc.opportunity_type = self.dependency(Kind.OPPORTUNITY_TYPE, opportunity_type)
		stage = values.optional_string("sales_stage")
		if stage:
			doc.sales_stage = self.stage(stage)
		doc.set("notes", crm_notes(values))
		set_custom(doc, values)
		doc.save(ignore_permissions=True)
		return Target(OPPORTUNITY, doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value(OPPORTUNITY, link.target.name, "status", CLOSED)

	def stage(self, name: str) -> str:
		"""The Sales Stage of a pipeline stage, named with the connection's abbreviation (made once; a stage
		has nothing else to keep, and stays for the opportunities that were at it)."""
		stage = self.context.suffixed(name)
		if not frappe.db.exists(SALES_STAGE, stage):
			frappe.get_doc({"doctype": SALES_STAGE, "stage_name": stage}).insert(ignore_permissions=True)
		return stage


class CommunicationWriter(PartyLinked):
	"""A message, recorded on its lead's or customer's timeline (never sent)."""

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doctype, party = self.party(values.required_record("with"))
		doc = self.existing(link) or frappe.new_doc(COMMUNICATION)
		doc.update(
			{
				"communication_type": "Communication",
				"communication_medium": values.string("medium"),
				"sent_or_received": "Sent" if values.flag("sent") else "Received",
				"communication_date": system_datetime(values.string("date")),
				"subject": values.string("subject")[:SUBJECT_LENGTH],
				"content": values.string("content"),
				"phone_no": values.optional_string("phone"),
				"sender": values.optional_string("email") if not values.flag("sent") else None,
				"recipients": values.optional_string("email") if values.flag("sent") else None,
				"reference_doctype": doctype,
				"reference_name": party,
				"status": "Linked",
			}
		)
		set_custom(doc, values)
		doc.save(ignore_permissions=True)
		return Target(COMMUNICATION, doc.name)

	def retire(self, link: Link) -> None:
		mark_retired(COMMUNICATION, link.target.name)


class EventWriter(PartyLinked):
	"""An appointment as a private Event, at its times as the source's clock shows them, with its leads and
	customers as participants."""

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = self.existing(link) or frappe.new_doc(EVENT)
		doc.update(
			{
				"subject": values.string("subject")[:SUBJECT_LENGTH],
				"starts_on": on_site_clock(values.string("starts_on")),
				"ends_on": on_site_clock(values.optional_string("ends_on")),
				"status": values.string("status"),
				"description": values.optional_string("description"),
				"event_type": "Private",
				"send_reminder": 0,
			}
		)
		participants = [self.party(party) for party in values.records("participants")]
		doc.set(
			"event_participants",
			[{"reference_doctype": doctype, "reference_docname": name} for doctype, name in participants],
		)
		set_custom(doc, values)
		doc.save(ignore_permissions=True)
		return Target(EVENT, doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value(EVENT, link.target.name, "status", CANCELLED)


class ToDoWriter(PartyLinked):
	"""A task about a lead or a customer (assigned to no one: the source's users aren't this site's)."""

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doctype, party = self.party(values.required_record("about"))
		doc = self.existing(link) or frappe.new_doc(TODO)
		doc.update(
			{
				"description": values.string("description"),
				"status": values.string("status"),
				"date": values.optional_string("date"),
				"reference_type": doctype,
				"reference_name": party,
			}
		)
		set_custom(doc, values)
		doc.save(ignore_permissions=True)
		return Target(TODO, doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value(TODO, link.target.name, "status", CANCELLED)
