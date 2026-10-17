# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Etsy → desired ERPNext state, one mapper per kind, in the shared field contract. Ported from
wp-woo-idempotent-importer's Etsy mappers (Fields, SectionMapper, ListingMapper, BuyerMapper,
ReceiptMapper, ReviewMapper); Etsy's states, money objects and timestamps are translated here."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from idempotent_importer.contract import (
	FEE,
	SHIPPING,
	TAX,
	Address,
	Charge,
	OrderLine,
	Shipment,
	address_fields,
	charge_fields,
	line_fields,
	order_line,
	rating_fraction,
	refund_balance,
	shipment_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.common import (
	ContentPageMapper,
	plain_html,
	required_unix_time,
	shifted,
	unescaped,
	unix_time,
)

WEIGHT_UNITS = {"g": "g", "kg": "kg", "oz": "oz", "lb": "lbs"}
PUBLISHED_STATES = frozenset({"active", "sold_out"})
DOWNLOAD_ONLY = "download"
# Receipt statuses whose payment Etsy has taken, and those still awaiting it; a cancelled receipt is gone.
PAID = frozenset({"paid", "completed", "partially refunded", "fully refunded"})
UNPAID = frozenset({"open", "payment processing"})
TAXES = {"Sales tax": "total_tax_cost", "VAT": "total_vat_cost"}
MAX_RATING = 5
REVIEW_AUTHOR = "Etsy buyer"


def money(node: Node, field: str) -> Decimal | None:
	"""An Etsy Money object (``amount`` / ``divisor``) as an exact Decimal."""
	value = node.child(field)
	if value is None:
		return None
	return shifted(value.integer("amount"), value.integer("divisor"), f"{node.path}.{field}")


def money_or_zero(node: Node, field: str) -> Decimal:
	return money(node, field) or Decimal(0)


def text(value: str | None) -> str:
	return (unescaped(value) or "").strip()


def variant_key(listing_id: int, value_ids: dict[int, str]) -> str:
	"""A variant's key: its listing plus the property values that pick it, ordered by property ("123" for a
	simple listing, "123-45.67" for values 45 and 67). Etsy renumbers inventory product ids on every
	inventory edit, so they can't identify a variant; this key is the same on the listing's inventory and
	on a receipt's transaction."""
	suffix = ".".join(value_ids[key] for key in sorted(value_ids))
	return f"{listing_id}-{suffix}" if suffix else str(listing_id)


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, type_: str, id_: int | str) -> str:
		return self.config.identity(type_, id_)

	def variant(self, listing_id: int, value_ids: dict[int, str]) -> str:
		return self.gid("ListingVariant", variant_key(listing_id, value_ids))


class SectionMapper:
	kind = Kind.COLLECTION
	version = 1

	def map(self, node: Node) -> Desired | None:
		name = text(node.string("title"))
		fields: Fields = {
			"name": name,
			"slug": slugify(name),
			"parent": None,
			"description": None,
			"image": None,
		}
		return Desired(self.kind, node.id(), fields)


class ListingMapper:
	"""A listing → an Item: a template with a variant per inventory product (identified by its property
	values), or one item when it has no variations. Materials, styles, who/when made, personalization,
	translations, shipping profiles, videos and taxonomy have no ERPNext home (coverage/etsy.md)."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		listing_id = node.integer("listing_id")
		title = text(node.string("title"))
		inventory = node.child("inventory")
		if inventory is None:
			raise SourceError(f"Listing {listing_id} came without its inventory.")
		products = [p for p in inventory.children("products") if not p.flag("is_deleted")]
		if not products:
			raise Unsupported("a listing with no inventory has nothing to sell")
		simple = not node.flag("has_variations") or (
			len(products) == 1 and not products[0].children("property_values")
		)
		variants = [self._variant(node, product, simple) for product in products]
		variant_fields = list[Field](variants)
		section = node.optional_integer("shop_section_id")
		virtual = node.string("listing_type") == DOWNLOAD_ONLY
		first = variants[0]
		fields: Fields = {
			"type": "simple" if simple else "variable",
			"name": title,
			"slug": slugify(title),
			"description": plain_html(node.string("description")),
			"short_description": None,
			"sku": first["sku"] if simple else None,
			"barcode": None,
			"brand": None,
			"collections": [self._context.gid("Section", section)] if section else [],
			"images": images(node),
			"published": node.string("state") in PUBLISHED_STATES,
			"is_stock_item": not virtual,
			"weight": first["weight"] if simple else None,
			"price": first["price"] if simple else None,
			"sale": None,
			"stock_qty": first["stock_qty"] if simple else None,
			"attributes": [] if simple else attributes(products),
			"specifications": [],
			"variants": [] if simple else variant_fields,
			"tags": [text(tag) for tag in node.strings("tags")],
		}
		warnings = tuple(
			f"its video {video.string('video_url')} isn't mirrored (no ERPNext home)"
			for video in node.children("videos")
		)
		# No legacy redirect: a listing's URL is on etsy.com, which the new site doesn't answer for.
		return Desired(self.kind, node.id(), fields, (), warnings)

	def _variant(self, listing: Node, product: Node, simple: bool) -> Fields:
		listing_id = listing.integer("listing_id")
		offerings = [offer for offer in product.children("offerings") if not offer.flag("is_deleted")]
		if not offerings:
			raise SourceError(f"Listing {listing_id} has an inventory product with no offering.")
		offering = offerings[0]
		options: dict[str, Field] = {}
		value_ids: dict[int, str] = {}
		for prop in product.children("property_values"):
			name = text(prop.optional_string("property_name") or str(prop.integer("property_id")))
			options[name] = text(", ".join(prop.strings("values")))
			value_ids[prop.integer("property_id")] = ".".join(str(v) for v in prop.integers("value_ids"))
		price = money(offering, "price")
		if price is None:
			raise SourceError(f"Listing {listing_id} has an offering with no price.")
		weight = listing.optional_decimal("item_weight")
		unit = listing.optional_string("item_weight_unit")
		return {
			"gid": self._context.variant(listing_id, {} if simple else value_ids),
			"sku": product.optional_string("sku"),
			"barcode": None,
			"attributes": options,
			"price": price,
			"sale": None,
			"stock_qty": Decimal(offering.integer("quantity") if offering.flag("is_enabled") else 0),
			"weight": {"value": weight, "unit": WEIGHT_UNITS[unit]} if weight is not None and unit else None,
			"image": None,
			"enabled": True,
		}


def attributes(products: list[Node]) -> list[Field]:
	"""Each property (in the order the products list them) with every value it takes."""
	values: dict[str, list[Field]] = {}
	for product in products:
		for prop in product.children("property_values"):
			name = text(prop.optional_string("property_name") or str(prop.integer("property_id")))
			value = text(", ".join(prop.strings("values")))
			if value not in values.setdefault(name, []):
				values[name].append(value)
	return [{"name": name, "values": list(taken)} for name, taken in values.items()]


def images(listing: Node) -> list[Field]:
	"""By rank: the first is the item's image."""
	ranked = sorted(listing.children("images"), key=lambda image: image.integer("rank"))
	return [
		{"url": image.string("url_fullxfull"), "alt": unescaped(image.optional_string("alt_text"))}
		for image in ranked
	]


class BuyerMapper:
	"""A buyer → a Customer. Etsy exposes only the buyer's user id and email (a receipt's name and address
	are the recipient's, often someone else), so that is all the customer holds; orders keep their own
	addresses in their remarks."""

	kind = Kind.CUSTOMER
	version = 1

	def map(self, node: Node) -> Desired | None:
		email = (node.optional_string("buyer_email") or "").lower() or None
		fields: Fields = {
			"name": email or f"Etsy buyer {node.integer('buyer_user_id')}",
			"type": "Individual",
			"first_name": None,
			"last_name": None,
			"company": None,
			"email": email,
			"phone": None,
			"addresses": [],
		}
		return Desired(self.kind, node.id(), fields)


class ReceiptMapper:
	"""A receipt → an invoice with Etsy's totals: transactions as lines (pointing at the listing and its
	variant), shipping, sales tax and VAT as charges (Etsy doesn't split them per line), gift wrap as a
	fee, refunds as credit notes (Etsy refunds name no lines, so each is one adjustment line), and the
	seller's note as a comment, and each shipment as a Shipment."""

	kind = Kind.ORDER
	version = 2

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		status = node.string("status")
		if status not in PAID | UNPAID:
			return None
		receipt_id = node.integer("receipt_id")
		currency = node.required_child("grandtotal").string("currency_code")
		buyer = node.optional_integer("buyer_user_id")
		submitted = status in PAID
		address = receipt_address(node)
		fields: Fields = {
			"number": f"#{receipt_id}",
			"currency": currency,
			"created_at": required_unix_time(node, "created_timestamp"),
			"submitted": submitted,
			"customer": self._context.gid("User", buyer) if buyer and buyer > 0 else None,
			"guest": None
			if buyer and buyer > 0
			else {
				"name": text(node.optional_string("name")) or None,
				"email": node.optional_string("buyer_email"),
				"phone": None,
			},
			"billing": address,
			"shipping": address,
			"lines": [line_fields(self._line(t, currency)) for t in node.children("transactions")],
			"discount": money_or_zero(node, "discount_amt"),
			"charges": [charge_fields(charge) for charge in charges(node)],
			"total": money_or_zero(node, "grandtotal"),
			"payment": self._payment(node) if submitted else None,
			"refunds": [self._refund(receipt_id, r, currency) for r in node.children("refunds")]
			if submitted
			else [],
			"notes": self._notes(node),
			"remarks": text(node.optional_string("message_from_buyer")) or None,
			"shipments": self._shipments(node),
		}
		return Desired(self.kind, node.id(), fields)

	def _line(self, transaction: Node, currency: str) -> OrderLine:
		listing = transaction.optional_integer("listing_id")
		value_ids = {
			variation.integer("property_id"): str(value_id)
			for variation in transaction.children("variations")
			if (value_id := variation.optional_integer("value_id")) is not None
		}
		quantity = Decimal(transaction.integer("quantity"))
		return order_line(
			self._context.gid("Transaction", transaction.integer("transaction_id")),
			self._context.gid("Listing", listing) if listing else None,
			self._context.variant(listing, value_ids) if listing else None,
			text(transaction.optional_string("title")),
			quantity,
			money_or_zero(transaction, "price") * quantity,
			currency,
		)

	@staticmethod
	def _payment(node: Node) -> Field:
		paid = next(
			(t for t in (unix_time(tr, "paid_timestamp") for tr in node.children("transactions")) if t), None
		)
		return {
			"method": f"Etsy ({node.string('payment_method')})",
			"reference": None,
			"date": paid or required_unix_time(node, "created_timestamp"),
		}

	def _refund(self, receipt_id: int, refund: Node, currency: str) -> Field:
		"""Etsy refunds have no id: one is identified by its receipt and the time it was issued."""
		created = refund.integer("created_timestamp")
		amount = money_or_zero(refund, "amount")
		gid = self._context.gid("Refund", f"{receipt_id}-{created}")
		balance = refund_balance(f"{gid}#adjustment", amount, [], [], currency)
		reasons = [
			r
			for r in (
				text(refund.optional_string("reason")),
				text(refund.optional_string("note_from_issuer")),
			)
			if r
		]
		return {
			"gid": gid,
			"date": required_unix_time(refund, "created_timestamp"),
			"reason": " — ".join(reasons) or None,
			"amount": amount,
			"lines": [line_fields(balance)] if balance else [],
			"charges": [],
		}

	def _notes(self, node: Node) -> list[Field]:
		receipt_id = node.integer("receipt_id")
		notes: list[Field] = []
		seller = text(node.optional_string("message_from_seller"))
		if seller:
			notes.append(
				{
					"gid": self._context.gid("SellerNote", receipt_id),
					"date": required_unix_time(node, "created_timestamp"),
					"author": "Seller",
					"text": seller,
					"customer_note": True,
				}
			)
		return notes

	def _shipments(self, node: Node) -> list[Field]:
		"""Each shipment the seller notified: its carrier and tracking code."""
		receipt_id = node.integer("receipt_id")
		return [
			shipment_fields(
				Shipment(
					gid=self._context.gid(
						"Shipment",
						shipment.optional_integer("receipt_shipping_id") or f"{receipt_id}-{index}",
					),
					date=required_unix_time(shipment, "shipment_notification_timestamp"),
					carrier=text(shipment.optional_string("carrier_name")) or None,
					service=None,
					tracking_number=text(shipment.optional_string("tracking_code")) or None,
					tracking_url=None,
					delivered=False,
					contents=None,
				)
			)
			for index, shipment in enumerate(node.children("shipments"))
		]


def charges(receipt: Node) -> list[Charge]:
	"""Shipping, the order-level taxes and gift wrap; zero amounts are left out."""
	shipping_title = next(
		(
			text(t.optional_string("shipping_method"))
			for t in receipt.children("transactions")
			if t.optional_string("shipping_method")
		),
		"Shipping",
	)
	found = [
		Charge(SHIPPING, shipping_title, money_or_zero(receipt, "total_shipping_cost")),
		*(Charge(TAX, title, money_or_zero(receipt, field)) for title, field in TAXES.items()),
		Charge(FEE, "Gift wrap", money_or_zero(receipt, "gift_wrap_price")),
	]
	return [charge for charge in found if charge.amount != 0]


def receipt_address(receipt: Node) -> Fields | None:
	"""A receipt's recipient address. Etsy gives one name for the recipient."""
	line1, city = text(receipt.optional_string("first_line")), text(receipt.optional_string("city"))
	if not line1 or not city:
		return None
	return address_fields(
		Address(
			address_type="Shipping",
			name=text(receipt.optional_string("name")) or None,
			company=None,
			line1=line1,
			line2=text(receipt.optional_string("second_line")) or None,
			city=city,
			state=receipt.optional_string("state"),
			postcode=receipt.optional_string("zip"),
			country_code=receipt.optional_string("country_iso"),
			email=None,
			phone=None,
		)
	)


class ReviewMapper:
	"""A transaction review → an Item Review on the listing's Website Item. Etsy shows reviewers' names only
	on etsy.com, so it is attributed to "Etsy buyer"."""

	kind = Kind.REVIEW
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		fields: Fields = {
			"product": self._context.gid("Listing", node.integer("listing_id")),
			"author_name": REVIEW_AUTHOR,
			"author_email": None,
			"text": text(node.optional_string("review")),
			"rating": rating_fraction(node.integer("rating"), MAX_RATING),
			"published_on": required_unix_time(node, "created_timestamp")[:10],
		}
		return Desired(self.kind, node.id(), fields)


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		ContentPageMapper(Kind.POLICY),
		SectionMapper(),
		ListingMapper(context),
		BuyerMapper(),
		ReceiptMapper(context),
		ReviewMapper(context),
	]
