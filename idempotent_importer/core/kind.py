# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every kind of object the importer tracks in its link table, whatever the source."""

from __future__ import annotations

from enum import StrEnum


class Kind(StrEnum):
	"""The values are persisted in Importer Link; never rename one. Synced kinds run in SYNC_ORDER so
	that what an object refers to is mirrored before it. The other kinds identify parts of a synced
	object that get their own ERPNext record (a variant's Item, a refund's credit note)."""

	ASSET = "asset"
	COLLECTION = "collection"
	PRODUCT = "product"
	VARIANT = "variant"
	PRICE = "price"
	FILE = "file"
	TAG = "tag"
	CUSTOMER_GROUP = "customer_group"
	CUSTOMER = "customer"
	CONTACT = "contact"
	ADDRESS = "address"
	DISCOUNT = "discount"
	COUPON = "coupon"
	ORDER = "order"
	INVOICE = "invoice"
	PAYMENT = "payment"
	REFUND = "refund"
	REFUND_PAYMENT = "refund_payment"
	ORDER_NOTE = "order_note"
	SHIPMENT = "shipment"
	DRAFT_ORDER = "draft_order"
	PAGE = "page"
	POLICY = "policy"
	BLOG = "blog"
	BLOGGER = "blogger"
	ARTICLE = "article"
	COMMENT = "comment"
	REVIEW = "review"
	URL_REDIRECT = "url_redirect"
	LEGACY_REDIRECT = "legacy_redirect"
	DELIVERY_PROFILE = "delivery_profile"
	SHIPPING_RULE = "shipping_rule"
	# The books and the business around them (ERP and HR sources).
	ACCOUNT = "account"
	COST_CENTER = "cost_center"
	WAREHOUSE = "warehouse"
	SUPPLIER = "supplier"
	PROJECT = "project"
	LEAD = "lead"
	OPPORTUNITY = "opportunity"
	DEPARTMENT = "department"
	DESIGNATION = "designation"
	EMPLOYEE = "employee"
	BRANCH = "branch"
	LEAVE_TYPE = "leave_type"
	SHIFT_ASSIGNMENT = "shift_assignment"
	EMPLOYEE_CHECKIN = "employee_checkin"
	LEAVE_APPLICATION = "leave_application"
	ATTENDANCE = "attendance"
	PROSPECT = "prospect"
	OPPORTUNITY_TYPE = "opportunity_type"
	COMMUNICATION = "communication"
	EVENT = "event"
	TODO = "todo"
	SALES_ORDER = "sales_order"
	PURCHASE_ORDER = "purchase_order"
	SALES_INVOICE = "sales_invoice"
	PURCHASE_INVOICE = "purchase_invoice"
	PAYMENT_ENTRY = "payment_entry"
	JOURNAL_ENTRY = "journal_entry"
	TIMESHEET = "timesheet"
	TAX_RATE = "tax_rate"
	# Stored value, written to the Square account it lives in (Square Gift Cards, Square Loyalty).
	GIFT_CARD = "gift_card"
	LOYALTY_ACCOUNT = "loyalty_account"

	@classmethod
	def parse(cls, value: str) -> Kind:
		try:
			return cls(value)
		except ValueError:
			raise ValueError(f"Unknown kind: {value}") from None


SYNC_ORDER: tuple[Kind, ...] = (
	Kind.ACCOUNT,
	Kind.COST_CENTER,
	Kind.TAX_RATE,
	Kind.WAREHOUSE,
	Kind.ASSET,
	Kind.COLLECTION,
	Kind.PRODUCT,
	Kind.CUSTOMER_GROUP,
	Kind.CUSTOMER,
	Kind.SUPPLIER,
	Kind.DEPARTMENT,
	Kind.DESIGNATION,
	Kind.BRANCH,
	Kind.EMPLOYEE,
	Kind.LEAVE_TYPE,
	Kind.SHIFT_ASSIGNMENT,
	Kind.EMPLOYEE_CHECKIN,
	# A leave HRMS books marks its days' attendance itself, before the source's own attendance.
	Kind.LEAVE_APPLICATION,
	Kind.ATTENDANCE,
	Kind.PROJECT,
	# Time worked on a project, for its customer.
	Kind.TIMESHEET,
	Kind.LEAD,
	# An organisation lists its people, who are leads first.
	Kind.PROSPECT,
	Kind.OPPORTUNITY_TYPE,
	Kind.OPPORTUNITY,
	Kind.COMMUNICATION,
	Kind.EVENT,
	Kind.TODO,
	Kind.DISCOUNT,
	Kind.ORDER,
	Kind.GIFT_CARD,
	Kind.LOYALTY_ACCOUNT,
	Kind.DRAFT_ORDER,
	Kind.SALES_ORDER,
	Kind.PURCHASE_ORDER,
	Kind.SALES_INVOICE,
	Kind.PURCHASE_INVOICE,
	Kind.PAYMENT_ENTRY,
	Kind.JOURNAL_ENTRY,
	Kind.PAGE,
	Kind.POLICY,
	Kind.BLOG,
	Kind.ARTICLE,
	Kind.COMMENT,
	Kind.REVIEW,
	Kind.URL_REDIRECT,
	Kind.DELIVERY_PROFILE,
)
