# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Agendrix → desired ERPNext (HRMS) state: sites as Branches, positions as Designations, members as
Employees, time off types as Leave Types, shifts as Shift Assignments, clock logs as Employee Checkins, a
member's time entries of one day as that day's Attendance, and time off requests as Leave Applications.
Field meanings are the OpenAPI document's (github.com/agendrix/openapi, en/open_api.json). What ERPNext
can't hold exactly is Unsupported, naming the card that plans it."""

from __future__ import annotations

from idempotent_importer.contract import (
	Attendance,
	Checkin,
	Employee,
	LeaveApplication,
	ShiftAssignment,
	attendance_fields,
	checkin_fields,
	employee_fields,
	leave_application_fields,
	leave_type_fields,
	shift_assignment_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.ledger import day

PLANNED = "planned: card 386"
PUBLISHED = "published"
CLOCK_ACTIONS = {"clocked_in": "IN", "clocked_out": "OUT"}
LEAVE_STATUSES = {"approved": "Approved", "declined": "Rejected", "canceled": "Cancelled", "pending": "Open"}
# An ISO 8601 time's time of day, as the source's clock shows it: 2026-10-05T08:00:00-04:00 → 08:00:00.
WALL_CLOCK = slice(11, 19)


class Context:
	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config

	def gid(self, type_: str, id_: str | None) -> str | None:
		return self.config.identity(type_, id_) if id_ else None

	def member(self, node: Node) -> str:
		member = node.optional_string("member_id")
		if member is None:
			raise Unsupported(f"it belongs to no member (an open shift has no first-party home: {PLANNED})")
		return self.config.identity("Member", member)


def wall_time(node: Node, field: str) -> str:
	"""A time of day as the source's clock shows it."""
	value = node.string(field)
	if len(value) < WALL_CLOCK.stop or value[WALL_CLOCK.start - 1] != "T":
		raise SourceError(f"{node.path}.{field} is not an ISO 8601 date and time")
	return value[WALL_CLOCK]


class BranchMapper:
	"""A site (archived ones too: a Branch has no disabled state, and its employees keep it)."""

	kind = Kind.BRANCH
	version = 1

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), {"name": node.string("name")})


class DesignationMapper:
	"""A position; a sub-position is named after its position too ("Kitchen - Prep")."""

	kind = Kind.DESIGNATION
	version = 1

	def map(self, node: Node) -> Desired | None:
		name = node.string("name")
		parent = node.optional_string("parent_name")
		return Desired(self.kind, node.id(), {"name": f"{parent} - {name}" if parent else name})


class EmployeeMapper:
	"""A member: their profile's names, birthdate, email and first phone, their seniority date as the date
	of joining, identification number, main position and site; an archived member has left, on the day
	they were archived. Agendrix keeps no gender."""

	kind = Kind.EMPLOYEE
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		profile = node.child("profile") or Node({}, f"{node.path}.profile")
		first_name = profile.optional_string("first_name")
		if first_name is None:
			raise Unsupported("a member with no first name")
		left = day(node.optional_string("deleted_at"))
		employee = Employee(
			first_name=first_name,
			status="Left" if left else "Active",
			last_name=profile.optional_string("last_name"),
			date_of_birth=day(profile.optional_string("birthdate")),
			date_of_joining=day(node.optional_string("seniority_date")),
			relieving_date=left,
			employee_number=node.optional_string("identification_number"),
			designation=self.context.gid("Position", node.optional_string("main_position_id")),
			branch=self.context.gid("Site", node.optional_string("main_site_id")),
			company_email=profile.optional_string("email"),
			phone=profile.optional_string("phone_1_number"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class LeaveTypeMapper:
	kind = Kind.LEAVE_TYPE
	version = 1

	def map(self, node: Node) -> Desired | None:
		return Desired(
			self.kind, node.id(), leave_type_fields(node.string("name"), node.optional_flag("paid") is True)
		)


class ShiftMapper:
	"""A member's shift, on the day it starts at its hours (published ones submitted, drafts kept as
	drafts). A time off shift is its request's (a Leave Application); one scheduled without a request, and
	an open shift no one has taken, have no first-party home yet."""

	kind = Kind.SHIFT_ASSIGNMENT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.has("deleted_at"):
			return None
		if node.optional_flag("time_off"):
			if node.has("leave_request_id"):
				return None
			raise Unsupported(f"a time off scheduled without a request ({PLANNED})")
		if node.optional_flag("open"):
			raise Unsupported(f"an open shift no one has taken ({PLANNED})")
		date = day(node.optional_string("start_date") or node.optional_string("date"))
		if date is None:
			raise SourceError(f"{node.path} has no date")
		shift = ShiftAssignment(
			employee=self.context.member(node),
			date=date,
			start_time=wall_time(node, "start_at"),
			end_time=wall_time(node, "end_at"),
			submitted=node.optional_string("status") == PUBLISHED,
		)
		return Desired(self.kind, node.id(), shift_assignment_fields(shift))


class CheckinMapper:
	"""A successful clock in or out (a failed attempt is no punch)."""

	kind = Kind.EMPLOYEE_CHECKIN
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("success") is not True:
			return None
		action = node.string("action")
		if action not in CLOCK_ACTIONS:
			raise Unsupported(f'a clock log "{action}" that is neither a clock in nor out')
		checkin = Checkin(
			employee=self.context.member(node),
			time=node.time("created_at"),
			log_type=CLOCK_ACTIONS[action],
			device=node.optional_string("type"),
		)
		return Desired(self.kind, node.id(), checkin_fields(checkin))


class AttendanceMapper:
	"""A member's day: present for the time their entries worked (regular and overtime minutes), from the
	first start to the last end (no end while an entry is still open), submitted once every entry is
	approved. A day of time off only is its leave's (HRMS marks it from the Leave Application)."""

	kind = Kind.ATTENDANCE
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		entries = [entry for entry in node.children("entries") if not entry.has("deleted_at")]
		worked = [entry for entry in entries if not entry.optional_flag("time_off")]
		if not worked:
			return None
		starts = sorted(entry.time("start_at") for entry in worked)
		ends = [entry.optional_time("end_at") for entry in worked]
		attendance = Attendance(
			employee=self.context.config.identity("Member", node.string("member_id")),
			date=node.string("date"),
			status="Present",
			working_minutes=sum(
				(entry.optional_integer("regular_minutes") or 0)
				+ (entry.optional_integer("overtime_minutes") or 0)
				for entry in worked
			),
			in_time=starts[0],
			out_time=None if None in ends else max(end for end in ends if end),
			submitted=all(entry.optional_flag("approved") for entry in entries),
		)
		return Desired(self.kind, node.id(), attendance_fields(attendance))


class LeaveApplicationMapper:
	"""A time off request, from its first to its last day, with its status and justification. ERPNext books
	leave in whole or half days: a request for part of a single day has no exact home yet."""

	kind = Kind.LEAVE_APPLICATION
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		status = self._status(node)
		if not (node.optional_flag("all_day") or node.optional_flag("multiple_days")):
			raise Unsupported(f"a time off of part of a single day ({PLANNED})")
		leave_type = node.child("leave_type")
		type_id = leave_type.optional_string("id") if leave_type else None
		if type_id is None:
			raise SourceError(f"{node.path} names no time off type")
		leave = LeaveApplication(
			employee=self.context.member(node),
			leave_type=self.context.config.identity("LeaveType", type_id),
			from_date=_required_day(node, "start_date", "start_at"),
			to_date=_required_day(node, "end_date", "end_at"),
			status=status,
			posting_date=_required_day(node, "created_at", "created_at"),
			description=node.optional_string("justification"),
		)
		return Desired(self.kind, node.id(), leave_application_fields(leave))

	@staticmethod
	def _status(node: Node) -> str:
		"""The request's status, from the moments Agendrix records its decision (a request carries no status
		of its own; the list filters by approved, canceled, declined and pending)."""
		for field, status in (
			("canceled_at", "canceled"),
			("declined_at", "declined"),
			("approved_at", "approved"),
		):
			if node.has(field):
				return LEAVE_STATUSES[status]
		return LEAVE_STATUSES["pending"]


def _required_day(node: Node, field: str, fallback: str) -> str:
	value = day(node.optional_string(field) or node.optional_string(fallback))
	if value is None:
		raise SourceError(f"{node.path}.{field} is missing")
	return value


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		BranchMapper(),
		DesignationMapper(),
		EmployeeMapper(context),
		LeaveTypeMapper(),
		ShiftMapper(context),
		CheckinMapper(context),
		LeaveApplicationMapper(context),
		AttendanceMapper(context),
	]
