# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Square Online → desired ERPNext state, one mapper per kind, in the shared field contract. Ported from
wp-woo-idempotent-importer's Square Online mappers (Fields, CategoryMapper, ItemMapper, CustomerMapper,
OrderMapper); Square's money (minor units), quantities, catalog presence and order states are translated
here. Identities arrive scoped to the connection, with each object's Square id as ``square_id``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from idempotent_importer.contract import (
	FEE,
	SHIPPING,
	TAX,
	Address,
	Charge,
	OrderLine,
	address_fields,
	charge_fields,
	line_fields,
	order_line,
	refund_balance,
	variant_attributes,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_of, from_minor_units
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import path_of, slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.common import ContentPageMapper, plain_html
from idempotent_importer.sources.square_online.reader import STOCK_STATE

UNKNOWN_COUNTRY = "ZZ"
# Product types sold as ordinary items; gift cards, appointments, events, donations and legacy Square Online
# services and memberships have no ERPNext equivalent.
PRODUCT_TYPES = frozenset({"REGULAR", "FOOD_AND_BEV", "DIGITAL"})
DEFAULT_PRODUCT_TYPE = "REGULAR"
DIGITAL = "DIGITAL"
VARIABLE_PRICING = "VARIABLE_PRICING"
# The attribute that tells variations apart when an item has no item options.
VARIATION_ATTRIBUTE = "Variation"
APPORTIONED = "APPORTIONED_TREATMENT"
SHIPPED_FULFILLMENTS = frozenset({"SHIPMENT", "DELIVERY"})
SHIPPING_CHARGE = re.compile(r"\b(shipping|delivery)\b", re.IGNORECASE)
# Each fulfillment state → the timestamps Square records it with (shipments, pickups, deliveries).
STATE_TIMES = {
	"PROPOSED": ("placed_at",),
	"RESERVED": ("in_progress_at", "accepted_at"),
	"PREPARED": ("packaged_at", "ready_at", "prepared_at"),
	"COMPLETED": ("shipped_at", "picked_up_at", "delivered_at", "completed_at"),
	"CANCELED": ("canceled_at",),
	"FAILED": ("failed_at", "rejected_at", "expired_at"),
}
DETAILS = {
	"SHIPMENT": "shipment_details",
	"PICKUP": "pickup_details",
	"DELIVERY": "delivery_details",
	"IN_STORE": "in_store_details",
}
APPROVED = "APPROVED"


def money(node: Node | None, field: str) -> Decimal | None:
	"""A Money object (``amount`` in the currency's smallest unit, ``currency``) as an exact Decimal."""
	value = node.child(field) if node else None
	amount = value.optional_integer("amount") if value else None
	if value is None or amount is None:
		return None
	try:
		return from_minor_units(amount, value.string("currency"))
	except ValueError as e:
		raise Unsupported(f"{field}: {e}") from e


def money_or_zero(node: Node | None, field: str) -> Decimal:
	return money(node, field) or Decimal(0)


def whole(quantity: str, what: str) -> Decimal:
	"""A quantity (Square sends decimal strings); a fractional one (sold by a measurement unit) is
	Unsupported, as ERPNext's mirrored items count whole units."""
	value = decimal_of(quantity)
	if value != value.to_integral_value():
		raise Unsupported(f"{what} has the fractional quantity {quantity}")
	return value.to_integral_value()


def is_deleted(node: Node) -> bool:
	return node.get("is_deleted") is True


def square_address(
	address_type: str, address: Node | None, name: str | None, company: str | None, phone: str | None
) -> Fields | None:
	"""A Square Address (lines 2 and 3 share the second line); the name, company and phone come from its
	owner where Square keeps them beside it."""
	if address is None or not address.text("address_line_1") or not address.text("locality"):
		return None
	country = address.optional_string("country")
	extra = [
		line
		for line in (address.optional_string("address_line_2"), address.optional_string("address_line_3"))
		if line
	]
	person = " ".join(p for p in (address.text("first_name"), address.text("last_name")) if p) or name
	return address_fields(
		Address(
			address_type=address_type,
			name=person,
			company=company,
			line1=address.string("address_line_1"),
			line2=", ".join(extra) or None,
			city=address.string("locality"),
			state=address.optional_string("administrative_district_level_1"),
			postcode=address.optional_string("postal_code"),
			country_code=None if country == UNKNOWN_COUNTRY else country,
			email=None,
			phone=phone,
		)
	)


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, type_: str, id_: str) -> str:
		return self.config.identity(type_, id_)


def images(owner: Node, image_ids: list[str]) -> list[Field]:
	"""The images an object references, in its order, from the image objects the reader attached; an image
	Square no longer has, or one without a URL, is left out."""
	found: dict[str, Field] = {}
	for image in owner.children("image_objects"):
		data = image.child("image_data")
		url = data.optional_string("url") if data else None
		if data is not None and url is not None:
			found[image.string("id")] = {"url": url, "alt": data.optional_string("caption")}
	return [found[image_id] for image_id in image_ids if image_id in found]


class CategoryMapper:
	kind = Kind.COLLECTION
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if is_deleted(node):
			return None
		category = node.required_child("category_data")
		name = category.optional_string("name")
		if name is None:
			raise Unsupported("a Square category without a name has no Item Group")
		seo = category.child("ecom_seo_data")
		parent = category.child("parent_category")
		parent_id = parent.optional_string("id") if parent else None
		first = (images(node, category.strings("image_ids")) or [None])[0]
		fields: Fields = {
			"name": name,
			"slug": (seo.optional_string("permalink") if seo else None) or slugify(name),
			"parent": self._context.gid("Category", parent_id) if parent_id else None,
			"description": None,
			"image": first["url"] if isinstance(first, dict) else None,
		}
		return Desired(self.kind, node.id(), fields)


class ItemMapper:
	"""A catalog item → an Item: one when it has one variation and no item options, else a template with a
	variant per variation (told apart by its item option values or, without options, by its name). Price,
	stock tracking and sold-out state are read at the main location (its override wins); stock is the main
	location's in-stock count. Archived items, and items not sold at the main location, are unpublished; a
	deleted item maps to nothing, so its copy is retired."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		if is_deleted(node):
			return None
		item = node.required_child("item_data")
		title = item.optional_string("name")
		if title is None:
			raise Unsupported("a Square item without a name has no Item")
		product_type = item.optional_string("product_type") or DEFAULT_PRODUCT_TYPE
		if product_type not in PRODUCT_TYPES:
			raise Unsupported(
				f'"{title}" is a Square {product_type} item, which has no ERPNext item equivalent'
			)
		variations = [v for v in item.children("variations") if not is_deleted(v)]
		if not variations:
			raise SourceError(f"Square item {node.string('square_id')} has no variations.")
		options = self._options(node, item)
		simple = len(variations) == 1 and not variations[0].required_child("item_variation_data").children(
			"item_option_values"
		)
		variants = [self._variant(node, item, variation, simple, options) for variation in variations]
		first = variants[0]
		seo = item.child("ecom_seo_data")
		legacy = path_of(item.optional_string("ecom_uri"))
		unpriced = sum(1 for variant in variants if variant["price"] is None)
		warnings = (
			(f"{unpriced} variable-priced variation(s) have no price, so they can't be bought",)
			if unpriced
			else ()
		)
		fields: Fields = {
			"type": "simple" if simple else "variable",
			"name": title,
			"slug": (seo.optional_string("permalink") if seo else None) or slugify(title),
			"description": item.optional_string("description_html")
			or plain_html(item.optional_string("description")),
			"short_description": None,
			"sku": first["sku"] if simple else None,
			"barcode": first["barcode"] if simple else None,
			"brand": None,
			"collections": self._collections(node, item),
			"images": images(node, item.strings("image_ids")),
			"published": self._published(node, item),
			"is_stock_item": product_type != DIGITAL and any(v["stock_qty"] is not None for v in variants),
			"weight": None,
			"price": first["price"] if simple else None,
			"sale": None,
			"stock_qty": first["stock_qty"] if simple else None,
			"attributes": [] if simple else variant_attributes(variants),
			"specifications": [],
			"variants": [] if simple else list[Field](variants),
			"tags": [],
		}
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else (), warnings)

	def _variant(
		self,
		item_node: Node,
		item: Node,
		variation: Node,
		simple: bool,
		options: dict[str, tuple[str, dict[str, str]]],
	) -> Fields:
		variation_id = variation.string("id")
		data = variation.required_child("item_variation_data")
		if data.optional_string("measurement_unit_id") is not None:
			raise Unsupported(f"variation {variation_id} is sold by a measurement unit")
		main = item_node.string("main_location_id")
		override = next(
			(o for o in data.children("location_overrides") if o.optional_string("location_id") == main), None
		)
		sold_out = override is not None and override.get("sold_out") is True
		tracked = sold_out or flag(override, "track_inventory", flag(data, "track_inventory", False))
		chosen = {} if simple else self._chosen(variation, data, options)
		return {
			"gid": self._context.gid("ItemVariation", variation_id),
			"sku": data.optional_string("sku"),
			"barcode": data.optional_string("upc"),
			"attributes": dict[str, Field](chosen),
			"price": price(variation_id, data, override),
			"sale": None,
			"stock_qty": (Decimal(0) if sold_out else in_stock(item_node, variation_id, main))
			if tracked
			else None,
			"weight": None,
			"image": url if (url := image_url(item_node, data.strings("image_ids"))) else None,
			"enabled": True,
		}

	@staticmethod
	def _options(item_node: Node, item: Node) -> dict[str, tuple[str, dict[str, str]]]:
		"""The item's options in its order: option id → its display name and its values (id → name)."""
		objects = {
			o.string("id"): o.required_child("item_option_data") for o in item_node.children("option_objects")
		}
		options: dict[str, tuple[str, dict[str, str]]] = {}
		for reference in item.children("item_options"):
			option_id = reference.string("item_option_id")
			option = objects.get(option_id)
			if option is None:
				raise Unsupported(f"the item uses item option {option_id}, which Square no longer has")
			values = {}
			for value in option.children("values"):
				value_data = value.child("item_option_value_data")
				values[value.string("id")] = (
					value_data.optional_string("name") if value_data else None
				) or value.string("id")
			name = option.optional_string("display_name") or option.optional_string("name") or option_id
			options[option_id] = (name, values)
		return options

	@staticmethod
	def _chosen(
		variation: Node, data: Node, options: dict[str, tuple[str, dict[str, str]]]
	) -> dict[str, str]:
		"""What tells a variation apart: its option values (in the item's option order), or its name."""
		if not options:
			return {VARIATION_ATTRIBUTE: data.optional_string("name") or variation.string("id")}
		picked = {
			v.text("item_option_id"): v.text("item_option_value_id")
			for v in data.children("item_option_values")
		}
		chosen: dict[str, str] = {}
		for option_id, (name, values) in options.items():
			value_id = picked.get(option_id)
			if value_id is None or value_id not in values:
				raise Unsupported(
					f'variation {variation.string("id")} has no current value for option "{name}"'
				)
			chosen[name] = values[value_id]
		return chosen

	def _collections(self, item_node: Node, item: Node) -> list[Field]:
		"""The item's categories that are mirrored (live and named), in the item's order."""
		named = {
			c.string("id")
			for c in item_node.children("category_objects")
			if (c.child("category_data") or Node({})).optional_string("name")
		}
		ids = [c.string("id") for c in item.children("categories") if c.string("id") in named]
		return [self._context.gid("Category", category_id) for category_id in dict.fromkeys(ids)]

	@staticmethod
	def _published(item_node: Node, item: Node) -> bool:
		"""Archived items, and items not sold at the main location, are unpublished. Square's presence is
		everywhere except the absent list (the default), or only the present list."""
		if item.get("is_archived") is True:
			return False
		main = item_node.string("main_location_id")
		if item_node.get("present_at_all_locations") is not False:
			return main not in item_node.strings("absent_at_location_ids")
		return main in item_node.strings("present_at_location_ids")


def flag(node: Node | None, field: str, default: bool) -> bool:
	value = node.get(field) if node else None
	return value if isinstance(value, bool) else default


def price(variation_id: str, data: Node, override: Node | None) -> Decimal | None:
	"""The price at the main location (its override wins); None when the price is set at the time of sale."""
	pricing = (override.optional_string("pricing_type") if override else None) or data.optional_string(
		"pricing_type"
	)
	if pricing == VARIABLE_PRICING:
		return None
	found = money(override, "price_money") or money(data, "price_money")
	if found is None:
		raise SourceError(f"Variation {variation_id} has a fixed price but no price.")
	return found


def in_stock(item_node: Node, variation_id: str, location_id: str) -> Decimal:
	"""The variation's in-stock count at the main location (none recorded is zero)."""
	for count in item_node.children("inventory_counts"):
		if (
			count.optional_string("catalog_object_id") == variation_id
			and count.optional_string("location_id") == location_id
			and count.optional_string("state") == STOCK_STATE
		):
			return whole(count.optional_string("quantity") or "0", f"variation {variation_id}'s stock")
	return Decimal(0)


def image_url(owner: Node, image_ids: list[str]) -> str | None:
	first = (images(owner, image_ids) or [None])[0]
	url = first.get("url") if isinstance(first, dict) else None
	return url if isinstance(url, str) else None


class CustomerMapper:
	"""A customer profile → a Customer, its Contact and its one address."""

	kind = Kind.CUSTOMER
	version = 1

	def map(self, node: Node) -> Desired | None:
		first, last = node.text("given_name"), node.text("family_name")
		company = node.optional_string("company_name")
		email = (node.optional_string("email_address") or "").lower() or None
		phone = node.optional_string("phone_number")
		person = f"{first} {last}".strip()
		address = square_address("Billing", node.child("address"), person or None, company, phone)
		fields: Fields = {
			"name": person or company or email or node.string("square_id"),
			"type": "Company" if company and not person else "Individual",
			"first_name": first or None,
			"last_name": last or None,
			"company": company,
			"email": email,
			"phone": phone,
			"addresses": [address] if address else [],
		}
		return Desired(self.kind, node.id(), fields)


class OrderMapper:
	"""A sale order → an invoice with Square's totals: lines at their pre-tax prices before discounts (on
	the item's variation), Square's discounts as the invoice's discount, each tax (inclusive or additive) as
	a charge of what Square applied, shipping and delivery service charges as shipping, other service
	charges, tips and cash rounding as fees; approved refunds as credit notes and each fulfillment state as
	a comment. Return and exchange orders, and service charges apportioned into lines, have no exact home."""

	kind = Kind.ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		order_id = node.string("square_id")
		state = node.string("state")
		if state == "CANCELED":
			return None
		if node.children("returns"):
			raise Unsupported(
				"a Square return or exchange order has no invoice of its own (refunds are credit notes)"
			)
		currency = node.required_child("total_money").string("currency")
		tenders = node.children("tenders")
		paid = bool(tenders) and money_or_zero(node, "net_amount_due_money") == 0
		submitted = state == "COMPLETED" or paid
		recipient = self._recipient(node)
		customer = node.optional_string("customer_id")
		name = recipient.optional_string("display_name") if recipient else None
		phone = recipient.optional_string("phone_number") if recipient else None
		address = square_address(
			"Shipping", recipient.child("address") if recipient else None, name, None, phone
		)
		refunds = [r for r in node.children("refunds") if r.string("status") == APPROVED]
		fields: Fields = {
			"number": f"#{order_id}",
			"currency": currency,
			"created_at": node.time("created_at"),
			"submitted": submitted,
			"customer": self._context.gid("Customer", customer) if customer else None,
			"guest": None
			if customer
			else {
				"name": name,
				"email": (recipient.optional_string("email_address") if recipient else None),
				"phone": phone,
			},
			"billing": address,
			"shipping": address,
			"lines": [line_fields(self._line(node, line, currency)) for line in node.children("line_items")],
			"discount": money_or_zero(node, "total_discount_money"),
			"charges": [charge_fields(charge) for charge in self._charges(node)],
			"total": money_or_zero(node, "total_money"),
			"payment": {
				"method": "Square (" + ", ".join(dict.fromkeys(t.string("type") for t in tenders)) + ")",
				"reference": tenders[0].optional_string("payment_id") or tenders[0].optional_string("id"),
				"date": tenders[0].optional_time("created_at") or node.time("created_at"),
			}
			if submitted and tenders
			else None,
			"refunds": [self._refund(refund, currency) for refund in refunds] if submitted else [],
			"notes": self._notes(node),
			"remarks": self._customer_note(node),
		}
		return Desired(self.kind, node.id(), fields)

	def _line(self, order: Node, line: Node, currency: str) -> OrderLine:
		uid = line.optional_string("uid") or line.text("catalog_object_id")
		variation = line.optional_string("catalog_object_id")
		items = order.child("variation_items")
		item_id = items.optional_string(variation) if items and variation else None
		quantity = whole(line.string("quantity"), f'line "{line.text("name")}"')
		before_discount = pre_tax(line) + money_or_zero(line, "total_discount_money")
		return order_line(
			self._context.gid("OrderLineItem", f"{order.string('square_id')}-{uid}"),
			self._context.gid("Item", item_id) if item_id else None,
			self._context.gid("ItemVariation", variation) if item_id and variation else None,
			line.optional_string("name") or line.text("variation_name"),
			quantity,
			before_discount,
			currency,
		)

	@staticmethod
	def _charges(order: Node) -> list[Charge]:
		titles = {tax.text("uid"): tax.optional_string("name") or "Tax" for tax in order.children("taxes")}
		taxed: dict[str, Decimal] = {}
		charged = [*order.children("line_items"), *order.children("service_charges")]
		for line in charged:
			for applied in line.children("applied_taxes"):
				title = titles.get(applied.string("tax_uid"), "Tax")
				taxed[title] = taxed.get(title, Decimal(0)) + money_or_zero(applied, "applied_money")
		charges = [Charge(TAX, title, amount) for title, amount in taxed.items()]
		fulfillments = order.children("fulfillments")
		ships = any(f.optional_string("type") in SHIPPED_FULFILLMENTS for f in fulfillments)
		shipping_types = {
			shipment.optional_string("shipping_type")
			for f in fulfillments
			if (shipment := f.child("shipment_details")) is not None
		}
		for charge in order.children("service_charges"):
			if charge.optional_string("treatment_type") == APPORTIONED:
				raise Unsupported("a service charge apportioned into its line items has no exact invoice row")
			name = charge.optional_string("name") or "Service charge"
			is_shipping = ships and (SHIPPING_CHARGE.search(name) is not None or name in shipping_types)
			charges.append(Charge(SHIPPING if is_shipping else FEE, name, pre_tax(charge)))
		tip = money_or_zero(order, "total_tip_money")
		if tip:
			charges.append(Charge(FEE, "Tip", tip))
		rounding = order.child("rounding_adjustment")
		if rounding is not None and money_or_zero(rounding, "amount_money"):
			charges.append(
				Charge(
					FEE,
					rounding.optional_string("name") or "Rounding",
					money_or_zero(rounding, "amount_money"),
				)
			)
		return [charge for charge in charges if charge.amount != 0]

	def _refund(self, refund: Node, currency: str) -> Field:
		gid = self._context.gid("Refund", refund.string("id"))
		amount = money_or_zero(refund, "amount_money")
		balance = refund_balance(f"{gid}#adjustment", amount, [], [], currency)
		return {
			"gid": gid,
			"date": refund.time("created_at"),
			"reason": refund.optional_string("reason"),
			"amount": amount,
			"lines": [line_fields(balance)] if balance else [],
			"charges": [],
		}

	@staticmethod
	def _recipient(order: Node) -> Node | None:
		"""Who the order goes to: the first fulfillment's recipient."""
		for fulfillment in order.children("fulfillments"):
			details_field = DETAILS.get(fulfillment.text("type"))
			details = fulfillment.child(details_field) if details_field else None
			recipient = details.child("recipient") if details else None
			if recipient is not None:
				return recipient
		return None

	@staticmethod
	def _customer_note(order: Node) -> str | None:
		for fulfillment in order.children("fulfillments"):
			for field, note in (
				("shipment_details", "shipping_note"),
				("pickup_details", "note"),
				("delivery_details", "note"),
			):
				details = fulfillment.child(field)
				if details is not None and details.optional_string(note):
					return details.optional_string(note)
		return None

	def _notes(self, order: Node) -> list[Field]:
		"""One comment per fulfillment state reached (its type, state and, for shipments, the tracking)."""
		order_id = order.string("square_id")
		notes: list[Field] = []
		for index, fulfillment in enumerate(order.children("fulfillments")):
			kind = fulfillment.optional_string("type") or "FULFILLMENT"
			state = fulfillment.optional_string("state") or "PROPOSED"
			details = fulfillment.child(DETAILS.get(kind, ""))
			at = next(
				(t for f in STATE_TIMES.get(state, ()) if details and (t := details.optional_time(f))), None
			)
			tracking = (
				" ".join(
					v
					for v in (
						details.optional_string(f) for f in ("carrier", "tracking_number", "tracking_url")
					)
					if v
				)
				if details
				else ""
			)
			uid = fulfillment.optional_string("uid") or f"{order_id}-{index}"
			label = f"Square {kind.lower().replace('_', ' ')} {state.lower()}"
			notes.append(
				{
					"gid": self._context.gid("Fulfillment", f"{uid}-{state}"),
					"date": at or order.time("created_at"),
					"author": None,
					"text": f"{label}: {tracking}" if tracking else label,
					"customer_note": False,
				}
			)
		return notes


def pre_tax(line: Node) -> Decimal:
	"""What a line or service charge comes to before tax (Square's total less its tax, inclusive or not)."""
	return money_or_zero(line, "total_money") - money_or_zero(line, "total_tax_money")


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		ContentPageMapper(Kind.POLICY),
		CategoryMapper(context),
		ItemMapper(context),
		CustomerMapper(),
		OrderMapper(context),
	]
