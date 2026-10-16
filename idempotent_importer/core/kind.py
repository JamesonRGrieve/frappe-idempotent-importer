# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every kind of object the importer tracks in its link table, whatever the source."""

from __future__ import annotations

from enum import StrEnum


class Kind(StrEnum):
	"""The values are persisted in Importer Link; never rename one. Synced kinds run in SYNC_ORDER so
	that what an object refers to is mirrored before it. The other kinds identify parts of a synced
	object that get their own ERPNext record (a variant's Item, a refund's credit note)."""

	COLLECTION = "collection"
	PRODUCT = "product"
	VARIANT = "variant"
	PRICE = "price"
	FILE = "file"
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
	PAGE = "page"
	BLOG = "blog"
	BLOGGER = "blogger"
	ARTICLE = "article"
	COMMENT = "comment"
	REVIEW = "review"
	URL_REDIRECT = "url_redirect"
	LEGACY_REDIRECT = "legacy_redirect"

	@classmethod
	def parse(cls, value: str) -> Kind:
		try:
			return cls(value)
		except ValueError:
			raise ValueError(f"Unknown kind: {value}") from None


SYNC_ORDER: tuple[Kind, ...] = (
	Kind.COLLECTION,
	Kind.PRODUCT,
	Kind.CUSTOMER,
	Kind.DISCOUNT,
	Kind.ORDER,
	Kind.PAGE,
	Kind.BLOG,
	Kind.ARTICLE,
	Kind.COMMENT,
	Kind.REVIEW,
	Kind.URL_REDIRECT,
)
