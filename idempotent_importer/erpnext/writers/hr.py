# SPDX-License-Identifier: AGPL-3.0-or-later
"""A source's people (HRMS): Departments (the company's tree, under its root "All Departments"),
Designations and Branches (named with the connection's abbreviation: ERPNext names them globally) and
Employees with their department, designation, branch and manager; and their time: Shift Assignments (each
to the Shift Type of its hours, named by them and the connection's abbreviation, made the first time a
shift has those hours), Employee Checkins, Attendance, Leave Types and Leave Applications.

ERPNext requires an employee's gender, date of birth and date of joining. A source that doesn't keep one
leaves it empty: the employee is saved without it (``ignore_mandatory``), never with an invented value,
and a person fills it in before editing the record. Retiring disables a department and makes an employee
inactive; a designation, a branch and a leave type have no disabled state and stay.

The source keeps its employees' leave balances, so the leave types it names are made allowing a negative
balance and counting holidays: ERPNext never refuses a leave the source granted for want of an allocation
or because it falls on a holiday. A leave the source approved or refused is submitted (HRMS then marks the
approved days' attendance "On Leave" itself); one still open or cancelled stays a draft. Shift
assignments, attendance and leaves follow the cancel-and-amend rule of every submitted document; retiring
cancels a submitted one, makes a draft shift inactive, cancels a draft leave and marks a draft attendance
or a punch."""

from __future__ import annotations

from typing import Any

import frappe

from idempotent_importer.contract import DECIDED_LEAVE
from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import system_datetime
from idempotent_importer.erpnext.writers.base import SUBMITTED, Writer, mark_retired, writable
from idempotent_importer.erpnext.writers.books import TreeWriter

DEPARTMENT, DESIGNATION, EMPLOYEE, BRANCH = "Department", "Designation", "Employee", "Branch"
SHIFT_TYPE, SHIFT_ASSIGNMENT = "Shift Type", "Shift Assignment"
EMPLOYEE_CHECKIN, ATTENDANCE = "Employee Checkin", "Attendance"
LEAVE_TYPE, LEAVE_APPLICATION = "Leave Type", "Leave Application"
ROOT_DEPARTMENT = "All Departments"
INACTIVE = "Inactive"
CANCELLED_LEAVE = "Cancelled"
REQUIRED_EMPLOYEE_FIELDS = ("gender", "date_of_birth", "date_of_joining")
REMOVED_DEVICE = "[Removed at source]"
MINUTES_PER_HOUR = 60


class DepartmentWriter(TreeWriter):
	doctype, kind, name_field, parent_field = (
		DEPARTMENT,
		Kind.DEPARTMENT,
		"department_name",
		"parent_department",
	)

	def root(self, values: Values) -> str:
		return ROOT_DEPARTMENT


class NamedWriter(Writer):
	"""A record ERPNext names globally by its one field (a Designation, a Branch), named with the
	connection's abbreviation and renamed when the source renames it. It has no disabled state: retiring
	leaves it, and whatever names it keeps it."""

	doctype: str
	name_field: str

	def write(self, desired: Desired, link: Link | None) -> Target:
		name = self.context.suffixed(desired.values().string("name"))
		doc = self.existing(link)
		if doc is None:
			doc = frappe.new_doc(self.doctype)
			doc.set(self.name_field, name)
			doc.insert(ignore_permissions=True)
		elif doc.name != name:
			frappe.rename_doc(self.doctype, doc.name, name, force=True)
		return Target(self.doctype, name)

	def retire(self, link: Link) -> None:
		"""Nothing to switch off: the record has no disabled state."""


class DesignationWriter(NamedWriter):
	doctype, name_field = DESIGNATION, "designation_name"


class BranchWriter(NamedWriter):
	doctype, name_field = BRANCH, "branch"


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
				"branch": self._linked(Kind.BRANCH, values.optional_string("branch")),
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


class LeaveTypeWriter(Writer):
	"""A kind of leave, named with the connection's abbreviation: leave without pay when it is unpaid; a
	negative balance allowed, and holidays counted, since the source keeps the balances."""

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		name = self.context.suffixed(values.string("name"))
		doc = self.existing(link)
		if doc is not None and doc.name != name:
			frappe.rename_doc(LEAVE_TYPE, doc.name, name, force=True)
			doc = frappe.get_doc(LEAVE_TYPE, name)
		if doc is None:
			doc = frappe.new_doc(LEAVE_TYPE)
			doc.leave_type_name = name
		doc.update({"is_lwp": int(not values.flag("paid")), "allow_negative": 1, "include_holiday": 1})
		doc.save(ignore_permissions=True)
		return Target(LEAVE_TYPE, doc.name)

	def retire(self, link: Link) -> None:
		"""Nothing to switch off: a Leave Type has no disabled state, and the leaves of it keep it."""


class EmployeeTimeWriter(Writer):
	"""What every record of an employee's time shares: its employee, and the cancel-and-amend of a
	submitted one."""

	doctype: str

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = writable(self.doctype, self.existing(link))
		doc.update({"company": self.context.company, "employee": self.employee(values)})
		self.fill(doc, values)
		doc.save(ignore_permissions=True)
		if self.submits(values):
			doc.submit()
		return Target(self.doctype, doc.name)

	def employee(self, values: Values) -> str:
		return self.dependency(Kind.EMPLOYEE, values.string("employee"))

	def fill(self, doc: Any, values: Values) -> None:
		raise NotImplementedError

	def submits(self, values: Values) -> bool:
		return values.flag("submitted")

	def retire(self, link: Link) -> None:
		doc = frappe.get_doc(self.doctype, link.target.name)
		if doc.docstatus == SUBMITTED:
			doc.cancel()
		else:
			self.retire_draft(doc)

	def retire_draft(self, doc: Any) -> None:
		mark_retired(self.doctype, doc.name)


class ShiftAssignmentWriter(EmployeeTimeWriter):
	doctype = SHIFT_ASSIGNMENT

	def fill(self, doc: Any, values: Values) -> None:
		day = values.string("date")
		doc.update(
			{
				"shift_type": self.shift_type(values.required_record("shift")),
				"start_date": day,
				"end_date": day,
				"status": values.string("status"),
			}
		)

	def shift_type(self, shift: Values) -> str:
		"""The Shift Type of the shift's hours, made the first time a shift has them (shared by every shift of
		the connection with those hours, so never retired)."""
		name = self.context.suffixed(shift.string("name"))
		if not frappe.db.exists(SHIFT_TYPE, name):
			doc = frappe.new_doc(SHIFT_TYPE)
			doc.update({"start_time": shift.string("start_time"), "end_time": shift.string("end_time")})
			self.save(doc, name)
		return name

	def retire_draft(self, doc: Any) -> None:
		frappe.db.set_value(SHIFT_ASSIGNMENT, doc.name, "status", INACTIVE)


class CheckinWriter(EmployeeTimeWriter):
	"""A punch of the time clock, at its time in the site's time zone. Retiring keeps it out of HRMS's
	automatic attendance and marks its device."""

	doctype = EMPLOYEE_CHECKIN

	def fill(self, doc: Any, values: Values) -> None:
		doc.update(
			{
				"time": system_datetime(values.string("time")),
				"log_type": values.string("log_type"),
				"device_id": values.optional_string("device"),
				"skip_auto_attendance": 0,
			}
		)

	def submits(self, values: Values) -> bool:
		return False

	def retire(self, link: Link) -> None:
		device = frappe.db.get_value(EMPLOYEE_CHECKIN, link.target.name, "device_id") or ""
		frappe.db.set_value(
			EMPLOYEE_CHECKIN,
			link.target.name,
			{"skip_auto_attendance": 1, "device_id": f"{REMOVED_DEVICE} {device}".strip()},
		)


class AttendanceWriter(EmployeeTimeWriter):
	"""A day's attendance, its punches in the site's time zone. A day HRMS has already marked from a leave
	it booked keeps that attendance: the source's own for that day is Unsupported."""

	doctype = ATTENDANCE

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		marked = frappe.db.get_value(
			ATTENDANCE,
			{
				"employee": self.employee(values),
				"attendance_date": values.string("date"),
				"docstatus": ("<", 2),
				"name": ("!=", link.target.name if link else ""),
			},
			["name", "leave_application"],
			as_dict=True,
		)
		if marked:
			raise Unsupported(
				f"HRMS already marked {values.string('date')} as {marked.name}"
				+ (f" from leave {marked.leave_application}" if marked.leave_application else "")
			)
		return super().write(desired, link)

	def fill(self, doc: Any, values: Values) -> None:
		leave_type = values.optional_string("leave_type")
		in_time, out_time = values.optional_string("in_time"), values.optional_string("out_time")
		doc.update(
			{
				"attendance_date": values.string("date"),
				"status": values.string("status"),
				"working_hours": values.integer("working_minutes") / MINUTES_PER_HOUR,
				"in_time": system_datetime(in_time) if in_time else None,
				"out_time": system_datetime(out_time) if out_time else None,
				"leave_type": self.dependency(Kind.LEAVE_TYPE, leave_type) if leave_type else None,
			}
		)


class LeaveApplicationWriter(EmployeeTimeWriter):
	"""A leave as the source has it; no mail goes to its approver or the employee."""

	doctype = LEAVE_APPLICATION

	def fill(self, doc: Any, values: Values) -> None:
		doc.update(
			{
				"leave_type": self.dependency(Kind.LEAVE_TYPE, values.string("leave_type")),
				"from_date": values.string("from_date"),
				"to_date": values.string("to_date"),
				"half_day": int(values.flag("half_day")),
				"half_day_date": values.string("from_date") if values.flag("half_day") else None,
				"status": values.string("status"),
				"posting_date": values.string("posting_date"),
				"description": values.optional_string("description"),
				"follow_via_email": 0,
			}
		)

	def submits(self, values: Values) -> bool:
		return values.string("status") in DECIDED_LEAVE

	def retire_draft(self, doc: Any) -> None:
		frappe.db.set_value(LEAVE_APPLICATION, doc.name, "status", CANCELLED_LEAVE)
