# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Magento → desired ERPNext state, one mapper per kind, in the shared field contract. Ported from
wp-woo-idempotent-importer's Magento mappers; Magento's custom attributes, product types, configurable
products, cart price rules, order states, applied taxes and base-currency totals are translated here.
Identities arrive scoped to the connection, with Magento's id as ``source_id``. Money arrives as JSON numbers
and is read exactly as spelled."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from urllib.parse import urlsplit

from idempotent_importer.contract import (
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
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.paths import path_of, slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.html import embedded_images, hosted_on
from idempotent_importer.sources.magento.reader import CONFIGURABLE, NOT_VISIBLE_INDIVIDUALLY

# Magento's storefront URL suffix for products and categories (its default; the setting isn't in the API).
URL_SUFFIX = ".html"
WEIGHT_UNITS = {"kgs": "kg", "lbs": "lbs"}
PRODUCT_MEDIA_PATH = "catalog/product"
SIMPLE_TYPES = frozenset({"simple", "virtual", "downloadable"})
VIRTUAL_TYPES = frozenset({"virtual", "downloadable"})
MIRRORED_LINE_TYPES = frozenset({"simple", "virtual", "downloadable", CONFIGURABLE})
GIFT_CARD = "giftcard"
DISABLED = 2
IMAGE = "image"
HOME_IDENTIFIER = "home"
# Order states: a cancelled order has no sale; a paid one is booked.
CANCELED = "canceled"
NO_COUPON = "NO_COUPON"
NO_FREE_SHIPPING = "0"
FREE_SHIPPING = {"1": "for the matching items only", "2": "for the shipment with matching items"}
COMBINE_ALL, COMBINE_TRUE = "all", "1"
AT_LEAST = ">="
# A rule's cart conditions ERPNext's pricing checks: the subtotal and the quantity, at least so much.
CART_MINIMUMS = {"base_subtotal": "min_amount", "total_qty": "min_qty"}
# An item condition naming categories ("is", "is one of").
CATEGORY_IDS = "category_ids"
CATEGORY_MATCHES = frozenset({"==", "()"})
# A tier price for every customer group.
ALL_GROUPS = 32000
METHOD_TITLE = "method_title"
# ERPNext keeps an exchange rate to nine places.
RATE_PLACES = Decimal("0.000000001")


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, type_: str, id_: int | str) -> str:
		return self.config.identity(type_, id_)

	def variant(self, product_id: int) -> str:
		"""The product bought: a simple product is its own one variant; a configurable product's variants are
		its child products, which is also what an order's child line names."""
		return self.gid("Variant", product_id)


def custom_attributes(node: Node) -> dict[str, JsonValue]:
	return {a.string("attribute_code"): a.get("value") for a in node.children("custom_attributes")}


def attribute(attributes: dict[str, JsonValue], code: str) -> str | None:
	value = attributes.get(code)
	return value if isinstance(value, str) and value else None


def decimal_attribute(attributes: dict[str, JsonValue], code: str) -> Decimal | None:
	value = attribute(attributes, code)
	try:
		return Decimal(value) if value is not None else None
	except ArithmeticError:
		raise SourceError(f"{code} is not a number: {value}") from None


def amount_of(node: Node, field: str) -> Decimal:
	return node.optional_decimal(field) or Decimal(0)


def whole(node: Node, field: str) -> Decimal:
	"""A whole quantity sent as a JSON number ("2.0000" → 2); a fractional one has no mirrored equivalent."""
	quantity = amount_of(node, field)
	if quantity != quantity.to_integral_value():
		raise Unsupported(f"a quantity of {quantity} isn't whole")
	return quantity.to_integral_value()


def legacy_path(url_path: str | None) -> tuple[str, ...]:
	"""A url key or path's old storefront path ("outerwear/rain-jackets" → "/outerwear/rain-jackets.html")."""
	path = path_of("/" + url_path.strip("/") + URL_SUFFIX) if url_path else None
	return (path,) if path else ()


def rule_date(date: str | None) -> str | None:
	"""A rule's from or to date: Magento dates rules by the store's calendar day, as ERPNext's pricing rules
	are, so the date is kept as it is."""
	if not date:
		return None
	try:
		return datetime.strptime(date[:10], "%Y-%m-%d").date().isoformat()
	except ValueError:
		raise SourceError(f"{date} is not a date.") from None


def magento_address(address_type: str, node: Node | None) -> Fields | None:
	"""A customer or order address: the first street line, the rest joined as the second; the region by its
	code (a customer address sends an object, an order address region_code), else by name."""
	if node is None:
		return None
	street = node.strings("street")
	if not street or not node.text("city"):
		return None
	region = node.child("region") if isinstance(node.get("region"), dict) else None
	state = (
		(region.optional_string("region_code") or region.optional_string("region"))
		if region
		else node.optional_string("region_code") or node.optional_string("region")
	)
	name = " ".join(p for p in (node.text("firstname"), node.text("lastname")) if p) or None
	return address_fields(
		Address(
			address_type=address_type,
			name=name,
			company=node.optional_string("company"),
			line1=street[0],
			line2=", ".join(street[1:]) or None,
			city=node.string("city"),
			state=state,
			postcode=node.optional_string("postcode"),
			country_code=node.optional_string("country_id"),
			email=node.optional_string("email"),
			phone=node.optional_string("telephone"),
		)
	)


def media_url(store: Node, file: str) -> str:
	return f"{store.string('media_url').rstrip('/')}/{PRODUCT_MEDIA_PATH}/{file.lstrip('/')}"


class CategoryMapper:
	"""A category below a store root → an Item Group: name, url key, description and image; its parent unless
	it sits directly under a root. Its old URL (url path + .html) redirects to it."""

	kind = Kind.COLLECTION
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		name = node.string("name")
		attributes = custom_attributes(node)
		store = node.required_child("store")
		parent = node.optional_integer("parent_id") or 0
		image = attribute(attributes, "image")
		if image and not image.startswith(("http://", "https://")):
			image = (
				store.string("base_url").rstrip("/") + image
				if image.startswith("/")
				else f"{store.string('media_url').rstrip('/')}/catalog/category/{image}"
			)
		fields: Fields = {
			"name": name,
			"slug": attribute(attributes, "url_key") or slugify(name),
			"parent": None
			if parent == 0 or parent in store.integers("root_category_ids")
			else self._context.gid("Category", parent),
			"description": attribute(attributes, "description"),
			"image": image,
		}
		return Desired(
			self.kind,
			node.id(),
			fields,
			legacy_path(attribute(attributes, "url_path") or attribute(attributes, "url_key")),
		)


class ProductMapper:
	"""A product → an Item. A simple, virtual or downloadable product is one Item (virtual and downloadable
	ones aren't stock items); a configurable product is a template whose variants are its enabled child
	products on the store's website, with the configurable attributes' option labels. A special price below
	the price is the sale. Stock follows each product's stock item. A disabled product is unpublished. Bundle,
	grouped and gift card products are Unsupported; a product not on the store's website maps to nothing."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		title = node.optional_string("name") or node.string("sku")
		product_type = node.string("type_id")
		store = node.required_child("store")
		if not on_website(node, store):
			return None
		simple = product_type in SIMPLE_TYPES
		if not simple and product_type != CONFIGURABLE:
			raise Unsupported(f"a Magento {product_type} product has no exact ERPNext item")
		unit = WEIGHT_UNITS.get((store.optional_string("weight_unit") or "").strip().lower())
		variants = (
			[self._variant(node, node, node.integer("source_id"), unit, {})]
			if simple
			else self._children(node, unit)
		)
		if not variants:
			raise Unsupported("a configurable product with no enabled child products")
		attributes = custom_attributes(node)
		url_key = attribute(attributes, "url_key")
		warnings: list[str] = []
		if node.children("options"):
			warnings.append("custom options, which Items can't ask for")
		if node.children("tier_prices"):
			warnings.append("tier or customer-group prices; the Item has one price")
		if attribute(attributes, "special_from_date") or attribute(attributes, "special_to_date"):
			warnings.append("a special price with dates; the sale is mirrored without them")
		extension = node.child("extension_attributes")
		if extension is not None and extension.children("downloadable_product_links"):
			warnings.append("downloadable files, which aren't mirrored")
		if unit is None and node.optional_decimal("weight"):
			warnings.append(f"a weight in {store.optional_string('weight_unit')}, which has no ERPNext UOM")
		first = variants[0]
		fields: Fields = {
			"type": "simple" if simple else "variable",
			"name": title,
			"slug": url_key or slugify(title),
			"description": attribute(attributes, "description"),
			"short_description": attribute(attributes, "short_description"),
			"sku": first["sku"] if simple else None,
			"barcode": None,
			"brand": None,
			"collections": [self._context.gid("Category", c) for c in category_ids(node, store)],
			"images": images(node, store),
			"published": node.optional_integer("status") != DISABLED,
			"is_stock_item": product_type not in VIRTUAL_TYPES
			and any(v["stock_qty"] is not None for v in variants),
			"weight": first["weight"] if simple else None,
			"price": first["price"] if simple else None,
			"sale": first["sale"] if simple else None,
			"stock_qty": first["stock_qty"] if simple else None,
			"attributes": [] if simple else variant_attributes(variants),
			"specifications": [],
			"variants": [] if simple else list[Field](variants),
			"tags": [],
		}
		return Desired(self.kind, node.id(), fields, legacy_path(url_key), tuple(warnings))

	def _children(self, product: Node, unit: str | None) -> list[Fields]:
		"""The enabled child products on the store's website, each with its configurable attributes' option
		labels."""
		store = product.required_child("store")
		attributes = {a.integer("attribute_id"): a for a in product.children("attributes")}
		options = sorted(
			product.required_child("extension_attributes").children("configurable_product_options"),
			key=lambda option: option.optional_integer("position") or 0,
		)
		variants: list[Fields] = []
		for child in product.children("children"):
			if child.optional_integer("status") == DISABLED or not on_website(child, store):
				continue
			values = custom_attributes(child)
			chosen: dict[str, Field] = {}
			for option in options:
				definition = attributes.get(option.integer("attribute_id"))
				if definition is None:
					raise SourceError(
						f"{product.path} names attribute {option.string('attribute_id')}, which wasn't read."
					)
				label = (
					option.optional_string("label")
					or definition.optional_string("default_frontend_label")
					or definition.string("attribute_code")
				)
				chosen[label] = option_label(
					definition, attribute(values, definition.string("attribute_code")), child
				)
			variants.append(self._variant(product, child, child.integer("id"), unit, chosen))
		return variants

	def _variant(
		self, product: Node, sold: Node, product_id: int, unit: str | None, chosen: dict[str, Field]
	) -> Fields:
		"""One variant: the product itself (simple) or one of its child products (configurable); ``product_id``
		is the Magento id of the product sold."""
		attributes = custom_attributes(sold)
		price = sold.optional_decimal("price")
		if price is None:
			raise SourceError(f"Magento product {product_id} has no price.")
		special = decimal_attribute(attributes, "special_price")
		extension = sold.child("extension_attributes")
		stock = extension.child("stock_item") if extension else None
		managed = stock is not None and (
			stock.get("use_config_manage_stock") is not False or stock.get("manage_stock") is True
		)
		weight = sold.optional_decimal("weight") or product.optional_decimal("weight")
		virtual = sold.optional_string("type_id") in VIRTUAL_TYPES
		pictures = images(sold, product.required_child("store"))
		return {
			"gid": self._context.variant(product_id),
			"sku": sold.optional_string("sku"),
			"barcode": None,
			"attributes": chosen,
			"price": price,
			"sale": {"price": special, "from": None, "to": None}
			if special is not None and special < price
			else None,
			"stock_qty": max(Decimal(0), whole(stock, "qty")) if stock is not None and managed else None,
			"weight": {"value": weight, "unit": unit} if unit and weight and not virtual else None,
			"image": pictures[0]["url"] if chosen and pictures and isinstance(pictures[0], dict) else None,
			"enabled": True,
		}


def on_website(product: Node, store: Node) -> bool:
	extension = product.child("extension_attributes")
	websites = (
		extension.integers("website_ids") if extension is not None and extension.has("website_ids") else None
	)
	return websites is None or store.integer("website_id") in websites


def option_label(definition: Node, value: str | None, child: Node) -> str:
	"""The label of the option a child product has (the child stores the option's value id)."""
	for option in definition.children("options"):
		if value is not None and option.string("value") == value:
			return option.string("label")
	raise SourceError(
		f"Magento product {child.integer('id')} has no {definition.string('attribute_code')} option its configurable product offers."
	)


def category_ids(product: Node, store: Node) -> list[int]:
	roots = store.integers("root_category_ids")
	extension = product.child("extension_attributes")
	links = extension.children("category_links") if extension else []
	return list(dict.fromkeys(i for link in links if (i := int(link.string("category_id"))) not in roots))


def images(product: Node, store: Node) -> list[Field]:
	"""The enabled gallery images, the base image first, then by position."""
	entries = [
		e
		for e in product.children("media_gallery_entries")
		if e.string("media_type") == IMAGE and e.get("disabled") is not True and e.optional_string("file")
	]
	entries.sort(key=lambda e: (IMAGE not in e.strings("types"), e.optional_integer("position") or 0))
	return [{"url": media_url(store, e.string("file")), "alt": e.optional_string("label")} for e in entries]


class CustomerMapper:
	"""A customer account → a Customer in its customer group, its Contact, and every saved address (the
	default billing first)."""

	kind = Kind.CUSTOMER
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		first, last = node.text("firstname"), node.text("lastname")
		saved = node.children("addresses")
		billing_id = node.optional_string("default_billing")
		billing = next((a for a in saved if billing_id and str(a.integer("id")) == billing_id), None) or next(
			(a for a in saved if a.get("default_billing") is True), None
		)
		addresses: list[Field] = []
		for entry in sorted(saved, key=lambda a: a is not billing):
			address = magento_address("Billing" if entry is billing else "Shipping", entry)
			if address is not None:
				addresses.append(address)
		phone = next((a.optional_string("telephone") for a in saved if a.optional_string("telephone")), None)
		fields: Fields = {
			"name": f"{first} {last}".strip() or node.string("email"),
			"type": "Individual",
			"first_name": first or None,
			"last_name": last or None,
			"company": None,
			"email": node.string("email").lower(),
			"phone": phone,
			"addresses": addresses,
		}
		group = node.optional_integer("group_id")
		if group is not None:
			fields["group"] = self._context.gid("CustomerGroup", group)
		return Desired(self.kind, node.id(), fields)


class CustomerGroupMapper:
	"""A customer group → a Customer Group its customers and discounts name."""

	kind = Kind.CUSTOMER_GROUP
	version = 1

	def map(self, node: Node) -> Desired | None:
		return Desired(self.kind, node.id(), {"name": node.string("code")})


class DiscountMapper:
	"""Every cart price rule (with coupons, or applied without one) and every product's tier prices → a
	Pricing Rule (a Promotional Scheme for tiers), enforcing natively what ERPNext's pricing does and recording
	the rest as conditions that hold it (``contract.CONDITIONS``).

	A rule: a percentage or amount off each matching item, an amount off the cart, or buy X get Y free (the
	same item, for every X); on every item or some categories; after a minimum subtotal or quantity; its
	dates; for its customer groups (one, or every group, with codes; several, without); each coupon's use
	limit and expiry. Recorded and held: per-customer limits (of a coupon: that coupon alone), discounts on
	shipping and free shipping (card 405); a limit on the discounted quantity, discount steps, other item and
	cart conditions, several customer groups on codes (card 407). An inactive rule, or one not on the store's
	website, maps to nothing. Catalog price rules have no REST API (an operator decision, coverage/magento.md).

	A product's tier prices (``TierPrice``, per customer group): a fixed unit price or a percentage off from
	each quantity, a scheme on the product for the group (every group for "ALL GROUPS")."""

	kind = Kind.DISCOUNT
	version = 3

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if Identity.parse(node.id()).type == "TierPrice":
			return self._tier_prices(node)
		store = node.required_child("store")
		if node.get("is_active") is not True or store.integer("website_id") not in node.integers(
			"website_ids"
		):
			return None
		later: list[Condition] = []
		automatic = node.string("coupon_type") == NO_COUPON
		groups = self._groups(node, automatic, later)
		if uses := node.integer("uses_per_customer"):
			later.append(Condition("per_customer_limit", {"uses": uses}))
		min_amount, min_qty = self._cart(node.child("condition"), later)
		categories = self._items(node, later)
		value = amount_of(node, "discount_amount")
		action = node.optional_string("simple_action") or ""
		free = None
		match action:
			case "by_percent":
				kind, applies = "percentage", "groups" if categories else "transaction"
			case "by_fixed":
				kind, applies = "amount", "groups" if categories else "all_items"
			case "cart_fixed":
				kind, applies = "amount", "transaction"
				if categories:
					later.append(Condition("spread_amount", {"groups": list[Field](categories)}))
					categories = []
			case "buy_x_get_y":
				step = node.optional_integer("discount_step") or 0
				if step < 1:
					raise SourceError(f'Magento rule "{node.optional_string("name")}" buys none to get some.')
				kind, applies = "percentage", "groups" if categories else "all_items"
				free = FreeItem(None, None, value, recursive=True, recurse_for=Decimal(step))
				min_qty = min_qty or Decimal(step)
				value = Decimal(0)
			case other:
				raise SourceError(f"A Magento rule's action {other}.")
		self._limits(node, action, kind, value, later)
		coupons = node.children("coupons")
		created = sorted(t for c in coupons if (t := c.optional_time("created_at")))
		per_coupon = node.integer("uses_per_coupon") or None
		discount = Discount(
			codes=[] if automatic else [c.string("code") for c in coupons if c.optional_string("code")],
			description=node.optional_string("name") or f"Rule {node.integer('rule_id')}",
			discount=kind,
			applies=applies,
			value=value,
			items=[],
			groups=categories,
			min_amount=min_amount,
			min_qty=min_qty,
			mixed=min_qty is not None and free is None,
			valid_from=rule_date(node.optional_string("from_date"))
			or date_of(created[0] if created else None),
			valid_upto=rule_date(node.optional_string("to_date")),
			max_uses=per_coupon,
			automatic=automatic,
			customer_groups=groups,
			free=free,
			conditions=later,
			code_limits={}
			if automatic
			else {c.string("code"): self._coupon(c) for c in coupons if c.optional_string("code")},
		)
		return discount_desired(node.id(), discount)

	def _groups(self, rule: Node, automatic: bool, later: list[Condition]) -> list[str]:
		"""The rule's customer groups: none when it is for every group; a coupon applies one Pricing Rule, so
		a coupon for several (but not every) group is a condition."""
		chosen = rule.integers("customer_group_ids")
		if not set(rule.integers("all_customer_group_ids")) - set(chosen):
			return []
		groups = [self._context.gid("CustomerGroup", g) for g in chosen]
		if len(groups) > 1 and not automatic:
			later.append(Condition("several_customer_groups", {"groups": list[Field](groups)}))
			return []
		return groups

	@staticmethod
	def _cart(condition: Node | None, later: list[Condition]) -> tuple[Decimal | None, Decimal | None]:
		"""The rule's minimum subtotal and quantity (all of its cart conditions true); any other cart
		condition is recorded."""
		parts = condition.children("conditions") if condition else []
		if condition is None or not parts:
			return None, None
		minimums: dict[str, Decimal] = {}
		rest = []
		combined = (
			condition.optional_string("aggregator_type") == COMBINE_ALL
			and condition.optional_string("value") == COMBINE_TRUE
		)
		for part in parts:
			attribute = part.optional_string("attribute_name")
			if (
				combined
				and not part.children("conditions")
				and part.optional_string("operator") == AT_LEAST
				and attribute in CART_MINIMUMS
			):
				minimums[CART_MINIMUMS[attribute]] = part.decimal("value")
			else:
				rest.append(part)
		if rest:
			later.append(Condition("rule_condition", {"source": source_terms(condition.raw())}))
		return minimums.get("min_amount"), minimums.get("min_qty")

	def _items(self, rule: Node, later: list[Condition]) -> list[str]:
		"""The categories the rule's action applies to (all of them true: an item in one of these); any other
		item condition is recorded."""
		action = rule.child("action_condition")
		parts = action.children("conditions") if action else []
		if action is None or not parts:
			return []
		only = parts[0] if len(parts) == 1 else None
		if (
			action.optional_string("aggregator_type") == COMBINE_ALL
			and action.optional_string("value") == COMBINE_TRUE
			and only is not None
			and not only.children("conditions")
			and only.optional_string("attribute_name") == CATEGORY_IDS
			and only.optional_string("operator") in CATEGORY_MATCHES
		):
			ids = [i.strip() for i in only.string("value").split(",") if i.strip()]
			if all(i.isdigit() for i in ids):
				return [self._context.gid("Category", int(i)) for i in ids]
		later.append(Condition("item_matcher", {"source": source_terms(action.raw())}))
		return []

	@staticmethod
	def _limits(rule: Node, action: str, kind: str, value: Decimal, later: list[Condition]) -> None:
		"""A cap on the discounted quantity, a step (but buy X get Y's), shipping discounts and free
		shipping."""
		if most := amount_of(rule, "discount_qty"):
			later.append(Condition("item_limit", {"qty": most}))
		step = rule.optional_integer("discount_step") or 0
		if step > 0 and action != "buy_x_get_y":
			later.append(Condition("item_choice", {"source": {"discount_step": step}}))
		if rule.get("apply_to_shipping") is True:
			off: Fields = {"discount": kind, "value": value}
			if kind != "percentage":
				# A fixed amount is spread over the items and shipping together, not taken off shipping again:
				# kept as Magento's own action, so it stays held.
				off["source"] = {"simple_action": action}
			later.append(Condition("shipping_discount", off))
		free_shipping = rule.optional_string("simple_free_shipping") or NO_FREE_SHIPPING
		if free_shipping != NO_FREE_SHIPPING:
			later.append(
				Condition("free_shipping", {"source": FREE_SHIPPING.get(free_shipping, free_shipping)})
			)

	@staticmethod
	def _coupon(coupon: Node) -> Code:
		"""A coupon's own use limit (else the rule's uses per coupon) and expiry; its per-customer limit is a
		condition of the coupon."""
		per_customer = coupon.optional_integer("usage_per_customer")
		return Code(
			max_uses=coupon.optional_integer("usage_limit") or None,
			valid_upto=rule_date(coupon.optional_string("expiration_date")),
			conditions=[Condition("per_customer_limit", {"uses": per_customer})] if per_customer else [],
		)

	def _tier_prices(self, node: Node) -> Desired | None:
		"""One customer group's tier prices on a product: each from its quantity up to the next tier's."""
		prices = sorted(node.children("tier_prices"), key=lambda t: t.decimal("qty"))
		if not prices:
			return None
		tiers = []
		for index, price in enumerate(prices):
			following = prices[index + 1].decimal("qty") if index + 1 < len(prices) else None
			extension = price.child("extension_attributes")
			percentage = extension.optional_decimal("percentage_value") if extension else None
			tiers.append(
				Tier(
					discount="percentage" if percentage is not None else "rate",
					value=percentage if percentage is not None else price.decimal("value"),
					min_qty=price.decimal("qty"),
					max_qty=following - 1 if following is not None else None,
				)
			)
		group = node.integer("customer_group_id")
		product = node.integer("product_id")
		# A configurable product's child is sold as one of its variants.
		child = node.optional_integer("visibility") == NOT_VISIBLE_INDIVIDUALLY
		return discount_desired(
			node.id(),
			Discount(
				codes=[],
				description=f"Tier prices, product {product}",
				discount=tiers[0].discount,
				applies="items",
				value=Decimal(0),
				items=[] if child else [self._context.gid("Product", product)],
				variants=[(None, self._context.variant(product))] if child else [],
				groups=[],
				min_amount=None,
				valid_from=None,
				valid_upto=None,
				max_uses=None,
				automatic=True,
				customer_groups=[] if group == ALL_GROUPS else [self._context.gid("CustomerGroup", group)],
				tiers=tiers,
			),
		)


class OrderMapper:
	"""An order → an invoice with Magento's totals (in the order's currency): the lines a customer sees at
	their row totals (a configurable product's line on the child product bought), the discount, each applied
	tax, the shipping; credit memos as credit notes (their lines at their row totals, an adjustment for the
	rest), each shipment as a Shipment and each order comment as a comment. Paid orders are submitted; unpaid ones are
	drafts; cancelled orders map to nothing. An order in another currency than the store's base currency
	carries Magento's base total and the rate it implies."""

	kind = Kind.ORDER
	version = 3

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_string("state") == CANCELED:
			return None
		currency = node.string("order_currency_code")
		items = node.children("items")
		lines = [item for item in items if item.optional_integer("parent_item_id") is None]
		children = {
			item.integer("parent_item_id"): item
			for item in items
			if item.optional_integer("parent_item_id") is not None
		}
		extension = node.child("extension_attributes")
		assignments = extension.children("shipping_assignments") if extension else []
		shipping = assignments[0].child("shipping") if assignments else None
		customer = node.optional_integer("customer_id") or 0
		guest = customer == 0 or node.optional_integer("customer_is_guest") == 1
		billing = node.child("billing_address")
		submitted = bool(node.optional_decimal("total_paid"))
		payment = node.child("payment")
		mapped = {
			line.integer("item_id"): self._line(line, children.get(line.integer("item_id")), currency)
			for line in lines
		}
		fields: Fields = {
			"number": f"#{node.string('increment_id')}",
			"currency": currency,
			"created_at": node.time("created_at"),
			"submitted": submitted,
			"customer": None if guest else self._context.gid("Customer", customer),
			"guest": {
				"name": " ".join(
					p for p in (node.text("customer_firstname"), node.text("customer_lastname")) if p
				)
				or None,
				"email": (node.optional_string("customer_email") or "").lower() or None,
				"phone": billing.optional_string("telephone") if billing else None,
			}
			if guest
			else None,
			"billing": magento_address("Billing", billing),
			"shipping": magento_address("Shipping", shipping.child("address") if shipping else None),
			"lines": [line_fields(line) for line in mapped.values()],
			"discount": abs(amount_of(node, "discount_amount")),
			"charges": [charge_fields(charge) for charge in charges(node, extension)],
			"total": amount_of(node, "grand_total"),
			"payment": {
				"method": payment_title(extension, payment),
				"reference": payment.optional_string("last_trans_id") if payment else None,
				"date": node.time("created_at"),
			}
			if submitted
			else None,
			"refunds": [self._refund(memo, mapped, currency) for memo in node.children("credit_memos")]
			if submitted
			else [],
			"notes": self._notes(node),
			"shipments": self._shipments(node),
			"remarks": node.optional_string("customer_note"),
		}
		base_currency = node.optional_string("base_currency_code")
		if base_currency and base_currency != currency:
			fields |= base_fields(node, base_currency)
		return Desired(self.kind, node.id(), fields)

	def _line(self, line: Node, child: Node | None, currency: str) -> OrderLine:
		line_type = line.optional_string("product_type") or ""
		if line_type == GIFT_CARD:
			raise Unsupported("a gift card sale has no ERPNext item (coverage/magento.md)")
		product = line.optional_integer("product_id") or 0
		mirrored = product > 0 and line_type in MIRRORED_LINE_TYPES
		bought = (child.optional_integer("product_id") if child else None) or product
		return order_line(
			self._context.gid("OrderItem", line.integer("item_id")),
			self._context.gid("Product", product) if mirrored else None,
			self._context.variant(bought) if mirrored else None,
			line.optional_string("name") or line.string("sku"),
			whole(line, "qty_ordered"),
			amount_of(line, "row_total"),
			currency,
		)

	def _refund(self, memo: Node, lines: dict[int, OrderLine], currency: str) -> Field:
		gid = self._context.gid("CreditMemo", memo.integer("entity_id"))
		returned: list[OrderLine] = []
		for item in memo.children("items"):
			quantity = whole(item, "qty")
			sold = lines.get(item.integer("order_item_id"))
			if quantity <= 0 or sold is None:
				continue
			returned.append(
				order_line(
					sold.source_line,
					sold.product,
					sold.variant,
					sold.name,
					-quantity,
					-amount_of(item, "row_total"),
					currency,
				)
			)
		amount = amount_of(memo, "grand_total")
		balance = refund_balance(f"{gid}#adjustment", amount, returned, [], currency)
		comments = memo.children("comments")
		return {
			"gid": gid,
			"date": memo.optional_time("created_at"),
			"reason": comments[0].optional_string("comment") if comments else None,
			"amount": amount,
			"lines": [line_fields(line) for line in [*returned, *([balance] if balance else [])]],
			"charges": [],
		}

	def _shipments(self, order: Node) -> list[Field]:
		"""Each shipment: its tracks' carrier and numbers, and the items it held."""
		found: list[Field] = []
		for shipment in order.children("shipments"):
			tracks = shipment.children("tracks")
			found.append(
				shipment_fields(
					Shipment(
						gid=self._context.gid("Shipment", shipment.integer("entity_id")),
						date=shipment.optional_time("created_at") or order.time("created_at"),
						carrier=next(
							(
								c
								for t in tracks
								if (c := t.optional_string("title") or t.optional_string("carrier_code"))
							),
							None,
						),
						service=None,
						tracking_number=", ".join(
							n for t in tracks if (n := t.optional_string("track_number"))
						)
						or None,
						tracking_url=None,
						delivered=False,
						contents=contents_of(
							[
								(item.text("name"), whole(item, "qty"))
								for item in shipment.children("items")
								if item.optional_decimal("qty")
							]
						),
					)
				)
			)
		return found

	def _notes(self, order: Node) -> list[Field]:
		created = order.time("created_at")
		notes: list[Field] = []
		for history in order.children("status_histories"):
			comment = history.optional_string("comment")
			if comment:
				notes.append(
					{
						"gid": self._context.gid("OrderComment", history.integer("entity_id")),
						"date": history.optional_time("created_at") or created,
						"author": None,
						"text": comment,
						"customer_note": False,
					}
				)
		return notes


def base_fields(order: Node, base_currency: str) -> Fields:
	"""An order in another currency than the store's base: Magento's base total, and the rate it implies to
	ERPNext's nine places (so the invoice's base total rounds to Magento's); without a base total (or a
	total to divide it by), Magento's own rate (``base_to_order_rate``, order currency per base currency)."""
	total = amount_of(order, "grand_total")
	base_total = order.optional_decimal("base_grand_total")
	if not total or base_total is None:
		rate = order.optional_decimal("base_to_order_rate")
		if not rate:
			raise SourceError(
				f"Magento order {order.string('source_id')} has no base total or exchange rate."
			)
		return {
			"exchange_rate": (Decimal(1) / rate).quantize(RATE_PLACES, rounding=ROUND_HALF_EVEN),
			"base_currency": base_currency,
		}
	return {
		"exchange_rate": (base_total / total).quantize(RATE_PLACES, rounding=ROUND_HALF_EVEN),
		"base_total": base_total,
		"base_currency": base_currency,
	}


def charges(order: Node, extension: Node | None) -> list[Charge]:
	"""Each applied tax (the order's tax total when Magento itemises none), then the shipping."""
	found = [
		Charge(
			TAX,
			tax.optional_string("title") or tax.optional_string("code") or "Tax",
			amount_of(tax, "amount"),
		)
		for tax in (extension.children("applied_taxes") if extension else [])
	]
	if not found and amount_of(order, "tax_amount"):
		found = [Charge(TAX, "Tax", amount_of(order, "tax_amount"))]
	if order.optional_integer("is_virtual") != 1:
		found.append(
			Charge(
				SHIPPING,
				order.optional_string("shipping_description") or "Shipping",
				amount_of(order, "shipping_amount"),
			)
		)
	return [charge for charge in found if charge.amount != 0]


def payment_title(extension: Node | None, payment: Node | None) -> str:
	for info in extension.children("payment_additional_info") if extension else []:
		if info.optional_string("key") == METHOD_TITLE and info.optional_string("value"):
			return info.string("value")
	return (payment.optional_string("method") if payment else None) or "Magento"


class PageMapper:
	"""A CMS page → a Web Page with its content, its identifier's last segment as the slug; an inactive page
	is unpublished. Its old URL (/<identifier>) redirects to it (the home page has none). Magento's content
	directives ({{media}}, {{widget}}, Page Builder markup) are kept as written."""

	kind = Kind.PAGE
	version = 1

	def __init__(self, store_host: str | None) -> None:
		self._owned = hosted_on(store_host or "")

	def map(self, node: Node) -> Desired | None:
		identifier = node.string("identifier").strip("/")
		title = node.optional_string("title") or identifier
		body = node.text("content")
		path = path_of(f"/{identifier}")
		fields: Fields = {
			"title": title,
			"slug": identifier.rsplit("/", 1)[-1] or slugify(title),
			"parent": None,
			"published": node.get("active") is not False,
			"html": body,
			"images": list(embedded_images(body, self._owned)),
		}
		return Desired(
			self.kind, node.id(), fields, () if path is None or identifier == HOME_IDENTIFIER else (path,)
		)


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		CategoryMapper(context),
		ProductMapper(context),
		CustomerGroupMapper(),
		CustomerMapper(context),
		DiscountMapper(context),
		OrderMapper(context),
		PageMapper(urlsplit(config.store_url.strip()).hostname),
	]
