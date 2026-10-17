# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure BigCommerce → desired ERPNext state, one mapper per kind, in the shared field contract. Ported from
wp-woo-idempotent-importer's BigCommerce mappers; BigCommerce's status ids, coupon types, inventory tracking
and weight units are translated here. Identities arrive scoped to the connection, with BigCommerce's id as
``source_id``."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

from idempotent_importer.contract import (
	FEE,
	SHIPPING,
	TAX,
	Address,
	Charge,
	Discount,
	OrderLine,
	address_fields,
	charge_fields,
	date_of,
	discount_fields,
	line_fields,
	order_line,
	rating_fraction,
	refund_balance,
	variant_attributes,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import path_of, redirect_target, slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.bigcommerce.reader import COUPON_PROMOTION

WEIGHT_UNITS = {
	"lbs": "lbs",
	"lb": "lbs",
	"pounds": "lbs",
	"ounces": "oz",
	"oz": "oz",
	"kgs": "kg",
	"kg": "kg",
	"kilograms": "kg",
	"grams": "g",
	"g": "g",
}
DIGITAL = "digital"
TRACK_NONE, TRACK_PRODUCT, TRACK_VARIANT = "none", "product", "variant"
# Order status ids: 0 is an abandoned checkout; cancelled (5) and declined (6) orders have no invoice.
INCOMPLETE = 0
NOT_SOLD = frozenset({INCOMPLETE, 5, 6})
PAID = frozenset({"captured", "paid", "partially refunded", "refunded"})
REFUNDED_PRODUCT = "PRODUCT"
BLOG_DEFAULT_NAME, BLOG_DEFAULT_PATH = "Blog", "/blog/"
IMAGES_DIRECTORY = "/product_images/"
URL_TARGET = "url"
MAX_RATING = 5
APPROVED = "approved"
ENABLED = "ENABLED"
ABSOLUTE = re.compile(r"^https?://", re.IGNORECASE)
CONTENT_PAGE_TYPES = frozenset({"page", "raw"})


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, type_: str, id_: int | str) -> str:
		return self.config.identity(type_, id_)


def amount_or_zero(node: Node, field: str) -> Decimal:
	return node.optional_decimal(field) or Decimal(0)


def set_amount(node: Node, field: str) -> Decimal | None:
	"""An amount BigCommerce leaves at zero when it isn't set (a sale price, a cost): None then."""
	value = node.optional_decimal(field)
	return value if value else None


def slug_of(url: str | None, title: str) -> str:
	"""The slug a storefront path ends in ("/kitchen/trail-mug/" → "trail-mug"), else one from the title."""
	path = path_of(url)
	return (path.rsplit("/", 1)[-1] if path else "") or slugify(title)


def legacy(url: str | None) -> tuple[str, ...]:
	path = path_of(url)
	return (path,) if path else ()


def bc_address(
	address_type: str, node: Node | None, line1: str, line2: str, state: str, postcode: str, country: str
) -> Fields | None:
	if node is None or not node.text(line1) or not node.text("city"):
		return None
	name = " ".join(p for p in (node.text("first_name"), node.text("last_name")) if p) or None
	return address_fields(
		Address(
			address_type=address_type,
			name=name,
			company=node.optional_string("company"),
			line1=node.string(line1),
			line2=node.optional_string(line2),
			city=node.string("city"),
			state=node.optional_string(state),
			postcode=node.optional_string(postcode),
			country_code=node.optional_string(country),
			email=node.optional_string("email"),
			phone=node.optional_string("phone"),
		)
	)


def order_address(address_type: str, node: Node | None) -> Fields | None:
	return bc_address(address_type, node, "street_1", "street_2", "state", "zip", "country_iso2")


class CategoryMapper:
	kind = Kind.COLLECTION
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		name = node.string("name")
		url_node = node.child("url")
		url = url_node.optional_string("path") if url_node else None
		parent = node.optional_integer("parent_id") or 0
		fields: Fields = {
			"name": name,
			"slug": slug_of(url, name),
			"parent": self._context.gid("Category", parent) if parent else None,
			"description": node.optional_string("description"),
			"image": node.optional_string("image_url"),
		}
		return Desired(self.kind, node.id(), fields, legacy(url))


class ProductMapper:
	"""A product → an Item: one (its base variant) when its only variant has no option values, else a
	template with a variant per variant. A variant's price, sale price and weight fall back to the product's
	when BigCommerce leaves them unset; a zero sale price means none. Stock follows the product's inventory
	tracking (none, the product, or each variant). Hidden products are unpublished."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		title = node.string("name")
		variants = node.children("variants")
		if not variants:
			raise SourceError(f"BigCommerce product {node.string('source_id')} has no variants.")
		simple = len(variants) == 1 and not variants[0].children("option_values")
		tracking = node.optional_string("inventory_tracking") or TRACK_NONE
		unit = WEIGHT_UNITS.get((node.optional_string("weight_units") or "").strip().lower())
		mapped = [self._variant(node, variant, simple, tracking, unit) for variant in variants]
		warnings: list[str] = []
		if not simple and tracking == TRACK_PRODUCT:
			warnings.append("it tracks stock for the whole product; its variants are mirrored untracked")
		if unit is None and node.optional_decimal("weight"):
			warnings.append(f"its weight unit {node.optional_string('weight_units')} has no ERPNext UOM")
		first = mapped[0]
		custom_url = node.child("custom_url")
		url = custom_url.optional_string("url") if custom_url else None
		fields: Fields = {
			"type": "simple" if simple else "variable",
			"name": title,
			"slug": slug_of(url, title),
			"description": node.optional_string("description"),
			"short_description": None,
			"sku": first["sku"] if simple else None,
			"barcode": first["barcode"] if simple else None,
			"brand": node.optional_string("brand"),
			"collections": [self._context.gid("Category", c) for c in category_ids(node)],
			"images": images(node),
			"published": node.get("is_visible") is not False,
			"is_stock_item": node.optional_string("type") != DIGITAL
			and any(v["stock_qty"] is not None for v in mapped),
			"weight": first["weight"] if simple else None,
			"price": first["price"] if simple else None,
			"sale": first["sale"] if simple else None,
			"stock_qty": first["stock_qty"] if simple else None,
			"attributes": [] if simple else variant_attributes(mapped),
			"specifications": [
				{"label": f.string("name"), "value": f.string("value")}
				for f in node.children("custom_fields")
			],
			"variants": [] if simple else list[Field](mapped),
			"tags": [],
		}
		return Desired(self.kind, node.id(), fields, legacy(url), tuple(warnings))

	def _variant(self, product: Node, variant: Node, simple: bool, tracking: str, unit: str | None) -> Fields:
		variant_id = variant.integer("id")
		price = variant.optional_decimal("price") or product.optional_decimal("price")
		if price is None:
			raise SourceError(f"BigCommerce variant {variant_id} has no price.")
		sale = set_amount(variant, "sale_price") or (
			None if variant.has("sale_price") else set_amount(product, "sale_price")
		)
		managed = tracking == TRACK_VARIANT or (simple and tracking == TRACK_PRODUCT)
		stock = (
			variant.optional_integer("inventory_level")
			if tracking == TRACK_VARIANT
			else product.optional_integer("inventory_level")
		)
		weight = variant.optional_decimal("weight") or product.optional_decimal("weight")
		barcode = variant.optional_string("gtin") or variant.optional_string("upc")
		if simple:
			barcode = barcode or product.optional_string("gtin") or product.optional_string("upc")
		return {
			"gid": self._context.gid("Variant", variant_id),
			"sku": variant.optional_string("sku") or (product.optional_string("sku") if simple else None),
			"barcode": barcode,
			"attributes": {}
			if simple
			else {
				v.string("option_display_name"): v.string("label") for v in variant.children("option_values")
			},
			"price": price,
			"sale": {"price": sale, "from": None, "to": None} if sale is not None and sale < price else None,
			"stock_qty": Decimal(max(0, stock or 0)) if managed else None,
			"weight": {"value": weight, "unit": unit} if unit and weight else None,
			"image": variant.optional_string("image_url"),
			"enabled": True,
		}


def category_ids(product: Node) -> list[int]:
	"""The product's category ids (BigCommerce sends them as JSON numbers), in order, once each."""
	found: list[int] = []
	raw = product.get("categories")
	for value in raw if isinstance(raw, list) else []:
		if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
			raise SourceError(f"{product.path}.categories has a non-integer category id.")
		if int(value) not in found:
			found.append(int(value))
	return found


def images(product: Node) -> list[Field]:
	"""The thumbnail first, then the others by sort order; each at its largest size."""
	ordered = sorted(
		product.children("images"),
		key=lambda image: (
			image.get("is_thumbnail") is not True,
			image.optional_integer("sort_order") or 0,
			image.optional_integer("id") or 0,
		),
	)
	found: list[Field] = []
	for image in ordered:
		url = (
			image.optional_string("url_zoom")
			or image.optional_string("url_standard")
			or image.optional_string("image_url")
		)
		if url:
			found.append({"url": url, "alt": image.optional_string("description")})
	return found


class CustomerMapper:
	"""A customer → a Customer, its Contact, and every saved address (the first as billing)."""

	kind = Kind.CUSTOMER
	version = 1

	def map(self, node: Node) -> Desired | None:
		first, last = node.text("first_name"), node.text("last_name")
		company = node.optional_string("company")
		email = (node.optional_string("email") or "").lower() or None
		person = f"{first} {last}".strip()
		addresses: list[Field] = []
		for index, saved in enumerate(node.children("addresses")):
			address = bc_address(
				"Billing" if index == 0 else "Shipping",
				saved,
				"address1",
				"address2",
				"state_or_province",
				"postal_code",
				"country_code",
			)
			if address is not None:
				addresses.append(address)
		fields: Fields = {
			"name": person or company or email or node.string("source_id"),
			"type": "Company" if company and not person else "Individual",
			"first_name": first or None,
			"last_name": last or None,
			"company": company,
			"email": email,
			"phone": node.optional_string("phone"),
			"addresses": addresses,
		}
		return Desired(self.kind, node.id(), fields)


class DiscountMapper:
	"""Legacy coupons and coupon promotions → a coupon Pricing Rule with its codes. Only what ERPNext's
	coupons apply exactly: a legacy coupon's percentage (on everything, some products or some categories),
	fixed amount per item of some products or categories, or fixed amount off the order, with its minimum
	spend, expiry and use limit; a promotion with codes whose one rule takes a percentage or fixed amount off
	the order, at most after a minimum spend. Everything else is Unsupported. A disabled coupon or promotion
	maps to nothing, so its rule is disabled."""

	kind = Kind.DISCOUNT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		return self._promotion(node) if "/Promotion/" in node.id() else self._coupon(node)

	def _coupon(self, node: Node) -> Desired | None:
		if node.get("enabled") is False:
			return None
		name, coupon_type = node.string("name"), node.string("type")
		restricted = node.child("restricted_to")
		if node.strings("shipping_methods") or (restricted is not None and restricted.raw()):
			raise Unsupported(
				"a coupon limited to some countries or shipping methods has no ERPNext equivalent"
			)
		if (node.optional_integer("max_uses_per_customer") or 0) > 0:
			raise Unsupported("a per-customer use limit has no ERPNext coupon equivalent")
		products, groups = self._applies_to(node.child("applies_to"))
		match coupon_type:
			case "percentage_discount":
				kind, applies = "percentage", "items" if products else "groups" if groups else "transaction"
			case "per_item_discount" if products or groups:
				kind, applies = "amount", "items" if products else "groups"
			case "per_total_discount" if not products and not groups:
				kind, applies = "amount", "transaction"
			case _:
				raise Unsupported(f"a {coupon_type} coupon on these products has no ERPNext pricing rule")
		return Desired(
			self.kind,
			node.id(),
			discount_fields(
				Discount(
					codes=[node.string("code")],
					description=name,
					discount=kind,
					applies=applies,
					value=amount_or_zero(node, "amount"),
					items=products,
					groups=groups,
					min_amount=set_amount(node, "min_purchase"),
					valid_from=date_of(node.time("date_created")),
					valid_upto=date_of(node.optional_time("expires")),
					max_uses=node.optional_integer("max_uses") or None,
				)
			),
		)

	def _applies_to(self, applies_to: Node | None) -> tuple[list[str], list[str]]:
		"""A legacy coupon's products and categories (category 0 is every product)."""
		ids = [i for i in applies_to.integers("ids") if i != 0] if applies_to else []
		match applies_to.optional_string("entity") if applies_to else None:
			case "products":
				return [self._context.gid("Product", i) for i in ids], []
			case "categories" | None:
				return [], [self._context.gid("Category", i) for i in ids]
			case _:
				raise SourceError("A BigCommerce coupon applies to an unknown kind of object.")

	def _promotion(self, node: Node) -> Desired | None:
		if node.string("status") != ENABLED:
			return None
		if node.string("redemption_type") != COUPON_PROMOTION:
			raise Unsupported("an automatic promotion has no ERPNext coupon equivalent")
		for limit in ("shipping_address", "schedule"):
			if node.child(limit) is not None:
				raise Unsupported(f"a promotion with a {limit} condition has no ERPNext coupon equivalent")
		customer = node.child("customer")
		if customer is not None and (
			customer.integers("group_ids")
			or customer.integers("excluded_group_ids")
			or (customer.optional_integer("minimum_order_count") or 0) > 0
			or customer.child("segments") is not None
		):
			raise Unsupported("a promotion for some customers has no ERPNext coupon equivalent")
		rules = node.children("rules")
		if len(rules) != 1:
			raise Unsupported(f"a promotion with {len(rules)} rules has no single ERPNext pricing rule")
		action = rules[0].required_child("action").child("cart_value")
		if action is None:
			raise Unsupported(
				"a promotion that discounts items, shipping or gifts has no ERPNext coupon equivalent"
			)
		if action.has("maximum_allowed_discount_amount"):
			raise Unsupported("a capped discount has no ERPNext coupon equivalent")
		discount = action.required_child("discount")
		percentage = discount.optional_decimal("percentage_amount")
		fixed = discount.optional_decimal("fixed_amount")
		if percentage is None and fixed is None:
			raise SourceError(f'"{node.string("name")}" has a discount with no amount.')
		codes = node.children("codes")
		for code in codes:
			if code.optional_integer("max_uses_per_customer"):
				raise Unsupported("a per-customer use limit has no ERPNext coupon equivalent")
			if code.child("schedule") is not None or code.optional_integer("max_uses"):
				raise Unsupported(
					"a code with its own schedule or use limit has no ERPNext coupon equivalent"
				)
		max_uses = node.optional_integer("max_uses") or None
		if max_uses is not None and len(codes) > 1:
			raise Unsupported("a use limit shared across several codes has no ERPNext coupon equivalent")
		return Desired(
			self.kind,
			node.id(),
			discount_fields(
				Discount(
					codes=[code.string("code") for code in codes],
					description=node.string("name"),
					discount="percentage" if percentage is not None else "amount",
					applies="transaction",
					value=percentage if percentage is not None else fixed or Decimal(0),
					items=[],
					groups=[],
					min_amount=minimum_spend(rules[0]),
					valid_from=date_of(node.time("start_date")),
					valid_upto=date_of(node.optional_time("end_date")),
					max_uses=max_uses,
				)
			),
		)


def minimum_spend(rule: Node) -> Decimal | None:
	"""The rule's minimum spend, the only condition an ERPNext coupon can check."""
	condition = rule.child("condition")
	if condition is None:
		return None
	cart = condition.child("cart")
	if cart is None or set(cart.raw()) - {"minimum_spend"}:
		raise Unsupported("a promotion condition beyond a minimum spend has no ERPNext coupon equivalent")
	return cart.optional_decimal("minimum_spend")


class OrderMapper:
	"""An order → an invoice with BigCommerce's totals: lines at their pre-tax prices before discounts (on
	the variant's item), the order's coupon and manual discounts as the invoice's discount, each tax as a
	charge, each shipping address's cost as shipping (its handling as a fee), gift wrapping and the order's
	fees as fees; refunds as credit notes (their refunded products as returned lines), each shipment and the
	staff notes as comments. Abandoned, cancelled, declined and archived orders map to nothing."""

	kind = Kind.ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if node.integer("status_id") in NOT_SOLD or node.get("is_deleted") is True:
			return None
		currency = node.string("currency_code")
		store_currency = node.optional_string("store_default_currency_code")
		if store_currency is not None and currency != store_currency:
			raise Unsupported(
				f"an order in {currency}, not the store's {store_currency}: BigCommerce records its exchange rate "
				"but not its total in the store's currency"
			)
		submitted = node.optional_string("payment_status") in PAID
		billing = node.child("billing_address")
		addresses = node.children("shipping_addresses")
		customer = node.optional_integer("customer_id") or 0
		lines = {line.integer("id"): line for line in node.children("products")}
		fields: Fields = {
			"number": f"#{node.string('source_id')}",
			"currency": currency,
			"created_at": node.time("date_created"),
			"submitted": submitted,
			"customer": self._context.gid("Customer", customer) if customer > 0 else None,
			"guest": None
			if customer > 0
			else {
				"name": " ".join(p for p in (billing.text("first_name"), billing.text("last_name")) if p)
				or None
				if billing
				else None,
				"email": billing.optional_string("email") if billing else None,
				"phone": billing.optional_string("phone") if billing else None,
			},
			"billing": order_address("Billing", billing),
			"shipping": order_address("Shipping", addresses[0] if addresses else None),
			"lines": [line_fields(self._line(line, currency)) for line in lines.values()],
			"discount": amount_or_zero(node, "discount_amount") + amount_or_zero(node, "coupon_discount"),
			"charges": [charge_fields(charge) for charge in charges(node, addresses)],
			"total": amount_or_zero(node, "total_inc_tax"),
			"payment": {
				"method": node.optional_string("payment_method") or "BigCommerce",
				"reference": None,
				"date": node.time("date_created"),
			}
			if submitted
			else None,
			"refunds": [self._refund(refund, lines, currency) for refund in node.children("refunds")]
			if submitted
			else [],
			"notes": self._notes(node),
			"remarks": node.optional_string("customer_message"),
		}
		return Desired(self.kind, node.id(), fields)

	def _line(self, line: Node, currency: str) -> OrderLine:
		product = line.optional_integer("product_id") or 0
		variant = line.optional_integer("variant_id") or 0
		quantity = Decimal(line.integer("quantity"))
		return order_line(
			self._context.gid("OrderProduct", line.integer("id")),
			self._context.gid("Product", product) if product > 0 else None,
			self._context.gid("Variant", variant) if product > 0 and variant > 0 else None,
			line.text("name"),
			quantity,
			amount_or_zero(line, "total_ex_tax"),
			currency,
		)

	def _refund(self, refund: Node, lines: dict[int, Node], currency: str) -> Field:
		gid = self._context.gid("Refund", refund.integer("id"))
		returned: list[OrderLine] = []
		for item in refund.children("items"):
			quantity = item.optional_integer("quantity") or 0
			if item.optional_string("item_type") != REFUNDED_PRODUCT or quantity <= 0:
				continue
			source = lines.get(item.integer("item_id"))
			if source is None:
				raise Unsupported("a refund names a line the order doesn't have")
			product = source.optional_integer("product_id") or 0
			variant = source.optional_integer("variant_id") or 0
			returned.append(
				order_line(
					self._context.gid("OrderProduct", item.integer("item_id")),
					self._context.gid("Product", product) if product > 0 else None,
					self._context.gid("Variant", variant) if product > 0 and variant > 0 else None,
					source.text("name"),
					-Decimal(quantity),
					-amount_or_zero(item, "requested_amount"),
					currency,
				)
			)
		amount = amount_or_zero(refund, "total_amount")
		balance = refund_balance(f"{gid}#adjustment", amount, returned, [], currency)
		return {
			"gid": gid,
			"date": refund.time("created"),
			"reason": refund.optional_string("reason"),
			"amount": amount,
			"lines": [line_fields(line) for line in [*returned, *([balance] if balance else [])]],
			"charges": [],
		}

	def _notes(self, order: Node) -> list[Field]:
		notes: list[Field] = []
		for shipment in order.children("shipments"):
			carrier = shipment.optional_string("shipping_provider_display_name") or shipment.optional_string(
				"tracking_carrier"
			)
			link = shipment.optional_string("tracking_link") or shipment.optional_string(
				"generated_tracking_link"
			)
			tracking = " ".join(v for v in (carrier, shipment.optional_string("tracking_number"), link) if v)
			notes.append(
				{
					"gid": self._context.gid("Shipment", shipment.integer("id")),
					"date": shipment.optional_time("date_created") or order.time("date_created"),
					"author": None,
					"text": f"BigCommerce shipment: {tracking}".strip(),
					"customer_note": False,
				}
			)
		staff = order.optional_string("staff_notes")
		if staff:
			notes.append(
				{
					"gid": self._context.gid("StaffNote", order.string("source_id")),
					"date": order.time("date_created"),
					"author": "Staff",
					"text": staff,
					"customer_note": False,
				}
			)
		return notes


def charges(order: Node, addresses: list[Node]) -> list[Charge]:
	"""Each tax (its rows summed, or the order's total tax when BigCommerce gives no rows), each shipping
	address's cost and handling, gift wrapping and the order's fees; zero amounts left out."""
	taxes: dict[str, Decimal] = {}
	for tax in order.children("taxes"):
		title = tax.optional_string("name") or tax.optional_string("class") or "Tax"
		taxes[title] = taxes.get(title, Decimal(0)) + amount_or_zero(tax, "line_amount")
	if not taxes and amount_or_zero(order, "total_tax"):
		taxes["Tax"] = amount_or_zero(order, "total_tax")
	found = [Charge(TAX, title, amount) for title, amount in taxes.items()]
	for address in addresses:
		found.append(
			Charge(
				SHIPPING,
				address.optional_string("shipping_method") or "Shipping",
				amount_or_zero(address, "cost_ex_tax"),
			)
		)
		found.append(Charge(FEE, "Handling", amount_or_zero(address, "handling_cost_ex_tax")))
	found.append(Charge(FEE, "Gift wrapping", amount_or_zero(order, "wrapping_cost_ex_tax")))
	for fee in order.children("fees"):
		name = (
			fee.optional_string("display_name_customer")
			or fee.optional_string("display_name_merchant")
			or "Fee"
		)
		found.append(Charge(FEE, name, amount_or_zero(fee, "cost_ex_tax")))
	return [charge for charge in found if charge.amount != 0]


class PageMapper:
	"""A content page (a page, or a raw page) → a Web Page; a hidden or customers-only page is unpublished.
	Contact-form and link pages have no Web Page equivalent."""

	kind = Kind.PAGE
	version = 1

	def map(self, node: Node) -> Desired | None:
		name, page_type = node.string("name"), node.string("type")
		if page_type not in CONTENT_PAGE_TYPES:
			raise Unsupported(f"a BigCommerce {page_type} page has no Web Page equivalent")
		url = node.optional_string("url")
		fields: Fields = {
			"title": name,
			"slug": slug_of(url, name),
			"parent": None,
			"published": node.get("is_visible") is not False and node.get("is_customers_only") is not True,
			"html": node.text("body"),
			"images": [],
		}
		return Desired(self.kind, node.id(), fields, legacy(url))


class BlogMapper:
	"""The store's blog → a Blog Category named after its blog page ("Blog" when the store has none)."""

	kind = Kind.BLOG
	version = 1

	def map(self, node: Node) -> Desired | None:
		page = node.child("page")
		name = (page.optional_string("name") if page else None) or BLOG_DEFAULT_NAME
		url = (page.optional_string("url") if page else None) or BLOG_DEFAULT_PATH
		fields: Fields = {"title": name, "slug": slug_of(url, name), "description": None}
		return Desired(self.kind, node.id(), fields, legacy(url))


class ArticleMapper:
	"""A blog post → a Blog Post in the blog's category, by its author; its thumbnail is a path on the
	store, served under its ``/product_images/`` directory (kept as the post's first image in its HTML is
	not: Blog Post has no image of its own)."""

	kind = Kind.ARTICLE
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		title = node.string("title")
		url = node.optional_string("url")
		author = node.optional_string("author")
		published_date = node.child("published_date")
		published = node.optional_time("published_date_iso8601") or (
			published_date.time("date") if published_date else None
		)
		if published is None:
			raise SourceError(f"{node.path} has no publication date.")
		fields: Fields = {
			"title": title,
			"slug": slug_of(url, title),
			"category": self._context.gid("Blog", node.string("blog_id")),
			"author": {"key": author or "unknown", "name": author or "Unknown author"},
			"published": node.get("is_published") is True,
			"published_on": published[:10],
			"html": node.text("body"),
			"intro": node.optional_string("summary"),
			"images": [],
			"tags": [html.unescape(tag) for tag in node.strings("tags")],
		}
		return Desired(self.kind, node.id(), fields, legacy(url))


class ReviewMapper:
	"""An approved product review → an Item Review; pending and disapproved ones aren't mirrored."""

	kind = Kind.REVIEW
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_string("status") != APPROVED:
			return None
		title, text = node.optional_string("title"), node.text("text")
		fields: Fields = {
			"product": self._context.gid("Product", node.integer("product_id")),
			"author_name": node.optional_string("name") or "Customer",
			"author_email": (node.optional_string("email") or "").lower() or None,
			"text": f"{title}\n\n{text}" if title else text,
			"rating": rating_fraction(node.integer("rating"), MAX_RATING),
			"published_on": node.time("date_reviewed")[:10],
		}
		return Desired(self.kind, node.id(), fields)


class UrlRedirectMapper:
	"""A storefront 301 → a Website Route Redirect: a redirect to a URL keeps it; one to a product, category,
	brand, page or post targets that object's storefront path (whose own redirect leads on to the object)."""

	kind = Kind.URL_REDIRECT
	version = 1

	def __init__(self, store_host: str | None) -> None:
		self._store_host = store_host

	def map(self, node: Node) -> Desired | None:
		source = path_of(node.string("from_path"))
		to = node.required_child("to")
		target_url = (
			to.optional_string("url") if to.string("type") == URL_TARGET else node.optional_string("to_url")
		)
		target = redirect_target(target_url, self._store_host) if target_url else None
		if source is None or target is None:
			return None
		return Desired(self.kind, node.id(), {"source": source, "target": target, "status": 301})


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	host = urlsplit(config.store_url.strip()).hostname
	return [
		CategoryMapper(context),
		ProductMapper(context),
		CustomerMapper(),
		DiscountMapper(context),
		OrderMapper(context),
		PageMapper(),
		BlogMapper(),
		ArticleMapper(context),
		ReviewMapper(context),
		UrlRedirectMapper(host),
	]
