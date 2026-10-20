# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure BigCommerce → desired ERPNext state, one mapper per kind, in the shared field contract. Ported from
wp-woo-idempotent-importer's BigCommerce mappers; BigCommerce's status ids, coupon types, inventory tracking
and weight units are translated here. Identities arrive scoped to the connection, with BigCommerce's id as
``source_id``."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from urllib.parse import urlsplit

from idempotent_importer.contract import (
	FEE,
	SHIPPING,
	TAX,
	Address,
	Charge,
	Code,
	Condition,
	Discount,
	FreeItem,
	OrderLine,
	Shipment,
	Tier,
	address_fields,
	charge_fields,
	contents_of,
	date_of,
	discount_desired,
	line_fields,
	order_line,
	rating_fraction,
	refund_balance,
	shipment_fields,
	source_terms,
	variant_attributes,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.identity import Identity
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
# The storefront's channel: a promotion for other channels only doesn't apply on it.
STOREFRONT_CHANNEL = 1
ANY_CURRENCY = "*"
# A customer group discount rule's method, and a bulk pricing rule's type, as a discount: a percentage off,
# an amount off each unit, or a fixed price.
GROUP_METHODS = {"percent": "percentage", "fixed": "amount", "price": "rate"}
BULK_TYPES = {"percent": "percentage", "price": "amount", "fixed": "rate"}


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
	"""A customer → a Customer in its customer group, its Contact, and every saved address (the first as
	billing)."""

	kind = Kind.CUSTOMER
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

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
		if group := node.optional_integer("customer_group_id"):
			fields["group"] = self._context.gid("CustomerGroup", group)
		return Desired(self.kind, node.id(), fields)


class CustomerGroupMapper:
	"""A customer group → a Customer Group its customers and discounts name."""

	kind = Kind.CUSTOMER_GROUP
	version = 1

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), {"name": node.string("name")})


class Matched:
	"""What a BigCommerce item matcher picks: products, categories and variants (identities), or ``None``
	when it picks by something else (brands, options, custom fields, combinations)."""

	def __init__(self, products: list[str], groups: list[str], variants: list[str]) -> None:
		self.products, self.groups, self.variants = products, groups, variants

	def applies(self) -> str:
		return "items" if self.products or self.variants else "groups" if self.groups else "all_items"

	def terms(self) -> Fields:
		return {
			"items": list[Field](self.products) or None,
			"groups": list[Field](self.groups) or None,
			"variants": list[Field](self.variants) or None,
		}


class DiscountMapper:
	"""Every BigCommerce discount → a Pricing Rule (a Promotional Scheme for tiers), enforcing natively what
	ERPNext's pricing does and recording the rest as conditions that hold it (``contract.CONDITIONS``):

	- legacy coupons: a percentage or amount per item (everything, some products or categories), an amount
	  off the order, free shipping or an amount off shipping; minimum spend, expiry and use limit; per-customer
	  limits, an order amount on some products, shipping methods and countries recorded;
	- promotions, automatic or with codes (each code's own use limit and dates): one rule's discount off the
	  order or some items, a gift item or free shipping, after a minimum spend or quantity, for customer
	  groups, in one currency; caps, exclusions, item limits and choices, shipping discounts, schedules,
	  customer conditions, other channels, combination settings and further rules recorded;
	- a customer group's discount rules (``CustomerGroupRule``): a percentage or amount off, or a fixed
	  price, on everything, a category or a product, for the group;
	- a product's bulk pricing rules (``BulkPricing``): its quantity tiers, a scheme on the product.

	A disabled coupon or promotion maps to nothing, so its rule is disabled."""

	kind = Kind.DISCOUNT
	version = 2

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		match Identity.parse(node.id()).type:
			case "Promotion":
				return self._promotion(node)
			case "CustomerGroupRule":
				return self._group_rule(node)
			case "BulkPricing":
				return self._bulk_pricing(node)
			case _:
				return self._coupon(node)

	# -- legacy coupons ---------------------------------------------------------------------------------

	def _coupon(self, node: Node) -> Desired | None:
		if node.get("enabled") is False:
			return None
		later: list[Condition] = []
		restricted = node.child("restricted_to")
		methods = node.strings("shipping_methods")
		if methods or (restricted is not None and restricted.raw()):
			shipping: Fields = {
				"methods": list[Field](methods) or None,
				"source": source_terms(restricted.raw())
				if restricted is not None and restricted.raw()
				else None,
			}
			later.append(Condition("shipping_limits", shipping))
		if per_customer := node.optional_integer("max_uses_per_customer"):
			later.append(Condition("per_customer_limit", {"uses": per_customer}))
		products, groups = self._applies_to(node.child("applies_to"))
		amount = amount_or_zero(node, "amount")
		kind, applies, value = "percentage", "transaction", amount
		match node.string("type"):
			case "percentage_discount":
				applies = "items" if products else "groups" if groups else "transaction"
			case "per_item_discount":
				kind, applies = "amount", "items" if products else "groups" if groups else "all_items"
			case "per_total_discount":
				kind = "amount"
				if products or groups:
					later.append(Condition("spread_amount", Matched(products, groups, []).terms()))
				products, groups = [], []
			case "free_shipping":
				later.append(Condition("free_shipping", Matched(products, groups, []).terms()))
				products, groups, value = [], [], Decimal(0)
			case "shipping_discount":
				shipping_off: Fields = {
					"discount": "amount",
					"value": amount,
					**Matched(products, groups, []).terms(),
				}
				later.append(Condition("shipping_discount", shipping_off))
				products, groups, value = [], [], Decimal(0)
			case other:
				raise SourceError(f"A BigCommerce coupon of type {other}.")
		return discount_desired(
			node.id(),
			Discount(
				codes=[node.string("code")],
				description=node.string("name"),
				discount=kind,
				applies=applies,
				value=value,
				items=products,
				groups=groups,
				min_amount=set_amount(node, "min_purchase"),
				valid_from=date_of(node.time("date_created")),
				valid_upto=date_of(node.optional_time("expires")),
				max_uses=node.optional_integer("max_uses") or None,
				conditions=later,
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

	# -- promotions -------------------------------------------------------------------------------------

	def _promotion(self, node: Node) -> Desired | None:
		if node.string("status") != ENABLED:
			return None
		automatic = node.string("redemption_type") != COUPON_PROMOTION
		later: list[Condition] = []
		customer_groups = self._customer(node.child("customer"), later)
		channels = [channel.integer("id") for channel in node.children("channels")]
		if channels and STOREFRONT_CHANNEL not in channels:
			later.append(Condition("channels", {"channels": list[Field](channels)}))
		for field, kind in (("shipping_address", "shipping_limits"), ("schedule", "schedule")):
			limit = node.child(field)
			if limit is not None:
				later.append(Condition(kind, {"source": source_terms(limit.raw())}))
		combination: Fields = {
			"exclusive": node.get("can_be_used_with_other_promotions") is False or None,
			"stop": node.get("stop") is True or None,
			"source": source_terms(node.get("coupon_overrides_automatic_when_offering_higher_discounts"))
			if node.get("coupon_overrides_automatic_when_offering_higher_discounts") is True
			else None,
		}
		if any(value is not None for value in combination.values()):
			later.append(Condition("combination", combination))
		codes = node.children("codes")
		max_uses = node.optional_integer("max_uses") or None
		if max_uses is not None and (automatic or len(codes) > 1):
			later.append(Condition("shared_use_limit", {"uses": max_uses}))
		rules = node.children("rules")
		if not rules:
			raise SourceError(f'BigCommerce promotion "{node.string("name")}" has no rules.')
		if len(rules) > 1:
			later.append(Condition("further_rules", {"source": [source_terms(r.raw()) for r in rules[1:]]}))
		currency = node.optional_string("currency_code")
		discount = replace(
			self._rule(rules[0], later),
			codes=[code.string("code") for code in codes],
			description=node.string("name"),
			valid_from=date_of(node.time("start_date")),
			valid_upto=date_of(node.optional_time("end_date")),
			max_uses=None if automatic or len(codes) > 1 else max_uses,
			automatic=automatic,
			customer_groups=customer_groups,
			code_limits={code.string("code"): self._code(code) for code in codes},
			currency=currency if currency and currency != ANY_CURRENCY else None,
			conditions=later,
		)
		return discount_desired(node.id(), discount)

	def _customer(self, customer: Node | None, later: list[Condition]) -> list[str]:
		"""The customer groups a promotion is for; who else it excludes or asks for is recorded."""
		if customer is None:
			return []
		if excluded := customer.integers("excluded_group_ids"):
			groups: list[Field] = [self._context.gid("CustomerGroup", i) for i in excluded]
			later.append(Condition("excluded_customer_groups", {"groups": groups}))
		if orders := customer.optional_integer("minimum_order_count"):
			later.append(Condition("customer_history", {"min_orders": orders}))
		segments = customer.child("segments")
		if segments is not None:
			later.append(Condition("customer_segments", {"source": source_terms(segments.raw())}))
		return [self._context.gid("CustomerGroup", i) for i in customer.integers("group_ids")]

	@staticmethod
	def _code(code: Node) -> Code:
		"""A promotion code's own use limit and dates; its per-customer limit is a condition of the code."""
		schedule = code.child("schedule")
		per_customer = code.optional_integer("max_uses_per_customer")
		return Code(
			max_uses=code.optional_integer("max_uses") or None,
			valid_from=date_of(schedule.optional_time("valid_from")) if schedule else None,
			valid_upto=date_of(schedule.optional_time("valid_until")) if schedule else None,
			conditions=[Condition("per_customer_limit", {"uses": per_customer})] if per_customer else [],
		)

	def _rule(self, rule: Node, later: list[Condition]) -> Discount:
		"""A promotion's rule: its condition's minimum spend or quantity (and the items it asks for), and its
		action's discount, gift or shipping."""
		bought: Matched | None = None
		min_amount = min_qty = None
		condition = rule.child("condition")
		if condition is not None:
			cart = condition.child("cart")
			if cart is None:
				later.append(Condition("rule_condition", {"source": source_terms(condition.raw())}))
			else:
				min_amount = cart.optional_decimal("minimum_spend")
				quantity = cart.optional_integer("minimum_quantity")
				min_qty = Decimal(quantity) if quantity else None
				matcher = cart.child("items")
				if matcher is not None:
					bought = self._matcher(matcher)
					if bought is None:
						later.append(Condition("item_matcher", {"source": source_terms(matcher.raw())}))
		base = Discount(
			codes=[],
			description=None,
			discount="percentage",
			applies="transaction",
			value=Decimal(0),
			items=[],
			groups=[],
			min_amount=min_amount,
			min_qty=min_qty,
			mixed=min_qty is not None,
			valid_from=None,
			valid_upto=None,
			max_uses=None,
		)
		action = rule.required_child("action")
		if (cart_value := action.child("cart_value")) is not None:
			if bought is not None:
				# A discount off the order once the cart holds these items: ERPNext's transaction rules
				# don't look at the items.
				later.append(
					Condition(
						"rule_condition", {"source": source_terms(rule.required_child("condition").raw())}
					)
				)
			self._cap(cart_value, later)
			kind, value = self._discount(cart_value.required_child("discount"))
			return replace(base, discount=kind, value=value)
		if (cart_items := action.child("cart_items")) is not None:
			return self._cart_items(base, cart_items, bought, rule, later)
		if (gift := action.child("gift_item")) is not None:
			return self._gift(base, gift, bought, rule)
		if (fixed := action.child("fixed_price_set")) is not None:
			matcher = fixed.child("items")
			target = self._matcher(matcher) if matcher is not None else Matched([], [], [])
			price: Fields = {
				"qty": Decimal(fixed.integer("quantity")),
				"price": fixed.decimal("fixed_price"),
				**(
					target.terms() if target else {"source": source_terms(matcher.raw() if matcher else None)}
				),
			}
			later.append(Condition("fixed_price_set", price))
			return base
		if (shipping := action.child("shipping")) is not None:
			zones = shipping.get("zone_ids")
			later.append(Condition("free_shipping", {"zones": source_terms(zones)}))
			return base
		if (shipping_discount := action.child("shipping_discount")) is not None:
			kind, value = self._discount(shipping_discount.required_child("discount"))
			off: Fields = {
				"discount": kind,
				"value": value,
				"cap": shipping_discount.optional_decimal("maximum_allowed_discount_amount"),
				"methods": source_terms(shipping_discount.get("method_ids")),
			}
			later.append(Condition("shipping_discount", off))
			return base
		raise SourceError("A BigCommerce promotion rule with an unknown action.")

	def _cart_items(
		self, base: Discount, action: Node, bought: Matched | None, rule: Node, later: list[Condition]
	) -> Discount:
		"""A discount off matching items: per item, or once across them (``as_total``); on at most so many,
		the cheapest or dearest first; never on sale items."""
		matcher = action.child("items")
		target = self._matcher(matcher) if matcher is not None else Matched([], [], [])
		if target is None:
			later.append(
				Condition("item_matcher", {"source": source_terms(matcher.raw() if matcher else None)})
			)
			target = Matched([], [], [])
		self._cap(action, later)
		if most := action.optional_integer("quantity"):
			later.append(Condition("item_limit", {"qty": Decimal(most)}))
		if action.get("exclude_items_on_sale") is True:
			later.append(Condition("excluded_sale_items"))
		choice: Fields = {
			"strategy": action.optional_string("strategy"),
			"source": source_terms(
				{
					key: action.get(key)
					for key in ("include_items_considered_by_condition", "add_free_item")
					if action.get(key) is True
				}
			)
			or None,
		}
		if any(value is not None for value in choice.values()):
			later.append(Condition("item_choice", choice))
		kind, value = self._discount(action.required_child("discount"))
		if kind == "amount" and action.get("as_total") is True:
			later.append(Condition("spread_amount", target.terms()))
			return replace(base, discount=kind, value=value)
		discounted = replace(
			base,
			discount=kind,
			value=value,
			applies=target.applies(),
			items=target.products,
			groups=target.groups,
			variants=[(None, v) for v in target.variants],
		)
		return self._buying(discounted, bought, target, rule, later)

	def _buying(
		self, discount: Discount, bought: Matched | None, target: Matched, rule: Node, later: list[Condition]
	) -> Discount:
		"""The items a rule's condition asks for, when they differ from those it discounts: ERPNext applies
		the rule on what is bought and discounts the other item or group (one of them)."""
		if bought is None or bought.terms() == target.terms() or bought.applies() == "all_items":
			return discount
		other = (
			("items", target.products[0])
			if len(target.products) == 1 and not target.groups and not target.variants
			else ("groups", target.groups[0])
			if len(target.groups) == 1 and not target.products and not target.variants
			else None
		)
		if other is None or len(bought.variants) > 0:
			later.append(Condition("rule_condition", {"source": source_terms(rule.raw())}))
			return discount
		return replace(
			discount,
			applies=bought.applies(),
			items=bought.products,
			groups=bought.groups,
			variants=[],
			other=other,
		)

	def _gift(self, base: Discount, gift: Node, bought: Matched | None, rule: Node) -> Discount:
		"""A gift item: free, once, or for every so many bought when the rule repeats (``apply_once`` off)."""
		product, variant = gift.optional_integer("product_id"), gift.optional_integer("variant_id")
		repeats = rule.get("apply_once") is False
		bought = bought or Matched([], [], [])
		free = FreeItem(
			product=self._context.gid("Product", product) if product else None,
			variant=self._context.gid("Variant", variant) if variant else None,
			qty=Decimal(gift.integer("quantity")),
			recursive=repeats,
			recurse_for=(base.min_qty or Decimal(1)) if repeats else None,
		)
		return replace(
			base,
			applies="transaction" if bought.applies() == "all_items" else bought.applies(),
			items=bought.products,
			groups=bought.groups,
			variants=[(None, v) for v in bought.variants],
			free=free,
		)

	def _matcher(self, matcher: Node) -> Matched | None:
		"""Products, categories or variants; anything else (brands, options, custom fields, and/or/not) is
		None."""
		raw = matcher.raw()
		if set(raw) == {"products"}:
			return Matched([self._context.gid("Product", i) for i in matcher.integers("products")], [], [])
		if set(raw) == {"categories"}:
			return Matched([], [self._context.gid("Category", i) for i in matcher.integers("categories")], [])
		if set(raw) == {"variants"}:
			return Matched([], [], [self._context.gid("Variant", i) for i in matcher.integers("variants")])
		return None

	@staticmethod
	def _discount(discount: Node) -> tuple[str, Decimal]:
		percentage = discount.optional_decimal("percentage_amount")
		if percentage is not None:
			return "percentage", percentage
		return "amount", discount.decimal("fixed_amount")

	@staticmethod
	def _cap(action: Node, later: list[Condition]) -> None:
		cap = action.optional_decimal("maximum_allowed_discount_amount")
		if cap is not None:
			later.append(Condition("cap", {"amount": cap}))

	# -- customer groups and bulk pricing ---------------------------------------------------------------

	def _group_rule(self, node: Node) -> Desired | None:
		"""One of a customer group's discount rules, automatic for the group's customers."""
		later: list[Condition] = []
		applies, products, groups = "all_items", [], []
		match node.string("type"):
			case "all":
				pass
			case "category":
				applies, groups = "groups", [self._context.gid("Category", node.integer("category_id"))]
			case "product":
				applies, products = "items", [self._context.gid("Product", node.integer("product_id"))]
			case "price_list":
				later.append(Condition("price_list", {"source": source_terms(node.integer("price_list_id"))}))
			case other:
				raise SourceError(f"A BigCommerce customer group discount of type {other}.")
		kind = GROUP_METHODS.get(node.optional_string("method") or "percent")
		if kind is None:
			raise SourceError(f"A BigCommerce customer group discount by {node.optional_string('method')}.")
		return discount_desired(
			node.id(),
			Discount(
				codes=[],
				description=f"{node.string('group_name')}: {node.string('type')}",
				discount=kind,
				applies=applies,
				value=node.optional_decimal("amount") or Decimal(0),
				items=products,
				groups=groups,
				min_amount=None,
				valid_from=None,
				valid_upto=None,
				max_uses=None,
				automatic=True,
				customer_groups=[self._context.gid("CustomerGroup", node.integer("group_id"))],
				conditions=later,
			),
		)

	def _bulk_pricing(self, node: Node) -> Desired | None:
		"""A product's bulk pricing rules: a tier per quantity band (a fixed price, a percentage or an amount
		off each unit)."""
		tiers = []
		for rule in node.children("bulk_pricing_rules"):
			kind = BULK_TYPES.get(rule.string("type"))
			if kind is None:
				raise SourceError(f"A BigCommerce bulk pricing rule of type {rule.string('type')}.")
			most = rule.integer("quantity_max")
			tiers.append(
				Tier(
					discount=kind,
					value=rule.decimal("amount"),
					min_qty=Decimal(rule.integer("quantity_min")),
					max_qty=Decimal(most) if most else None,
				)
			)
		if not tiers:
			return None
		product = node.integer("product_id")
		return discount_desired(
			node.id(),
			Discount(
				codes=[],
				description=f"Bulk pricing, product {product}",
				discount=tiers[0].discount,
				applies="items",
				value=Decimal(0),
				items=[self._context.gid("Product", product)],
				groups=[],
				min_amount=None,
				valid_from=None,
				valid_upto=None,
				max_uses=None,
				automatic=True,
				tiers=tiers,
			),
		)


# ERPNext keeps an exchange rate to nine places.
EXCHANGE_RATE_PLACES = Decimal("0.000000001")


def foreign_currency(order: Node, store_currency: str) -> Fields:
	"""An order paid in another currency than the store's: the rate BigCommerce recorded
	(``store_default_to_transactional_exchange_rate``, order currency per store currency) as the books
	take it, store currency per order currency. BigCommerce books no total in the store's currency."""
	rate = order.decimal("store_default_to_transactional_exchange_rate")
	if rate <= 0:
		raise SourceError(f"BigCommerce order {order.string('source_id')} has the exchange rate {rate}.")
	return {
		"exchange_rate": (Decimal(1) / rate).quantize(EXCHANGE_RATE_PLACES),
		"base_currency": store_currency,
	}


class OrderMapper:
	"""An order → an invoice with BigCommerce's totals: lines at their pre-tax prices before discounts (on
	the variant's item), the order's coupon and manual discounts as the invoice's discount, each tax as a
	charge, each shipping address's cost as shipping (its handling as a fee), gift wrapping and the order's
	fees as fees; refunds as credit notes (their refunded products as returned lines), each shipment as a
	Shipment and the staff notes as a comment. Abandoned, cancelled, declined and archived orders map to nothing."""

	kind = Kind.ORDER
	version = 3

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if node.integer("status_id") in NOT_SOLD or node.get("is_deleted") is True:
			return None
		# The transactional currency the shopper paid in (its amounts are in it); currency_code is only the
		# currency prices were displayed in.
		currency = node.optional_string("default_currency_code") or node.string("currency_code")
		store_currency = node.optional_string("store_default_currency_code")
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
			"shipments": self._shipments(node, lines),
		}
		if store_currency is not None and currency != store_currency:
			fields |= foreign_currency(node, store_currency)
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

	def _shipments(self, order: Node, lines: dict[int, Node]) -> list[Field]:
		"""Each shipment: its carrier, method, tracking number and link, and the products it held."""
		found: list[Field] = []
		for shipment in order.children("shipments"):
			items = [
				(lines[item.integer("order_product_id")].text("name"), Decimal(item.integer("quantity")))
				for item in shipment.children("items")
				if item.integer("order_product_id") in lines
			]
			found.append(
				shipment_fields(
					Shipment(
						gid=self._context.gid("Shipment", shipment.integer("id")),
						date=shipment.optional_time("date_created") or order.time("date_created"),
						carrier=shipment.optional_string("shipping_provider_display_name")
						or shipment.optional_string("tracking_carrier")
						or shipment.optional_string("shipping_provider"),
						service=shipment.optional_string("shipping_method"),
						tracking_number=shipment.optional_string("tracking_number"),
						tracking_url=shipment.optional_string("tracking_link")
						or shipment.optional_string("generated_tracking_link"),
						delivered=False,
						contents=contents_of(items),
					)
				)
			)
		return found

	def _notes(self, order: Node) -> list[Field]:
		notes: list[Field] = []
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
		CustomerGroupMapper(),
		CustomerMapper(context),
		DiscountMapper(context),
		OrderMapper(context),
		PageMapper(),
		BlogMapper(),
		ArticleMapper(context),
		ReviewMapper(context),
		UrlRedirectMapper(host),
	]
