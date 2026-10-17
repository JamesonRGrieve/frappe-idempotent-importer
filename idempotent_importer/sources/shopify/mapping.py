# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Shopify → desired ERPNext state, one mapper per kind, in the shared field contract
(``idempotent_importer.contract``). Shopify's enums and units (statuses, weight units, discount types)
are translated here; writers never see a Shopify value. Identities arrive already scoped to the
connection (see ShopifyReader)."""

from __future__ import annotations

from decimal import Decimal

from idempotent_importer.contract import (
	Address,
	Charge,
	OrderLine,
	address_fields,
	charge_fields,
	line_fields,
	order_line,
	refund_balance,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import path_of, redirect_target
from idempotent_importer.core.source import ConnectionConfig

ACTIVE = "ACTIVE"
DEFAULT_OPTION = "Title"
WEIGHT_UNITS = {"GRAMS": "g", "KILOGRAMS": "kg", "OUNCES": "oz", "POUNDS": "lbs"}
# Financial statuses of an order whose payment Shopify has taken.
PAID = frozenset({"PAID", "PARTIALLY_REFUNDED", "REFUNDED"})
# Awaiting payment: a draft invoice, submitted once paid.
UNPAID = frozenset({"PENDING", "AUTHORIZED", "PARTIALLY_PAID", "EXPIRED"})
PAYMENT_KINDS = frozenset({"SALE", "CAPTURE"})
SUCCESS = "SUCCESS"
COMMENT_EVENT = "CommentEvent"
MEDIA_IMAGE = "MediaImage"


def money(node: Node | None, field: str) -> Decimal:
	"""A MoneyBag's shop-currency amount (zero when the bag is absent)."""
	bag = node.child(field) if node else None
	shop = bag.child("shopMoney") if bag else None
	return shop.decimal("amount") if shop else Decimal(0)


def shopify_address(address_type: str, node: Node | None) -> Address | None:
	if node is None or not node.text("address1") or not node.text("city"):
		return None
	return Address(
		address_type=address_type,
		name=" ".join(p for p in (node.text("firstName"), node.text("lastName")) if p) or None,
		company=node.optional_string("company"),
		line1=node.string("address1"),
		line2=node.optional_string("address2"),
		city=node.string("city"),
		state=node.optional_string("province"),
		postcode=node.optional_string("zip"),
		country_code=node.optional_string("countryCodeV2"),
		email=None,
		phone=node.optional_string("phone"),
	)


class CollectionMapper:
	kind = Kind.COLLECTION
	version = 1

	def map(self, node: Node) -> Desired | None:
		image = node.child("image")
		fields: Fields = {
			"name": node.string("title"),
			"slug": node.string("handle"),
			"parent": None,
			"description": node.optional_string("descriptionHtml"),
			"image": image.optional_string("url") if image else None,
		}
		warnings = (
			("its smart-collection rules aren't kept: an Item Group's members are set per item",)
			if node.child("ruleSet")
			else ()
		)
		legacy = path_of(f"/collections/{node.string('handle')}")
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else (), warnings)


class ProductMapper:
	kind = Kind.PRODUCT
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.flag("isGiftCard"):
			raise Unsupported("gift cards have no ERPNext home (coverage/shopify.md)")
		variants = node.children("variants")
		if not variants:
			raise Unsupported("a product with no variants has nothing to sell")
		simple = node.flag("hasOnlyDefaultVariant")
		images: list[Field] = [
			{
				"url": media.required_child("image").string("url"),
				"alt": media.required_child("image").optional_string("altText"),
			}
			for media in node.children("media")
			if media.typename() == MEDIA_IMAGE and media.child("image")
		]
		warnings = tuple(
			f"its {media.typename()} media isn't mirrored (no ERPNext home)"
			for media in node.children("media")
			if media.typename() != MEDIA_IMAGE
		)
		first = variants[0]
		product_type = node.optional_string("productType")
		fields: Fields = {
			"type": "simple" if simple else "variable",
			"name": node.string("title"),
			"slug": node.string("handle"),
			"description": node.optional_string("descriptionHtml"),
			"short_description": None,
			"sku": first.optional_string("sku") if simple else None,
			"barcode": first.optional_string("barcode") if simple else None,
			"brand": node.optional_string("vendor"),
			"collections": [c.id() for c in node.children("collections")],
			"images": images,
			"published": node.string("status") == ACTIVE and node.has("publishedAt"),
			"is_stock_item": self._tracked(first) if simple else any(self._tracked(v) for v in variants),
			"weight": self._weight(first) if simple else None,
			"price": self._regular(first) if simple else None,
			"sale": self._sale(first) if simple else None,
			"stock_qty": self._stock(first) if simple else None,
			"attributes": []
			if simple
			else [
				{
					"name": option.string("name"),
					"values": [v.string("name") for v in option.children("optionValues")],
				}
				for option in node.children("options")
			],
			"specifications": [{"label": "Product type", "value": product_type}] if product_type else [],
			"variants": [] if simple else [self._variant(v) for v in variants],
		}
		legacy = path_of(f"/products/{node.string('handle')}")
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else (), warnings)

	def _variant(self, variant: Node) -> Field:
		media = variant.child("media")
		image_nodes = media.children("nodes") if media else []
		image = image_nodes[0].child("image") if image_nodes else None
		return {
			"gid": variant.id(),
			"sku": variant.optional_string("sku"),
			"barcode": variant.optional_string("barcode"),
			"attributes": {o.string("name"): o.string("value") for o in variant.children("selectedOptions")},
			"price": self._regular(variant),
			"sale": self._sale(variant),
			"stock_qty": self._stock(variant),
			"weight": self._weight(variant),
			"image": image.optional_string("url") if image else None,
			"enabled": True,
		}

	@staticmethod
	def _regular(variant: Node) -> Decimal:
		"""The price before a sale: Shopify's compare-at price when it is above the price."""
		price = variant.decimal("price")
		compare = variant.optional_decimal("compareAtPrice")
		return compare if compare is not None and compare > price else price

	@staticmethod
	def _sale(variant: Node) -> Field:
		"""Shopify's price, when a higher compare-at price makes it a sale (an open-ended one)."""
		price = variant.decimal("price")
		compare = variant.optional_decimal("compareAtPrice")
		if compare is None or compare <= price:
			return None
		return {"price": price, "from": None, "to": None}

	@staticmethod
	def _tracked(variant: Node) -> bool:
		item = variant.child("inventoryItem")
		return bool(item and item.flag("tracked"))

	def _stock(self, variant: Node) -> Decimal | None:
		if not self._tracked(variant):
			return None
		return Decimal(variant.optional_integer("inventoryQuantity") or 0)

	@staticmethod
	def _weight(variant: Node) -> Field:
		item = variant.child("inventoryItem")
		measurement = item.child("measurement") if item else None
		weight = measurement.child("weight") if measurement else None
		if weight is None:
			return None
		unit = WEIGHT_UNITS.get(weight.string("unit"))
		if unit is None:
			raise Unsupported(f"the weight unit {weight.string('unit')} has no ERPNext UOM")
		return {"value": weight.decimal("value"), "unit": unit}


class CustomerMapper:
	kind = Kind.CUSTOMER
	version = 1

	def map(self, node: Node) -> Desired | None:
		email = node.child("defaultEmailAddress")
		phone = node.child("defaultPhoneNumber")
		first, last = node.text("firstName"), node.text("lastName")
		person = f"{first} {last}".strip()
		default = node.child("defaultAddress")
		company = default.text("company") if default else ""
		default_id = default.id() if default else None
		addresses: list[Field] = []
		for address in node.children("addressesV2"):
			mapped = shopify_address("Billing" if address.id() == default_id else "Shipping", address)
			if mapped is not None:
				addresses.append(address_fields(mapped))
		email_address = email.optional_string("emailAddress") if email else None
		fields: Fields = {
			"name": person or company or email_address or node.id(),
			"type": "Company" if company and not person else "Individual",
			"first_name": first or None,
			"last_name": last or None,
			"company": company or None,
			"email": email_address,
			"phone": phone.optional_string("phoneNumber") if phone else None,
			"addresses": addresses,
		}
		return Desired(self.kind, node.id(), fields)


class DiscountMapper:
	"""Basic code discounts (a percentage or an amount off everything, some products or some collections).
	Other discount types and restrictions ERPNext's coupons can't enforce are Unsupported."""

	kind = Kind.DISCOUNT
	version = 1

	def map(self, node: Node) -> Desired | None:
		discount = node.required_child("discount")
		typename = discount.typename()
		if typename != "DiscountCodeBasic":
			raise Unsupported(f"{typename} has no ERPNext coupon equivalent")
		if discount.string("status") != ACTIVE:
			return None
		if discount.flag("appliesOncePerCustomer"):
			raise Unsupported("a once-per-customer limit has no ERPNext coupon equivalent")
		context = discount.child("context")
		if context and context.typename() != "DiscountBuyerSelectionAll":
			raise Unsupported("a discount for selected customers has no ERPNext coupon equivalent")
		gets = discount.required_child("customerGets")
		value = gets.required_child("value")
		match value.typename():
			case "DiscountPercentage":
				kind, amount = "percentage", value.decimal("percentage") * 100
			case "DiscountAmount":
				if not value.flag("appliesOnEachItem"):
					raise Unsupported("an amount spread across the order has no ERPNext coupon equivalent")
				kind, amount = "amount", value.required_child("amount").decimal("amount")
			case other:
				raise Unsupported(f"a {other} value has no ERPNext coupon equivalent")
		items = gets.required_child("items")
		products: list[Field] = []
		groups: list[Field] = []
		match items.typename():
			case "AllDiscountItems":
				applies = "transaction" if kind == "percentage" else "all_items"
			case "DiscountProducts":
				if items.child("productVariants") and items.required_child("productVariants").children(
					"nodes"
				):
					raise Unsupported("a discount on single variants has no ERPNext coupon equivalent")
				products = [p.id() for p in items.required_child("products").children("nodes")]
				applies = "items"
			case "DiscountCollections":
				groups = [c.id() for c in items.required_child("collections").children("nodes")]
				applies = "groups"
			case other:
				raise Unsupported(f"discounted items of type {other} have no ERPNext equivalent")
		if applies == "all_items":
			raise Unsupported("a fixed discount on every product has no ERPNext coupon pricing rule")
		minimum = discount.child("minimumRequirement")
		min_amount = None
		if minimum is not None:
			if minimum.typename() != "DiscountMinimumSubtotal":
				raise Unsupported("a minimum quantity has no ERPNext coupon equivalent")
			min_amount = minimum.required_child("greaterThanOrEqualToSubtotal").decimal("amount")
		fields: Fields = {
			"codes": [code.string("code") for code in discount.required_child("codes").children("nodes")],
			"description": discount.optional_string("title"),
			"discount": kind,
			"applies": applies,
			"value": amount,
			"items": products,
			"groups": groups,
			"min_amount": min_amount,
			"valid_from": (discount.optional_time("startsAt") or "")[:10] or None,
			"valid_upto": (discount.optional_time("endsAt") or "")[:10] or None,
			"max_uses": discount.optional_integer("usageLimit"),
		}
		return Desired(self.kind, node.id(), fields)


class OrderMapper:
	kind = Kind.ORDER
	version = 1

	def map(self, node: Node) -> Desired | None:
		status = node.string("displayFinancialStatus")
		if node.has("cancelledAt") or status not in PAID | UNPAID:
			return None
		if node.flag("taxesIncluded"):
			raise Unsupported("tax-inclusive prices can't be split into ERPNext's tax rows exactly")
		if money(node, "originalTotalDutiesSet"):
			raise Unsupported("duties have no ERPNext sales tax and charge type")
		currency = node.string("currencyCode")
		submitted = status in PAID
		customer = node.child("customer")
		lines = {line.id(): line for line in node.children("lineItems")}
		fields: Fields = {
			"number": node.string("name"),
			"currency": currency,
			"created_at": node.time("createdAt"),
			"submitted": submitted,
			"customer": customer.id() if customer else None,
			"guest": None
			if customer
			else {
				"name": None,
				"email": node.optional_string("email"),
				"phone": node.optional_string("phone"),
			},
			"billing": address_fields(a)
			if (a := shopify_address("Billing", node.child("billingAddress")))
			else None,
			"shipping": address_fields(a)
			if (a := shopify_address("Shipping", node.child("shippingAddress")))
			else None,
			"lines": [line_fields(self._line(line, currency)) for line in lines.values()],
			"discount": money(node, "totalDiscountsSet"),
			"charges": [charge_fields(c) for c in self._charges(node)],
			"total": money(node, "totalPriceSet"),
			"payment": self._payment(node) if submitted else None,
			"refunds": [self._refund(r, lines, currency) for r in node.children("refunds")]
			if submitted
			else [],
			"notes": [
				{
					"gid": event.id(),
					"date": event.time("createdAt"),
					"author": event.required_child("author").optional_string("name")
					if event.child("author")
					else None,
					"text": event.string("rawMessage"),
					"customer_note": False,
				}
				for event in node.children("events")
				if event.typename() == COMMENT_EVENT
			],
			"remarks": node.optional_string("note"),
		}
		return Desired(self.kind, node.id(), fields)

	@staticmethod
	def _line(line: Node, currency: str) -> OrderLine:
		product = line.child("product")
		variant = line.child("variant")
		quantity = Decimal(line.integer("quantity"))
		return order_line(
			line.id(),
			product.id() if product else None,
			variant.id() if variant else None,
			line.string("name"),
			quantity,
			money(line, "originalUnitPriceSet") * quantity,
			currency,
		)

	@staticmethod
	def _charges(node: Node) -> list[Charge]:
		charges = [
			Charge("tax", tax.string("title"), money(tax, "priceSet")) for tax in node.children("taxLines")
		]
		charges += [
			Charge("shipping", s.string("title"), money(s, "discountedPriceSet"))
			for s in node.children("shippingLines")
		]
		charges += [
			Charge("fee", f.string("name"), money(f, "price")) for f in node.children("additionalFees")
		]
		tip = money(node, "totalTipReceivedSet")
		if tip:
			charges.append(Charge("fee", "Tip", tip))
		return [charge for charge in charges if charge.amount != 0]

	@staticmethod
	def _payment(node: Node) -> Field:
		payments = [
			t
			for t in node.children("transactions")
			if t.string("kind") in PAYMENT_KINDS and t.string("status") == SUCCESS
		]
		first = payments[0] if payments else None
		return {
			"method": first.string("gateway") if first else ", ".join(node.strings("paymentGatewayNames")),
			"reference": first.optional_string("authorizationCode") if first else None,
			"date": first.time("processedAt") if first else node.time("processedAt"),
		}

	def _refund(self, refund: Node, lines: dict[str, Node], currency: str) -> Field:
		returned: list[OrderLine] = []
		taxes = Decimal(0)
		for item in refund.children("refundLineItems"):
			source = lines.get(item.required_child("lineItem").id())
			if source is None:
				raise Unsupported("a refund line refers to a line the order doesn't have")
			product = source.child("product")
			variant = source.child("variant")
			returned.append(
				order_line(
					source.id(),
					product.id() if product else None,
					variant.id() if variant else None,
					source.string("name"),
					-Decimal(item.integer("quantity")),
					-money(item, "subtotalSet"),
					currency,
				)
			)
			taxes += money(item, "totalTaxSet")
		total = money(refund, "totalRefundedSet")
		charges = [Charge("tax", "Tax refunded", -taxes)] if taxes else []
		balance = refund_balance(f"{refund.id()}#adjustment", total, returned, charges, currency)
		credit_lines: list[Field] = [
			line_fields(line) for line in [*returned, *([balance] if balance else [])]
		]
		return {
			"gid": refund.id(),
			"date": refund.time("createdAt"),
			"reason": refund.optional_string("note"),
			"amount": total,
			"lines": credit_lines,
			"charges": [charge_fields(charge) for charge in charges],
		}


class PageMapper:
	kind = Kind.PAGE
	version = 1

	def map(self, node: Node) -> Desired | None:
		fields: Fields = {
			"title": node.string("title"),
			"slug": node.string("handle"),
			"parent": None,
			"published": node.flag("isPublished"),
			"html": node.text("body"),
		}
		legacy = path_of(f"/pages/{node.string('handle')}")
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else ())


class PolicyMapper:
	"""A legal policy (refund, privacy, terms, …) as a page at Shopify's own path, ``policies/<type>``."""

	kind = Kind.POLICY
	version = 1

	def map(self, node: Node) -> Desired | None:
		slug = f"policies/{node.string('type').lower().replace('_', '-')}"
		fields: Fields = {
			"title": node.string("title"),
			"slug": slug,
			"parent": None,
			"published": True,
			"html": node.text("body"),
		}
		legacy = path_of(f"/{slug}")
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else ())


class BlogMapper:
	kind = Kind.BLOG
	version = 1

	def map(self, node: Node) -> Desired | None:
		fields: Fields = {"title": node.string("title"), "slug": node.string("handle"), "description": None}
		legacy = path_of(f"/blogs/{node.string('handle')}")
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else ())


class ArticleMapper:
	kind = Kind.ARTICLE
	version = 1

	def map(self, node: Node) -> Desired | None:
		blog = node.required_child("blog")
		author = node.child("author")
		author_name = author.optional_string("name") if author else None
		fields: Fields = {
			"title": node.string("title"),
			"slug": node.string("handle"),
			"category": blog.id(),
			"author": {"key": author_name or "unknown", "name": author_name or "Unknown author"},
			"published": node.flag("isPublished"),
			"published_on": (node.optional_time("publishedAt") or node.time("createdAt"))[:10],
			"html": node.text("body"),
			"intro": node.optional_string("summary"),
		}
		legacy = path_of(f"/blogs/{blog.string('handle')}/{node.string('handle')}")
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else ())


class CommentMapper:
	kind = Kind.COMMENT
	version = 1

	def map(self, node: Node) -> Desired | None:
		if node.string("status") != "PUBLISHED":
			return None
		author = node.required_child("author")
		fields: Fields = {
			"article": node.required_child("article").id(),
			"author_name": author.string("name"),
			"author_email": author.optional_string("email"),
			"html": node.string("body"),
			"published_on": (node.optional_time("publishedAt") or node.time("createdAt"))[:10],
		}
		return Desired(self.kind, node.id(), fields)


class UrlRedirectMapper:
	kind = Kind.URL_REDIRECT
	version = 1

	def __init__(self, store_host: str | None) -> None:
		self._store_host = store_host

	def map(self, node: Node) -> Desired | None:
		source = path_of(node.string("path"))
		target = redirect_target(node.string("target"), self._store_host)
		if source is None or target is None:
			return None
		return Desired(self.kind, node.id(), {"source": source, "target": target, "status": 301})


def mappers(config: ConnectionConfig) -> list[Mapper]:
	return [
		CollectionMapper(),
		ProductMapper(),
		CustomerMapper(),
		DiscountMapper(),
		OrderMapper(),
		PageMapper(),
		PolicyMapper(),
		BlogMapper(),
		ArticleMapper(),
		CommentMapper(),
		UrlRedirectMapper(config.store_url.strip().lower() or None),
	]
