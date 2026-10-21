# SPDX-License-Identifier: AGPL-3.0-or-later
"""The fields this app adds to ERPNext's records, created on install and kept on every migrate.

Pricing Rule, Promotional Scheme and Coupon Code record a mirrored discount's conditions ERPNext's pricing
doesn't enforce (``contract.CONDITIONS``) for the Webshop fork, and whether the discount is held awaiting
that enforcement (``erpnext.writers.discounts``). The records a CRM or business system's users extend with
fields of their own keep those fields' values, by label, in Source Fields (``contract.with_custom``)."""

from __future__ import annotations

from typing import Any

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

DISCOUNT_DOCTYPES = {
	"Pricing Rule": "disable",
	"Promotional Scheme": "disable",
	"Coupon Code": "pricing_rule",
}
CUSTOM_VALUES = "importer_custom_fields"
SOURCE_FIELD_DOCTYPES = (
	"Item",
	"Lead",
	"Prospect",
	"Opportunity",
	"Customer",
	"Supplier",
	"Communication",
	"Event",
	"ToDo",
)


def discount_fields(after: str) -> list[dict[str, Any]]:
	return [
		{
			"fieldname": "importer_hold",
			"label": "Importer Hold",
			"fieldtype": "Data",
			"insert_after": after,
			"read_only": 1,
			"no_copy": 1,
			"description": "Held by the importer until the Webshop fork enforces the source's conditions",
		},
		{
			"fieldname": "importer_conditions",
			"label": "Source Conditions",
			"fieldtype": "JSON",
			"insert_after": "importer_hold",
			"read_only": 1,
			"no_copy": 1,
			"description": "The source's conditions ERPNext's pricing doesn't enforce: kind, card and terms",
		},
	]


def source_fields() -> list[dict[str, Any]]:
	return [
		{
			"fieldname": CUSTOM_VALUES,
			"label": "Source Fields",
			"fieldtype": "JSON",
			"read_only": 1,
			"no_copy": 1,
			"description": "The values of the fields the source's users defined themselves, by label",
		}
	]


def custom_fields() -> dict[str, list[dict[str, Any]]]:
	fields = {doctype: discount_fields(after) for doctype, after in DISCOUNT_DOCTYPES.items()}
	return fields | dict.fromkeys(SOURCE_FIELD_DOCTYPES, source_fields())


def install() -> None:
	"""``after_install`` and ``after_migrate``: creates the fields, or brings them up to date."""
	create_custom_fields(custom_fields(), update=True)
