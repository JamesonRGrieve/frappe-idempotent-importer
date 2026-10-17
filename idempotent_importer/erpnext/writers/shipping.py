# SPDX-License-Identifier: AGPL-3.0-or-later
"""A source's shipping rates as Shipping Rules: each flat rate (to the countries it serves) a fixed-amount
selling Shipping Rule on the connection's company, booked to its shipping account. Each rate is a part of
its delivery profile, so a rate the source drops is disabled; the profile itself has no record of its own
and is mirrored onto the company that charges its rates."""

from __future__ import annotations

import frappe

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.hasher import part_hash
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import money
from idempotent_importer.erpnext.writers.base import Writer
from idempotent_importer.erpnext.writers.parties import country_of

SHIPPING_RULE = "Shipping Rule"


class DeliveryProfileWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		wanted: set[str] = set()
		for rule in values.records("rules"):
			key = f"{desired.gid}#{rule.string('gid')}"
			wanted.add(key)
			hash_ = part_hash(rule.all())
			part = self.links.find(Kind.SHIPPING_RULE, key)
			if part is not None and not part.retired and part.hash == hash_ and self.exists(part):
				continue
			doc = self.existing(part) or frappe.new_doc(SHIPPING_RULE)
			doc.update(
				{
					"label": self.context.suffixed(rule.string("label")),
					"disabled": int(not rule.flag("enabled")),
					"shipping_rule_type": "Selling",
					"company": self.context.company,
					"account": self.context.shipping_account,
					"cost_center": self.context.cost_center,
					"calculate_based_on": "Fixed",
					"shipping_amount": money(rule.decimal("amount")),
				}
			)
			doc.set("countries", [{"country": country_of(code)} for code in rule.strings("countries")])
			doc.save(ignore_permissions=True)
			self.links.save(Kind.SHIPPING_RULE, key, Target(SHIPPING_RULE, doc.name), hash_, 1)
		for part in self.links.children(Kind.SHIPPING_RULE, f"{desired.gid}#"):
			if part.gid not in wanted:
				self._disable(part)
		return Target("Company", self.context.company)

	def retire(self, link: Link) -> None:
		for part in self.links.children(Kind.SHIPPING_RULE, f"{link.gid}#"):
			self._disable(part)

	def _disable(self, part: Link) -> None:
		frappe.db.set_value(SHIPPING_RULE, part.target.name, "disabled", 1)
		self.links.retire(Kind.SHIPPING_RULE, part.gid)
