# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Squarespace → desired ERPNext state, one mapper per kind, in the shared field contract. Ported from
wp-woo-idempotent-importer's Squarespace mappers; Squarespace's money (MonetaryAmount objects whose value
is a JSON number, read exactly as spelled), weights, product types, discount templates and order states are
translated here. Identities arrive scoped to the connection, with Squarespace's id as ``source_id``."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from idempotent_importer.contract import (
	INCLUDED_TAX,
	PER_ITEM_OR_TOTAL,
	SHIPPING,
	SHIPPING_TAX,
	TAX,
	Address,
	Charge,
	Condition,
	Discount,
	FreeItem,
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
	refund_balance,
	shipment_fields,
	source_terms,
	variant_attributes,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import path_of, slugify
from idempotent_importer.core.source import ConnectionConfig

WEIGHT_UNITS = {"KILOGRAM": "kg", "POUND": "lbs"}
DIGITAL, SERVICE, GIFT_CARD = "DIGITAL", "SERVICE", "GIFT_CARD"
GIFT_CARD_LINE = "GIFT_CARD"
INCLUSIVE = "INCLUSIVE"
CANCELED = "CANCELED"
# Payment states whose order is paid in full (its refunds are credit notes); FAILED orders have no sale.
PAID = frozenset({"PAID", "REFUNDED", "REFUND_PENDING", "REFUND_FAILED"})
FAILED = "FAILED"
PROMO_CODE = "CODE"
EXPIRED = "EXPIRED"
BUY_X_GET_Y = "BUY_X_GET_Y"
FULL = Decimal(100)
# A discount that doesn't apply to subscription or payment-plan payments (the defaults).
NOT_RECURRING = frozenset({"EXCLUDED", "NONE"})
# The places of a tax rate derived from an unpaid order's tax total (ERPNext keeps a rate to nine).
TAX_RATE_PLACES = Decimal("0.000001")


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, type_: str, id_: str) -> str:
		return self.config.identity(type_, id_)

	def variant(self, product_id: str, variant_id: str | None) -> str:
		"""A variant's identity: its Squarespace variant id (what an order line names); a download has no
		variants, so its one variant is keyed by its product id."""
		return self.gid("ProductVariant", variant_id or product_id)


def money(node: Node | None, field: str) -> Decimal | None:
	"""A MonetaryAmount's ``value``, exactly as its JSON spelled it."""
	amount = node.child(field) if node else None
	return amount.optional_decimal("value") if amount else None


def money_or_zero(node: Node | None, field: str) -> Decimal:
	return money(node, field) or Decimal(0)


def currency_of(amount: Node) -> str | None:
	"""A MonetaryAmount's currency: Squarespace's docs send a code, its schema an object with a code."""
	currency = amount.child("currency") if isinstance(amount.get("currency"), dict) else None
	return currency.optional_string("currencyCode") if currency else amount.optional_string("currency")


def squarespace_address(address_type: str, address: Node | None, email: str | None) -> Fields | None:
	if address is None or not address.text("address1") or not address.text("city"):
		return None
	name = " ".join(p for p in (address.text("firstName"), address.text("lastName")) if p) or None
	return address_fields(
		Address(
			address_type=address_type,
			name=name,
			company=None,
			line1=address.string("address1"),
			line2=address.optional_string("address2"),
			city=address.string("city"),
			state=address.optional_string("state"),
			postcode=address.optional_string("postalCode"),
			country_code=address.optional_string("countryCode"),
			email=email,
			phone=address.optional_string("phone"),
		)
	)


def is_customer(profile: Node) -> bool:
	"""A profile is a customer when it has orders or an account on the site (not only a subscriber)."""
	return profile.get("isCustomer") is True or profile.get("hasAccount") is True


class StorePageMapper:
	"""A store page (the page products are listed on) → an Item Group; Squarespace gives it no description
	or image."""

	kind = Kind.COLLECTION
	version = 1

	def map(self, node: Node) -> Desired | None:
		name = node.string("title")
		slug = node.optional_string("urlSlug")
		fields: Fields = {
			"name": name,
			"slug": slug or slugify(name),
			"parent": None,
			"description": None,
			"image": None,
		}
		return Desired(self.kind, node.id(), fields, (f"/{slug}",) if slug else ())


class ProductMapper:
	"""A physical or service product → an Item: one (a single variant without attributes) or a template with
	a variant per variant, in the product's attribute order. A download has no variants: one Item priced at
	the product level. Services and downloads aren't stock items; unlimited stock is untracked. A variant's
	sale price counts while it is on sale. Hidden products are unpublished; gift cards are Unsupported."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		title = node.text("name")
		product_type = node.string("type")
		if product_type == GIFT_CARD:
			raise Unsupported("gift cards have no ERPNext home (coverage/squarespace.md)")
		warnings: list[str] = []
		names = node.strings("variantAttributes")
		if product_type == DIGITAL:
			variants = [self._variant(node, node, None, {})]
			simple = True
			warnings.append("a download: Squarespace's API doesn't give its file")
		else:
			sources = node.children("variants")
			if not sources:
				raise SourceError(f"Squarespace product {node.string('source_id')} has no variants.")
			options = [ordered_attributes(variant, names) for variant in sources]
			simple = len(sources) == 1 and not options[0]
			variants = [
				self._variant(node, variant, variant.string("id"), {} if simple else chosen)
				for variant, chosen in zip(sources, options, strict=True)
			]
			for variant in sources:
				measured = variant.child("shippingMeasurements")
				weight = measured.child("weight") if measured else None
				unit = weight.optional_string("unit") if weight else None
				if unit is not None and unit not in WEIGHT_UNITS:
					warnings.append(f"a variant weighed in {unit}, which has no ERPNext UOM")
		first = variants[0]
		fields: Fields = {
			"type": "simple" if simple else "variable",
			"name": title,
			"slug": node.optional_string("urlSlug") or slugify(title),
			"description": node.optional_string("description"),
			"short_description": None,
			"sku": first["sku"] if simple else None,
			"barcode": first["barcode"] if simple else None,
			"brand": None,
			"collections": [self._context.gid("StorePage", node.string("storePageId"))],
			"images": images(node),
			"published": node.get("isVisible") is not False,
			"is_stock_item": product_type not in (DIGITAL, SERVICE)
			and any(v["stock_qty"] is not None for v in variants),
			"weight": first["weight"] if simple else None,
			"price": first["price"] if simple else None,
			"sale": first["sale"] if simple else None,
			"stock_qty": first["stock_qty"] if simple else None,
			"attributes": [] if simple else variant_attributes(variants),
			"specifications": [],
			"variants": [] if simple else list[Field](variants),
			"tags": list[Field](node.strings("tags")),
		}
		url = path_of(node.optional_string("url"))
		return Desired(self.kind, node.id(), fields, (url,) if url else (), tuple(dict.fromkeys(warnings)))

	def _variant(
		self, product: Node, variant: Node, variant_id: str | None, chosen: dict[str, str]
	) -> Fields:
		pricing = variant.child("pricing")
		price = money(pricing, "basePrice")
		if pricing is None or price is None:
			raise SourceError(
				f"Squarespace product {product.string('source_id')} has a variant with no base price."
			)
		sale = money(pricing, "salePrice") if pricing.get("onSale") is True else None
		stock = variant.child("stock") if variant_id else None
		tracked = stock is not None and stock.get("unlimited") is not True
		measured = variant.child("shippingMeasurements")
		weight = measured.child("weight") if measured else None
		unit = WEIGHT_UNITS.get(weight.optional_string("unit") or "") if weight else None
		value = weight.optional_decimal("value") if weight else None
		return {
			"gid": self._context.variant(product.string("source_id"), variant_id),
			"sku": variant.optional_string("sku"),
			"barcode": variant.optional_string("gtin"),
			"attributes": dict[str, Field](chosen),
			"price": price,
			"sale": {"price": sale, "from": None, "to": None} if sale is not None and sale < price else None,
			"stock_qty": Decimal(max(0, stock.optional_integer("quantity") or 0))
			if stock is not None and tracked
			else None,
			"weight": {"value": value, "unit": unit} if unit and value else None,
			"image": image.optional_string("url")
			if variant_id and (image := variant.child("image"))
			else None,
			"enabled": True,
		}


def ordered_attributes(variant: Node, names: list[str]) -> dict[str, str]:
	"""A variant's attributes, name → value: in the product's attribute order, then any others."""
	raw = variant.get("attributes")
	if raw is None:
		return {}
	if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
		raise SourceError(f"{variant.path}.attributes is not an object of strings.")
	attributes = {name: value for name, value in raw.items() if isinstance(value, str)}
	ordered = {name: attributes[name] for name in names if name in attributes}
	return ordered | {name: value for name, value in attributes.items() if name not in ordered}


def images(product: Node) -> list[Field]:
	"""The product's images by their position (the first is the Item's image)."""
	ordered = sorted(product.children("images"), key=lambda image: image.optional_integer("orderIndex") or 0)
	return [
		{"url": url, "alt": image.optional_string("altText")}
		for image in ordered
		if (url := image.optional_string("url"))
	]


class ProfileMapper:
	"""A profile that is a customer (it has orders) or has an account → a Customer, its Contact and its
	address. A mailing-list subscriber or donor maps to nothing."""

	kind = Kind.CUSTOMER
	version = 1

	def map(self, node: Node) -> Desired | None:
		if not is_customer(node):
			return None
		first, last = node.text("firstName"), node.text("lastName")
		email = (node.optional_string("email") or "").lower() or None
		address_node = node.child("address")
		address = squarespace_address("Billing", address_node, email)
		fields: Fields = {
			"name": f"{first} {last}".strip() or email or node.string("source_id"),
			"type": "Individual",
			"first_name": first or None,
			"last_name": last or None,
			"company": None,
			"email": email,
			"phone": address_node.optional_string("phone") if address_node else None,
			"addresses": [address] if address else [],
		}
		return Desired(self.kind, node.id(), fields)


class DiscountMapper:
	"""Every discount, promo code or automatic → a Pricing Rule, enforcing natively what ERPNext's pricing does
	and recording the rest as conditions that hold it (``contract.CONDITIONS``): a percentage or amount off
	the order, for any order or above a minimum cart total; a percentage off one product; buy X get Y (the
	same product free, or a discount on another product) with its dates and site-wide use limit (of a code).
	Recorded and held: free shipping, once-per-customer limits and an automatic discount's use limit (card
	405); an amount off one product (Squarespace doesn't say per item or in total), how many reward items are
	discounted and how often per cart, several products to buy or reward, and discounts on subscription or
	payment-plan payments (card 407). An expired discount maps to nothing."""

	kind = Kind.DISCOUNT
	version = 3

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_string("status") == EXPIRED:
			return None
		name = node.string("name")
		trigger = node.required_child("trigger")
		automatic = trigger.string("type") != PROMO_CODE
		later: list[Condition] = []
		if node.get("isOncePerCustomer") is True:
			later.append(Condition("once_per_customer"))
		uses = node.optional_integer("maxUsesAllowed") if node.get("isLimitedUses") is True else None
		if uses is not None and automatic:
			later.append(Condition("shared_use_limit", {"uses": uses}))
		for options in ("subscriptionOptions", "paymentPlanOptions"):
			applies_to = node.child(options)
			if applies_to is not None and applies_to.string("type") not in NOT_RECURRING:
				later.append(Condition("item_matcher", {"source": {options: applies_to.string("type")}}))
		template = node.required_child("template")
		criteria = node.required_child("criteria")
		base = Discount(
			codes=[] if automatic else [trigger.string("promoCode")],
			description=name,
			discount="percentage",
			applies="transaction",
			value=Decimal(0),
			items=[],
			groups=[],
			min_amount=None,
			valid_from=date_of(node.optional_time("validFrom")),
			valid_upto=date_of(node.optional_time("validTo")),
			max_uses=None if automatic else uses,
			automatic=automatic,
			conditions=later,
		)
		match template.string("type"):
			case "PERCENTAGE":
				discount = replace(base, value=template.decimal("percentage"))
			case "FIXED_AMOUNT":
				amount = money(template, "discountAmount")
				if amount is None:
					raise SourceError(f'"{name}" has no discount amount.')
				discount = replace(base, discount="amount", value=amount)
			case "FREE_SHIPPING":
				shipping: Fields = {
					"methods": source_terms(template.get("selectedShippingOption")),
					"source": template.optional_string("appliesToFulfillmentOptionType"),
				}
				later.append(Condition("free_shipping", shipping))
				discount = base
			case "BUY_X_GET_Y_PERCENTAGE" | "BUY_X_GET_Y_FIXED_AMOUNT":
				return discount_desired(node.id(), self._buy_get(base, template, criteria, later))
			case other:
				raise SourceError(f"A Squarespace discount template {other}.")
		match criteria.string("type"):
			case "ANY_ORDER":
				pass
			case "CART_TOTAL":
				discount = replace(discount, min_amount=money(criteria, "minimumCartTotal"))
			case "PRODUCT":
				product = [self._context.gid("Product", criteria.string("productId"))]
				if discount.discount == "amount":
					later.append(
						Condition(
							"spread_amount",
							{"items": list[Field](product), "undocumented": [PER_ITEM_OR_TOTAL]},
						)
					)
				else:
					discount = replace(discount, applies="items", items=product)
			case other:
				raise SourceError(f'"{name}" has {other} criteria for a {template.string("type")} discount.')
		return discount_desired(node.id(), discount)

	def _buy_get(self, base: Discount, template: Node, criteria: Node, later: list[Condition]) -> Discount:
		"""Buy X (at full price) of the criteria products, get Y of the reward products discounted: the same
		single product free for every X is ERPNext's own recursive free item; another single product is a
		discount on that other item; how many are discounted, and how often per cart, are recorded."""
		if criteria.string("type") != BUY_X_GET_Y:
			raise SourceError(f'"{base.description}" is a buy X get Y discount without its criteria.')
		bought = [self._context.gid("Product", p) for p in criteria.strings("criteriaProductIds")]
		rewards = [self._context.gid("Product", p) for p in criteria.strings("rewardProductIds")]
		required = Decimal(criteria.integer("requiredFullPriceQuantity"))
		eligible = Decimal(criteria.integer("eligibleForDiscountQuantity"))
		percentage = template.optional_decimal("discountPercentage")
		repeats = template.optional_integer("maxApplicationsPerCart") or 0
		if percentage == FULL and len(bought) == 1 and bought == rewards and repeats == 0:
			free = FreeItem(None, None, eligible, recursive=True, recurse_for=required)
			return replace(base, applies="items", items=bought, min_qty=required, free=free)
		kind, value = (
			("percentage", percentage)
			if percentage is not None
			else ("amount", money(template, "discountAmount"))
		)
		if value is None:
			raise SourceError(f'"{base.description}" has no discount amount.')
		later.append(Condition("item_limit", {"qty": eligible}))
		if repeats:
			later.append(Condition("item_choice", {"source": {"maxApplicationsPerCart": repeats}}))
		if len(bought) != 1 or len(rewards) != 1:
			later.append(
				Condition(
					"item_matcher", {"source": {"buy": list[Field](bought), "reward": list[Field](rewards)}}
				)
			)
			return replace(base, discount=kind, value=value, applies="items", items=rewards, min_qty=required)
		discount = replace(base, discount=kind, value=value, applies="items", items=bought, min_qty=required)
		return discount if bought == rewards else replace(discount, other=("items", rewards[0]))


class OrderMapper:
	"""An order → an invoice with Squarespace's totals: lines at the unit price paid (before order
	discounts) on the variant's item, the discount total, each tax (from the order's transactions, summed
	by name; the tax total when there are none), each shipping line; payments' refunds as credit notes, and
	fulfillments as Shipments and internal notes as comments. Paid and refunded orders are submitted; unpaid ones are
	drafts. Test-mode, cancelled and failed orders map to nothing; a tax-inclusive order's taxes are included
	in its prices (``inclusive_charges``); gift card sales are Unsupported."""

	kind = Kind.ORDER
	version = 3

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		payment_state = node.optional_string("paymentState")
		if (
			node.get("testmode") is True
			or node.optional_string("fulfillmentStatus") == CANCELED
			or payment_state == FAILED
		):
			return None
		grand_total = node.required_child("grandTotal")
		currency = currency_of(grand_total)
		if currency is None:
			raise SourceError(f"Squarespace order {node.string('source_id')} has no currency.")
		submitted = payment_state in PAID
		documents = [d for d in node.children("transactions") if d.get("voided") is not True]
		payments = [payment for document in documents for payment in document.children("payments")]
		email = (node.optional_string("customerEmail") or "").lower() or None
		billing = node.child("billingAddress")
		profile = node.child("customer_profile")
		customer = (
			self._context.gid("Profile", profile.string("id")) if profile and is_customer(profile) else None
		)
		paid_on = sorted(t for p in payments if (t := p.optional_time("paidOn")))
		lines = [self._line(line, currency) for line in node.children("lineItems")]
		inclusive = node.optional_string("priceTaxInterpretation") == INCLUSIVE
		if inclusive:
			lines, found = inclusive_charges(node, documents, lines)
		else:
			found = charges(node, documents)
		fields: Fields = {
			"number": f"#{node.optional_string('orderNumber') or node.string('source_id')}",
			"currency": currency,
			"created_at": node.time("createdOn"),
			"submitted": submitted,
			"customer": customer,
			"guest": None
			if customer
			else {
				"name": " ".join(p for p in (billing.text("firstName"), billing.text("lastName")) if p)
				or None
				if billing
				else None,
				"email": email,
				"phone": billing.optional_string("phone") if billing else None,
			},
			"billing": squarespace_address("Billing", billing, email),
			"shipping": squarespace_address("Shipping", node.child("shippingAddress"), None),
			"lines": [line_fields(line) for line in lines],
			"discount": money_or_zero(node, "discountTotal"),
			"charges": [charge_fields(charge) for charge in found],
			"total": money_or_zero(node, "grandTotal"),
			"payment": {
				"method": payment_method(payments[0]) if payments else "Squarespace",
				"reference": payments[0].optional_string("externalTransactionId") if payments else None,
				"date": paid_on[0] if paid_on else node.time("createdOn"),
			}
			if submitted
			else None,
			"refunds": [self._refund(refund, currency) for p in payments for refund in p.children("refunds")]
			if submitted
			else [],
			"notes": self._notes(node),
			"shipments": self._shipments(node),
			"remarks": None,
		}
		if inclusive:
			fields["taxes_included"] = True
		return Desired(self.kind, node.id(), fields)

	def _line(self, line: Node, currency: str) -> OrderLine:
		if line.optional_string("lineItemType") == GIFT_CARD_LINE:
			raise Unsupported("a gift card sale has no ERPNext item (coverage/squarespace.md)")
		product = line.optional_string("productId")
		quantity = Decimal(line.integer("quantity"))
		choices = [*line.children("variantOptions"), *line.children("customizations")]
		chosen = ", ".join(
			f"{label}: {value}"
			for choice in choices
			if (label := choice.optional_string("optionName") or choice.optional_string("label"))
			and (value := choice.optional_string("value"))
		)
		name = line.text("productName")
		return order_line(
			self._context.gid("OrderLineItem", line.string("id")),
			self._context.gid("Product", product) if product else None,
			self._context.variant(product, line.optional_string("variantId")) if product else None,
			f"{name} ({chosen})" if chosen else name,
			quantity,
			money_or_zero(line, "unitPricePaid") * quantity,
			currency,
		)

	def _refund(self, refund: Node, currency: str) -> Field:
		gid = self._context.gid("Refund", refund.string("id"))
		amount = money_or_zero(refund, "amount")
		balance = refund_balance(f"{gid}#adjustment", amount, [], [], currency)
		return {
			"gid": gid,
			"date": refund.optional_time("refundedOn"),
			"reason": refund.optional_string("reason"),
			"amount": amount,
			"lines": [line_fields(balance)] if balance else [],
			"charges": [],
		}

	def _shipments(self, order: Node) -> list[Field]:
		"""Each fulfillment (a shipment): its carrier, service, tracking number and link. A fulfillment has no
		id, so each is keyed by its position (Squarespace only ever adds them)."""
		order_id = order.string("source_id")
		return [
			shipment_fields(
				Shipment(
					gid=self._context.gid("Fulfillment", f"{order_id}-{index}"),
					date=fulfillment.optional_time("shipDate") or order.time("createdOn"),
					carrier=fulfillment.optional_string("carrierName"),
					service=fulfillment.optional_string("service"),
					tracking_number=fulfillment.optional_string("trackingNumber"),
					tracking_url=fulfillment.optional_string("trackingUrl"),
					delivered=False,
					contents=None,
				)
			)
			for index, fulfillment in enumerate(order.children("fulfillments"))
		]

	def _notes(self, order: Node) -> list[Field]:
		"""Each internal note; it has no id, so each is keyed by its position (Squarespace only ever adds
		them)."""
		created = order.time("createdOn")
		order_id = order.string("source_id")
		notes: list[Field] = []
		for index, note in enumerate(order.children("internalNotes")):
			content = note.optional_string("content")
			if content:
				notes.append(
					{
						"gid": self._context.gid("InternalNote", f"{order_id}-{index}"),
						"date": created,
						"author": "Staff",
						"text": content,
						"customer_note": False,
					}
				)
		return notes


def payment_method(payment: Node) -> str:
	"""The payment's gateway and card ("STRIPE (VISA)")."""
	provider = payment.optional_string("provider") or "Squarespace"
	card = payment.optional_string("creditCardType")
	return f"{provider} ({card})" if card else provider


def charges(order: Node, documents: list[Node]) -> list[Charge]:
	"""Each tax summed by name over the transactions' sale and shipping items (the order's tax total when it
	has no transactions), then each shipping line (one for the shipping total when there are none); zero
	amounts left out."""
	taxes: dict[str, Decimal] = {}
	for document in documents:
		for item in [*document.children("salesLineItems"), *document.children("shippingLineItems")]:
			for tax in item.children("taxes"):
				title = tax.optional_string("name") or "Tax"
				taxes[title] = taxes.get(title, Decimal(0)) + money_or_zero(tax, "amount")
	if not taxes and money_or_zero(order, "taxTotal"):
		taxes["Tax"] = money_or_zero(order, "taxTotal")
	found = [Charge(TAX, title, amount) for title, amount in taxes.items()]
	return [charge for charge in [*found, *shipping_charges(order)] if charge.amount != 0]


def shipping_charges(order: Node) -> list[Charge]:
	"""Each shipping line (one for the shipping total when there are none)."""
	option = order.optional_string("shippingOptionName")
	shipping = [
		Charge(
			SHIPPING, line.optional_string("method") or option or "Shipping", money_or_zero(line, "amount")
		)
		for line in order.children("shippingLines")
	]
	return shipping or [Charge(SHIPPING, option or "Shipping", money_or_zero(order, "shippingTotal"))]


def paired[T](wanted: list[Decimal], items: list[tuple[Decimal, T]]) -> list[T | None]:
	"""Each wanted amount's item: the first unpaired item of that amount, then (for any left over) the
	unpaired items in their order. Squarespace's transaction items don't name the order line or shipping
	line they are for; their amounts and order do."""
	free = list(range(len(items)))
	chosen: list[int | None] = []
	for amount in wanted:
		match = next((i for i in free if items[i][0] == amount), None)
		if match is not None:
			free.remove(match)
		chosen.append(match)
	result: list[T | None] = []
	for match in chosen:
		taken = free.pop(0) if match is None and free else match
		result.append(items[taken][1] if taken is not None else None)
	return result


def inclusive_charges(
	order: Node, documents: list[Node], lines: list[OrderLine]
) -> tuple[list[OrderLine], list[Charge]]:
	"""A tax-inclusive order's taxes (``priceTaxInterpretation`` INCLUSIVE: the unit prices paid include
	them): each tax on the sale items an included tax, at the rates of each line's own sale item (paired
	by amount); each shipping line at its price less its taxes, then each of those taxes at its rate. An
	order with no transactions (not yet paid) has only its tax total, included at the one rate that comes
	to it, which ERPNext then checks exactly."""
	sales = [item for document in documents for item in document.children("salesLineItems")]
	shipped = [item for document in documents for item in document.children("shippingLineItems")]
	amounts: dict[str, Decimal] = {}
	for item in sales:
		for tax in item.children("taxes"):
			title = tax.optional_string("name") or "Tax"
			amounts[title] = amounts.get(title, Decimal(0)) + money_or_zero(tax, "amount")
	found: list[Charge] = []
	shipping_lines = shipping_charges(order)
	shipping_taxes = paired(
		[charge.amount for charge in shipping_lines], [(money_or_zero(i, "amount"), i) for i in shipped]
	)
	for charge, shipped_item in zip(shipping_lines, shipping_taxes, strict=True):
		taxes = shipped_item.children("taxes") if shipped_item is not None else []
		net = charge.amount - sum((money_or_zero(t, "amount") for t in taxes), Decimal(0))
		if net == 0:
			continue
		found.append(Charge(SHIPPING, charge.description, net))
		found += [
			Charge(
				SHIPPING_TAX,
				f"{tax.optional_string('name') or 'Tax'} (shipping)",
				money_or_zero(tax, "amount"),
				tax.decimal("rate"),
			)
			for tax in taxes
			if money_or_zero(tax, "amount")
		]
	if not sales:
		total = money_or_zero(order, "taxTotal") - sum(
			(c.amount for c in found if c.kind == SHIPPING_TAX), Decimal(0)
		)
		if not total:
			return lines, found
		gross = sum((line.qty * line.rate for line in lines), Decimal(0)) - money_or_zero(
			order, "discountTotal"
		)
		rate = (total * 100 / (gross - total)).quantize(TAX_RATE_PLACES)
		return lines, [Charge(INCLUDED_TAX, "Tax", total, rate), *found]
	items = paired(
		[line.qty * line.rate for line in lines], [(money_or_zero(i, "totalSales"), i) for i in sales]
	)
	rates: list[LineRates] = []
	for sale in items:
		line_rates: LineRates = {}
		for tax in sale.children("taxes") if sale is not None else []:
			title = tax.optional_string("name") or "Tax"
			line_rates[title] = line_rates.get(title, Decimal(0)) + tax.decimal("rate")
		rates.append(line_rates)
	lines, included = included_taxes(lines, rates, amounts)
	return lines, [*included, *found]


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		StorePageMapper(),
		ProductMapper(context),
		ProfileMapper(),
		DiscountMapper(context),
		OrderMapper(context),
	]
