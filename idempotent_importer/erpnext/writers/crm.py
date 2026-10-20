# SPDX-License-Identifier: AGPL-3.0-or-later
"""A source's sales pipeline in ERPNext's CRM: Leads (the person, their organisation and how to reach
them) and Opportunities with a lead or a customer. Retiring disables a lead and closes an opportunity."""

from __future__ import annotations

import frappe

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import money
from idempotent_importer.erpnext.writers.base import Writer

LEAD, OPPORTUNITY, CUSTOMER = "Lead", "Opportunity", "Customer"
CLOSED = "Closed"
TITLE_LENGTH = 140
PARTY_DOCTYPES = {Kind.LEAD.value: (Kind.LEAD, LEAD), Kind.CUSTOMER.value: (Kind.CUSTOMER, CUSTOMER)}


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
				"disabled": 0,
			}
		)
		notes = values.optional_string("notes")
		doc.set("notes", [{"note": notes}] if notes else [])
		doc.save(ignore_permissions=True)
		return Target(LEAD, doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value(LEAD, link.target.name, "disabled", 1)


class OpportunityWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		kind, doctype = PARTY_DOCTYPES[values.string("party_kind")]
		doc = self.existing(link) or frappe.new_doc(OPPORTUNITY)
		amount = values.optional_decimal("amount")
		probability = values.optional_decimal("probability")
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
		notes = values.optional_string("notes")
		doc.set("notes", [{"note": notes}] if notes else [])
		doc.save(ignore_permissions=True)
		return Target(OPPORTUNITY, doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value(OPPORTUNITY, link.target.name, "status", CLOSED)
