# SPDX-License-Identifier: AGPL-3.0-or-later
"""Discounts, through ERPNext's own pricing: a Pricing Rule per source discount (a Promotional Scheme when it
has tiers, or applies to several customers or customer groups), with a Coupon Code per code.

A rule discounts a price (a percentage, an amount, or a fixed rate) or gives an item (free, or at a rate;
the same item bought, recursively for every so many), on the transaction, some items (variants included),
some item groups or every item of the connection; after a minimum (and up to a maximum) quantity or spend;
for one customer or customer group; optionally on other items than the ones bought (buy X, get Y). An
automatic discount has no codes.

What a source's discount holds that ERPNext's pricing doesn't enforce (``contract.CONDITIONS``) is recorded on
the record, as JSON in ``importer_conditions`` (each condition's kind, the card that enforces it, and the
source's terms), for the Webshop fork to read. A discount with any condition the fork doesn't enforce yet is
held, failing closed: written disabled, marked in ``importer_hold`` ("held: awaiting enforcement (card N)"),
logged once as a warning, and its codes not redeemable. A Coupon Code has no disabled flag, so a held code
expires as a dropped code does (valid until yesterday, no start); the source's dates stay on its rule. A
code with conditions of its own is held alone. Once the fork enforces every condition (``ENFORCED``), the
discount's change signal changes and the next run enables it.

Coupon Codes are scoped to the connection's Webshop Store (the Webshop fork makes a code unique per store, and
a store-less code applies on every store). Two connections on different stores can hold the same code at
once; a code a person (or another connection) already holds on this store, or store-less, is Unsupported for
this one rather than taken over. A connection whose Public Site has no store can't mirror codes: a store-less
copy would apply on, and clash with, every store."""

from __future__ import annotations

import logging
from typing import Any

import frappe
from frappe.utils import add_days, nowdate

from idempotent_importer.contract import conditions_json, conditions_summary, hold_label
from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import UnresolvedDependency, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import money, quantity
from idempotent_importer.erpnext.writers.base import PART_HASH, Writer

logger = logging.getLogger(__name__)

PRICING_RULE, SCHEME = "Pricing Rule", "Promotional Scheme"
APPLY_ON = {
	"transaction": "Transaction",
	"items": "Item Code",
	"groups": "Item Group",
	"all_items": "Item Group",
}
OTHER = {"items": ("Item Code", "other_item_code"), "groups": ("Item Group", "other_item_group")}
RATE_OR_DISCOUNT = {"percentage": "Discount Percentage", "amount": "Discount Amount", "rate": "Rate"}
TITLE_LENGTH = 140
# A rule's benefit fields a scheme's slab doesn't have (a scheme's slab table says which it is).
RULE_ONLY = frozenset({"price_or_product_discount", "apply_discount_on"})


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
		scheme = self.is_scheme(values)
		if scheme and codes:
			raise Unsupported("a coupon code applies one Pricing Rule: no tiers or several customers")
		doctype = SCHEME if scheme else PRICING_RULE
		existing = self.existing(link)
		if link is not None and existing is not None and existing.doctype != doctype:
			# The discount gained or lost its tiers or customers: the old record is disabled, not reused.
			self.retire(link)
			existing = None
		doc = existing or frappe.new_doc(doctype)
		doc.update(self._common(values))
		if scheme:
			self._slabs(doc, values)
			self.save(doc, self.context.item_code(None, desired.gid))
		else:
			doc.update(self._rule(values, bool(codes)))
			self.save(doc)
		held = sorted(
			{
				*values.strings("held"),
				*(c for code in codes for c in code_limits(values, code).strings("held")),
			}
		)
		if held:
			conditions = [
				*values.records("conditions"),
				*(c for code in codes for c in code_limits(values, code).records("conditions")),
			]
			logger.warning("%s is %s: %s", desired.gid, hold_label(held), conditions_summary(conditions))
		for code in codes:
			self._coupon(desired.gid, code, doc.name, values)
		for coupon in self.links.children(Kind.COUPON, f"{desired.gid}#"):
			if coupon.gid.split("#", 1)[1] not in codes:
				# A code the source dropped: it expires (a Coupon Code has no disabled flag).
				frappe.db.set_value("Coupon Code", coupon.target.name, "valid_upto", yesterday())
				self.links.retire(Kind.COUPON, coupon.gid)
		return Target(doctype, doc.name)

	def retire(self, link: Link) -> None:
		if link.target.doctype == SCHEME:
			scheme = frappe.get_doc(SCHEME, link.target.name)
			if not scheme.disable:
				scheme.disable = 1
				scheme.save(ignore_permissions=True)
			return
		frappe.db.set_value(PRICING_RULE, link.target.name, "disable", 1)

	@staticmethod
	def is_scheme(values: Values) -> bool:
		"""A discount ERPNext holds as a Promotional Scheme (one Pricing Rule per tier and customer)."""
		return bool(values.records("tiers")) or any(
			len(values.strings(f)) > 1 for f in ("customers", "customer_groups")
		)

	# -- what a rule and a scheme share -----------------------------------------------------------------

	def _common(self, values: Values) -> dict[str, Any]:
		applies = values.string("applies")
		items = self._items(values)
		groups = [self.dependency(Kind.COLLECTION, gid) for gid in values.strings("groups")]
		if applies == "all_items":
			groups = [self.context.item_group]
		fields: dict[str, Any] = {
			"apply_on": APPLY_ON[applies],
			"mixed_conditions": int(values.has("mixed") and values.flag("mixed")),
			"selling": 1,
			"buying": 0,
			"company": self.context.company,
			"currency": values.optional_string("currency") or self.context.currency,
			"valid_from": values.optional_string("valid_from"),
			"valid_upto": values.optional_string("valid_upto"),
			"disable": int(bool(values.strings("held"))),
			"importer_conditions": conditions_json(values.records("conditions")),
			"importer_hold": hold_label(values.strings("held")) if values.has("held") else None,
			"apply_rule_on_other": "",
			**self._applicable_for(values),
		}
		other = values.record("other")
		if other is not None:
			on, field = OTHER[other.string("applies")]
			kind = Kind.PRODUCT if on == "Item Code" else Kind.COLLECTION
			fields |= {"apply_rule_on_other": on, field: self.dependency(kind, other.string("target"))}
		return fields | {
			"items": [{"item_code": code} for code in items],
			"item_groups": [{"item_group": g} for g in groups],
		}

	def _items(self, values: Values) -> list[str]:
		"""The products, and single variants, a discount applies to, as item codes."""
		items = [self.dependency(Kind.PRODUCT, gid) for gid in values.strings("items")]
		for variant in values.records("variants"):
			items.append(self._variant(variant.optional_string("product"), variant.string("variant")))
		return list(dict.fromkeys(items))

	def _variant(self, product: str | None, variant: str) -> str:
		"""A variant's item: by its product and itself, or (a source that names only the variant) by itself."""
		item = (
			self.links.target(Kind.VARIANT, f"{product}#{variant}")
			if product
			else self.links.ending(Kind.VARIANT, f"#{variant}")
		)
		if item is None:
			raise UnresolvedDependency(f"{variant} isn't mirrored yet")
		return item

	def _applicable_for(self, values: Values) -> dict[str, Any]:
		customers = [self.dependency(Kind.CUSTOMER, gid) for gid in values.strings("customers")]
		groups = [self.dependency(Kind.CUSTOMER_GROUP, gid) for gid in values.strings("customer_groups")]
		if customers and groups:
			raise Unsupported(
				"a discount for some customers and some customer groups has no one ERPNext rule"
			)
		if self.is_scheme(values):
			if customers:
				return {"applicable_for": "Customer", "customer": [{"customer": c} for c in customers]}
			if groups:
				return {
					"applicable_for": "Customer Group",
					"customer_group": [{"customer_group": g} for g in groups],
				}
			return {"applicable_for": ""}
		if customers:
			return {"applicable_for": "Customer", "customer": customers[0]}
		if groups:
			return {"applicable_for": "Customer Group", "customer_group": groups[0]}
		return {"applicable_for": "", "customer": None, "customer_group": None}

	# -- a Pricing Rule ---------------------------------------------------------------------------------

	def _rule(self, values: Values, coupon: bool) -> dict[str, Any]:
		codes = values.strings("codes")
		return {
			"title": (values.optional_string("description") or ", ".join(codes) or "Discount")[:TITLE_LENGTH],
			"coupon_code_based": int(coupon),
			"min_qty": quantity(values.decimal("min_qty")) if values.has("min_qty") else 0,
			"max_qty": quantity(values.decimal("max_qty")) if values.has("max_qty") else 0,
			"min_amt": money(values.decimal("min_amount")) if values.has("min_amount") else 0,
			"max_amt": money(values.decimal("max_amount")) if values.has("max_amount") else 0,
			**self._benefit(values),
		}

	def _benefit(self, values: Values) -> dict[str, Any]:
		"""What the discount gives: a price discount (with its rate or amount), or an item."""
		free = values.record("free")
		if free is None:
			discount = values.string("discount")
			amount = money(values.decimal("value"))
			return {
				"price_or_product_discount": "Price",
				"rate_or_discount": RATE_OR_DISCOUNT[discount],
				"discount_percentage": amount if discount == "percentage" else 0,
				"discount_amount": amount if discount == "amount" else 0,
				"rate": amount if discount == "rate" else 0,
				"apply_discount_on": "Net Total",
			}
		item = free.optional_string("item")
		variant = free.optional_string("variant")
		free_item = None
		if variant:
			free_item = self._variant(item, variant)
		elif item:
			free_item = self.dependency(Kind.PRODUCT, item)
		return {
			"price_or_product_discount": "Product",
			"rate_or_discount": "",
			"same_item": int(item is None),
			"free_item": free_item,
			"free_qty": quantity(free.decimal("qty")),
			"free_item_rate": money(free.decimal("rate")) if free.has("rate") else 0,
			"is_recursive": int(free.has("recursive") and free.flag("recursive")),
			"recurse_for": quantity(free.decimal("recurse_for")) if free.has("recurse_for") else 0,
		}

	# -- a Promotional Scheme ----------------------------------------------------------------------------

	def _slabs(self, scheme: Any, values: Values) -> None:
		"""The scheme's tiers (or its one discount) as its price or product discount slabs. ERPNext keys each
		slab's Pricing Rules by the slab row, so rows are updated in place; a tier the source drops is
		disabled."""
		tiers = values.records("tiers") or [Values({k: v for k, v in values.all().items() if k != "tiers"})]
		product = values.record("free") is not None
		field = "product_discount_slabs" if product else "price_discount_slabs"
		other = "price_discount_slabs" if product else "product_discount_slabs"
		rows = list(scheme.get(field) or [])
		title = values.optional_string("description") or "Discount"
		for index, tier in enumerate(tiers):
			slab = {
				"disable": 0,
				"rule_description": f"{title}, tier {index + 1}"[:TITLE_LENGTH],
				"min_qty": quantity(tier.decimal("min_qty")) if tier.has("min_qty") else 0,
				"max_qty": quantity(tier.decimal("max_qty")) if tier.has("max_qty") else 0,
				"min_amount": money(tier.decimal("min_amount")) if tier.has("min_amount") else 0,
				"max_amount": money(tier.decimal("max_amount")) if tier.has("max_amount") else 0,
				**{
					k: v
					for k, v in self._benefit(tier).items()
					if k not in RULE_ONLY and not (product and k == "rate_or_discount")
				},
			}
			if index < len(rows):
				rows[index].update(slab)
			else:
				scheme.append(field, slab)
		for row in [*rows[len(tiers) :], *(scheme.get(other) or [])]:
			row.disable = 1

	# -- coupon codes -----------------------------------------------------------------------------------

	@staticmethod
	def _holders(code: str, store: str | None) -> list[str]:
		"""The Coupon Codes that would clash with ``code`` on ``store``: the same code on the store, or
		store-less (the database compares codes case-insensitively, as the fork's check does)."""
		rows = frappe.get_all("Coupon Code", filters={"coupon_code": code}, fields=["name", "webshop_store"])
		return [str(row.name) for row in rows if not row.webshop_store or row.webshop_store == store]

	def _coupon(self, gid: str, code: str, rule: str, values: Values) -> None:
		"""One code: the discount's use limit, dates and conditions, with what the source gives the code of its
		own. A held code (its discount's conditions, or its own) is valid until yesterday."""
		key = f"{gid}#{code}"
		existing = self.links.target(Kind.COUPON, key)
		doc = frappe.get_doc("Coupon Code", existing) if existing else frappe.new_doc("Coupon Code")
		limits = code_limits(values, code)
		conditions = [*values.records("conditions"), *limits.records("conditions")]
		held = sorted({*values.strings("held"), *limits.strings("held")})
		fields: dict[str, Any] = {
			"coupon_name": self.context.suffixed(code),
			"coupon_code": code,
			"webshop_store": self.context.webshop_store,
			"coupon_type": "Promotional",
			"pricing_rule": rule,
			# The code comes from another platform: it may name its rule while the rule is held (disabled).
			"from_external_ecomm_platform": 1,
			"maximum_use": limits.optional_integer("max_uses") or values.optional_integer("max_uses") or 0,
			"valid_from": limits.optional_string("valid_from") or values.optional_string("valid_from"),
			"valid_upto": limits.optional_string("valid_upto") or values.optional_string("valid_upto"),
			"importer_conditions": conditions_json(conditions),
			"importer_hold": hold_label(held) if held else None,
		}
		if held:
			fields |= {"valid_from": None, "valid_upto": yesterday()}
		doc.update(fields)
		doc.save(ignore_permissions=True)
		self.links.save(Kind.COUPON, key, Target("Coupon Code", doc.name), PART_HASH, 1)


def code_limits(values: Values, code: str) -> Values:
	"""What the source gives one code of its own (its use limit, expiry and conditions), if anything."""
	return (values.record("code_limits") or Values({})).record(code) or Values({})


def yesterday() -> str:
	"""The last day a retired or held code was valid: a Coupon Code has no disabled flag."""
	return str(add_days(nowdate(), -1))
