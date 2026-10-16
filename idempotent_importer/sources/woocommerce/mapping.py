# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure WooCommerce → desired ERPNext state, one mapper per kind, in the shared field contract
(``idempotent_importer.contract``). Each mapper translates WooCommerce's vocabulary (statuses, coupon
types, catalog visibility) into that contract; writers never see a WooCommerce value."""

from __future__ import annotations

import html
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

from idempotent_importer.contract import (
	Address,
	Charge,
	OrderLine,
	address_fields,
	charge_fields,
	line_fields,
	order_line,
	rating_fraction,
	refund_balance,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import path_of, redirect_target
from idempotent_importer.core.source import ConnectionConfig

SIMPLE = "simple"
VARIABLE = "variable"
PUBLISHED = "publish"
HIDDEN = "hidden"
# Orders whose payment WooCommerce has taken (the books show the sale and its payment).
PAID_STATUSES = frozenset({"processing", "completed", "refunded"})
# Orders awaiting payment: a draft invoice, submitted once paid.
UNPAID_STATUSES = frozenset({"pending", "on-hold"})
# Coupon restrictions ERPNext's Pricing Rule + Coupon Code can't enforce: mirroring the coupon without
# them would let it be used more widely than at the source.
UNENFORCEABLE_COUPON_RESTRICTIONS = (
	"excluded_product_ids",
	"excluded_product_categories",
	"email_restrictions",
)
WOO_MAX_RATING = 5


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, type_: str, id_: int | str) -> str:
		return self.config.identity(type_, id_)


def text(value: str | None) -> str | None:
	"""WooCommerce sends names HTML-escaped ("Hats &amp; Gloves")."""
	return html.unescape(value).strip() or None if value else None


class CategoryMapper:
	kind = Kind.COLLECTION
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		parent = node.integer("parent")
		image = node.child("image")
		fields: Fields = {
			"name": text(node.string("name")) or node.string("slug"),
			"slug": node.string("slug"),
			"parent": self._context.gid("ProductCategory", parent) if parent else None,
			"description": node.optional_string("description"),
			"image": image.optional_string("src") if image else None,
		}
		legacy = path_of(f"/product-category/{node.string('slug')}")
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else ())


class ProductMapper:
	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		product_type = node.string("type")
		if product_type not in (SIMPLE, VARIABLE):
			raise Unsupported(f"a {product_type} product has no ERPNext item type (coverage/woocommerce.md)")
		published = node.string("status") == PUBLISHED
		attributes = (
			[a for a in node.children("attributes") if a.flag("variation")]
			if product_type == VARIABLE
			else []
		)
		specifications = [
			a for a in node.children("attributes") if product_type == SIMPLE or not a.flag("variation")
		]
		fields: Fields = {
			"type": product_type,
			"name": text(node.string("name")) or node.string("slug"),
			"slug": node.string("slug"),
			"description": node.optional_string("description"),
			"short_description": node.optional_string("short_description"),
			"sku": node.optional_string("sku"),
			"barcode": node.optional_string("global_unique_id"),
			"brand": text(node.children("brands")[0].string("name")) if node.children("brands") else None,
			"collections": [
				self._context.gid("ProductCategory", c.integer("id")) for c in node.children("categories")
			],
			"images": [
				{"url": image.string("src"), "alt": image.optional_string("alt")}
				for image in node.children("images")
			],
			"published": published and node.string("catalog_visibility") != HIDDEN,
			# A variable product tracks stock when its variations do (variants share the template's setting).
			"is_stock_item": not node.flag("virtual")
			and (
				node.flag("manage_stock")
				or any(v.get("manage_stock") is True for v in node.children("variation_nodes"))
			),
			"weight": self._weight(node, node.optional_string("weight_unit")),
			"price": node.optional_decimal("regular_price"),
			"sale": self._sale(node),
			"stock_qty": self._stock(node),
			"attributes": [
				{"name": text(a.string("name")) or "", "values": [text(v) or v for v in a.strings("options")]}
				for a in attributes
			],
			"specifications": [
				{
					"label": text(a.string("name")) or "",
					"value": ", ".join(text(v) or v for v in a.strings("options")),
				}
				for a in specifications
			],
			"variants": [
				self._variant(v, node.optional_string("weight_unit"))
				for v in node.children("variation_nodes")
			],
		}
		if product_type == VARIABLE and not fields["variants"]:
			raise Unsupported("a variable product with no variations has nothing to sell")
		legacy = path_of(node.optional_string("permalink"))
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else ())

	def _variant(self, variant: Node, weight_unit: str | None) -> Field:
		image = variant.child("image")
		return {
			"gid": self._context.gid("ProductVariation", variant.integer("id")),
			"sku": variant.optional_string("sku"),
			"barcode": variant.optional_string("global_unique_id"),
			"attributes": {
				text(a.string("name")) or "": text(a.string("option")) or ""
				for a in variant.children("attributes")
			},
			"price": variant.optional_decimal("regular_price"),
			"sale": self._sale(variant),
			"stock_qty": self._stock(variant),
			"weight": self._weight(variant, weight_unit),
			"image": image.optional_string("src") if image else None,
			"enabled": variant.string("status") == PUBLISHED,
		}

	@staticmethod
	def _sale(node: Node) -> Field:
		price = node.optional_decimal("sale_price")
		if price is None:
			return None
		return {
			"price": price,
			"from": node.optional_time("date_on_sale_from_gmt"),
			"to": node.optional_time("date_on_sale_to_gmt"),
		}

	@staticmethod
	def _stock(node: Node) -> Decimal | None:
		if node.get("manage_stock") is not True:
			return None
		quantity = node.optional_integer("stock_quantity")
		return Decimal(quantity or 0)

	@staticmethod
	def _weight(node: Node, unit: str | None) -> Field:
		"""A weight in the store's unit (WooCommerce keeps one unit for the whole store)."""
		weight = node.optional_decimal("weight")
		if weight is None or unit is None:
			return None
		return {"value": weight, "unit": unit}


class CustomerMapper:
	kind = Kind.CUSTOMER
	version = 1

	def map(self, node: Node) -> Desired | None:
		billing = node.child("billing")
		shipping = node.child("shipping")
		first, last = node.text("first_name"), node.text("last_name")
		company = billing.text("company") if billing else ""
		person = f"{first} {last}".strip()
		addresses: list[Field] = [
			address_fields(address)
			for address in (woo_address("Billing", billing), woo_address("Shipping", shipping))
			if address is not None
		]
		fields: Fields = {
			"name": person or company or node.string("email"),
			"type": "Company" if company and not person else "Individual",
			"first_name": first or None,
			"last_name": last or None,
			"company": company or None,
			"email": node.optional_string("email"),
			"phone": billing.optional_string("phone") if billing else None,
			"addresses": addresses,
		}
		return Desired(self.kind, node.id(), fields)


def woo_address(address_type: str, node: Node | None) -> Address | None:
	"""A WooCommerce billing or shipping address; None without a street and city."""
	if node is None or not node.text("address_1") or not node.text("city"):
		return None
	return Address(
		address_type=address_type,
		name=" ".join(part for part in (node.text("first_name"), node.text("last_name")) if part) or None,
		company=node.optional_string("company"),
		line1=node.string("address_1"),
		line2=node.optional_string("address_2"),
		city=node.string("city"),
		state=node.optional_string("state"),
		postcode=node.optional_string("postcode"),
		country_code=node.optional_string("country"),
		email=node.optional_string("email"),
		phone=node.optional_string("phone"),
	)


class CouponMapper:
	kind = Kind.DISCOUNT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		for restriction in UNENFORCEABLE_COUPON_RESTRICTIONS:
			if node.get(restriction):
				raise Unsupported(f"its {restriction} restriction has no ERPNext coupon equivalent")
		if node.get("exclude_sale_items") is True:
			raise Unsupported("excluding sale items has no ERPNext coupon equivalent")
		if node.get("free_shipping") is True:
			raise Unsupported("free shipping has no ERPNext coupon equivalent")
		for limit in ("usage_limit_per_user", "limit_usage_to_x_items"):
			if node.optional_integer(limit):
				raise Unsupported(f"its {limit} limit has no ERPNext coupon equivalent")
		if node.optional_decimal("maximum_amount"):
			raise Unsupported("a maximum spend has no ERPNext coupon equivalent")
		products: list[Field] = [self._context.gid("Product", pid) for pid in node.integers("product_ids")]
		groups: list[Field] = [
			self._context.gid("ProductCategory", cid) for cid in node.integers("product_categories")
		]
		if products and groups:
			raise Unsupported(
				"a coupon limited to both products and categories has no single ERPNext pricing rule"
			)
		discount_type = node.string("discount_type")
		match discount_type:
			case "percent":
				kind, applies = "percentage", "items" if products else "groups" if groups else "transaction"
			case "fixed_cart":
				if products or groups:
					raise Unsupported(
						"a fixed cart discount limited to some products has no ERPNext equivalent"
					)
				kind, applies = "amount", "transaction"
			case "fixed_product":
				kind, applies = "amount", "items" if products else "groups" if groups else "all_items"
			case _:
				raise Unsupported(f"the {discount_type} discount type has no ERPNext pricing rule")
		if applies == "all_items":
			raise Unsupported("a fixed discount on every product has no ERPNext coupon pricing rule")
		minimum = node.optional_decimal("minimum_amount")
		fields: Fields = {
			"codes": [node.string("code")],
			"description": node.optional_string("description"),
			"discount": kind,
			"applies": applies,
			"value": node.decimal("amount"),
			"items": products,
			"groups": groups,
			"min_amount": minimum if minimum else None,
			"valid_from": None,
			"valid_upto": (node.optional_time("date_expires_gmt") or "")[:10] or None,
			"max_uses": node.optional_integer("usage_limit"),
		}
		return Desired(self.kind, node.id(), fields)


class OrderMapper:
	"""An order's invoice, payment, credit notes and notes. Unpaid orders are draft invoices; cancelled,
	failed and checkout-draft orders have none (an earlier copy is retired)."""

	kind = Kind.ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		status = node.string("status")
		if status not in PAID_STATUSES | UNPAID_STATUSES:
			return None
		paid_at = node.optional_time("date_paid_gmt")
		submitted = status in PAID_STATUSES and paid_at is not None
		customer_id = node.integer("customer_id")
		billing = node.child("billing")
		currency = node.string("currency")
		fields: Fields = {
			"number": node.text("number") or str(node.integer("source_id")),
			"currency": currency,
			"created_at": node.time("date_created_gmt"),
			"submitted": submitted,
			"customer": self._context.gid("Customer", customer_id) if customer_id else None,
			"guest": None if customer_id else self._guest(billing),
			"billing": address_fields(a) if (a := woo_address("Billing", billing)) else None,
			"shipping": address_fields(a) if (a := woo_address("Shipping", node.child("shipping"))) else None,
			"lines": [line_fields(self._line(item, currency)) for item in node.children("line_items")],
			"discount": node.decimal("discount_total"),
			"charges": [charge_fields(charge) for charge in self._charges(node)],
			"total": node.decimal("total"),
			"payment": (
				{
					"method": node.text("payment_method_title") or node.text("payment_method"),
					"reference": node.optional_string("transaction_id"),
					"date": paid_at,
				}
				if submitted
				else None
			),
			"refunds": [self._refund(refund, currency) for refund in node.children("refund_nodes")]
			if submitted
			else [],
			"notes": [self._note(note) for note in node.children("note_nodes")],
			"remarks": node.optional_string("customer_note"),
		}
		return Desired(self.kind, node.id(), fields)

	@staticmethod
	def _guest(billing: Node | None) -> Field:
		if billing is None:
			return None
		name = " ".join(part for part in (billing.text("first_name"), billing.text("last_name")) if part)
		return {
			"name": name or None,
			"email": billing.optional_string("email"),
			"phone": billing.optional_string("phone"),
		}

	def _line(self, item: Node, currency: str) -> OrderLine:
		product_id = item.integer("product_id")
		variation_id = item.integer("variation_id")
		return order_line(
			str(item.integer("id")),
			self._context.gid("Product", product_id) if product_id else None,
			self._context.gid("ProductVariation", variation_id) if variation_id else None,
			text(item.string("name")) or "",
			Decimal(item.integer("quantity")),
			item.decimal("subtotal"),
			currency,
		)

	@staticmethod
	def _charges(node: Node) -> list[Charge]:
		charges = [
			Charge(
				"tax",
				text(tax.text("label")) or "Tax",
				tax.decimal("tax_total") + tax.decimal("shipping_tax_total"),
			)
			for tax in node.children("tax_lines")
		]
		charges += [
			Charge("shipping", text(s.text("method_title")) or "Shipping", s.decimal("total"))
			for s in node.children("shipping_lines")
		]
		charges += [
			Charge("fee", text(f.text("name")) or "Fee", f.decimal("total"))
			for f in node.children("fee_lines")
		]
		return [charge for charge in charges if charge.amount != 0]

	def _refund(self, refund: Node, currency: str) -> Field:
		gid = self._context.gid("OrderRefund", refund.integer("id"))
		returned = [
			self._line(item, currency) for item in refund.children("line_items") if item.integer("quantity")
		]
		charges = self._charges(refund)
		balance = refund_balance(f"{gid}#adjustment", refund.decimal("amount"), returned, charges, currency)
		lines: list[Field] = [line_fields(line) for line in [*returned, *([balance] if balance else [])]]
		return {
			"gid": gid,
			"date": refund.time("date_created_gmt"),
			"reason": refund.optional_string("reason"),
			"amount": refund.decimal("amount"),
			"lines": lines,
			"charges": [charge_fields(charge) for charge in charges],
		}

	def _note(self, note: Node) -> Field:
		return {
			"gid": self._context.gid("OrderNote", note.integer("id")),
			"date": note.time("date_created_gmt"),
			"author": note.optional_string("author"),
			"text": note.string("note"),
			"customer_note": note.flag("customer_note"),
		}


class PageMapper:
	kind = Kind.PAGE
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		parent = node.optional_integer("parent") or 0
		fields: Fields = {
			"title": text(node.required_child("title").string("rendered")) or node.string("slug"),
			"slug": node.string("slug"),
			"parent": self._context.gid("Page", parent) if parent else None,
			"published": node.string("status") == PUBLISHED,
			"html": node.required_child("content").text("rendered"),
		}
		legacy = path_of(node.optional_string("link"))
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else ())


class PostCategoryMapper:
	kind = Kind.BLOG
	version = 1

	def map(self, node: Node) -> Desired | None:
		fields: Fields = {
			"title": text(node.string("name")) or node.string("slug"),
			"slug": node.string("slug"),
			"description": node.optional_string("description"),
		}
		legacy = path_of(node.optional_string("link"))
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else ())


class PostMapper:
	kind = Kind.ARTICLE
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		categories = node.integers("categories")
		if not categories:
			raise Unsupported("a post without a category has no Blog Category")
		author = node.integer("author")
		fields: Fields = {
			"title": text(node.required_child("title").string("rendered")) or node.string("slug"),
			"slug": node.string("slug"),
			"category": self._context.gid("Category", categories[0]),
			"author": {"key": str(author), "name": node.optional_string("author_name") or f"Author {author}"},
			"published": node.string("status") == PUBLISHED,
			"published_on": (node.optional_time("date_gmt") or node.time("modified_gmt"))[:10],
			"html": node.required_child("content").text("rendered"),
			"intro": node.required_child("excerpt").optional_string("rendered")
			if node.child("excerpt")
			else None,
		}
		warnings = (
			(
				f"filed under {len(categories)} categories; a Blog Post has one, so it is filed under the first",
			)
			if len(categories) > 1
			else ()
		)
		legacy = path_of(node.optional_string("link"))
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else (), warnings)


class ReviewMapper:
	kind = Kind.REVIEW
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if node.string("status") != "approved":
			return None
		fields: Fields = {
			"product": self._context.gid("Product", node.integer("product_id")),
			"author_name": node.string("reviewer"),
			"author_email": node.optional_string("reviewer_email"),
			"text": node.string("review"),
			"rating": rating_fraction(node.integer("rating"), WOO_MAX_RATING),
			"published_on": node.time("date_created_gmt")[:10],
		}
		return Desired(self.kind, node.id(), fields)


class RedirectMapper:
	kind = Kind.URL_REDIRECT
	version = 1

	def __init__(self, store_host: str | None) -> None:
		self._store_host = store_host

	def map(self, node: Node) -> Desired | None:
		if node.string("status") != "enabled" or node.string("action_type") != "url":
			return None
		if node.flag("regex"):
			raise Unsupported("a regular-expression redirect can't be scoped to one site's prefix")
		source = path_of(node.string("url"))
		target_url = node.required_child("action_data").optional_string("url")
		target = redirect_target(target_url, self._store_host) if target_url else None
		if source is None or target is None:
			return None
		fields: Fields = {"source": source, "target": target, "status": node.integer("action_code")}
		return Desired(self.kind, node.id(), fields)


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		CategoryMapper(context),
		ProductMapper(context),
		CustomerMapper(),
		CouponMapper(context),
		OrderMapper(context),
		PageMapper(context),
		PostCategoryMapper(),
		PostMapper(context),
		ReviewMapper(context),
		RedirectMapper(urlsplit(config.store_url).hostname),
	]
