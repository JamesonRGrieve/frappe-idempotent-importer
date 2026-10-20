# SPDX-License-Identifier: AGPL-3.0-or-later
"""An ERP source's own sales and purchase documents: Sales Orders, Purchase Orders, Sales Invoices and
Purchase Invoices (credit and debit notes as their returns), each to its mirrored customer or supplier.

Lines are the source's quantities at their exact net prices, on the item the line names (the connection's
fallback item for a line that names none) and, where the source says, its income or expense account,
cost center and project (a stock item bought without a receipt books to Stock Received But Not Billed, as
ERPNext's perpetual inventory requires); taxes and other charges are "Actual" amounts on their mirrored account (else
the connection's tax account). The document's grand total must equal the source's exactly, or it isn't
kept. A document in another currency is booked at the rate the source recorded.

Nothing here moves stock or books a payment: invoices never update stock (stock is mirrored absolutely,
see ``catalog``), orders only reserve, and payments are their own Payment Entries (``books``), allocated
to the invoices they settle. Documents are submitted once the source has confirmed or posted them; one
that changes after that is cancelled and amended. Retiring cancels a submitted document and marks a
draft."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import frappe

from idempotent_importer.contract import FEE, PARTY_KINDS, SHIPPING, TAX
from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import money
from idempotent_importer.erpnext.writers.sales import (
	CANCELLED,
	DRAFT,
	RETIRED_MARK,
	RETIRED_REMARK,
	SUBMITTED,
	SalesDocuments,
)

REMARK_LENGTH = 140


@dataclass(frozen=True)
class TradeSpec:
	"""How one kind of trade document is held in ERPNext."""

	doctype: str
	kind: Kind
	party_type: str
	party_field: str
	is_order: bool

	@property
	def selling(self) -> bool:
		return self.party_type == "Customer"

	@property
	def date_field(self) -> str:
		"""An order's planned date: its delivery date (sales) or the date it is wanted (purchases)."""
		return "delivery_date" if self.selling else "schedule_date"

	@property
	def remarks_field(self) -> str:
		"""Where the source's number and remarks go: an invoice's remarks, or an order's title (an order has
		no remarks)."""
		return "title" if self.is_order else "remarks"

	@property
	def account_field(self) -> str:
		return "income_account" if self.selling else "expense_account"


SALES_ORDER = TradeSpec("Sales Order", Kind.SALES_ORDER, "Customer", "customer", True)
PURCHASE_ORDER = TradeSpec("Purchase Order", Kind.PURCHASE_ORDER, "Supplier", "supplier", True)
SALES_INVOICE = TradeSpec("Sales Invoice", Kind.SALES_INVOICE, "Customer", "customer", False)
PURCHASE_INVOICE = TradeSpec("Purchase Invoice", Kind.PURCHASE_INVOICE, "Supplier", "supplier", False)


class TradeWriter(SalesDocuments):
	spec: TradeSpec

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = self.existing(link)
		if doc is None or doc.docstatus == CANCELLED:
			doc = frappe.new_doc(self.spec.doctype)
		elif doc.docstatus == SUBMITTED:
			doc.cancel()
			amended = frappe.copy_doc(doc)
			amended.docstatus = DRAFT
			amended.amended_from = doc.name
			doc = amended
		self._fill(doc, values)
		doc.save(ignore_permissions=True)
		# Every tax row is an amount ERPNext books as given; only the totals are computed.
		charges = [Values({"kind": TAX, **tax.all()}) for tax in values.records("taxes")]
		self.check_totals(doc, values.decimal("total"), charges, values.optional_decimal("base_total"))
		if values.flag("submitted"):
			doc.submit()
		return Target(self.spec.doctype, doc.name)

	def retire(self, link: Link) -> None:
		doc = frappe.get_doc(self.spec.doctype, link.target.name)
		if doc.docstatus == SUBMITTED:
			doc.cancel()
		elif doc.docstatus == DRAFT:
			field = self.spec.remarks_field
			text = str(doc.get(field) or "")
			if self.spec.is_order and not text.startswith(RETIRED_MARK):
				frappe.db.set_value(
					self.spec.doctype, doc.name, field, f"{RETIRED_MARK}{text}"[:REMARK_LENGTH]
				)
			elif not self.spec.is_order and RETIRED_REMARK not in text:
				frappe.db.set_value(self.spec.doctype, doc.name, field, f"{RETIRED_REMARK}\n{text}".strip())

	def _fill(self, doc: Any, values: Values) -> None:
		spec = self.spec
		party = self.dependency(PARTY_KINDS[spec.party_type], values.string("party"))
		date = values.string("date")
		planned = values.optional_string("due_date") or date
		remarks = values.optional_string("remarks")
		doc.update(
			{
				"company": self.context.company,
				spec.party_field: party,
				**self.currency(values, party, spec.party_type),
				"ignore_pricing_rule": 1,
				"disable_rounded_total": 1,
				"taxes_and_charges": None,
				"transaction_date": date,
			}
		)
		number = values.string("number")
		text = f"{number}: {remarks}" if remarks else number
		doc.set(spec.remarks_field, text[:REMARK_LENGTH] if spec.is_order else text)
		if spec.selling:
			doc.update(
				{
					"selling_price_list": self.context.price_list,
					"price_list_currency": self.context.currency,
					"plc_conversion_rate": 1,
					"po_no": values.optional_string("reference"),
				}
			)
		if spec.is_order:
			doc.update({spec.date_field: planned, "set_warehouse": self.context.warehouse})
			if not spec.selling:
				doc.order_confirmation_no = values.optional_string("reference")
		else:
			returned = values.optional_string("return_against")
			doc.update(
				{
					"posting_date": date,
					"set_posting_time": 1,
					"due_date": planned,
					"update_stock": 0,
					"cost_center": self.context.cost_center,
					"is_return": int(values.flag("is_return")),
					"return_against": self.dependency(spec.kind, returned) if returned else None,
					# A return against an invoice settles it, as the source reconciled them, unless the source
					# settled the return on its own.
					"update_outstanding_for_self": int(not (returned and values.flag("return_settles"))),
				}
			)
			if not spec.selling:
				doc.update({"bill_no": values.optional_string("reference"), "bill_date": date})
			own = values.optional_string("party_account")
			if own:
				doc.set("debit_to" if spec.selling else "credit_to", self.dependency(Kind.ACCOUNT, own))
		discount = values.optional_decimal("discount")
		doc.update({"apply_discount_on": "Net Total", "discount_amount": money(discount) if discount else 0})
		doc.set("items", [self._trade_line(line, planned) for line in values.records("lines")])
		doc.set("taxes", [self._tax(tax) for tax in values.records("taxes")])

	def _trade_line(self, line: Values, planned: str) -> dict[str, Any]:
		row = self._line(line)
		account = line.optional_string("account")
		cost_center = line.optional_string("cost_center")
		project = line.optional_string("project")
		if self.spec.is_order:
			row |= {self.spec.date_field: planned, "warehouse": self.context.warehouse}
			row.pop("cost_center", None)
		else:
			if account:
				row[self.spec.account_field] = self.dependency(Kind.ACCOUNT, account)
			if cost_center:
				row["cost_center"] = self.dependency(Kind.COST_CENTER, cost_center)
		if project:
			row["project"] = self.dependency(Kind.PROJECT, project)
		return row

	def _tax(self, tax: Values) -> dict[str, Any]:
		account = tax.optional_string("account")
		row: dict[str, Any] = {
			"charge_type": "Actual",
			"account_head": self.dependency(Kind.ACCOUNT, account) if account else self._role_account(tax),
			"description": tax.string("description")[:REMARK_LENGTH],
			"tax_amount": money(tax.decimal("amount")),
			"cost_center": self.context.cost_center,
		}
		if not self.spec.selling:
			row |= {"category": "Total", "add_deduct_tax": "Add"}
		return row

	def _role_account(self, tax: Values) -> str:
		"""The connection's account for a charge the source books to none of its own: its tax account, or
		on a sale its shipping or fee account (a purchase's freight or fee needs the source's account)."""
		role = tax.optional_string("role") or TAX
		if role == TAX:
			return self.context.tax_account
		account = {SHIPPING: self.context.shipping_account, FEE: self.context.fee_account}.get(role)
		if not self.spec.selling or account is None:
			raise Unsupported(f'its {role} "{tax.string("description")}" names no account to book to')
		return account


class SalesOrderWriter(TradeWriter):
	spec = SALES_ORDER


class PurchaseOrderWriter(TradeWriter):
	spec = PURCHASE_ORDER


class SalesInvoiceWriter(TradeWriter):
	spec = SALES_INVOICE


class PurchaseInvoiceWriter(TradeWriter):
	spec = PURCHASE_INVOICE
