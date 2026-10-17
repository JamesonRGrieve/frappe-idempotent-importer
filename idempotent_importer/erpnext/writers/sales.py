# SPDX-License-Identifier: AGPL-3.0-or-later
"""Orders as the books record a sale: a Sales Invoice (lines at their prices before discounts, the order's
discount, its taxes, shipping and fees as charges), a Payment Entry for the payment into the connection's
deposit account, a credit note (a Sales Invoice return) and a refund Payment Entry per refund, and the
order's notes as comments. An unpaid order is a draft invoice, submitted once paid. A draft order (a
proposal sent to a customer) is a draft Quotation, never booked.

Taxes are "Actual" amounts, unless the source's line prices include tax: then each included tax is an "On
Net Total" row at its rate, included in the line rates, and every row ERPNext computes must equal the
source's amount. An order in another currency than the company's is booked in its own currency at the
exchange rate the source recorded, and its total in the company's currency must equal the source's too.

Invoices never update stock: the source's stock levels are mirrored absolutely (see ``catalog``), so a
sale that moved stock as well would count it twice. Every invoice's grand total is compared with the
source's total, exactly; an order whose books wouldn't match is rolled back and reported, never kept
approximately. A submitted invoice that changes at the source is cancelled and amended (ERPNext's own
correction), its payment with it."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import frappe
from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

from idempotent_importer.contract import (
	FEE,
	INCLUDED_TAX,
	SHIPPING,
	SHIPPING_TAX,
	TAX,
	booked,
	order_remarks,
)
from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import UnresolvedDependency, Unsupported
from idempotent_importer.core.fields import Field, Values
from idempotent_importer.core.hasher import part_hash
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_text
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import money, quantity, system_datetime
from idempotent_importer.erpnext.writers.base import Writer

INVOICE = "Sales Invoice"
QUOTATION = "Quotation"
PAYMENT = "Payment Entry"
DRAFT, SUBMITTED, CANCELLED = 0, 1, 2
UOM = "Nos"
ACCOUNT_NAME_LENGTH = 140
# The charges ERPNext computes from a rate rather than books as given: each must come to the source's amount.
COMPUTED = frozenset({INCLUDED_TAX, SHIPPING_TAX})
ITEM_NAME_LENGTH = 140
TITLE_LENGTH = 140
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
# Booked only when set, so the books of an order that has none of them (most) never change.
OPTIONAL_INVOICE_FIELDS = ("taxes_included", "exchange_rate", "base_total")
RETIRED_REMARK = "Retired: the order was cancelled at the source."
RETIRED_MARK = "[Removed at source] "


class OrderMismatch(Exception):
	"""ERPNext's totals for an invoice differ from the source's, so it isn't kept."""


class SalesDocuments(Writer):
	"""What an invoice, a credit note and a quotation share: their lines, charges, currency and the exact
	check of their totals."""

	def currency(self, values: Values) -> dict[str, Any]:
		"""The document's currency fields: the company's, or the order's own at the source's rate."""
		currency = values.string("currency")
		if currency == self.context.currency:
			return {"currency": currency, "conversion_rate": 1}
		rate = values.optional_decimal("exchange_rate")
		if rate is None or values.optional_decimal("base_total") is None:
			raise Unsupported(
				f"the order is in {currency}, the company books in {self.context.currency}, and the source "
				"recorded no exchange rate and total in it"
			)
		base_currency = values.optional_string("base_currency")
		if base_currency is not None and base_currency != self.context.currency:
			raise Unsupported(
				f"the source booked the order's total in {base_currency}, not the company's {self.context.currency}"
			)
		# ERPNext books a customer's invoices in the currency of its receivable account.
		receivable = frappe.db.get_value(
			"Account",
			{
				"company": self.context.company,
				"account_type": "Receivable",
				"account_currency": currency,
				"is_group": 0,
			},
		)
		if not receivable:
			raise Unsupported(f"{self.context.company} has no receivable account in {currency} for the order")
		return {"currency": currency, "conversion_rate": float(rate), "debit_to": receivable}

	def common(self, values: Values, posted_at: str) -> dict[str, Any]:
		posted = system_datetime(posted_at)
		return {
			"company": self.context.company,
			**self.currency(values),
			"selling_price_list": self.context.price_list,
			"price_list_currency": self.context.currency,
			"plc_conversion_rate": 1,
			"ignore_pricing_rule": 1,
			"disable_rounded_total": 1,
			"cost_center": self.context.cost_center,
			"taxes_and_charges": None,
			"transaction_date": posted.date(),
			"posting_date": posted.date(),
			"posting_time": posted.time(),
		}

	def fill_lines(self, doc: Any, values: Values) -> None:
		included = values.has("taxes_included") and values.flag("taxes_included")
		doc.update(
			{
				"apply_discount_on": "Grand Total" if included else "Net Total",
				"additional_discount_percentage": 0,
				"discount_amount": money(values.decimal("discount")) if values.has("discount") else 0,
			}
		)
		lines = values.records("lines")
		charges = values.records("charges")
		accounts = self.included_tax_accounts(lines, charges)
		doc.set("items", [self._line(line, accounts) for line in lines])
		doc.set("taxes", self.charges(charges, accounts))

	def included_tax_accounts(self, lines: list[Values], charges: list[Values]) -> dict[str, str]:
		"""The account of each tax included in the line prices, by title. ERPNext keys a line's tax rates by
		account, so when the lines carry their own rates, several taxes are included, or shipping is taxed at
		another rate, each included tax books to an account of its own (``<title> - <abbr>`` beside the
		connection's tax account); a single tax at one rate for everything books to the connection's tax
		account."""
		titles = [c.string("description") for c in charges if c.string("kind") == INCLUDED_TAX]
		rates = {c.optional_decimal("rate") for c in charges if c.string("kind") in COMPUTED}
		if len(titles) <= 1 and len(rates) <= 1 and not any(line.has("taxes") for line in lines):
			return {}
		return {title: self._tax_account(title) for title in titles}

	def _tax_account(self, title: str) -> str:
		parent, currency = frappe.db.get_value(
			"Account", self.context.tax_account, ["parent_account", "account_currency"]
		)
		account_name = self.context.suffixed(title)[:ACCOUNT_NAME_LENGTH]
		existing = frappe.db.get_value(
			"Account", {"company": self.context.company, "account_name": account_name, "is_group": 0}
		)
		if existing:
			return str(existing)
		account = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"parent_account": parent,
				"company": self.context.company,
				"account_type": "Tax",
				"account_currency": currency,
			}
		)
		account.insert(ignore_permissions=True)
		return str(account.name)

	def _item_tax_template(self, rates: list[tuple[str, str, Decimal]]) -> str:
		"""The Item Tax Template that taxes a line at exactly ``rates`` (tax title, its account, percent),
		named after them ("GST at 5, PST at 7 - <abbr>": Frappe takes no "%" in a name) and shared by every
		line taxed alike."""
		title = self.context.suffixed(", ".join(f"{tax} at {decimal_text(rate)}" for tax, _, rate in rates))[
			:TITLE_LENGTH
		]
		existing = frappe.db.get_value("Item Tax Template", {"title": title, "company": self.context.company})
		if existing:
			return str(existing)
		template = frappe.get_doc(
			{
				"doctype": "Item Tax Template",
				"title": title,
				"company": self.context.company,
				"taxes": [{"tax_type": account, "tax_rate": float(rate)} for _, account, rate in rates],
			}
		)
		template.insert(ignore_permissions=True)
		return str(template.name)

	def _line(self, line: Values, accounts: dict[str, str] | None = None) -> dict[str, Any]:
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
		row: dict[str, Any] = {
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
		if line.has("taxes"):
			# A line exempt from every included tax has no template: the rows' rates (none) apply.
			rates = [
				(title, (accounts or {})[title], tax.decimal("rate"))
				for tax in line.records("taxes")
				if (title := tax.string("title"))
			]
			row["item_tax_template"] = self._item_tax_template(rates) if rates else None
		return row

	def charges(self, charges: list[Values], included: dict[str, str] | None = None) -> list[dict[str, Any]]:
		"""The Sales Taxes and Charges rows: amounts as "Actual" rows; a tax included in the line prices
		as an "On Net Total" row at its rate (none when each line carries its own, through its Item Tax
		Template), on its own account when it has one (``included_tax_accounts``); a tax on shipping at its
		rate of the shipping row before it (ERPNext keys a document's tax rates by account, so the shipping
		tax can't be an "Actual" row on the tax account an included rate uses)."""
		included = included or {}
		accounts = {
			TAX: self.context.tax_account,
			INCLUDED_TAX: self.context.tax_account,
			SHIPPING_TAX: self.context.tax_account,
			SHIPPING: self.context.shipping_account,
			FEE: self.context.fee_account,
		}
		rows: list[dict[str, Any]] = []
		for charge in charges:
			kind = charge.string("kind")
			description = charge.string("description")
			account = included.get(description, accounts[kind]) if kind == INCLUDED_TAX else accounts[kind]
			if account is None:
				raise Unsupported(f'its {kind} "{description}" needs the connection\'s Fee Account')
			row: dict[str, Any] = {
				"charge_type": "Actual",
				"account_head": account,
				"description": description,
				"tax_amount": money(charge.decimal("amount")),
				"cost_center": self.context.cost_center,
			}
			if kind == INCLUDED_TAX:
				rate = charge.optional_decimal("rate")
				row |= {
					"charge_type": "On Net Total",
					"rate": float(rate) if rate is not None else 0,
					"included_in_print_rate": 1,
					"tax_amount": 0,
				}
			elif kind == SHIPPING_TAX:
				if not rows or rows[-1]["charge_type"] != "Actual":
					raise Unsupported(f'its shipping tax "{description}" follows no shipping')
				row |= {
					"charge_type": "On Previous Row Amount",
					"row_id": str(len(rows)),
					"rate": float(charge.decimal("rate")),
					"tax_amount": 0,
				}
			rows.append(row)
		return rows

	@classmethod
	def check_order(cls, doc: Any, values: Values) -> None:
		cls.check_totals(
			doc, values.decimal("total"), values.records("charges"), values.optional_decimal("base_total")
		)

	@staticmethod
	def check_totals(
		doc: Any, total: Decimal, charges: list[Values], base_total: Decimal | None = None
	) -> None:
		"""The document's grand total (and, in another currency, its total in the company's currency, and
		any tax included in its prices) must be exactly the source's."""
		precision = int(doc.precision("grand_total") or 2)
		quantum = Decimal(1).scaleb(-precision)

		def same(booked_value: Any, source: Decimal, what: str) -> None:
			booked_amount = Decimal(str(booked_value)).quantize(quantum)
			if booked_amount != source.quantize(quantum):
				raise OrderMismatch(
					f"{doc.doctype}'s {what} would be {booked_amount}; the source's is {source}"
				)

		same(doc.grand_total, total, "total")
		if base_total is not None:
			same(doc.base_grand_total, base_total, "total in the company's currency")
		for row, charge in zip(doc.taxes, charges, strict=True):
			if charge.string("kind") in COMPUTED:
				same(row.tax_amount, charge.decimal("amount"), f'"{charge.string("description")}"')


class OrderWriter(SalesDocuments):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		self.currency(values)
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
		fields = values.all()
		content: dict[str, Field] = {field: fields.get(field) for field in INVOICE_FIELDS}
		content |= {field: fields[field] for field in OPTIONAL_INVOICE_FIELDS if fields.get(field)}
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
		self.check_order(invoice, values)
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
		amended.docstatus = DRAFT
		amended.amended_from = invoice.name
		self._fill(amended, values)
		amended.insert(ignore_permissions=True)
		return amended

	def _fill(self, invoice: Any, values: Values) -> None:
		customer = values.optional_string("customer")
		posted = system_datetime(values.string("created_at"))
		invoice.update(
			{
				**self.common(values, values.string("created_at")),
				"customer": self.dependency(Kind.CUSTOMER, customer)
				if customer
				else self.context.guest_customer,
				"set_posting_time": 1,
				"due_date": posted.date(),
				"update_stock": 0,
				"po_no": values.string("number"),
				"remarks": order_remarks(values),
			}
		)
		self.fill_lines(invoice, values)

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
		if invoice.currency != self.context.currency:
			# The order's own rate, not a rate of the day: the payment settles the invoice exactly.
			entry.source_exchange_rate = invoice.conversion_rate
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
			note = self._credit_note(invoice, values, refund)
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

	def _credit_note(self, invoice: Any, order: Values, refund: Values) -> Any:
		"""A credit note for one refund, against the same customer: the returned lines (negative quantities,
		any amount refunded beyond them as an adjustment line) and the refunded charges, in the order's
		currency at its rate."""
		posted = system_datetime(refund.string("date"))
		note = frappe.new_doc(INVOICE)
		note.update(
			{
				**self.common(order, refund.string("date")),
				"customer": invoice.customer,
				"is_return": 1,
				"set_posting_time": 1,
				"due_date": posted.date(),
				"update_stock": 0,
				"po_no": invoice.po_no,
				"remarks": f"Refund of {invoice.name}"
				+ (f": {refund.optional_string('reason')}" if refund.has("reason") else ""),
			}
		)
		note.set("items", [self._line(line) for line in refund.records("lines")])
		note.set("taxes", self.charges(refund.records("charges")))
		note.insert(ignore_permissions=True)
		self.check_totals(note, -refund.decimal("amount"), refund.records("charges"))
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


class DraftOrderWriter(SalesDocuments):
	"""A draft order (a proposal the merchant sent or kept open) as a draft Quotation to its customer. It
	is never submitted: a completed draft became an order, which is mirrored as such, so the draft's copy
	is retired (its title marked; a draft Quotation has no closed state)."""

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		quotation = self.existing(link) or frappe.new_doc(QUOTATION)
		if quotation.docstatus != DRAFT:
			quotation = frappe.new_doc(QUOTATION)
		customer = values.optional_string("customer")
		quotation.update(
			{
				**self.common(values, values.string("created_at")),
				"quotation_to": "Customer",
				"party_name": self.dependency(Kind.CUSTOMER, customer)
				if customer
				else self.context.guest_customer,
				"order_type": "Sales",
				"title": f"Draft order {values.string('number')}"[:TITLE_LENGTH],
				"valid_till": None,
			}
		)
		self.fill_lines(quotation, values)
		quotation.save(ignore_permissions=True)
		self.check_order(quotation, values)
		return Target(QUOTATION, quotation.name)

	def retire(self, link: Link) -> None:
		title = str(frappe.db.get_value(QUOTATION, link.target.name, "title") or "")
		if not title.startswith(RETIRED_MARK):
			frappe.db.set_value(QUOTATION, link.target.name, "title", f"{RETIRED_MARK}{title}"[:TITLE_LENGTH])


def cancel(doctype: str, name: str) -> None:
	if frappe.db.get_value(doctype, name, "docstatus") == SUBMITTED:
		frappe.get_doc(doctype, name).cancel()
