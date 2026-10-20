# SPDX-License-Identifier: AGPL-3.0-or-later
"""Customers and Suppliers, each with a Contact (name, email, phone) and an Address per source address,
linked to them; customers in the Customer Group their source group is mirrored to (else the
connection's)."""

from __future__ import annotations

from typing import Any

import frappe

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.writers.base import PART_HASH, Writer


def country_of(code: str | None) -> str:
	"""ERPNext's Country for an ISO 3166-1 alpha-2 code."""
	country = frappe.db.get_value("Country", {"code": (code or "").lower()}) if code else None
	if not country:
		raise Unsupported(f"the country code {code!r} has no ERPNext Country")
	return str(country)


CUSTOMER_GROUP = "Customer Group"
ROOT_CUSTOMER_GROUP = "All Customer Groups"
ROOT_SUPPLIER_GROUP = "All Supplier Groups"


class CustomerGroupWriter(Writer):
	"""A source's customer group (what its discounts are for) as a Customer Group ``<name> - <abbr>``. A
	group the source drops stays (customers may still be in it); its link is retired."""

	def write(self, desired: Desired, link: Link | None) -> Target:
		name = self.context.suffixed(desired.values().string("name"))
		doc = self.existing(link)
		if doc is None:
			doc = frappe.new_doc(CUSTOMER_GROUP)
			doc.update(
				{"customer_group_name": name, "parent_customer_group": ROOT_CUSTOMER_GROUP, "is_group": 0}
			)
			doc.insert(ignore_permissions=True)
		elif doc.name != name:
			frappe.rename_doc(CUSTOMER_GROUP, doc.name, name, force=True)
		return Target(CUSTOMER_GROUP, name)

	def retire(self, link: Link) -> None:
		"""Nothing to switch off: a Customer Group has no disabled state, and its customers keep it."""


class PartyWriter(Writer):
	"""What a Customer and a Supplier share: the party itself, a Contact (name, email, phone) and an
	Address per source address linked to it, and its primary contact and address."""

	doctype: str
	name_field: str
	type_field: str

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = self.existing(link) or frappe.new_doc(self.doctype)
		if doc.is_new():
			doc.update(self.defaults())
		self.fill(doc, values)
		doc.update({self.name_field: values.string("name"), self.type_field: values.string("type")})
		if values.has("tax_id"):
			doc.tax_id = values.string("tax_id")
		doc.disabled = int(bool(values.all().get("disabled")))
		doc.save(ignore_permissions=True)
		contact = self._contact(desired.gid, doc.name, values)
		self._people(desired.gid, doc.name, values.records("contacts"))
		addresses = [
			self._address(f"{desired.gid}#address:{index}", doc.name, address)
			for index, address in enumerate(values.records("addresses"))
		]
		prefix = self.doctype.lower()
		primary = {
			f"{prefix}_primary_contact": contact,
			f"{prefix}_primary_address": addresses[0] if addresses else None,
		}
		if any(doc.get(field) != value for field, value in primary.items()):
			doc.update(primary)
			doc.save(ignore_permissions=True)
		return Target(self.doctype, doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value(self.doctype, link.target.name, "disabled", 1)

	def defaults(self) -> dict[str, Any]:
		"""A new record's fields the source doesn't map."""
		return {}

	def fill(self, doc: Any, values: Values) -> None:
		"""The party's own mapped fields beyond its name, type and tax id."""

	def _contact(self, gid: str, party: str, values: Values) -> str | None:
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
		link_to(doc, party, self.doctype)
		doc.save(ignore_permissions=True)
		self.links.save(Kind.CONTACT, key, Target("Contact", doc.name), PART_HASH, 1)
		return str(doc.name)

	def _people(self, gid: str, party: str, people: list[Values]) -> None:
		"""A Contact for each of the party's other people; one the source drops is unlinked from the party
		(a Contact has no disabled state)."""
		wanted: set[str] = set()
		for person in people:
			key = f"{gid}#person:{person.string('gid')}"
			wanted.add(key)
			existing = self.links.target(Kind.CONTACT, key)
			doc = (
				frappe.get_doc("Contact", existing)
				if existing and frappe.db.exists("Contact", existing)
				else frappe.new_doc("Contact")
			)
			email, phone = person.optional_string("email"), person.optional_string("phone")
			doc.update(
				{
					"first_name": person.string("name"),
					"designation": person.optional_string("job_title"),
					"is_primary_contact": 0,
				}
			)
			doc.set("email_ids", [{"email_id": email, "is_primary": 1}] if email else [])
			doc.set("phone_nos", [{"phone": phone, "is_primary_phone": 1}] if phone else [])
			link_to(doc, party, self.doctype)
			doc.save(ignore_permissions=True)
			self.links.save(Kind.CONTACT, key, Target("Contact", doc.name), PART_HASH, 1)
		for link in self.links.children(Kind.CONTACT, f"{gid}#person:"):
			if link.gid not in wanted:
				if frappe.db.exists("Contact", link.target.name):
					contact = frappe.get_doc("Contact", link.target.name)
					contact.set(
						"links",
						[
							row
							for row in contact.links
							if not (row.link_doctype == self.doctype and row.link_name == party)
						],
					)
					contact.save(ignore_permissions=True)
				self.links.retire(Kind.CONTACT, link.gid)

	def _address(self, key: str, party: str, address: Values) -> str:
		existing = self.links.target(Kind.ADDRESS, key)
		doc = (
			frappe.get_doc("Address", existing)
			if existing and frappe.db.exists("Address", existing)
			else frappe.new_doc("Address")
		)
		doc.update(address_values(address))
		link_to(doc, party, self.doctype)
		doc.save(ignore_permissions=True)
		self.links.save(Kind.ADDRESS, key, Target("Address", doc.name), PART_HASH, 1)
		return str(doc.name)


class CustomerWriter(PartyWriter):
	"""A Customer, in the Customer Group its source group is mirrored to (else the connection's)."""

	doctype = "Customer"
	name_field = "customer_name"
	type_field = "customer_type"

	def defaults(self) -> dict[str, Any]:
		return {"customer_group": self.context.customer_group, "territory": self.context.territory}

	def fill(self, doc: Any, values: Values) -> None:
		if values.has("group"):
			group = values.optional_string("group")
			doc.customer_group = (
				self.dependency(Kind.CUSTOMER_GROUP, group) if group else self.context.customer_group
			)
		if values.has("currency"):
			doc.default_currency = values.string("currency")


class SupplierWriter(PartyWriter):
	"""A Supplier, in the root Supplier Group (a source's vendor groups have no home of their own yet)."""

	doctype = "Supplier"
	name_field = "supplier_name"
	type_field = "supplier_type"

	def defaults(self) -> dict[str, Any]:
		return {"supplier_group": ROOT_SUPPLIER_GROUP}

	def fill(self, doc: Any, values: Values) -> None:
		if values.has("currency"):
			doc.default_currency = values.string("currency")


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


def link_to(doc: Any, name: str, doctype: str = "Customer") -> None:
	"""Links a Contact or Address to its party (once)."""
	if not any(row.link_doctype == doctype and row.link_name == name for row in doc.links):
		doc.append("links", {"link_doctype": doctype, "link_name": name})
