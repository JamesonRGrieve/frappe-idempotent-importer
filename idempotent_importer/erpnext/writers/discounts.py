# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discount codes: one coupon-based Pricing Rule per source discount, and a Coupon Code per code, scoped to
the connection's Webshop Store (the Webshop fork makes a code unique per store, and a store-less code
applies on every store). Two connections on different stores can hold the same code at once; a code a
person (or another connection) already holds on this store, or store-less, is Unsupported for this one
rather than taken over. A connection whose Public Site has no store can't mirror codes: a store-less copy
would apply on, and clash with, every store."""

from __future__ import annotations

import frappe
from frappe.utils import add_days, nowdate

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import money
from idempotent_importer.erpnext.writers.base import PART_HASH, Writer

APPLY_ON = {"transaction": "Transaction", "items": "Item Code", "groups": "Item Group"}
RATE_OR_DISCOUNT = {"percentage": "Discount Percentage", "amount": "Discount Amount"}
TITLE_LENGTH = 140


class DiscountWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		codes = values.strings("codes")
		store = self.context.webshop_store
		if codes and not store:
			raise Unsupported(
				"the connection's Public Site has no Webshop Store: a store-less coupon code would apply on "
				"every store"
			)
		for code in codes:
			ours = self.links.target(Kind.COUPON, f"{desired.gid}#{code}")
			holder = next((name for name in self._holders(code, store) if name != ours), None)
			if holder:
				raise Unsupported(
					f"the coupon code {code} is already held on this store by Coupon Code {holder}"
				)
		applies = values.string("applies")
		items = [self.dependency(Kind.PRODUCT, gid) for gid in values.strings("items")]
		groups = [self.dependency(Kind.COLLECTION, gid) for gid in values.strings("groups")]
		rule = self.existing(link) or frappe.new_doc("Pricing Rule")
		discount = values.string("discount")
		amount = money(values.decimal("value"))
		rule.update(
			{
				"title": (values.optional_string("description") or ", ".join(codes))[:TITLE_LENGTH],
				"apply_on": APPLY_ON[applies],
				"price_or_product_discount": "Price",
				"coupon_code_based": 1,
				"selling": 1,
				"buying": 0,
				"company": self.context.company,
				"currency": self.context.currency,
				"rate_or_discount": RATE_OR_DISCOUNT[discount],
				"discount_percentage": amount if discount == "percentage" else 0,
				"discount_amount": amount if discount == "amount" else 0,
				"apply_discount_on": "Net Total",
				"min_amt": money(values.decimal("min_amount")) if values.has("min_amount") else 0,
				"valid_from": values.optional_string("valid_from"),
				"valid_upto": values.optional_string("valid_upto"),
				"disable": 0,
			}
		)
		rule.set("items", [{"item_code": code} for code in items])
		rule.set("item_groups", [{"item_group": group} for group in groups])
		rule.save(ignore_permissions=True)
		for code in codes:
			self._coupon(desired.gid, code, rule.name, values)
		for coupon in self.links.children(Kind.COUPON, f"{desired.gid}#"):
			if coupon.gid.split("#", 1)[1] not in codes:
				# A code the source dropped: it expires (a Coupon Code has no disabled flag).
				frappe.db.set_value("Coupon Code", coupon.target.name, "valid_upto", add_days(nowdate(), -1))
				self.links.retire(Kind.COUPON, coupon.gid)
		return Target("Pricing Rule", rule.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value("Pricing Rule", link.target.name, "disable", 1)

	@staticmethod
	def _holders(code: str, store: str | None) -> list[str]:
		"""The Coupon Codes that would clash with ``code`` on ``store``: the same code on the store, or
		store-less (the database compares codes case-insensitively, as the fork's check does)."""
		rows = frappe.get_all("Coupon Code", filters={"coupon_code": code}, fields=["name", "webshop_store"])
		return [str(row.name) for row in rows if not row.webshop_store or row.webshop_store == store]

	def _coupon(self, gid: str, code: str, rule: str, values: Values) -> None:
		key = f"{gid}#{code}"
		existing = self.links.target(Kind.COUPON, key)
		doc = frappe.get_doc("Coupon Code", existing) if existing else frappe.new_doc("Coupon Code")
		doc.update(
			{
				"coupon_name": self.context.suffixed(code),
				"coupon_code": code,
				"webshop_store": self.context.webshop_store,
				"coupon_type": "Promotional",
				"pricing_rule": rule,
				"maximum_use": values.optional_integer("max_uses") or 0,
				"valid_from": values.optional_string("valid_from"),
				"valid_upto": values.optional_string("valid_upto"),
			}
		)
		doc.save(ignore_permissions=True)
		self.links.save(Kind.COUPON, key, Target("Coupon Code", doc.name), PART_HASH, 1)
