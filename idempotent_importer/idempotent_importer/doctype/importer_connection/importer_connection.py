# SPDX-License-Identifier: AGPL-3.0-or-later
import json
import re

import frappe
from frappe import _
from frappe.model.document import Document

from idempotent_importer.core.identity import SEGMENT

ABBREVIATION = re.compile(r"^[A-Z0-9]{1,10}$")


class ImporterConnection(Document):
	connection_id: str
	abbreviation: str
	store_url: str
	filters: str | None
	price_list: str
	company: str
	credential_hold: str | None
	credential_hold_since: str | None

	def validate(self) -> None:
		if not SEGMENT.match(self.connection_id or ""):
			frappe.throw(_("Connection ID must be lowercase letters, digits, dashes or underscores."))
		self.abbreviation = (self.abbreviation or "").strip().upper()
		if not ABBREVIATION.match(self.abbreviation):
			frappe.throw(_("Abbreviation must be 1 to 10 letters or digits."))
		self.store_url = (self.store_url or "").strip().rstrip("/")
		self.validate_filters()
		self.validate_currency()
		# Saving the connection is the operator's deliberate go-ahead: scheduled runs resume.
		self.credential_hold = None
		self.credential_hold_since = None

	def validate_filters(self) -> None:
		if not self.filters:
			return
		try:
			filters = json.loads(self.filters)
		except ValueError:
			frappe.throw(_("Filters must be a JSON object of kind → filter."))
		if not isinstance(filters, dict) or not all(isinstance(v, str) for v in filters.values()):
			frappe.throw(_("Filters must be a JSON object of kind → filter."))

	def validate_currency(self) -> None:
		price_list_currency = frappe.db.get_value("Price List", self.price_list, "currency")
		company_currency = frappe.db.get_value("Company", self.company, "default_currency")
		if price_list_currency and company_currency and price_list_currency != company_currency:
			frappe.throw(
				_(
					"The price list's currency ({0}) must be the company's ({1}): mirrored history is booked in it."
				).format(price_list_currency, company_currency)
			)
