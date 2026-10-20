# SPDX-License-Identifier: AGPL-3.0-or-later
"""A source's people (HRMS): Departments (the company's tree, under its root "All Departments"),
Designations (named with the connection's abbreviation: ERPNext names them globally) and Employees with
their department, designation and manager.

ERPNext requires an employee's gender, date of birth and date of joining. A source that doesn't keep one
leaves it empty: the employee is saved without it (``ignore_mandatory``), never with an invented value,
and a person fills it in before editing the record. Retiring disables a department and makes an employee
inactive; a designation has no disabled state and stays."""

from __future__ import annotations

from typing import Any

import frappe

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.writers.base import Writer
from idempotent_importer.erpnext.writers.books import TreeWriter

DEPARTMENT, DESIGNATION, EMPLOYEE = "Department", "Designation", "Employee"
ROOT_DEPARTMENT = "All Departments"
INACTIVE = "Inactive"
REQUIRED_EMPLOYEE_FIELDS = ("gender", "date_of_birth", "date_of_joining")


class DepartmentWriter(TreeWriter):
	doctype, kind, name_field, parent_field = (
		DEPARTMENT,
		Kind.DEPARTMENT,
		"department_name",
		"parent_department",
	)

	def root(self, values: Values) -> str:
		return ROOT_DEPARTMENT


class DesignationWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		name = self.context.suffixed(desired.values().string("name"))
		doc = self.existing(link)
		if doc is None:
			doc = frappe.new_doc(DESIGNATION)
			doc.designation_name = name
			doc.insert(ignore_permissions=True)
		elif doc.name != name:
			frappe.rename_doc(DESIGNATION, doc.name, name, force=True)
		return Target(DESIGNATION, name)

	def retire(self, link: Link) -> None:
		"""Nothing to switch off: a Designation has no disabled state, and employees keep theirs."""


class EmployeeWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = self.existing(link) or frappe.new_doc(EMPLOYEE)
		doc.update(
			{
				"company": self.context.company,
				"first_name": values.string("first_name"),
				"middle_name": values.optional_string("middle_name"),
				"last_name": values.optional_string("last_name"),
				"gender": values.optional_string("gender"),
				"date_of_birth": values.optional_string("date_of_birth"),
				"date_of_joining": values.optional_string("date_of_joining"),
				"relieving_date": values.optional_string("relieving_date"),
				"status": values.string("status"),
				"employee_number": values.optional_string("employee_number"),
				"department": self._linked(Kind.DEPARTMENT, values.optional_string("department")),
				"designation": self._linked(Kind.DESIGNATION, values.optional_string("designation")),
				"reports_to": self._linked(Kind.EMPLOYEE, values.optional_string("reports_to")),
				"company_email": values.optional_string("company_email"),
				"personal_email": values.optional_string("personal_email"),
				"cell_number": values.optional_string("phone"),
			}
		)
		# Saved without what the source doesn't keep, rather than with a made-up value.
		doc.flags.ignore_mandatory = any(not values.has(field) for field in REQUIRED_EMPLOYEE_FIELDS)
		doc.save(ignore_permissions=True)
		return Target(EMPLOYEE, doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value(EMPLOYEE, link.target.name, "status", INACTIVE)

	def _linked(self, kind: Kind, gid: str | None) -> Any:
		return self.dependency(kind, gid) if gid else None
