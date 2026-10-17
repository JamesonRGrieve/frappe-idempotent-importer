# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Wix → desired ERPNext state, one mapper per kind, in the shared field contract. Ported from
wp-woo-idempotent-importer's Wix mappers; Wix's two Stores catalogs (V1 and V3), money objects, coupon
specifications, order statuses and rich content are translated here. Identities arrive scoped to the
connection, with Wix's id as ``source_id``."""

from __future__ import annotations

import html
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
	LineRates,
	OrderLine,
	address_fields,
	charge_fields,
	date_of,
	discount_fields,
	included_taxes,
	line_fields,
	order_line,
	rating_fraction,
	refund_balance,
	taxed_charge,
	variant_attributes,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import path_of, redirect_target
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.html import embedded_images, hosted_on
from idempotent_importer.sources.common import from_unix
from idempotent_importer.sources.wix.reader import Catalog
from idempotent_importer.sources.wix.rich_content import rich_html

WEIGHT_UNITS = {"KG": "kg", "LB": "lbs"}
# The Wix Stores app, as an order line's catalog reference names it.
STORES_APP_ID = "215238eb-22a5-4c36-9e7b-e7c08025e04e"
MEDIA_HOST = "wixstatic.com"
PRODUCT_PAGE_PATH = "/product-page/"
DIGITAL_V3, DIGITAL_V1 = "DIGITAL", "digital"
IMAGE_V3, IMAGE_V1 = "IMAGE", "image"
BLOG_NAME, BLOG_PATH = "Blog", "/blog"
MILLISECONDS = 1000
# Order states: an approved order whose payment Wix took is booked; cancelled, rejected and declined ones
# aren't sales.
APPROVED = "APPROVED"
NOT_SALES = frozenset({"CANCELED", "REJECTED"})
PAID = frozenset({"PAID", "PARTIALLY_REFUNDED", "FULLY_REFUNDED"})
DECLINED = "DECLINED"
CHARGED = frozenset({"APPROVED", "REFUNDED", "PARTIALLY_REFUNDED"})
GIFT_CARD = "GIFT_CARD"
REFUND_SUCCEEDED = "SUCCEEDED"
STORES_COUPONS = "stores"
PUBLISHED_REVIEWS = frozenset({"APPROVED", "SKIPPED"})
MAX_RATING = 5


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, type_: str, id_: str) -> str:
		return self.config.identity(type_, id_)

	def variant(self, product_id: str, variant_id: str) -> str:
		"""Wix variant ids are unique only within their product (a product without options has a default
		variant whose id repeats across products), so a variant is keyed by both."""
		return self.gid("Variant", f"{product_id}:{variant_id}")

	def collection(self, catalog: Catalog, id_: str) -> str:
		return self.gid("Collection" if catalog is Catalog.V1 else "Category", id_)

	def hosted(self, fragment: str) -> list[Field]:
		"""The images an HTML fragment embeds from Wix's media host or the site itself."""
		owned = hosted_on(MEDIA_HOST, urlsplit(self.config.store_url).hostname or "")
		return list(embedded_images(fragment, owned))


def money(node: Node | None) -> Decimal | None:
	"""A Wix money object (a decimal string ``amount``)."""
	return node.optional_decimal("amount") if node else None


def money_of(parent: Node | None, field: str) -> Decimal:
	return (money(parent.child(field)) if parent else None) or Decimal(0)


def epoch_time(node: Node, field: str) -> str | None:
	"""A time Wix sends as milliseconds since the epoch (a coupon's)."""
	milliseconds = node.optional_integer(field)
	return None if milliseconds is None else from_unix(milliseconds // MILLISECONDS)


def text_html(text: str | None) -> str | None:
	"""Plain text Wix keeps as text, as HTML with its line breaks kept."""
	return html.escape(text).replace("\n", "<br>\n") if text else None


def state_of(subdivision: str | None, country: str | None) -> str | None:
	"""Wix sends ISO 3166-2 ("CA-SK") or the bare code; the address keeps the part after the country."""
	if subdivision and country and subdivision.upper().startswith(f"{country.upper()}-"):
		return subdivision[len(country) + 1 :]
	return subdivision


def wix_address(
	address_type: str, address: Node | None, name: str | None, company: str | None, phone: str | None
) -> Fields | None:
	if address is None:
		return None
	street = address.child("streetAddress")
	numbered = " ".join(p for p in ((street.text("number"), street.text("name")) if street else ()) if p)
	line1 = address.optional_string("addressLine") or numbered
	if not line1 or not address.text("city"):
		return None
	country = address.optional_string("country")
	return address_fields(
		Address(
			address_type=address_type,
			name=name,
			company=company,
			line1=line1,
			line2=address.optional_string("addressLine2")
			or (street.optional_string("apt") if street else None),
			city=address.string("city"),
			state=state_of(address.optional_string("subdivision"), country),
			postcode=address.optional_string("postalCode"),
			country_code=country,
			email=None,
			phone=phone,
		)
	)


def order_address(address_type: str, with_contact: Node | None) -> Fields | None:
	if with_contact is None:
		return None
	contact = with_contact.child("contactDetails")
	name = (
		" ".join(p for p in ((contact.text("firstName"), contact.text("lastName")) if contact else ()) if p)
		or None
	)
	return wix_address(
		address_type,
		with_contact.child("address"),
		name,
		contact.optional_string("company") if contact else None,
		contact.optional_string("phone") if contact else None,
	)


class CollectionMapper:
	"""A V3 category or a V1 collection → an Item Group: name, slug, plain-text description, image and (for a
	category) its parent."""

	kind = Kind.COLLECTION
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		v1 = "/Collection/" in node.id()
		media = node.child("media")
		main = media.child("mainMedia") if media else None
		image = (main.child("image") if main else None) if v1 else node.child("image")
		parent = None if v1 else node.child("parentCategory")
		parent_id = parent.optional_string("id") if parent else None
		fields: Fields = {
			"name": node.string("name"),
			"slug": node.string("slug"),
			"parent": self._context.collection(Catalog.V3, parent_id) if parent_id else None,
			"description": text_html(node.optional_string("description")),
			"image": image.optional_string("url") if image else None,
		}
		return Desired(self.kind, node.id(), fields)


class ProductMapper:
	"""A product of a V3 or V1 catalog → an Item: one (a product without options; its one variant) or a
	template with a variant per variant. A compare-at (V3) or undiscounted (V1) price above the selling price
	is the price and the selling price the sale. Stock is counted where Wix tracks quantities (V3: summed over
	the variant's inventory items; V1: the variant's stock). Hidden products are unpublished; downloads aren't
	stock items; the old product page redirects to the item."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		v1 = Catalog(node.string("catalog")) is Catalog.V1
		title = node.string("name")
		product_id = node.string("source_id")
		sources = node.children("variants")
		if not sources:
			raise SourceError(f"Wix product {product_id} has no variants.")
		warnings: list[str] = []
		if v1:
			simple = not node.children("productOptions")
			variants = [self._v1_variant(node, variant, simple) for variant in sources]
			if any((v.child("variant") or node).optional_decimal("weight") for v in sources):
				warnings.append("a weight, but Wix's V1 catalog doesn't say in which unit")
			collections = [self._context.collection(Catalog.V1, c) for c in node.strings("collectionIds")]
			page = node.child("productPageUrl")
			path = page.optional_string("path") if page else None
			brand = node.optional_string("brand")
			images = v1_images(node.child("media"))
		else:
			simple = not node.children("options")
			physical = node.child("physicalProperties")
			unit_info = physical.child("weightMeasurementUnitInfo") if physical else None
			unit = WEIGHT_UNITS.get(unit_info.text("weightMeasurementUnit")) if unit_info else None
			inventory: dict[str, list[Node]] = {}
			for item in node.children("inventory_items"):
				inventory.setdefault(item.string("variantId"), []).append(item)
			variants = [
				self._v3_variant(node, variant, simple, unit, inventory.get(variant.string("variantId"), []))
				for variant in sources
			]
			if node.children("modifiers"):
				warnings.append("it asks customers for modifiers, which Items can't")
			if unit is None and any(
				(p := v.child("physicalProperties")) and p.optional_decimal("weight") for v in sources
			):
				warnings.append("a weight in a unit ERPNext has no UOM for")
			categories = node.child("directCategoriesInfo")
			collections = [
				self._context.collection(Catalog.V3, c.string("id"))
				for c in (categories.children("categories") if categories else [])
			]
			url = node.child("url")
			path = url.optional_string("relativePath") if url else None
			brand_node = node.child("brand")
			brand = brand_node.optional_string("name") if brand_node else None
			images = v3_images(node.child("media"))
		shown = [v.child("variant") for v in sources] if v1 else list[Node | None](sources)
		hidden = sum(1 for v in shown if v is not None and v.get("visible") is False)
		if hidden:
			warnings.append(f"{hidden} variant(s) Wix hides are sold")
		digital = node.optional_string("productType") in (DIGITAL_V3, DIGITAL_V1)
		first = variants[0]
		fields: Fields = {
			"type": "simple" if simple else "variable",
			"name": title,
			"slug": node.string("slug"),
			"description": node.optional_string("description" if v1 else "plainDescription"),
			"short_description": None,
			"sku": first["sku"] if simple else None,
			"barcode": first["barcode"] if simple else None,
			"brand": brand,
			"collections": list[Field](collections),
			"images": images,
			"published": node.get("visible") is not False,
			"is_stock_item": not digital and any(v["stock_qty"] is not None for v in variants),
			"weight": first["weight"] if simple else None,
			"price": first["price"] if simple else None,
			"sale": first["sale"] if simple else None,
			"stock_qty": first["stock_qty"] if simple else None,
			"attributes": [] if simple else variant_attributes(variants),
			"specifications": [],
			"variants": [] if simple else list[Field](variants),
			"tags": [],
		}
		page_path = path_of(path) or f"{PRODUCT_PAGE_PATH}{node.string('slug')}"
		return Desired(self.kind, node.id(), fields, (page_path,), tuple(warnings))

	def _v3_variant(
		self, product: Node, variant: Node, simple: bool, unit: str | None, inventory: list[Node]
	) -> Fields:
		variant_id = variant.string("variantId")
		price = variant.required_child("price")
		actual = money(price.child("actualPrice"))
		if actual is None:
			raise SourceError(f"Wix variant {variant_id} has no price.")
		compare_at = money(price.child("compareAtPrice"))
		on_sale = compare_at is not None and compare_at > actual
		physical = variant.child("physicalProperties")
		weight = physical.optional_decimal("weight") if physical else None
		tracked = [item for item in inventory if item.get("trackQuantity") is True]
		chosen: dict[str, Field] = {}
		for choice in variant.children("optionChoices"):
			names = choice.required_child("optionChoiceNames")
			chosen[names.string("optionName")] = names.string("choiceName")
		media = variant.child("media")
		image = media.child("image") if media else None
		return {
			"gid": self._context.variant(product.string("source_id"), variant_id),
			"sku": variant.optional_string("sku"),
			"barcode": variant.optional_string("barcode"),
			"attributes": {} if simple else chosen,
			"price": compare_at if on_sale and compare_at is not None else actual,
			"sale": {"price": actual, "from": None, "to": None} if on_sale else None,
			"stock_qty": Decimal(max(0, sum(item.optional_integer("quantity") or 0 for item in tracked)))
			if tracked
			else None,
			"weight": {"value": weight, "unit": unit} if unit and weight else None,
			"image": image.optional_string("url") if image else None,
			"enabled": True,
		}

	def _v1_variant(self, product: Node, variant: Node, simple: bool) -> Fields:
		variant_id = variant.string("id")
		data = variant.child("variant")
		price_data = (data.child("priceData") if data else None) or product.child("priceData")
		price = price_data.optional_decimal("price") if price_data else None
		if price_data is None or price is None:
			raise SourceError(f"Wix variant {variant_id} has no price.")
		discounted = price_data.optional_decimal("discountedPrice")
		stock = variant.child("stock")
		tracked = stock is not None and stock.get("trackQuantity") is True
		choices = variant.get("choices") or {}
		if not isinstance(choices, dict) or not all(isinstance(v, str) for v in choices.values()):
			raise SourceError(f"Wix variant {variant_id} has choices that aren't text.")
		sku = (data.optional_string("sku") if data else None) or (
			product.optional_string("sku") if simple else None
		)
		return {
			"gid": self._context.variant(product.string("source_id"), variant_id),
			"sku": sku,
			"barcode": None,
			"attributes": {}
			if simple
			else {name: value for name, value in choices.items() if isinstance(value, str)},
			"price": price,
			"sale": {"price": discounted, "from": None, "to": None}
			if discounted is not None and discounted < price
			else None,
			"stock_qty": Decimal(max(0, stock.optional_integer("quantity") or 0))
			if stock is not None and tracked
			else None,
			"weight": None,
			"image": None,
			"enabled": True,
		}


def v3_images(media: Node | None) -> list[Field]:
	"""Every image of a V3 product in Wix's order (the first is its main media); videos aren't images."""
	info = media.child("itemsInfo") if media else None
	items = info.children("items") if info else []
	if not items and media is not None and media.child("main") is not None:
		items = [media.required_child("main")]
	found: list[Field] = []
	for item in items:
		image = item.child("image")
		url = image.optional_string("url") if image else None
		if url and (item.optional_string("mediaType") or IMAGE_V3) == IMAGE_V3 and image is not None:
			found.append(
				{"url": url, "alt": item.optional_string("altText") or image.optional_string("altText")}
			)
	return found


def v1_images(media: Node | None) -> list[Field]:
	"""Every image of a V1 product in Wix's order (the main media when the items don't list it)."""
	items = media.children("items") if media else []
	if not items and media is not None and media.child("mainMedia") is not None:
		items = [media.required_child("mainMedia")]
	found: list[Field] = []
	for item in items:
		image = item.child("image")
		url = image.optional_string("url") if image else None
		if url and (item.optional_string("mediaType") or IMAGE_V1) == IMAGE_V1 and image is not None:
			found.append({"url": url, "alt": image.optional_string("altText")})
	return found


class ContactMapper:
	"""A contact → a Customer, its Contact, and every saved address (the one tagged billing, else the
	default, else the first, as billing)."""

	kind = Kind.CUSTOMER
	version = 1

	def map(self, node: Node) -> Desired | None:
		name = node.child("name")
		first, last = (name.text("first"), name.text("last")) if name else ("", "")
		person = f"{first} {last}".strip()
		company_node = node.child("company")
		company = company_node.optional_string("name") if company_node else None
		email_node = node.child("email")
		email = (email_node.optional_string("email") or "").lower() or None if email_node else None
		phone_node = node.child("phone")
		phone = phone_node.optional_string("phone") if phone_node else None
		saved = node.children("addresses")
		billing = tagged(saved, "BILLING")
		addresses: list[Field] = []
		for entry in sorted(saved, key=lambda a: a is not billing):
			recipient = entry.child("recipient")
			recipient_name = (
				" ".join(p for p in (recipient.text("firstName"), recipient.text("lastName")) if p)
				if recipient
				else ""
			)
			address = wix_address(
				"Billing" if entry is billing else "Shipping",
				entry.child("address"),
				recipient_name or person or None,
				(recipient.optional_string("company") if recipient else None) or company,
				(recipient.optional_string("phone") if recipient else None) or phone,
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
			"phone": phone,
			"addresses": addresses,
		}
		return Desired(self.kind, node.id(), fields)


def tagged(addresses: list[Node], tag: str) -> Node | None:
	"""The address with ``tag``, else the default one, else the first."""
	return (
		next((a for a in addresses if a.optional_string("tag") == tag), None)
		or next((a for a in addresses if a.get("defaultAddress") is True), None)
		or (addresses[0] if addresses else None)
	)


class CouponMapper:
	"""A Wix Stores coupon → a coupon Pricing Rule, when ERPNext applies it exactly: a percentage off (the
	whole store, a product or a collection) or an amount off the order, with its minimum subtotal, dates
	and use limit. Fixed-price, buy-X-get-Y and free-shipping coupons, an amount off some products, coupons
	limited to one item or per customer, and other Wix apps' coupons are Unsupported. A deactivated coupon
	maps to nothing."""

	kind = Kind.DISCOUNT
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		spec = node.required_child("specification")
		if spec.get("active") is False:
			return None
		name = spec.optional_string("name") or spec.string("code")
		for field, label in (("fixedPriceAmount", "fixed-price"), ("buyXGetY", "buy-X-get-Y")):
			if spec.has(field):
				raise Unsupported(f"a {label} coupon has no ERPNext coupon equivalent")
		if spec.get("freeShipping") is True:
			raise Unsupported("ERPNext coupons can't discount shipping")
		if spec.get("limitedToOneItem") is True:
			raise Unsupported("a coupon limited to one item has no ERPNext coupon equivalent")
		if spec.optional_integer("limitPerCustomer"):
			raise Unsupported("a per-customer use limit has no ERPNext coupon equivalent")
		products, groups = self._scope(spec.child("scope"), Catalog(node.string("catalog")))
		percent = spec.optional_decimal("percentOffRate")
		amount_off = spec.optional_decimal("moneyOffAmount")
		if amount_off is not None and (products or groups):
			raise Unsupported("an amount off some products has no exact ERPNext coupon equivalent")
		if percent is None and amount_off is None:
			raise Unsupported("a coupon with no discount ERPNext can apply")
		started = epoch_time(spec, "startTime") or epoch_time(node, "dateCreated")
		minimum = spec.optional_decimal("minimumSubtotal")
		discount = Discount(
			codes=[spec.string("code")],
			description=name,
			discount="percentage" if percent is not None else "amount",
			applies="items" if products else "groups" if groups else "transaction",
			value=percent if percent is not None else amount_off or Decimal(0),
			items=products,
			groups=groups,
			min_amount=minimum or None,
			valid_from=date_of(started),
			valid_upto=date_of(epoch_time(spec, "expirationTime")),
			max_uses=spec.optional_integer("usageLimit"),
		)
		return Desired(self.kind, node.id(), discount_fields(discount))

	def _scope(self, scope: Node | None, catalog: Catalog) -> tuple[list[str], list[str]]:
		if scope is None:
			return [], []
		namespace = scope.string("namespace")
		if namespace != STORES_COUPONS:
			raise Unsupported(f"a {namespace} coupon doesn't apply to the store")
		group = scope.child("group")
		entity = group.optional_string("entityId") if group else None
		match group.optional_string("name") if group else None:
			case None:
				return [], []
			case "product":
				return ([self._context.gid("Product", entity)] if entity else []), []
			case "collection":
				return [], ([self._context.collection(catalog, entity)] if entity else [])
			case other:
				raise Unsupported(f"a coupon on a {other} group has no ERPNext coupon equivalent")


class OrderMapper:
	"""An eCommerce order → an invoice with Wix's totals: lines at their prices before discounts (on the
	variant's item), the order's discount, each tax summed by name over the lines, shipping and fees, the
	selected shipping option and the additional fees as charges; refunds that returned money as credit
	notes (their lines at the price paid, and an adjustment for the tax and the rest), each fulfillment as
	a comment. Approved orders that Wix took payment for are submitted; others are drafts. Cancelled,
	rejected and declined orders map to nothing; a tax-inclusive order's taxes are included in its prices
	(``inclusive_charges``); gift card sales are Unsupported."""

	kind = Kind.ORDER
	version = 2

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		status = node.string("status")
		payment_status = node.optional_string("paymentStatus")
		if status in NOT_SALES or payment_status == DECLINED:
			return None
		currency = node.string("currency")
		submitted = status == APPROVED and payment_status in PAID
		summary = node.required_child("priceSummary")
		buyer = node.child("buyerInfo")
		contact = buyer.optional_string("contactId") if buyer else None
		billing = node.child("billingInfo")
		shipping_info = node.child("shippingInfo")
		logistics = shipping_info.child("logistics") if shipping_info else None
		payment = next((p for p in node.children("payments") if p.optional_string("status") in CHARGED), None)
		lines = {line.string("id"): line for line in node.children("lineItems")}
		contact_details = billing.child("contactDetails") if billing else None
		inclusive = node.get("taxIncludedInPrices") is True
		sold = [self._line(line, currency, inclusive=inclusive) for line in lines.values()]
		if inclusive:
			sold, found = inclusive_charges(node, shipping_info, list(lines.values()), sold)
		else:
			found = charges(node, shipping_info)
		fields: Fields = {
			"number": f"#{node.optional_integer('number') or node.string('source_id')}",
			"currency": currency,
			"created_at": node.time("createdDate"),
			"submitted": submitted,
			"customer": self._context.gid("Contact", contact) if contact else None,
			"guest": None
			if contact
			else {
				"name": " ".join(
					p
					for p in (
						(contact_details.text("firstName"), contact_details.text("lastName"))
						if contact_details
						else ()
					)
					if p
				)
				or None,
				"email": (buyer.optional_string("email") or "").lower() or None if buyer else None,
				"phone": contact_details.optional_string("phone") if contact_details else None,
			},
			"billing": order_address("Billing", billing),
			"shipping": order_address(
				"Shipping", logistics.child("shippingDestination") if logistics else None
			),
			"lines": [line_fields(line) for line in sold],
			"discount": money_of(summary, "discount"),
			"charges": [charge_fields(charge) for charge in found],
			"total": money_of(summary, "total"),
			"payment": {
				"method": payment_method(payment) if payment else "Wix",
				"reference": payment_reference(payment) if payment else None,
				"date": payment.optional_time("createdDate") if payment else None,
			}
			if submitted
			else None,
			"refunds": [
				r for r in (self._refund(refund, lines, currency) for refund in node.children("refunds")) if r
			]
			if submitted
			else [],
			"notes": [self._note(f, node.time("createdDate")) for f in node.children("fulfillments")],
			"remarks": node.optional_string("buyerNote"),
		}
		if inclusive:
			fields["taxes_included"] = True
		return Desired(self.kind, node.id(), fields)

	def _line(self, line: Node, currency: str, *, inclusive: bool = False) -> OrderLine:
		"""A line at its price before discounts: before tax, or with it when the order's prices include
		tax."""
		item_type = line.child("itemType")
		if item_type is not None and item_type.optional_string("preset") == GIFT_CARD:
			raise Unsupported("a gift card sale has no ERPNext item (coverage/wix.md)")
		catalog = line.child("catalogReference")
		product = (
			catalog.optional_string("catalogItemId")
			if catalog and catalog.optional_string("appId") == STORES_APP_ID
			else None
		)
		options = catalog.child("options") if catalog else None
		variant = options.optional_string("variantId") if options else None
		name_node = line.child("productName")
		chosen = ", ".join(
			f"{label}: {value}"
			for description in line.children("descriptionLines")
			if (label := (n.optional_string("original") if (n := description.child("name")) else None))
			and (value := line_value(description))
		)
		name = name_node.text("original") if name_node else ""
		return order_line(
			self._context.gid("OrderLineItem", line.string("id")),
			self._context.gid("Product", product) if product else None,
			self._context.variant(product, variant) if product and variant else None,
			f"{name} ({chosen})" if chosen else name,
			Decimal(line.integer("quantity")),
			money_of(line, "totalPriceAfterTax" if inclusive else "totalPriceBeforeTax")
			+ money_of(line, "totalDiscount"),
			currency,
		)

	def _refund(self, refund: Node, lines: dict[str, Node], currency: str) -> Field | None:
		"""A refund that returned money (a failed or pending one returned none yet), its lines returned at the
		price paid and the rest (their tax, shipping, a goodwill amount) as an adjustment."""
		summary = refund.child("summary")
		refunded = money(summary.child("refunded")) if summary else None
		if refunded is None:
			refunded = sum(
				(
					money_of(t, "amount")
					for t in refund.children("transactions")
					if t.optional_string("refundStatus") == REFUND_SUCCEEDED
				),
				Decimal(0),
			)
		if not refunded:
			return None
		gid = self._context.gid("Refund", refund.string("id"))
		details = refund.child("details")
		returned: list[OrderLine] = []
		for item in details.children("lineItems") if details else []:
			source = lines.get(item.string("lineItemId"))
			if source is None:
				raise Unsupported("a refund names a line the order doesn't have")
			sold = self._line(source, currency)
			quantity = Decimal(item.integer("quantity"))
			paid = money_of(source, "price")
			returned.append(
				order_line(
					sold.source_line,
					sold.product,
					sold.variant,
					sold.name,
					-quantity,
					-paid * quantity,
					currency,
				)
			)
		balance = refund_balance(f"{gid}#adjustment", refunded, returned, [], currency)
		return {
			"gid": gid,
			"date": refund.optional_time("createdDate"),
			"reason": details.optional_string("reason") if details else None,
			"amount": refunded,
			"lines": [line_fields(line) for line in [*returned, *([balance] if balance else [])]],
			"charges": [],
		}

	def _note(self, fulfillment: Node, created: str) -> Field:
		tracking = fulfillment.child("trackingInfo")
		parts = (
			[tracking.optional_string(f) for f in ("shippingProvider", "trackingNumber", "trackingLink")]
			if tracking
			else []
		)
		detail = " ".join(p for p in parts if p)
		return {
			"gid": self._context.gid("Fulfillment", fulfillment.string("id")),
			"date": fulfillment.optional_time("createdDate") or created,
			"author": None,
			"text": f"Wix fulfillment: {detail}" if detail else "Wix fulfillment",
			"customer_note": False,
		}


def line_value(description: Node) -> str | None:
	for field in ("plainText", "colorInfo"):
		value = description.child(field)
		if value is not None and value.optional_string("original"):
			return value.optional_string("original")
	return None


def tax_amounts(tax_info: Node | None) -> list[tuple[str, Decimal]]:
	"""A line's (or the shipping's, or a fee's) taxes by name; one "Tax" when taxed without a breakdown."""
	if tax_info is None:
		return []
	found = [
		(
			tax.optional_string("taxName") or tax.optional_string("jurisdiction") or "Tax",
			money_of(tax, "taxAmount"),
		)
		for tax in tax_info.children("taxBreakdown")
	]
	total = money(tax_info.child("taxAmount"))
	return found or ([("Tax", total)] if total else [])


def tax_rates(tax_info: Node | None) -> list[tuple[str, Decimal, Decimal]]:
	"""A line's (or a charge's) taxes as (name, rate percent, amount); one "Tax" at its rate when taxed
	without a breakdown. Wix gives rates as fractions ("0.05")."""
	if tax_info is None:
		return []
	found = [
		(
			tax.optional_string("taxName") or tax.optional_string("jurisdiction") or "Tax",
			tax.decimal("rate") * 100,
			money_of(tax, "taxAmount"),
		)
		for tax in tax_info.children("taxBreakdown")
	]
	total = money(tax_info.child("taxAmount"))
	if found or not total:
		return found
	return [("Tax", tax_info.decimal("taxRate") * 100, total)]


def inclusive_charges(
	order: Node, shipping_info: Node | None, line_nodes: list[Node], lines: list[OrderLine]
) -> tuple[list[OrderLine], list[Charge]]:
	"""A tax-inclusive order's (``taxIncludedInPrices``) taxes: each line's an included tax at the line's
	own rates; the shipping option and each additional fee at their prices before tax, each followed by its
	taxes at their rates."""
	amounts: dict[str, Decimal] = {}
	rates: list[LineRates] = []
	for line in line_nodes:
		line_rates: LineRates = {}
		for title, rate, amount in tax_rates(line.child("taxInfo")):
			amounts[title] = amounts.get(title, Decimal(0)) + amount
			line_rates[title] = line_rates.get(title, Decimal(0)) + rate
		rates.append(line_rates)
	lines, included = included_taxes(lines, rates, amounts)
	found: list[Charge] = []
	if shipping_info is not None:
		cost = shipping_info.child("cost")
		taxes = tax_rates(cost.child("taxInfo") if cost else None)
		net = money_of(cost, "price") - sum((amount for _, _, amount in taxes), Decimal(0))
		found += taxed_charge(
			Charge(SHIPPING, shipping_info.optional_string("title") or "Shipping", net), taxes
		)
	for fee in order.children("additionalFees"):
		name = fee.optional_string("name") or "Fee"
		taxes = tax_rates(fee.child("taxInfo"))
		before_tax = money(fee.child("priceBeforeTax"))
		net = (
			before_tax
			if before_tax is not None
			else money_of(fee, "price") - sum((amount for _, _, amount in taxes), Decimal(0))
		)
		found += taxed_charge(Charge(FEE, name, net), taxes)
	return lines, [*included, *found]


def charges(order: Node, shipping_info: Node | None) -> list[Charge]:
	"""Each tax summed by name over the lines, the shipping and the fees (the order's tax total when nothing
	itemises it), the shipping option at its price before discounts, and the additional fees."""
	cost = shipping_info.child("cost") if shipping_info else None
	fees = order.children("additionalFees")
	taxes: dict[str, Decimal] = {}
	for tax_info in [
		*(line.child("taxInfo") for line in order.children("lineItems")),
		cost.child("taxInfo") if cost else None,
		*(fee.child("taxInfo") for fee in fees),
	]:
		for title, amount in tax_amounts(tax_info):
			taxes[title] = taxes.get(title, Decimal(0)) + amount
	summary_tax = money_of(order.required_child("priceSummary"), "tax")
	if not taxes and summary_tax:
		taxes["Tax"] = summary_tax
	found = [Charge(TAX, title, amount) for title, amount in taxes.items()]
	if shipping_info is not None:
		found.append(
			Charge(SHIPPING, shipping_info.optional_string("title") or "Shipping", money_of(cost, "price"))
		)
	for fee in fees:
		before_tax = money(fee.child("priceBeforeTax"))
		found.append(
			Charge(
				FEE,
				fee.optional_string("name") or "Fee",
				before_tax if before_tax is not None else money_of(fee, "price"),
			)
		)
	return [charge for charge in found if charge.amount != 0]


def payment_method(payment: Node) -> str:
	"""A payment's method as the store owner named it, else in the site's (or the buyer's) language."""
	if payment.child("giftcardPaymentDetails") is not None:
		return "Gift card"
	if payment.child("membershipPaymentDetails") is not None:
		return "Membership"
	regular = payment.child("regularPaymentDetails")
	name = regular.child("paymentMethodName") if regular else None
	user_defined = name.child("userDefinedName") if name else None
	return (
		(
			user_defined.optional_string("custom") or user_defined.optional_string("predefined")
			if user_defined
			else None
		)
		or (
			name.optional_string("siteLanguageName") or name.optional_string("buyerLanguageName")
			if name
			else None
		)
		or "Wix"
	)


def payment_reference(payment: Node) -> str | None:
	regular = payment.child("regularPaymentDetails")
	if regular is None:
		return None
	return regular.optional_string("providerTransactionId") or regular.optional_string("gatewayTransactionId")


class BlogMapper:
	"""The site's blog ("Blog" at /blog: posts in no category belong to it) and each blog category → a Blog
	Category with its label, slug and description; a category's old page redirects to it."""

	kind = Kind.BLOG
	version = 1

	def map(self, node: Node) -> Desired | None:
		if "/Blog/" in node.id():
			return Desired(
				self.kind,
				node.id(),
				{"title": BLOG_NAME, "slug": BLOG_PATH.lstrip("/"), "description": None},
				(BLOG_PATH,),
			)
		url = node.child("url")
		path = path_of(url.optional_string("path") if url else None)
		fields: Fields = {
			"title": node.string("label"),
			"slug": node.string("slug"),
			"description": node.optional_string("description"),
		}
		return Desired(self.kind, node.id(), fields, (path,) if path else ())


class ArticleMapper:
	"""A published blog post → a Blog Post in its first category (else the site's blog): its rich content as
	HTML, excerpt, hashtags as tags and first publication date; its old URL redirects to it. Wix names the
	writer by member id only, so posts are by one "Wix" blogger."""

	kind = Kind.ARTICLE
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		categories = node.strings("categoryIds")
		body = rich_html(node.child("richContent"))
		url = node.child("url")
		path = path_of(url.optional_string("path") if url else None)
		fields: Fields = {
			"title": node.string("title"),
			"slug": node.string("slug"),
			"category": self._context.gid("BlogCategory", categories[0])
			if categories
			else self._context.gid("Blog", node.string("blog_id")),
			"author": {"key": "wix", "name": "Wix"},
			"published": True,
			"published_on": node.time("firstPublishedDate")[:10],
			"html": body,
			"intro": node.optional_string("excerpt"),
			"images": self._context.hosted(body),
			"tags": list[Field](node.strings("hashtags")),
		}
		return Desired(self.kind, node.id(), fields, (path,) if path else ())


class ReviewMapper:
	"""A published Wix Stores product review (approved, or published without moderation) → an Item Review;
	reviews in moderation or rejected aren't mirrored."""

	kind = Kind.REVIEW
	version = 1

	def __init__(self, context: Context) -> None:
		self._context = context

	def map(self, node: Node) -> Desired | None:
		moderation = node.child("moderation")
		if (moderation.optional_string("moderationStatus") if moderation else None) not in PUBLISHED_REVIEWS:
			return None
		content = node.required_child("content")
		title, body = content.optional_string("title"), content.text("body")
		author = node.child("author")
		fields: Fields = {
			"product": self._context.gid("Product", node.string("entityId")),
			"author_name": (author.optional_string("authorName") if author else None) or "Customer",
			"author_email": None,
			"text": f"{title}\n\n{body}" if title else body,
			"rating": rating_fraction(content.integer("rating"), MAX_RATING),
			"published_on": (node.optional_time("reviewDate") or node.time("createdDate"))[:10],
		}
		return Desired(self.kind, node.id(), fields)


class RedirectMapper:
	"""A site redirect → a Website Route Redirect: to a full URL as it is, to a site path (which redirects on
	to wherever its object was mirrored). A group redirect (every path under its source) is Unsupported."""

	kind = Kind.URL_REDIRECT
	version = 1

	def __init__(self, store_host: str | None) -> None:
		self._store_host = store_host

	def map(self, node: Node) -> Desired | None:
		options = node.child("options")
		if options is not None and options.get("groupRedirect") is True:
			raise Unsupported(
				"a redirect of every path under its source; Website Route Redirects match exact paths"
			)
		source = path_of(node.string("from"))
		target = redirect_target(node.string("to"), self._store_host)
		if source is None or target is None:
			return None
		return Desired(self.kind, node.id(), {"source": source, "target": target, "status": 301})


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		CollectionMapper(context),
		ProductMapper(context),
		ContactMapper(),
		CouponMapper(context),
		OrderMapper(context),
		BlogMapper(),
		ArticleMapper(context),
		ReviewMapper(context),
		RedirectMapper(urlsplit(config.store_url.strip()).hostname),
	]
