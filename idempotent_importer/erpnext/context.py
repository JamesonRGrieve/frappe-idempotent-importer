# SPDX-License-Identifier: AGPL-3.0-or-later
"""One connection's ERPNext targets, read once per run from its Importer Connection and Public Site, and the
naming rules every writer shares."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import frappe
from frappe.utils import convert_utc_to_system_timezone

from idempotent_importer import naming
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.times import parse_utc


@dataclass(frozen=True)
class ConnectionContext:
	connection: str
	source: str
	abbreviation: str
	company: str
	currency: str
	route_prefix: str
	webshop_store: str | None
	item_group: str
	price_list: str
	warehouse: str
	customer_group: str
	territory: str
	guest_customer: str
	fallback_item: str
	mode_of_payment: str
	deposit_account: str
	cost_center: str
	tax_account: str
	shipping_account: str
	fee_account: str | None

	@classmethod
	def of(cls, doc: Any, source_id: str) -> ConnectionContext:
		company = str(doc.company)
		return cls(
			connection=str(doc.name),
			source=source_id,
			abbreviation=str(doc.abbreviation),
			company=company,
			currency=str(frappe.db.get_value("Company", company, "default_currency")),
			route_prefix=str(doc.route_prefix or "").strip("/"),
			webshop_store=doc.webshop_store or None,
			item_group=str(doc.item_group),
			price_list=str(doc.price_list),
			warehouse=str(doc.warehouse),
			customer_group=str(doc.customer_group),
			territory=str(doc.territory),
			guest_customer=str(doc.guest_customer),
			fallback_item=str(doc.fallback_item),
			mode_of_payment=str(doc.mode_of_payment),
			deposit_account=str(doc.deposit_account),
			cost_center=str(doc.cost_center or frappe.get_cached_value("Company", company, "cost_center")),
			tax_account=str(doc.tax_account),
			shipping_account=str(doc.shipping_account),
			fee_account=doc.fee_account or None,
		)

	def suffixed(self, name: str) -> str:
		return naming.suffixed(name, self.abbreviation)

	def item_code(self, sku: str | None, gid: str) -> str:
		return naming.item_code(self.abbreviation, sku, gid)

	def route(self, *segments: str) -> str:
		return naming.route(self.route_prefix, *segments)

	def identity(self, type_: str, id_: str) -> str:
		"""An identity this connection mints for something the source has no id for (an author, a URL)."""
		return Identity.of(self.source, self.connection, type_, id_)

	def media_gid(self, url: str) -> str:
		return self.identity("Media", hashlib.sha256(url.encode()).hexdigest()[:32])


def system_datetime(utc: str) -> datetime:
	"""A UTC ``YYYY-MM-DDTHH:MM:SSZ`` time in the site's system time zone, as Frappe stores datetimes."""
	return convert_utc_to_system_timezone(parse_utc(utc)).replace(tzinfo=None)


def money(value: Decimal) -> float:
	"""A Decimal for an ERPNext Currency field. ERPNext keeps currency as fixed-point DECIMAL columns but
	takes Python floats; every mirrored amount is first checked exact at the currency's precision, and
	totals are compared exactly after ERPNext computes them (see the order writer)."""
	return float(value)


def quantity(value: Decimal) -> float:
	return float(value)
