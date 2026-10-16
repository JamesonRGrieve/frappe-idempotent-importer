# SPDX-License-Identifier: AGPL-3.0-or-later
"""Customers, with a Contact (name, email, phone) and an Address per source address, linked to them."""

from __future__ import annotations

from typing import Any

import frappe

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.writers.base import Writer

PART_HASH = "part"


def country_of(code: str | None) -> str:
	"""ERPNext's Country for an ISO 3166-1 alpha-2 code."""
	country = frappe.db.get_value("Country", {"code": (code or "").lower()}) if code else None
	if not country:
		raise Unsupported(f"the country code {code!r} has no ERPNext Country")
	return str(country)


class CustomerWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = self.existing(link) or frappe.new_doc("Customer")
		if doc.is_new():
			doc.update({"customer_group": self.context.customer_group, "territory": self.context.territory})
		doc.update({"customer_name": values.string("name"), "customer_type": values.string("type")})
		doc.save(ignore_permissions=True)
		contact = self._contact(desired.gid, doc.name, values)
		addresses = [
			self._address(f"{desired.gid}#address:{index}", doc.name, address)
			for index, address in enumerate(values.records("addresses"))
		]
		primary = {
			"customer_primary_contact": contact,
			"customer_primary_address": addresses[0] if addresses else None,
		}
		if any(doc.get(field) != value for field, value in primary.items()):
			doc.update(primary)
			doc.save(ignore_permissions=True)
		return Target("Customer", doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value("Customer", link.target.name, "disabled", 1)

	def _contact(self, gid: str, customer: str, values: Values) -> str | None:
		email, phone = values.optional_string("email"), values.optional_string("phone")
		if not (email or phone or values.optional_string("first_name")):
			return None
		key = f"{gid}#contact"
		existing = self.links.target(Kind.CONTACT, key)
		doc = (
			frappe.get_doc("Contact", existing)
			if existing and frappe.db.exists("Contact", existing)
			else frappe.new_doc("Contact")
		)
		doc.update(
			{
				"first_name": values.optional_string("first_name") or values.string("name"),
				"last_name": values.optional_string("last_name"),
				"company_name": values.optional_string("company"),
				"is_primary_contact": 1,
			}
		)
		doc.set("email_ids", [{"email_id": email, "is_primary": 1}] if email else [])
		doc.set(
			"phone_nos", [{"phone": phone, "is_primary_phone": 1, "is_primary_mobile_no": 1}] if phone else []
		)
		link_to(doc, customer)
		doc.save(ignore_permissions=True)
		self.links.save(Kind.CONTACT, key, Target("Contact", doc.name), PART_HASH, 1)
		return str(doc.name)

	def _address(self, key: str, customer: str, address: Values) -> str:
		existing = self.links.target(Kind.ADDRESS, key)
		doc = (
			frappe.get_doc("Address", existing)
			if existing and frappe.db.exists("Address", existing)
			else frappe.new_doc("Address")
		)
		doc.update(address_values(address))
		link_to(doc, customer)
		doc.save(ignore_permissions=True)
		self.links.save(Kind.ADDRESS, key, Target("Address", doc.name), PART_HASH, 1)
		return str(doc.name)


def address_values(address: Values) -> dict[str, Any]:
	return {
		"address_title": address.optional_string("name")
		or address.optional_string("company")
		or address.string("line1"),
		"address_type": address.string("address_type"),
		"address_line1": address.string("line1"),
		"address_line2": address.optional_string("line2"),
		"city": address.string("city"),
		"state": address.optional_string("state"),
		"pincode": address.optional_string("postcode"),
		"country": country_of(address.optional_string("country_code")),
		"email_id": address.optional_string("email"),
		"phone": address.optional_string("phone"),
	}


def link_to(doc: Any, customer: str) -> None:
	if not any(row.link_doctype == "Customer" and row.link_name == customer for row in doc.links):
		doc.append("links", {"link_doctype": "Customer", "link_name": customer})
