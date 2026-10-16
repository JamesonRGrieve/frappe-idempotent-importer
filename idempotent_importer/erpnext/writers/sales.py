# SPDX-License-Identifier: AGPL-3.0-or-later
"""Orders as the books record a sale: a Sales Invoice (lines at their prices before discounts, the order's
discount on the net total, its taxes, shipping and fees as "Actual" charges), a Payment Entry for the
payment into the connection's deposit account, a credit note (a Sales Invoice return) and a refund Payment
Entry per refund, and the order's notes as comments. An unpaid order is a draft invoice, submitted once
paid.

Invoices never update stock: the source's stock levels are mirrored absolutely (see ``catalog``), so a
sale that moved stock as well would count it twice. Every submitted invoice's grand total is compared with
the source's total, exactly; an order whose books wouldn't match is rolled back and reported, never kept
approximately. A submitted invoice that changes at the source is cancelled and amended (ERPNext's own
correction), its payment with it."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

from idempotent_importer.contract import booked, order_remarks
from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import UnresolvedDependency, Unsupported
from idempotent_importer.core.fields import Field, Values
from idempotent_importer.core.hasher import part_hash
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import money, quantity, system_datetime
from idempotent_importer.erpnext.writers.base import Writer

INVOICE = "Sales Invoice"
PAYMENT = "Payment Entry"
DRAFT, SUBMITTED, CANCELLED = 0, 1, 2
UOM = "Nos"
ITEM_NAME_LENGTH = 140
INVOICE_FIELDS = (
	"number",
	"currency",
	"created_at",
	"customer",
	"guest",
	"billing",
	"shipping",
	"lines",
	"discount",
	"charges",
	"total",
	"remarks",
)
RETIRED_REMARK = "Retired: the order was cancelled at the source."


class OrderMismatch(Exception):
	"""ERPNext's totals for an invoice differ from the source's, so it isn't kept."""


class OrderWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		if values.string("currency") != self.context.currency:
			raise Unsupported(
				f"the order is in {values.string('currency')}, the company books in {self.context.currency}"
			)
		invoice = self._invoice(desired.gid, values)
		if invoice.docstatus == SUBMITTED:
			self._payment(desired.gid, values, invoice)
			self._refunds(desired.gid, values, invoice)
		self._notes(values, invoice)
		return Target(INVOICE, invoice.name)

	def retire(self, link: Link) -> None:
		for kind in (Kind.REFUND_PAYMENT, Kind.REFUND, Kind.PAYMENT):
			for part in self.links.children(kind, f"{link.gid}#"):
				cancel(part.target.doctype, part.target.name)
				self.links.retire(kind, part.gid)
		invoice = frappe.get_doc(INVOICE, link.target.name)
		if invoice.docstatus == SUBMITTED:
			invoice.cancel()
		elif invoice.docstatus == DRAFT and RETIRED_REMARK not in (invoice.remarks or ""):
			frappe.db.set_value(
				INVOICE, invoice.name, "remarks", f"{RETIRED_REMARK}\n{invoice.remarks or ''}".strip()
			)
		self.links.retire(Kind.INVOICE, f"{link.gid}#invoice")

	# -- invoice ---------------------------------------------------------------------------------------

	def _invoice(self, gid: str, values: Values) -> Any:
		key = f"{gid}#invoice"
		content: dict[str, Field] = {field: values.all().get(field) for field in INVOICE_FIELDS}
		wanted = part_hash(booked(content))
		part = self.links.find(Kind.INVOICE, key)
		invoice = self.existing(part) if part and not part.retired else None
		if invoice is None or invoice.docstatus == CANCELLED:
			invoice = frappe.new_doc(INVOICE)
			self._fill(invoice, values)
			invoice.insert(ignore_permissions=True)
		elif part is not None and part.hash != wanted:
			if invoice.docstatus == SUBMITTED:
				invoice = self._amend(gid, invoice, values)
			else:
				self._fill(invoice, values)
				invoice.save(ignore_permissions=True)
		self._check_total(invoice, values.decimal("total"))
		if values.flag("submitted") and invoice.docstatus == DRAFT:
			invoice.submit()
		self.links.save(Kind.INVOICE, key, Target(INVOICE, invoice.name), wanted, 1)
		return invoice

	def _amend(self, gid: str, invoice: Any, values: Values) -> Any:
		"""A submitted invoice changed at the source: cancel it (and its payment) and amend it."""
		for part in self.links.children(Kind.PAYMENT, f"{gid}#"):
			cancel(PAYMENT, part.target.name)
			self.links.retire(Kind.PAYMENT, part.gid)
		invoice = frappe.get_doc(INVOICE, invoice.name)
		invoice.cancel()
		amended = frappe.copy_doc(invoice)
		amended.amended_from = invoice.name
		self._fill(amended, values)
		amended.insert(ignore_permissions=True)
		return amended

	def _fill(self, invoice: Any, values: Values) -> None:
		posted = system_datetime(values.string("created_at"))
		customer = values.optional_string("customer")
		invoice.update(
			{
				"company": self.context.company,
				"customer": self.dependency(Kind.CUSTOMER, customer)
				if customer
				else self.context.guest_customer,
				"currency": self.context.currency,
				"conversion_rate": 1,
				"selling_price_list": self.context.price_list,
				"price_list_currency": self.context.currency,
				"plc_conversion_rate": 1,
				"set_posting_time": 1,
				"posting_date": posted.date(),
				"posting_time": posted.time(),
				"due_date": posted.date(),
				"update_stock": 0,
				"ignore_pricing_rule": 1,
				"disable_rounded_total": 1,
				"cost_center": self.context.cost_center,
				"po_no": values.string("number"),
				"remarks": order_remarks(values),
				"apply_discount_on": "Net Total",
				"additional_discount_percentage": 0,
				"discount_amount": money(values.decimal("discount")),
				"taxes_and_charges": None,
			}
		)
		invoice.set("items", [self._line(line) for line in values.records("lines")])
		invoice.set("taxes", [self._charge(charge) for charge in values.records("charges")])

	def _line(self, line: Values) -> dict[str, Any]:
		product = line.optional_string("product")
		variant = line.optional_string("variant")
		if product is None:
			item = self.context.fallback_item
		else:
			# A line names its variant; a product with a single (default) variant is one item.
			variant_item = self.links.target(Kind.VARIANT, f"{product}#{variant}") if variant else None
			item = variant_item or self.dependency(Kind.PRODUCT, product)
			if variant_item is None and frappe.db.get_value("Item", item, "has_variants"):
				raise UnresolvedDependency(f"{variant} of {product} isn't mirrored yet")
		name = line.string("name") or item
		return {
			"item_code": item,
			"item_name": name[:ITEM_NAME_LENGTH],
			"description": name,
			"qty": quantity(line.decimal("qty")),
			"uom": UOM,
			"conversion_factor": 1,
			"price_list_rate": money(line.decimal("rate")),
			"rate": money(line.decimal("rate")),
			"cost_center": self.context.cost_center,
		}

	def _charge(self, charge: Values) -> dict[str, Any]:
		accounts = {
			"tax": self.context.tax_account,
			"shipping": self.context.shipping_account,
			"fee": self.context.fee_account,
		}
		account = accounts[charge.string("kind")]
		if account is None:
			raise Unsupported(
				f'its {charge.string("kind")} "{charge.string("description")}" needs the connection\'s Fee Account'
			)
		return {
			"charge_type": "Actual",
			"account_head": account,
			"description": charge.string("description"),
			"tax_amount": money(charge.decimal("amount")),
			"cost_center": self.context.cost_center,
		}

	@staticmethod
	def _check_total(invoice: Any, total: Decimal) -> None:
		precision = int(invoice.precision("grand_total") or 2)
		booked = Decimal(str(invoice.grand_total)).quantize(Decimal(1).scaleb(-precision))
		if booked != total.quantize(Decimal(1).scaleb(-precision)):
			raise OrderMismatch(f"{invoice.doctype} would total {booked}; the source's total is {total}")

	# -- payment ----------------------------------------------------------------------------------------

	def _payment(self, gid: str, values: Values, invoice: Any) -> None:
		payment = values.record("payment")
		if payment is None:
			return
		key = f"{gid}#payment"
		wanted = part_hash({"payment": payment.all(), "invoice": invoice.name})
		part = self.links.find(Kind.PAYMENT, key)
		if part is not None and not part.retired and part.hash == wanted and self.exists(part):
			return
		if part is not None and not part.retired:
			cancel(PAYMENT, part.target.name)
		entry = self._pay(invoice, payment.string("date"), payment.optional_string("reference"))
		self.links.save(Kind.PAYMENT, key, Target(PAYMENT, entry.name), wanted, 1)

	def _pay(self, invoice: Any, paid_at: str, reference: str | None) -> Any:
		"""The invoice's outstanding amount paid in (or, for a credit note, paid out) on ``paid_at``."""
		when = system_datetime(paid_at)
		entry = get_payment_entry(INVOICE, invoice.name)
		if entry.payment_type == "Receive":
			entry.paid_to = self.context.deposit_account
		else:
			entry.paid_from = self.context.deposit_account
		entry.update(
			{
				"mode_of_payment": self.context.mode_of_payment,
				"posting_date": when.date(),
				"reference_no": reference or invoice.po_no or invoice.name,
				"reference_date": when.date(),
				"cost_center": self.context.cost_center,
			}
		)
		entry.setup_party_account_field()
		entry.set_missing_values()
		entry.set_amounts()
		entry.insert(ignore_permissions=True)
		entry.submit()
		return entry

	# -- refunds ----------------------------------------------------------------------------------------

	def _refunds(self, gid: str, values: Values, invoice: Any) -> None:
		wanted_keys: set[str] = set()
		for refund in values.records("refunds"):
			key = f"{gid}#{refund.string('gid')}"
			wanted_keys.add(key)
			wanted = part_hash(booked(refund.all()))
			part = self.links.find(Kind.REFUND, key)
			if part is not None and not part.retired and part.hash == wanted and self.exists(part):
				continue
			if part is not None and not part.retired:
				self._cancel_refund(key, part)
			note = self._credit_note(invoice, refund)
			self.links.save(Kind.REFUND, key, Target(INVOICE, note.name), wanted, 1)
			entry = self._pay(note, refund.string("date"), None)
			self.links.save(Kind.REFUND_PAYMENT, f"{key}#payment", Target(PAYMENT, entry.name), wanted, 1)
		for part in self.links.children(Kind.REFUND, f"{gid}#"):
			if part.gid not in wanted_keys:
				self._cancel_refund(part.gid, part)

	def _cancel_refund(self, key: str, part: Link) -> None:
		payment = self.links.target(Kind.REFUND_PAYMENT, f"{key}#payment")
		if payment:
			cancel(PAYMENT, payment)
			self.links.retire(Kind.REFUND_PAYMENT, f"{key}#payment")
		cancel(INVOICE, part.target.name)
		self.links.retire(Kind.REFUND, key)

	def _credit_note(self, invoice: Any, refund: Values) -> Any:
		"""A credit note for one refund, against the same customer: the returned lines (negative quantities,
		any amount refunded beyond them as an adjustment line) and the refunded charges."""
		posted = system_datetime(refund.string("date"))
		note = frappe.new_doc(INVOICE)
		note.update(
			{
				"company": self.context.company,
				"customer": invoice.customer,
				"is_return": 1,
				"currency": self.context.currency,
				"conversion_rate": 1,
				"selling_price_list": self.context.price_list,
				"price_list_currency": self.context.currency,
				"plc_conversion_rate": 1,
				"set_posting_time": 1,
				"posting_date": posted.date(),
				"posting_time": posted.time(),
				"due_date": posted.date(),
				"update_stock": 0,
				"ignore_pricing_rule": 1,
				"disable_rounded_total": 1,
				"cost_center": self.context.cost_center,
				"po_no": invoice.po_no,
				"remarks": f"Refund of {invoice.name}"
				+ (f": {refund.optional_string('reason')}" if refund.has("reason") else ""),
				"taxes_and_charges": None,
			}
		)
		note.set("items", [self._line(line) for line in refund.records("lines")])
		note.set("taxes", [self._charge(charge) for charge in refund.records("charges")])
		note.insert(ignore_permissions=True)
		self._check_total(note, -refund.decimal("amount"))
		note.submit()
		return note

	# -- notes ------------------------------------------------------------------------------------------

	def _notes(self, values: Values, invoice: Any) -> None:
		for note in values.records("notes"):
			key = note.string("gid")
			wanted = part_hash(note.all())
			part = self.links.find(Kind.ORDER_NOTE, key)
			if part is not None and part.hash == wanted and self.exists(part):
				continue
			comment = self.existing(part) or frappe.new_doc("Comment")
			author = note.optional_string("author") or "Source"
			audience = " (to the customer)" if note.flag("customer_note") else ""
			comment.update(
				{
					"comment_type": "Comment",
					"reference_doctype": INVOICE,
					"reference_name": invoice.name,
					"comment_by": author,
					"content": f"{note.string('text')}{audience} — {author}, {note.string('date')}",
				}
			)
			comment.save(ignore_permissions=True)
			self.links.save(Kind.ORDER_NOTE, key, Target("Comment", comment.name), wanted, 1)


def cancel(doctype: str, name: str) -> None:
	if frappe.db.get_value(doctype, name, "docstatus") == SUBMITTED:
		frappe.get_doc(doctype, name).cancel()
