# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Lightspeed eCom → desired ERPNext state, one mapper per kind, in the shared field contract. Ported
from wp-woo-idempotent-importer's Lightspeed mappers; Lightspeed's ``false``-for-unset fields, resource
links, split street addresses, prices on both sides of tax, discount codes and order states are translated
here. Identities arrive scoped to the connection, with Lightspeed's id as ``source_id``. Orders are booked
from Lightspeed's tax-exclusive amounts and its taxes, so an invoice matches the order whether the shop shows
prices with tax (retail) or without (B2B)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from idempotent_importer.contract import (
	FEE,
	SHIPPING,
	TAX,
	Address,
	Charge,
	Condition,
	Discount,
	OrderLine,
	Shipment,
	address_fields,
	charge_fields,
	date_of,
	discount_desired,
	line_fields,
	order_line,
	rating_fraction,
	refund_balance,
	shipment_fields,
	variant_attributes,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import normalise, path_of, redirect_target, slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.lightspeed.reader import linked_id, unset

WEIGHT_UNITS = {"g": "g", "kg": "kg", "oz": "oz", "lb": "lbs"}
PAGE_SUFFIX = ".html"
VARIANT_ATTRIBUTE = "Variant"
TRACKING_DISABLED = "disabled"
# Order states: a cancelled order has no sale; a paid one is booked. A cancelled shipment shipped nothing.
CANCELLED = "cancelled"
PAID = frozenset({"paid"})
CREDITED_PRODUCT = "product"
MAX_SCORE = 5
APPLIES_TO_ALL = "all"
# A discount code's type, and a discount rule's discount type, as a discount.
CODE_DISCOUNTS = {"percentage": "percentage", "price": "amount"}
RULE_DISCOUNTS = {"percentage": "percentage", "currency": "amount"}
# A discount rule's thresholds, whose meaning Lightspeed doesn't document.
RULE_THRESHOLDS = ("minCategoryAmount", "minCategoryProducts", "freeProductsCount")
APPLIES_TO_PRODUCTS = frozenset({"products", "productscategories"})
APPLIES_TO_CATEGORIES = frozenset({"categories", "productscategories"})
SHIPPING_UNCHANGED = "default"


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, type_: str, id_: int) -> str:
		return self.config.identity(type_, id_)


def text(node: Node, field: str) -> str | None:
	"""A string field Lightspeed may send as ``false`` or empty when unset."""
	value = node.get(field)
	return value.strip() or None if isinstance(value, str) else None


def amount_of(node: Node, field: str) -> Decimal:
	return node.optional_decimal(field) or Decimal(0)


def set_amount(node: Node, field: str) -> Decimal | None:
	value = node.optional_decimal(field)
	return value if value else None


def slug_of(url: str | None, title: str) -> str:
	"""The slug an object's URL ends in ("clothes/womens" → "womens"), else one made from the title."""
	last = (url or "").strip("/").rsplit("/", 1)[-1]
	return last or slugify(title)


def storefront(path: str) -> tuple[str, ...]:
	found = path_of(normalise(path))
	return (found,) if found else ()


def lightspeed_address(
	address_type: str, node: Node, prefix: str, first: str | None, last: str | None, phone: str | None
) -> Fields | None:
	"""An address with Lightspeed's field prefix (``addressBilling``, ``addressShipping``): the street,
	house number and extension as one line; the region's code when Lightspeed resolved one."""

	def field(name: str) -> str:
		return f"{prefix}{name}"

	street = " ".join(
		p
		for p in (
			text(node, field("Street")),
			(text(node, field("Number")) or "") + (text(node, field("Extension")) or ""),
		)
		if p
	)
	city = text(node, field("City"))
	if not street or not city:
		return None
	country = unset(node, field("Country"))
	region = unset(node, field("RegionData"))
	company = (
		text(node, "addressShippingCompany") if prefix == "addressShipping" else text(node, "companyName")
	)
	return address_fields(
		Address(
			address_type=address_type,
			name=text(node, field("Name")) or " ".join(p for p in (first, last) if p) or None,
			company=company,
			line1=street,
			line2=text(node, field("Street2")),
			city=city,
			state=(region.optional_string("code") or region.optional_string("name"))
			if region
			else text(node, field("Region")),
			postcode=text(node, field("Zipcode")),
			country_code=country.string("code").upper() if country else None,
			email=None,
			phone=phone,
		)
	)


def same_place(first: Fields, second: Fields) -> bool:
	"""Two addresses that differ only in their type (a customer's shipping address is often its billing one)."""
	return {k: v for k, v in first.items() if k != "address_type"} == {
		k: v for k, v in second.items() if k != "address_type"
	}


def taxes_by_name(node: Node, field: str) -> dict[str, Decimal]:
	"""A tax rate list (``[{name, rate, amount}]``) summed by name; rates that charged nothing left out."""
	found: dict[str, Decimal] = {}
	raw = node.get(field)
	for tax in node.children(field) if isinstance(raw, list) else []:
		amount = amount_of(tax, "amount")
		if amount:
			name = tax.optional_string("name") or "Tax"
			found[name] = found.get(name, Decimal(0)) + amount
	return found


class CategoryMapper:
	"""A category → an Item Group: title, the slug its URL ends in, its content (else its short description),
	image and parent; its old storefront URL redirects to it."""

	kind = Kind.COLLECTION
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		title, url = node.string("title"), node.string("url")
		image = unset(node, "image")
		parent = linked_id(node, "parent")
		fields: Fields = {
			"name": title,
			"slug": slug_of(url, title),
			"parent": self._context.gid("Category", parent) if parent is not None else None,
			"description": text(node, "content") or text(node, "description"),
			"image": image.string("src") if image else None,
		}
		return Desired(self.kind, node.id(), fields, storefront(f"/{url.strip('/')}"))


class ProductMapper:
	"""A product → an Item: one (a single variant without matrix options) or a template with a variant per
	variant (matrix options as attributes; several plain variants told apart by a "Variant" attribute of their
	titles). Prices as the shop shows them (with tax in a retail shop, without in a B2B shop); an old price
	above the price makes it the price and the price the sale. Stock follows each variant's tracking. Hidden
	products are unpublished; the old storefront URL (``/<url>.html``) redirects to the item."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		title, url = node.string("title"), node.string("url")
		sources = sorted(
			node.children("variants"), key=lambda v: (v.optional_integer("sortOrder") or 0, v.integer("id"))
		)
		if not sources:
			raise SourceError(f"Lightspeed product {node.string('source_id')} has no variants.")
		simple = len(sources) == 1 and not matrix_options(sources[0])
		includes_tax = node.get("shop_is_b2b") is not True
		variants = [self._variant(variant, simple, includes_tax) for variant in sources]
		warnings = tuple(
			f"a variant weighed in {text(v, 'weightUnit') or 'no unit'}, which has no ERPNext UOM"
			for v in sources
			if text(v, "weightUnit") not in WEIGHT_UNITS and v.optional_decimal("weightValue")
		)
		first = variants[0]
		fields: Fields = {
			"type": "simple" if simple else "variable",
			"name": title,
			"slug": slug_of(url, title),
			"description": text(node, "content"),
			"short_description": text(node, "description"),
			"sku": first["sku"] if simple else None,
			"barcode": first["barcode"] if simple else None,
			"brand": node.optional_string("brand"),
			"collections": [self._context.gid("Category", c) for c in node.integers("category_ids")],
			"images": images(node),
			"published": node.get("isVisible") is not False,
			"is_stock_item": any(v["stock_qty"] is not None for v in variants),
			"weight": first["weight"] if simple else None,
			"price": first["price"] if simple else None,
			"sale": first["sale"] if simple else None,
			"stock_qty": first["stock_qty"] if simple else None,
			"attributes": [] if simple else variant_attributes(variants),
			"specifications": [],
			"variants": [] if simple else list[Field](variants),
			"tags": [],
		}
		return Desired(
			self.kind,
			node.id(),
			fields,
			storefront(f"/{url.strip('/')}{PAGE_SUFFIX}"),
			tuple(dict.fromkeys(warnings)),
		)

	def _variant(self, variant: Node, simple: bool, includes_tax: bool) -> Fields:
		variant_id = variant.integer("id")
		chosen: dict[str, Field] = {} if simple else dict[str, Field](matrix_options(variant))
		if not simple and not chosen:
			chosen = {VARIANT_ATTRIBUTE: text(variant, "title") or str(variant_id)}
		price = variant.optional_decimal("priceIncl" if includes_tax else "priceExcl")
		if price is None:
			raise SourceError(f"Lightspeed variant {variant_id} has no price.")
		old = set_amount(variant, "oldPriceIncl" if includes_tax else "oldPriceExcl")
		tracked = (text(variant, "stockTracking") or TRACKING_DISABLED) != TRACKING_DISABLED
		unit = WEIGHT_UNITS.get((text(variant, "weightUnit") or "").lower())
		weight = variant.optional_decimal("weightValue")
		image = unset(variant, "image")
		on_sale = old is not None and old > price
		return {
			"gid": self._context.gid("Variant", variant_id),
			"sku": text(variant, "sku"),
			"barcode": text(variant, "ean"),
			"attributes": chosen,
			"price": old if on_sale and old is not None else price,
			"sale": {"price": price, "from": None, "to": None} if on_sale else None,
			"stock_qty": Decimal(max(0, variant.optional_integer("stockLevel") or 0)) if tracked else None,
			"weight": {"value": weight.normalize(), "unit": unit} if unit and weight else None,
			"image": image.string("src") if image else None,
			"enabled": True,
		}


def matrix_options(variant: Node) -> dict[str, str]:
	"""A matrix variant's options, option name → chosen value, in the variant's order."""
	if not isinstance(variant.get("options"), list):
		return {}
	return {
		option.string("name"): option.required_child("value").string("name")
		for option in variant.children("options")
	}


def images(product: Node) -> list[Field]:
	"""The product's images by sort order (else its one main image), at full size."""
	found = sorted(
		product.children("images"), key=lambda i: (i.optional_integer("sortOrder") or 0, i.integer("id"))
	)
	main = unset(product, "image")
	if not found and main is not None:
		found = [main]
	return [{"url": image.string("src"), "alt": None} for image in found]


class CustomerMapper:
	"""A customer → a Customer, its Contact, and its billing and shipping addresses."""

	kind = Kind.CUSTOMER
	version = 1

	def map(self, node: Node) -> Desired | None:
		first, last = text(node, "firstname"), text(node, "lastname")
		phone = text(node, "phone") or text(node, "mobile")
		email = (text(node, "email") or "").lower() or None
		company = text(node, "companyName")
		person = " ".join(p for p in (first, last) if p)
		billing = lightspeed_address("Billing", node, "addressBilling", first, last, phone)
		shipping = lightspeed_address("Shipping", node, "addressShipping", first, last, phone)
		if billing is not None and shipping is not None and same_place(billing, shipping):
			shipping = None
		addresses = [address for address in (billing, shipping) if address is not None]
		fields: Fields = {
			"name": person or company or email or node.string("source_id"),
			"type": "Company" if node.get("isCompany") is True and company else "Individual",
			"first_name": first,
			"last_name": last,
			"company": company,
			"email": email,
			"phone": phone,
			"addresses": list[Field](addresses),
		}
		return Desired(self.kind, node.id(), fields)


class DiscountMapper:
	"""Every discount code and discount rule → a Pricing Rule, enforcing natively what ERPNext's pricing does
	and recording the rest as conditions that hold it (``contract.CONDITIONS``).

	A code: a percentage or amount off the order, a percentage off some products or categories, with its
	minimum order amount, use limit and dates. Recorded and held: free shipping and shipping discounts, codes
	that don't combine with others (card 405); an amount off some products (Lightspeed doesn't say per item or
	in total), categories beside products, a minimum checked after the discount, a discount taken after tax
	(card 407).

	A rule (automatic): its dates; a discount off some categories is enforced. Lightspeed documents no meaning
	for a rule's thresholds (``minCategoryAmount``, ``minCategoryProducts``, ``freeProductsCount``), so a rule
	that uses them, a rule for customer groups or repeat customers, an order minimum, free items or free
	shipping, the cheapest item's discount and a use limit are recorded and held. An inactive code or rule maps
	to nothing."""

	kind = Kind.DISCOUNT
	version = 2

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if node.get("isActive") is not True:
			return None
		if Identity.parse(node.id()).type == "DiscountRule":
			return self._rule(node)
		code, discount_type = node.string("code"), node.string("type")
		applies_to = text(node, "applyTo") or APPLIES_TO_ALL
		later: list[Condition] = []
		products = (
			[self._context.gid("Product", p.integer("id")) for p in node.children("products")]
			if applies_to in APPLIES_TO_PRODUCTS
			else []
		)
		groups = (
			[self._context.gid("Category", c.integer("id")) for c in node.children("categories")]
			if applies_to in APPLIES_TO_CATEGORIES
			else []
		)
		if products and groups:
			later.append(Condition("groups_beside_items", {"groups": list[Field](groups)}))
			groups = []
		value = amount_of(node, "discount")
		kind = CODE_DISCOUNTS.get(discount_type)
		if kind is None:
			raise SourceError(f"A Lightspeed discount code of type {discount_type}.")
		if kind == "amount" and (products or groups):
			later.append(
				Condition(
					"spread_amount",
					{"items": list[Field](products) or None, "groups": list[Field](groups) or None},
				)
			)
			products, groups = [], []
		match text(node, "shipment") or SHIPPING_UNCHANGED:
			case "free":
				later.append(Condition("free_shipping"))
			case "discount":
				later.append(Condition("shipping_discount", {"discount": kind, "value": value}))
		if node.get("allowCombination") is False:
			later.append(Condition("combination", {"exclusive": True}))
		taken: Fields = {
			"before_tax": False if node.get("before_tax") is False else None,
			"minimum_after": True
			if node.get("minimum_after") is True and set_amount(node, "minimumAmount")
			else None,
		}
		if any(v is not None for v in taken.values()):
			later.append(Condition("rule_condition", {"source": taken}))
		discount = Discount(
			codes=[code],
			description=code,
			discount=kind,
			applies="items" if products else "groups" if groups else "transaction",
			value=value,
			items=products,
			groups=groups,
			min_amount=set_amount(node, "minimumAmount"),
			valid_from=text(node, "startDate") or date_of(node.optional_time("createdAt")),
			valid_upto=text(node, "endDate"),
			max_uses=node.optional_integer("usageLimit") or None,
			conditions=later,
		)
		return discount_desired(node.id(), discount)

	def _rule(self, node: Node) -> Desired | None:
		later: list[Condition] = []
		kind = RULE_DISCOUNTS.get(text(node, "discountType") or "")
		value = amount_of(node, "discountAmount") if kind else Decimal(0)
		if kind is None and text(node, "discountType"):
			later.append(
				Condition("rule_condition", {"source": {"discountType": text(node, "discountType")}})
			)
		categories = [self._context.gid("Category", i) for i in link_ids(node, "categories")]
		thresholds: Fields = {key: node.decimal(key) for key in RULE_THRESHOLDS if node.optional_decimal(key)}
		rule_type = node.string("type")
		applies, groups = "transaction", []
		match rule_type:
			case "get_x_discount_in_y_categories":
				applies, groups = "groups", categories
			case "get_x_discount_in_y_customer_groups":
				customer_groups: list[Field] = [
					self._context.gid("CustomerGroup", i) for i in link_ids(node, "customer_groups")
				]
				later.append(Condition("several_customer_groups", {"groups": customer_groups}))
			case "get_x_discount_for_repeat_customers":
				later.append(Condition("customer_history", {"returning": True}))
			case "get_x_discount_on_orders_over_y":
				later.append(Condition("rule_condition", {"source": {"type": rule_type, **thresholds}}))
				thresholds = {}
			case "get_free_shipping_on_orders_over_x" | "buy_x_items_get_free_shipping":
				later.append(Condition("free_shipping", {"source": {"type": rule_type, **thresholds}}))
				thresholds, value = {}, Decimal(0)
			case "buy_x_items_get_y_items_free":
				free: list[Field] = [self._context.gid("Product", i) for i in link_ids(node, "freeProducts")]
				later.append(
					Condition("rule_condition", {"source": {"type": rule_type, **thresholds}, "items": free})
				)
				thresholds, value = {}, Decimal(0)
			case "buy_x_items_from_cat_get_cheapest_free" | "buy_x_items_from_cat_get_y_discount_on_cheapest":
				applies, groups = "groups", categories
				later.append(
					Condition(
						"item_choice", {"strategy": "cheapest", "source": {"type": rule_type, **thresholds}}
					)
				)
				thresholds = {}
				if rule_type.endswith("cheapest_free"):
					kind, value = "percentage", Decimal(100)
			case other:
				raise SourceError(f"A Lightspeed discount rule of type {other}.")
		if thresholds:
			later.append(Condition("rule_condition", {"source": thresholds}))
		if uses := node.optional_integer("maximumUses"):
			later.append(Condition("shared_use_limit", {"uses": uses}))
		discount = Discount(
			codes=[],
			description=text(node, "title") or f"Discount rule {node.integer('id')}",
			discount=kind or "percentage",
			applies=applies,
			value=value,
			items=[],
			groups=groups,
			min_amount=None,
			valid_from=text(node, "startDate"),
			valid_upto=text(node, "endDate"),
			max_uses=None,
			automatic=True,
			conditions=later,
		)
		return discount_desired(node.id(), discount)


def link_ids(node: Node, field: str) -> list[int]:
	"""The ids a list of links names (``[{"resource": {"id": 12, …}}]``)."""
	ids = []
	for link in node.children(field):
		resource = unset(link, "resource")
		if resource is not None and resource.get("id") is not False:
			ids.append(resource.integer("id"))
	return ids


class OrderMapper:
	"""An order → an invoice with Lightspeed's totals in the shop's currency: lines at their tax-exclusive base
	prices on the variant's item, the lines' discounts, each tax the order charged, the shipping and the
	payment costs; credit notes as credit notes (their product lines, and an adjustment for the rest), each
	shipment and the staff memo as comments. Paid orders are submitted; others are drafts. Cancelled orders
	map to nothing."""

	kind = Kind.ORDER
	version = 2

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if node.string("status") == CANCELLED:
			return None
		currency = node.string("currency")
		first, last = text(node, "firstname"), text(node, "lastname")
		phone = text(node, "phone") or text(node, "mobile")
		email = (text(node, "email") or "").lower() or None
		customer = linked_id(node, "customer")
		submitted = text(node, "paymentStatus") in PAID
		products = node.children("products")
		lines = {line.integer("id"): self._line(line, currency) for line in products}
		fields: Fields = {
			"number": node.string("number"),
			"currency": currency,
			"created_at": node.time("createdAt"),
			"submitted": submitted,
			"customer": self._context.gid("Customer", customer) if customer is not None else None,
			"guest": None
			if customer is not None
			else {"name": " ".join(p for p in (first, last) if p) or None, "email": email, "phone": phone},
			"billing": lightspeed_address("Billing", node, "addressBilling", first, last, phone),
			"shipping": lightspeed_address("Shipping", node, "addressShipping", first, last, None),
			"lines": [line_fields(line) for line in lines.values()],
			"discount": sum((amount_of(line, "discountExcl") for line in products), Decimal(0)),
			"charges": [charge_fields(charge) for charge in charges(node)],
			"total": amount_of(node, "priceIncl"),
			"payment": {
				"method": text(node, "paymentTitle") or "Lightspeed",
				"reference": None,
				"date": node.time("createdAt"),
			}
			if submitted
			else None,
			"refunds": [self._refund(note, lines, currency) for note in node.children("credit_notes")]
			if submitted
			else [],
			"notes": self._notes(node),
			"shipments": self._shipments(node),
			"remarks": text(node, "comment"),
		}
		return Desired(self.kind, node.id(), fields)

	def _line(self, line: Node, currency: str) -> OrderLine:
		product = linked_id(line, "product")
		variant = linked_id(line, "variant")
		quantity = Decimal(line.integer("quantityOrdered"))
		name = text(line, "productTitle") or text(line, "sku") or "Product"
		variant_title = text(line, "variantTitle")
		if variant_title and variant_title not in (name, "Default"):
			name = f"{name} ({variant_title})"
		return order_line(
			self._context.gid("OrderProduct", line.integer("id")),
			self._context.gid("Product", product) if product is not None else None,
			self._context.gid("Variant", variant) if product is not None and variant is not None else None,
			name,
			quantity,
			amount_of(line, "basePriceExcl") * quantity,
			currency,
		)

	def _refund(self, note: Node, lines: dict[int, OrderLine], currency: str) -> Field:
		"""A credit note (an invoice crediting the order, with negative amounts)."""
		gid = self._context.gid("CreditNote", note.integer("id"))
		returned: list[OrderLine] = []
		for item in note.children("items"):
			sold = lines.get(item.optional_integer("orderProductId") or 0)
			quantity = abs(item.optional_integer("quantity") or 0)
			if text(item, "type") != CREDITED_PRODUCT or sold is None or quantity == 0:
				continue
			returned.append(
				order_line(
					sold.source_line,
					sold.product,
					sold.variant,
					sold.name,
					-Decimal(quantity),
					-abs(amount_of(item, "priceExcl")),
					currency,
				)
			)
		amount = abs(amount_of(note, "priceIncl"))
		balance = refund_balance(f"{gid}#adjustment", amount, returned, [], currency)
		return {
			"gid": gid,
			"date": note.optional_time("createdAt"),
			"reason": f"Lightspeed credit note {note.optional_string('number') or note.integer('id')}",
			"amount": amount,
			"lines": [line_fields(line) for line in [*returned, *([balance] if balance else [])]],
			"charges": [],
		}

	def _shipments(self, order: Node) -> list[Field]:
		"""Each shipment that wasn't cancelled: the order's shipping method and the shipment's tracking code."""
		return [
			shipment_fields(
				Shipment(
					gid=self._context.gid("Shipment", shipment.integer("id")),
					date=shipment.optional_time("createdAt") or order.time("createdAt"),
					carrier=None,
					service=text(order, "shipmentTitle"),
					tracking_number=text(shipment, "trackingCode"),
					tracking_url=None,
					delivered=False,
					contents=None,
				)
			)
			for shipment in order.children("shipments")
			if text(shipment, "status") != CANCELLED
		]

	def _notes(self, order: Node) -> list[Field]:
		created = order.time("createdAt")
		notes: list[Field] = []
		memo = text(order, "memo")
		if memo:
			notes.append(
				{
					"gid": self._context.gid("Memo", order.integer("source_id")),
					"date": created,
					"author": "Staff",
					"text": memo,
					"customer_note": False,
				}
			)
		return notes


def charges(order: Node) -> list[Charge]:
	"""Each tax the order charged (its tax total when Lightspeed itemises none), the shipping and the payment
	costs, tax-exclusive; zero amounts left out."""
	taxes = taxes_by_name(order, "taxRates")
	total_tax = amount_of(order, "priceIncl") - amount_of(order, "priceExcl")
	if not taxes and total_tax:
		taxes = {"Tax": total_tax}
	found = [Charge(TAX, name, amount) for name, amount in taxes.items()]
	found.append(
		Charge(SHIPPING, text(order, "shipmentTitle") or "Shipping", amount_of(order, "shipmentPriceExcl"))
	)
	found.append(
		Charge(FEE, text(order, "paymentTitle") or "Payment costs", amount_of(order, "paymentPriceExcl"))
	)
	return [charge for charge in found if charge.amount != 0]


class PageMapper:
	"""A text page → a Web Page; a hidden one is unpublished. Its old URL (``/service/<url>``) redirects."""

	kind = Kind.PAGE
	version = 1

	def map(self, node: Node) -> Desired | None:
		title, url = node.string("title"), node.string("url")
		fields: Fields = {
			"title": title,
			"slug": slug_of(url, title),
			"parent": None,
			"published": node.get("isVisible") is not False,
			"html": text(node, "content") or "",
			"images": [],
		}
		return Desired(self.kind, node.id(), fields, storefront(f"/service/{url.strip('/')}"))


class BlogMapper:
	"""A blog → a Blog Category named after it; its old URL (``/blogs/<url>``) redirects to it."""

	kind = Kind.BLOG
	version = 1

	def map(self, node: Node) -> Desired | None:
		title, url = node.string("title"), node.string("url")
		fields: Fields = {"title": title, "slug": slug_of(url, title), "description": None}
		return Desired(self.kind, node.id(), fields, storefront(f"/blogs/{url.strip('/')}"))


class ArticleMapper:
	"""A blog article → a Blog Post in its blog's category, by its author; its old URL
	(``/blogs/<blog url>/<url>.html``) redirects to it. An unpublished article is unpublished."""

	kind = Kind.ARTICLE
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		title, url = node.string("title"), node.string("url")
		blog = linked_id(node, "blog")
		if blog is None:
			raise SourceError(f"Lightspeed article {node.string('source_id')} has no blog.")
		author = text(node, "author")
		blog_url = text(node, "blog_url")
		published = node.optional_time("publishedAt") if isinstance(node.get("publishedAt"), str) else None
		fields: Fields = {
			"title": title,
			"slug": slug_of(url, title),
			"category": self._context.gid("Blog", blog),
			"author": {"key": author or "lightspeed", "name": author or "Lightspeed"},
			"published": node.get("isPublished") is True,
			"published_on": (published or node.time("createdAt"))[:10],
			"html": text(node, "content") or "",
			"intro": text(node, "summary"),
			"images": [],
			"tags": [],
		}
		paths = storefront(f"/blogs/{blog_url.strip('/')}/{url.strip('/')}{PAGE_SUFFIX}") if blog_url else ()
		return Desired(self.kind, node.id(), fields, paths)


class ReviewMapper:
	"""A review visible in the shop → an Item Review; a hidden one isn't mirrored."""

	kind = Kind.REVIEW
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if node.get("isVisible") is not True:
			return None
		product = linked_id(node, "product")
		if product is None:
			raise SourceError(f"Lightspeed review {node.string('source_id')} names no product.")
		fields: Fields = {
			"product": self._context.gid("Product", product),
			"author_name": text(node, "name") or "Customer",
			"author_email": None,
			"text": text(node, "content") or "",
			"rating": rating_fraction(node.integer("score"), MAX_SCORE),
			"published_on": node.time("createdAt")[:10],
		}
		return Desired(self.kind, node.id(), fields)


class RedirectMapper:
	"""A storefront redirect → a Website Route Redirect: a target on the shop's own domain (or a path) is an
	old storefront URL, which redirects on to its mirrored object; a target elsewhere keeps its URL."""

	kind = Kind.URL_REDIRECT
	version = 1

	def map(self, node: Node) -> Desired | None:
		source = path_of(node.string("url"))
		target = redirect_target(node.string("target"), text(node, "shop_domain"))
		if source is None or target is None:
			return None
		return Desired(self.kind, node.id(), {"source": source, "target": target, "status": 301})


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
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
		RedirectMapper(),
	]
