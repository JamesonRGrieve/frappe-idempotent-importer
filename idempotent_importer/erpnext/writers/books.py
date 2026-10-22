# SPDX-License-Identifier: AGPL-3.0-or-later
"""The company's own structure and its ledger, as an ERP source keeps them: the chart of Accounts, Cost
Centers, Warehouses and Projects, and the Journal Entries and Payment Entries posted to them.

Accounts, Cost Centers and Warehouses are company trees: a mirrored node goes under its mirrored parent,
or under the company's root of its kind (an account under the root of its root type), and is named with
the connection's abbreviation, so two connections into one company never collide. Retiring disables them.

Time worked is a Timesheet on its project, a sales tax a Sales Taxes and Charges Template at its rate.

Journal and Payment Entries are submitted as the source posted them (a draft stays a draft). One that
changes at the source after it was submitted is cancelled and posted again as an amendment; retiring
cancels it. A payment is allocated to the invoices it settles, which must be booked (submitted) first."""

from __future__ import annotations

from typing import Any

import frappe
from erpnext.accounts.party import get_party_account

from idempotent_importer.contract import PARTY_KINDS, RECEIVE
from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import money, system_datetime
from idempotent_importer.erpnext.writers.base import (
	DRAFT,
	SUBMITTED,
	Writer,
	cancel_booked,
	parties_enabled,
	writable,
)

ACCOUNT, COST_CENTER, WAREHOUSE, PROJECT = "Account", "Cost Center", "Warehouse", "Project"
TIMESHEET, TAX_TEMPLATE = "Timesheet", "Sales Taxes and Charges Template"
ON_NET_TOTAL = "On Net Total"
JOURNAL_ENTRY, PAYMENT_ENTRY = "Journal Entry", "Payment Entry"
NAME_LENGTH = 140
REMARK_LENGTH = 140
RETIRED_REMARK = "Retired: removed at the source."
NOT_SET = ("is", "not set")
PARTY_ACCOUNT_TYPES = frozenset({"Receivable", "Payable"})
# What a payment allocation's kind is booked as.
ALLOCATED_DOCTYPES: dict[str, tuple[Kind, str]] = {
	Kind.SALES_INVOICE.value: (Kind.SALES_INVOICE, "Sales Invoice"),
	Kind.PURCHASE_INVOICE.value: (Kind.PURCHASE_INVOICE, "Purchase Invoice"),
	Kind.ORDER.value: (Kind.ORDER, "Sales Invoice"),
}


class TreeWriter(Writer):
	"""A node of one of the company's trees, named ``<name> - <abbr>`` (ERPNext adds the company's own
	abbreviation to the record's name), under its mirrored parent or the company's root."""

	doctype: str
	kind: Kind
	name_field: str
	parent_field: str

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = self.existing(link) or frappe.new_doc(self.doctype)
		parent = values.optional_string("parent")
		doc.update(
			{
				self.name_field: self.context.suffixed(values.string("name"))[:NAME_LENGTH],
				self.parent_field: self.dependency(self.kind, parent) if parent else self.root(values),
				"company": self.context.company,
				"is_group": int(values.flag("is_group")),
				"disabled": int(bool(values.all().get("disabled"))),
			}
		)
		self.fill(doc, values)
		doc.save(ignore_permissions=True)
		return Target(self.doctype, doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value(self.doctype, link.target.name, "disabled", 1)

	def root(self, values: Values) -> str:
		"""The company's root node a top-level node goes under."""
		root = frappe.db.get_value(
			self.doctype,
			{"company": self.context.company, "is_group": 1, self.parent_field: NOT_SET},
		)
		if not root:
			raise Unsupported(f"{self.context.company} has no root {self.doctype}")
		return str(root)

	def fill(self, doc: Any, values: Values) -> None:
		"""The node's own mapped fields beyond its name, parent and group flag."""


class AccountWriter(TreeWriter):
	"""An account of the source's chart, with its number, root type, account type and currency; a
	top-level one under the company's root account of its root type."""

	doctype, kind, name_field, parent_field = ACCOUNT, Kind.ACCOUNT, "account_name", "parent_account"

	def root(self, values: Values) -> str:
		root = frappe.db.get_value(
			ACCOUNT,
			{
				"company": self.context.company,
				"is_group": 1,
				"root_type": values.string("root_type"),
				"parent_account": NOT_SET,
			},
		)
		if not root:
			raise Unsupported(f"{self.context.company} has no root {values.string('root_type')} account")
		return str(root)

	def fill(self, doc: Any, values: Values) -> None:
		doc.update(
			{
				"account_number": values.optional_string("number"),
				"root_type": values.string("root_type"),
				"account_type": values.optional_string("account_type") or "",
				"account_currency": values.optional_string("currency") or self.context.currency,
			}
		)


class CostCenterWriter(TreeWriter):
	doctype, kind, name_field, parent_field = (
		COST_CENTER,
		Kind.COST_CENTER,
		"cost_center_name",
		"parent_cost_center",
	)


class WarehouseWriter(TreeWriter):
	"""A warehouse (a source's location), with its address."""

	doctype, kind, name_field, parent_field = WAREHOUSE, Kind.WAREHOUSE, "warehouse_name", "parent_warehouse"

	def fill(self, doc: Any, values: Values) -> None:
		address = values.record("address")
		doc.update(
			{
				"address_line_1": address.optional_string("line1") if address else None,
				"address_line_2": address.optional_string("line2") if address else None,
				"city": address.optional_string("city") if address else None,
				"state": address.optional_string("state") if address else None,
				"pin": address.optional_string("postcode") if address else None,
				"phone_no": address.optional_string("phone") if address else None,
				"email_id": address.optional_string("email") if address else None,
			}
		)


class ProjectWriter(Writer):
	"""A Project of the company's, with its status, customer, cost center and expected dates. Retiring
	makes it inactive."""

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = self.existing(link) or frappe.new_doc(PROJECT)
		customer = values.optional_string("customer")
		cost_center = values.optional_string("cost_center")
		doc.update(
			{
				"project_name": values.string("name")[:NAME_LENGTH],
				"company": self.context.company,
				"status": values.string("status"),
				"is_active": "Yes",
				"customer": self.dependency(Kind.CUSTOMER, customer) if customer else None,
				"cost_center": self.dependency(Kind.COST_CENTER, cost_center) if cost_center else None,
				"expected_start_date": values.optional_string("start"),
				"expected_end_date": values.optional_string("end"),
				"notes": values.optional_string("notes"),
			}
		)
		doc.save(ignore_permissions=True)
		return Target(PROJECT, doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value(PROJECT, link.target.name, "is_active", "No")


class PostedWriter(Writer):
	"""A document the books post (submitted when the source posted it): written into a fresh draft, or
	the linked draft; a submitted one that changed is cancelled and amended. Retiring cancels a submitted
	one and marks a draft."""

	doctype: str
	remarks_field: str

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = writable(self.doctype, self.existing(link))
		self.fill(doc, values)
		with parties_enabled(doc):
			doc.save(ignore_permissions=True)
			if values.flag("submitted"):
				doc.submit()
		return Target(self.doctype, doc.name)

	def retire(self, link: Link) -> None:
		doc = frappe.get_doc(self.doctype, link.target.name)
		if doc.docstatus == SUBMITTED:
			cancel_booked(doc)
		elif doc.docstatus == DRAFT and RETIRED_REMARK not in (doc.get(self.remarks_field) or ""):
			marked = f"{RETIRED_REMARK}\n{doc.get(self.remarks_field) or ''}".strip()
			frappe.db.set_value(self.doctype, doc.name, self.remarks_field, marked)

	def fill(self, doc: Any, values: Values) -> None:
		raise NotImplementedError

	def party(self, party_type: str | None, party: str | None) -> str | None:
		"""The mirrored party a line or payment names."""
		if party_type is None or party is None:
			return None
		return self.dependency(PARTY_KINDS[party_type], party)

	def booked(self, kind: str, document: str) -> tuple[str, str]:
		"""The invoice a payment or journal line settles: it must be booked (submitted) to be settled."""
		settled, doctype = ALLOCATED_DOCTYPES[kind]
		name = self.dependency(settled, document)
		if frappe.db.get_value(doctype, name, "docstatus") != SUBMITTED:
			raise Unsupported(f"it settles {doctype} {name}, which isn't booked (submitted)")
		return doctype, name

	def in_company_currency(self, account: str) -> str:
		currency = frappe.db.get_value(ACCOUNT, account, "account_currency")
		if currency and currency != self.context.currency:
			raise Unsupported(
				f"{account} is in {currency}; posting in another currency is planned (card 386)"
			)
		return account


class TimesheetWriter(PostedWriter):
	"""Time worked as a Timesheet of the company's: one time log from its start for its hours, on its project
	and for its customer and employee where the source names them, billable or not; submitted once logged.
	Retiring cancels it."""

	doctype = TIMESHEET
	remarks_field = "note"

	def fill(self, doc: Any, values: Values) -> None:
		employee, customer, project = (
			values.optional_string(field) for field in ("employee", "customer", "project")
		)
		doc.update(
			{
				"company": self.context.company,
				"employee": self.dependency(Kind.EMPLOYEE, employee) if employee else None,
				"customer": self.dependency(Kind.CUSTOMER, customer) if customer else None,
				"parent_project": self.dependency(Kind.PROJECT, project) if project else None,
				"note": values.optional_string("description"),
			}
		)
		doc.set(
			"time_logs",
			[
				{
					"from_time": system_datetime(values.string("start")),
					"hours": float(values.decimal("hours")),
					"project": doc.parent_project,
					"is_billable": int(values.flag("billable")),
					"description": values.optional_string("description"),
					"completed": 1,
				}
			],
		)


class TaxRateWriter(Writer):
	"""A sales tax as a Sales Taxes and Charges Template of the company's (titled with the connection's
	abbreviation): one row on the net total at its rate, booked to the connection's tax account, so documents
	after the cut-over can charge it. Retiring disables it."""

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = self.existing(link) or frappe.new_doc(TAX_TEMPLATE)
		number = values.optional_string("number")
		name = values.string("name")
		doc.update(
			{
				"title": self.context.suffixed(name),
				"company": self.context.company,
				"disabled": 0,
			}
		)
		doc.set(
			"taxes",
			[
				{
					"charge_type": ON_NET_TOTAL,
					"account_head": self.context.tax_account,
					"description": f"{name} ({number})" if number else name,
					"rate": float(values.decimal("rate")),
				}
			],
		)
		doc.save(ignore_permissions=True)
		return Target(TAX_TEMPLATE, doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value(TAX_TEMPLATE, link.target.name, "disabled", 1)


class JournalEntryWriter(PostedWriter):
	"""A journal entry, line by line on the mirrored accounts, with each line's party, cost center and
	project, in the company's currency."""

	doctype, remarks_field = JOURNAL_ENTRY, "user_remark"

	def fill(self, doc: Any, values: Values) -> None:
		remarks = values.optional_string("remarks")
		doc.update(
			{
				"voucher_type": "Journal Entry",
				"company": self.context.company,
				"posting_date": values.string("date"),
				"cheque_no": values.string("number")[:REMARK_LENGTH],
				"cheque_date": values.string("date"),
				"user_remark": f"{values.string('number')}: {remarks}"
				if remarks
				else values.string("number"),
				"multi_currency": 0,
			}
		)
		doc.set("accounts", [self._line(line) for line in values.records("lines")])

	def _line(self, line: Values) -> dict[str, Any]:
		cost_center = line.optional_string("cost_center")
		project = line.optional_string("project")
		account = self.in_company_currency(self.dependency(Kind.ACCOUNT, line.string("account")))
		# ERPNext keeps a party only on a receivable or payable line; a source that names the contact of any
		# line (a bank line's payee, an expense's vendor) keeps it on those lines alone.
		party_type = (
			line.optional_string("party_type")
			if frappe.get_cached_value(ACCOUNT, account, "account_type") in PARTY_ACCOUNT_TYPES
			else None
		)
		return {
			"account": account,
			"debit_in_account_currency": money(line.decimal("debit")),
			"credit_in_account_currency": money(line.decimal("credit")),
			"party_type": party_type,
			"party": self.party(party_type, line.optional_string("party")),
			"cost_center": self.dependency(Kind.COST_CENTER, cost_center)
			if cost_center
			else self.context.cost_center,
			"project": self.dependency(Kind.PROJECT, project) if project else None,
			**self._against(line.record("against")),
		}

	def _against(self, against: Values | None) -> dict[str, Any]:
		"""The invoice a receivable or payable line settles."""
		if against is None:
			return {"reference_type": None, "reference_name": None}
		doctype, name = self.booked(against.string("kind"), against.string("document"))
		return {"reference_type": doctype, "reference_name": name}


class PaymentEntryWriter(PostedWriter):
	"""A payment received from or paid to a party, into or out of its mirrored bank or cash account (else
	the connection's deposit account), allocated to the booked invoices it settles; anything unallocated
	stays an advance. Payments in another currency than the company's are planned (card 386)."""

	doctype, remarks_field = PAYMENT_ENTRY, "remarks"

	def fill(self, doc: Any, values: Values) -> None:
		if values.string("currency") != self.context.currency:
			raise Unsupported(
				f"a payment in {values.string('currency')} (the company books in {self.context.currency}); "
				"payments in other currencies are planned (card 386)"
			)
		party_type = values.string("party_type")
		party = self.party(party_type, values.string("party"))
		account = values.optional_string("account")
		bank = self.in_company_currency(
			self.dependency(Kind.ACCOUNT, account) if account else self.context.deposit_account
		)
		own = values.optional_string("party_account")
		party_account = (
			self.dependency(Kind.ACCOUNT, own)
			if own
			else get_party_account(party_type, party, self.context.company)
		)
		receiving = values.string("payment_type") == RECEIVE
		amount = money(values.decimal("amount"))
		doc.update(
			{
				"payment_type": values.string("payment_type"),
				"company": self.context.company,
				"posting_date": values.string("date"),
				"mode_of_payment": self.context.mode_of_payment,
				"party_type": party_type,
				"party": party,
				"paid_from": party_account if receiving else bank,
				"paid_to": bank if receiving else party_account,
				"paid_amount": amount,
				"received_amount": amount,
				"source_exchange_rate": 1,
				"target_exchange_rate": 1,
				"reference_no": values.optional_string("reference") or values.string("party"),
				"reference_date": values.string("date"),
				"cost_center": self.context.cost_center,
				"remarks": values.optional_string("remarks"),
			}
		)
		doc.set("references", [self._allocation(allocation) for allocation in values.records("allocations")])
		doc.setup_party_account_field()
		doc.set_missing_values()
		doc.set_amounts()

	def _allocation(self, allocation: Values) -> dict[str, Any]:
		"""An allocation to the invoice it settles. A refund paid out on a credit note (or received on a debit
		note) settles a negative balance, which ERPNext allocates as a negative amount."""
		doctype, name = self.booked(allocation.string("kind"), allocation.string("document"))
		amount = allocation.decimal("amount")
		if frappe.db.get_value(doctype, name, "is_return"):
			amount = -amount
		return {
			"reference_doctype": doctype,
			"reference_name": name,
			"allocated_amount": money(amount),
		}
