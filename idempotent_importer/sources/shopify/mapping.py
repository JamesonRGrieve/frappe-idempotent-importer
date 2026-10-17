# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Shopify → desired ERPNext state, one mapper per kind, in the shared field contract
(``idempotent_importer.contract``). Shopify's enums and units (statuses, weight units, discount types)
are translated here; writers never see a Shopify value. Identities arrive already scoped to the
connection (see ShopifyReader)."""

from __future__ import annotations

import re
from dataclasses import replace
from decimal import Decimal

from idempotent_importer.contract import (
	COUPON_LIMITS,
	DISCOUNT_CAPS,
	FEE,
	SHIPPING,
	SHIPPING_TAX,
	TAX,
	Address,
	Charge,
	Discount,
	LineRates,
	OrderLine,
	Shipment,
	address_fields,
	charge_fields,
	date_of,
	discount_desired,
	included_taxes,
	line_fields,
	order_line,
	planned,
	refund_balance,
	shipment_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import path_of, redirect_target
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.html import absolute, embedded_images

ACTIVE = "ACTIVE"
# Where Shopify serves a store's media: its CDN, the store's myshopify domain, or /cdn/shop on its own.
SHOPIFY_MEDIA = re.compile(r"^https?://(?:cdn\.shopify\.com|[^/]+\.myshopify\.com|[^/]+/cdn/shop)/", re.I)
COMPLETED = "COMPLETED"
RATE_DEFINITION = "DeliveryRateDefinition"
DEFAULT_OPTION = "Title"
WEIGHT_UNITS = {"GRAMS": "g", "KILOGRAMS": "kg", "OUNCES": "oz", "POUNDS": "lbs"}
# Financial statuses of an order whose payment Shopify has taken.
PAID = frozenset({"PAID", "PARTIALLY_REFUNDED", "REFUNDED"})
# Awaiting payment: a draft invoice, submitted once paid.
UNPAID = frozenset({"PENDING", "AUTHORIZED", "PARTIALLY_PAID", "EXPIRED"})
PAYMENT_KINDS = frozenset({"SALE", "CAPTURE"})
SUCCESS = "SUCCESS"
COMMENT_EVENT = "CommentEvent"
AUTOMATIC = frozenset({"DiscountAutomaticBasic", "DiscountAutomaticFreeShipping"})
BASIC = frozenset({"DiscountCodeBasic", "DiscountAutomaticBasic"})
FREE_SHIPPING = frozenset({"DiscountCodeFreeShipping", "DiscountAutomaticFreeShipping"})
MEDIA_IMAGE = "MediaImage"


def money(node: Node | None, field: str) -> Decimal:
	"""A MoneyBag's shop-currency amount (zero when the bag is absent)."""
	bag = node.child(field) if node else None
	shop = bag.child("shopMoney") if bag else None
	return shop.decimal("amount") if shop else Decimal(0)


def shopify_hosted(url: str) -> bool:
	return SHOPIFY_MEDIA.match(url) is not None


def media_url(url: str) -> str:
	"""A Shopify media URL as one file: absolute, without its cache-busting query."""
	return absolute(url).split("?", 1)[0]


def html_images(fragment: str) -> list[Field]:
	"""The Shopify-hosted images a rich-text field embeds."""
	return list(embedded_images(fragment, shopify_hosted))


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


def sale_line(line: Node, currency: str) -> OrderLine:
	"""An order's or draft order's line at its price before discounts."""
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


type TaxKey = tuple[str, Decimal]


def tax_lines(node: Node) -> dict[TaxKey, Decimal]:
	"""A node's tax lines by (title, rate as a fraction), summed."""
	taxes: dict[TaxKey, Decimal] = {}
	for tax in node.children("taxLines"):
		key = (tax.string("title"), tax.decimal("rate"))
		taxes[key] = taxes.get(key, Decimal(0)) + money(tax, "priceSet")
	return taxes


def sale_charges(
	node: Node, lines: list[Node], shipping: list[Node], fees: list[Charge]
) -> tuple[list[OrderLine], list[Charge]]:
	"""The order's lines, then its taxes, shipping and fees. When the line prices include tax
	(``taxesIncluded``), each tax on the lines is an included tax, which ERPNext must compute to Shopify's
	amount, at each line's own rates when the lines are taxed differently; shipping is its price less its
	own taxes, each followed by those taxes at their rates."""
	currency = node.string("currencyCode")
	sold = [sale_line(line, currency) for line in lines]
	if not node.flag("taxesIncluded"):
		charges = [Charge(TAX, title, amount) for (title, _), amount in tax_lines(node).items()]
		charges += [Charge(SHIPPING, s.string("title"), money(s, "discountedPriceSet")) for s in shipping]
		return sold, [charge for charge in [*charges, *fees] if charge.amount != 0]
	on_lines: dict[str, Decimal] = {}
	for (title, _), amount in tax_lines(node).items():
		on_lines[title] = on_lines.get(title, Decimal(0)) + amount
	shipping_charges: list[Charge] = []
	for line in shipping:
		taxes = tax_lines(line)
		net = money(line, "discountedPriceSet") - sum(taxes.values(), Decimal(0))
		if net == 0:
			continue
		shipping_charges.append(Charge(SHIPPING, line.string("title"), net))
		for (title, rate), amount in taxes.items():
			on_lines[title] = on_lines.get(title, Decimal(0)) - amount
			shipping_charges.append(Charge(SHIPPING_TAX, f"{title} (shipping)", amount, rate * 100))
	rates: list[LineRates] = []
	for line in lines:
		line_rates: LineRates = {}
		for title, rate in tax_lines(line):
			line_rates[title] = line_rates.get(title, Decimal(0)) + rate * 100
		rates.append(line_rates)
	sold, included = included_taxes(sold, rates, on_lines)
	return sold, [charge for charge in [*included, *shipping_charges, *fees] if charge.amount != 0]


def sale_fields(node: Node, lines: list[Node], shipping: list[Node], fees: list[Charge]) -> Fields:
	"""What an order and a draft order share: its customer (or guest), addresses, lines, discount,
	charges and total."""
	currency = node.string("currencyCode")
	customer = node.child("customer")
	sold, charges = sale_charges(node, lines, shipping, fees)
	fields: Fields = {
		"number": node.string("name"),
		"currency": currency,
		"created_at": node.time("createdAt"),
		"customer": customer.id() if customer else None,
		"guest": None
		if customer
		else {"name": None, "email": node.optional_string("email"), "phone": node.optional_string("phone")},
		"billing": address_fields(a)
		if (a := shopify_address("Billing", node.child("billingAddress")))
		else None,
		"shipping": address_fields(a)
		if (a := shopify_address("Shipping", node.child("shippingAddress")))
		else None,
		"lines": [line_fields(line) for line in sold],
		"discount": money(node, "totalDiscountsSet"),
		"charges": [charge_fields(c) for c in charges],
		"total": money(node, "totalPriceSet"),
	}
	if node.flag("taxesIncluded"):
		fields["taxes_included"] = True
	return fields


class AssetMapper:
	"""A file from Shopify's Files section (an image, a video, a PDF, …) as a File."""

	kind = Kind.ASSET
	version = 1

	def map(self, node: Node) -> Desired | None:
		image = node.child("image")
		source = node.child("originalSource")
		url = (
			(image.optional_string("url") if image else None)
			or (source.optional_string("url") if source else None)
			or node.optional_string("url")
		)
		if url is None:
			raise Unsupported("the file has no downloadable URL yet (still processing in Shopify)")
		return Desired(self.kind, node.id(), {"url": media_url(url), "alt": node.optional_string("alt")})


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
	version = 2

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
			"tags": list(node.strings("tags")),
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
	"""Code and automatic discounts: a basic one's percentage or amount off everything, some products,
	single variants or some collections, after a minimum spend or quantity, for every customer or some;
	a free-shipping one's code. What ERPNext's pricing doesn't enforce is planned companion work (a
	once-per-customer limit and free shipping: coupon limits, card 405; customer segments and an amount
	spread across some items: card 407) and the rest is written. Buy X Get Y and app discounts are
	Unsupported: their fields aren't read (the query's cost is recorded from a live store, see
	coverage/shopify.md)."""

	kind = Kind.DISCOUNT
	version = 2

	def map(self, node: Node) -> Desired | None:
		discount = node.required_child("discount")
		typename = discount.typename()
		if typename not in BASIC | FREE_SHIPPING:
			raise Unsupported(f"{typename} isn't read yet (an operator decision: coverage/shopify.md)")
		if discount.string("status") != ACTIVE:
			return None
		automatic = typename in AUTOMATIC
		later: list[str] = []
		if discount.flag("appliesOncePerCustomer"):
			later.append(planned("its once-per-customer limit", COUPON_LIMITS))
		customers = self._customers(discount.child("context"), automatic, later)
		minimum = discount.child("minimumRequirement")
		min_amount = min_qty = None
		if minimum is not None:
			if minimum.typename() == "DiscountMinimumSubtotal":
				min_amount = minimum.required_child("greaterThanOrEqualToSubtotal").decimal("amount")
			else:
				min_qty = minimum.decimal("greaterThanOrEqualToQuantity")
		base = Discount(
			codes=[]
			if automatic
			else [c.string("code") for c in discount.required_child("codes").children("nodes")],
			description=discount.optional_string("title"),
			discount="percentage",
			applies="transaction",
			value=Decimal(0),
			items=[],
			groups=[],
			min_amount=min_amount,
			min_qty=min_qty,
			mixed=min_qty is not None,
			valid_from=date_of(discount.optional_time("startsAt")),
			valid_upto=date_of(discount.optional_time("endsAt")),
			max_uses=None if automatic else discount.optional_integer("usageLimit"),
			automatic=automatic,
			customers=customers,
			planned=later,
		)
		if typename in FREE_SHIPPING:
			later.append(planned("its free shipping", COUPON_LIMITS))
			return discount_desired(node.id(), base)
		gets = discount.required_child("customerGets")
		value = gets.required_child("value")
		items = gets.required_child("items")
		products: list[str] = []
		variants: list[tuple[str | None, str]] = []
		groups: list[str] = []
		match items.typename():
			case "AllDiscountItems":
				applies = "all_items"
			case "DiscountProducts":
				products = [p.id() for p in items.required_child("products").children("nodes")]
				if items.child("productVariants") is not None:
					variants = [
						(None, v.id()) for v in items.required_child("productVariants").children("nodes")
					]
				applies = "items"
			case "DiscountCollections":
				groups = [c.id() for c in items.required_child("collections").children("nodes")]
				applies = "groups"
			case other:
				raise SourceError(f"Discounted items of type {other}.")
		match value.typename():
			case "DiscountPercentage":
				kind, amount = "percentage", value.decimal("percentage") * 100
				if applies == "all_items":
					applies = "transaction"
			case "DiscountAmount":
				kind, amount = "amount", value.required_child("amount").decimal("amount")
				if not value.flag("appliesOnEachItem"):
					if applies != "all_items":
						later.append(planned("its amount spread across some items", DISCOUNT_CAPS))
					applies, products, variants, groups = "transaction", [], [], []
			case other:
				raise SourceError(f"A discount value of type {other}.")
		rule = replace(
			base,
			discount=kind,
			applies=applies,
			value=amount,
			items=products,
			groups=groups,
			variants=variants,
		)
		return discount_desired(node.id(), rule)

	@staticmethod
	def _customers(context: Node | None, automatic: bool, later: list[str]) -> list[str]:
		"""The customers a discount is for: every one (none listed), or some. A code applies one Pricing
		Rule, so a code for several customers, like one for customer segments, is planned (card 407)."""
		if context is None or context.typename() == "DiscountBuyerSelectionAll":
			return []
		if context.typename() != "DiscountCustomers":
			later.append(planned("its customer segments", DISCOUNT_CAPS))
			return []
		customers = [c.id() for c in context.children("customers")]
		if len(customers) > 1 and not automatic:
			later.append(planned("its several customers", DISCOUNT_CAPS))
			return []
		return customers


class OrderMapper:
	kind = Kind.ORDER
	version = 3

	def map(self, node: Node) -> Desired | None:
		status = node.string("displayFinancialStatus")
		if node.has("cancelledAt") or status not in PAID | UNPAID:
			return None
		currency = node.string("currencyCode")
		submitted = status in PAID
		lines = {line.id(): line for line in node.children("lineItems")}
		fees = [Charge(FEE, f.string("name"), money(f, "price")) for f in node.children("additionalFees")]
		# Duties are collected for customs, as a tax is for its authority: a tax charge.
		duties = money(node, "originalTotalDutiesSet")
		if duties:
			fees.insert(0, Charge(TAX, "Duties", duties))
		tip = money(node, "totalTipReceivedSet")
		if tip:
			fees.append(Charge(FEE, "Tip", tip))
		included = node.flag("taxesIncluded")
		fields: Fields = {
			**sale_fields(node, list(lines.values()), node.children("shippingLines"), fees),
			"submitted": submitted,
			"payment": self._payment(node) if submitted else None,
			"refunds": [self._refund(r, lines, currency, included) for r in node.children("refunds")]
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
			"shipments": [shipment_fields(s) for s in shipments(node)],
		}
		return Desired(self.kind, node.id(), fields)

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

	def _refund(self, refund: Node, lines: dict[str, Node], currency: str, included: bool) -> Field:
		"""One refund as a credit note. In an order whose prices include tax the returned lines carry
		their tax, so no tax row is added (any difference is the adjustment line)."""
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
		charges = [Charge(TAX, "Tax refunded", -taxes)] if taxes and not included else []
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


# Fulfillments that shipped (not cancelled, failed or in error).
SHIPPED = frozenset({"SUCCESS", "OPEN", "PENDING"})


def shipments(order: Node) -> list[Shipment]:
	"""Each fulfillment that shipped: its first tracking company, its tracking numbers and link."""
	found: list[Shipment] = []
	for fulfillment in order.children("fulfillments"):
		if fulfillment.string("status") not in SHIPPED:
			continue
		tracking = fulfillment.children("trackingInfo")
		found.append(
			Shipment(
				gid=fulfillment.id(),
				date=fulfillment.time("createdAt"),
				carrier=next((c for t in tracking if (c := t.optional_string("company"))), None),
				service=None,
				tracking_number=", ".join(n for t in tracking if (n := t.optional_string("number"))) or None,
				tracking_url=next((u for t in tracking if (u := t.optional_string("url"))), None),
				delivered=fulfillment.optional_time("deliveredAt") is not None,
				contents=None,
			)
		)
	return found


class DraftOrderMapper:
	"""An open draft order (a proposal, an invoice sent) as a draft Quotation. A completed draft became an
	order, mirrored as such, so the draft's copy is retired."""

	kind = Kind.DRAFT_ORDER
	version = 2

	def map(self, node: Node) -> Desired | None:
		if node.string("status") == COMPLETED:
			return None
		shipping = node.child("shippingLine")
		fields: Fields = {
			**sale_fields(node, node.children("lineItems"), [shipping] if shipping else [], []),
			"remarks": node.optional_string("note2"),
		}
		return Desired(self.kind, node.id(), fields)


class DeliveryProfileMapper:
	"""The store's general delivery profile as Shipping Rules: each flat rate of a zone made of whole
	countries is a fixed Shipping Rule for them. ERPNext's rules apply by country, charge outside a price or
	weight band as if free, and have no carrier rates, so a zone limited to provinces or "rest of world", a
	rate with conditions and a carrier-calculated rate are gaps (warnings), never approximated."""

	kind = Kind.DELIVERY_PROFILE
	version = 1

	def map(self, node: Node) -> Desired | None:
		name = node.string("name")
		if not node.flag("default"):
			raise Unsupported(
				f'the delivery profile "{name}" applies to some products only; Shipping Rules are store-wide'
			)
		rules: list[Field] = []
		warnings: list[str] = []
		for group in node.children("profileLocationGroups"):
			for group_zone in group.children("locationGroupZones"):
				zone = group_zone.required_child("zone")
				zone_name = zone.string("name")
				countries, gap = self._countries(zone)
				for method in group_zone.children("methodDefinitions"):
					reason = gap or self._gap(method)
					if reason:
						warnings.append(
							f'shipping "{method.string("name")}" to {zone_name} not mirrored: {reason}'
						)
						continue
					rules.append(
						{
							"gid": method.id(),
							"label": f"{method.string('name')} ({zone_name})",
							"countries": list[Field](countries),
							"amount": method.required_child("rateProvider")
							.required_child("price")
							.decimal("amount"),
							"enabled": method.flag("active"),
						}
					)
		return Desired(self.kind, node.id(), {"name": name, "rules": rules}, warnings=tuple(warnings))

	@staticmethod
	def _countries(zone: Node) -> tuple[list[str], str | None]:
		codes: list[str] = []
		for country in zone.children("countries"):
			code = country.required_child("code")
			if code.flag("restOfWorld"):
				return [], "a rest-of-world zone has no ERPNext equivalent"
			if country.children("provinces"):
				return [], "ERPNext's Shipping Rules apply to whole countries, not provinces"
			codes.append(code.string("countryCode"))
		return codes, None

	@staticmethod
	def _gap(method: Node) -> str | None:
		if method.required_child("rateProvider").typename() != RATE_DEFINITION:
			return "carrier-calculated rates have no ERPNext equivalent"
		if method.children("methodConditions"):
			return "ERPNext charges nothing outside a rule's price or weight band instead of not offering it"
		return None


class PageMapper:
	kind = Kind.PAGE
	version = 2

	def map(self, node: Node) -> Desired | None:
		fields: Fields = {
			"title": node.string("title"),
			"slug": node.string("handle"),
			"parent": None,
			"published": node.flag("isPublished"),
			"html": node.text("body"),
			"images": html_images(node.text("body")),
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
			"images": html_images(node.text("body")),
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
	version = 2

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
			"images": html_images(node.text("body")),
			"tags": list(node.strings("tags")),
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
		AssetMapper(),
		CollectionMapper(),
		ProductMapper(),
		CustomerMapper(),
		DiscountMapper(),
		OrderMapper(),
		DraftOrderMapper(),
		PageMapper(),
		PolicyMapper(),
		BlogMapper(),
		ArticleMapper(),
		CommentMapper(),
		UrlRedirectMapper(config.store_url.strip().lower() or None),
		DeliveryProfileMapper(),
	]
