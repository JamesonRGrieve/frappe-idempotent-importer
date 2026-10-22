# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Webflow → desired ERPNext state: assets as Files, e-commerce categories as Item Groups, products as
Items (a template with a variant per SKU when the product has options), the customers orders name as
Customers, orders as invoices with their payment, refund and shipment, pages as Web Pages with their content,
CMS collections as Blog Categories and their items as Blog Posts, and redirects as Website Route Redirects.

Field meanings are Webflow's OpenAPI document's (webflow/openapi-spec, openapi/v2.yml). Money is in the
currency's smallest unit (``value``, with its ``unit``: 1999 USD is 19.99). A CMS item's body is its rich text
fields' HTML in the collection's field order, its images its image fields' (and those its body embeds from
Webflow's hosts), its introduction its first plain text field; every other field is kept in its Source
Fields, by name (an option as its name, a switch as Yes or No). What ERPNext can't hold exactly is
Unsupported, naming the card that plans it."""

from __future__ import annotations

import html
import json
from decimal import Decimal
from urllib.parse import urlsplit

from idempotent_importer.contract import (
	SHIPPING,
	TAX,
	Address,
	Charge,
	OrderLine,
	Party,
	Shipment,
	address_fields,
	charge_fields,
	contents_of,
	line_fields,
	order_line,
	party_fields,
	refund_balance,
	shipment_fields,
	variant_attributes,
	with_custom,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_text, from_minor_units
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.paths import path_of, redirect_target, slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.html import embedded_images, hosted_on
from idempotent_importer.sources.ledger import country_code

PLANNED = "planned: card 386"
# Webflow's asset and CMS file hosts.
MEDIA_HOSTS = (
	"cdn.prod.website-files.com",
	"uploads-ssl.webflow.com",
	"assets.website-files.com",
	"assets-global.website-files.com",
)
PRODUCT_PATH = "/product/"
CATEGORY_PATH = "/category/"
SUBSCRIPTION = "subscription"
FINITE = "finite"
NOT_SALES = frozenset({"pending"})
REFUNDED = frozenset({"refunded", "dispute-lost"})
DISCOUNTS = frozenset({"discount", "discount-shipping"})
BODY_TYPES = frozenset({"RichText"})
IMAGE_TYPES = frozenset({"Image", "MultiImage"})
# The product fields the mapper reads itself; the rest are the site's own, kept by name.
PRODUCT_FIELDS = frozenset(
	{
		"name",
		"slug",
		"description",
		"shippable",
		"sku-properties",
		"category",
		"tax-category",
		"default-sku",
		"ec-product-type",
	}
)
NAME_SLUG = frozenset({"name", "slug"})
AUTHOR = {"key": "webflow", "name": "Webflow"}


class Context:
	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config
		self.store_host = urlsplit(config.store_url).hostname

	def gid(self, type_: str, id_: str) -> str:
		return self.config.identity(type_, id_)

	def hosted(self, fragment: str) -> list[str]:
		"""The images an HTML fragment embeds from Webflow's hosts or the site itself."""
		return list(embedded_images(fragment, hosted_on(*MEDIA_HOSTS, self.store_host or "")))


def money(node: Node | None) -> Decimal | None:
	"""A Webflow price (``value`` in the currency's smallest unit, its ``unit`` the currency)."""
	if node is None or not node.has("value"):
		return None
	value = node.decimal("value")
	if value != value.to_integral_value():
		raise Unsupported(f"a price of {decimal_text(value)} smallest units")
	currency = node.optional_string("unit") or node.string("currency")
	return from_minor_units(int(value), currency)


def currency_of(node: Node | None) -> str | None:
	return (node.optional_string("unit") or node.optional_string("currency")) if node else None


def image_url(value: JsonValue) -> str | None:
	"""An image field's URL (an image object, or the URL itself)."""
	if isinstance(value, dict):
		url = value.get("url")
		return url if isinstance(url, str) and url else None
	return value if isinstance(value, str) and value else None


def shown(field: Node | None, value: JsonValue) -> str | None:
	"""A field's value as text: an option as its name, a switch as Yes or No, an image or file as its URL, a
	number exactly as sent, a list joined with "; "."""
	if value is None or value in ("", []):
		return None
	kind = field.optional_string("type") if field else None
	if kind == "Option" and isinstance(value, str):
		validations = field.child("validations") if field else None
		options = (
			{o.string("id"): o.string("name") for o in validations.children("options")} if validations else {}
		)
		return options.get(value, value)
	if isinstance(value, bool):
		return "Yes" if value else "No"
	if isinstance(value, int | float):
		return decimal_text(Decimal(repr(value)) if isinstance(value, float) else Decimal(value))
	if isinstance(value, list):
		return "; ".join(text for item in value if (text := shown(field, item)))
	if isinstance(value, dict):
		return image_url(value) or json.dumps(value, sort_keys=True)
	return str(value)


def custom_values(fields: list[Node], data: Node, own: frozenset[str]) -> dict[str, str]:
	"""The fields the site added, by name: every field of ``data`` the mapper doesn't read itself."""
	by_slug = {field.optional_string("slug") or "": field for field in fields}
	found: dict[str, str] = {}
	for slug, value in data.items():
		if slug in own:
			continue
		field = by_slug.get(slug)
		text = shown(field, value)
		if text is not None:
			found[field.string("displayName") if field else slug] = text
	return found


class AssetMapper:
	"""An asset (an image or file the site holds) as a public File."""

	kind = Kind.ASSET
	version = 1

	def map(self, node: Node) -> Desired | None:
		return Desired(
			self.kind, node.id(), {"url": node.string("hostedUrl"), "alt": node.optional_string("altText")}
		)


class CategoryMapper:
	"""An e-commerce category as an Item Group, its old page redirecting to it; a draft or archived one isn't
	kept."""

	kind = Kind.COLLECTION
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("isArchived") or node.optional_flag("isDraft"):
			return None
		data = node.required_child("fieldData")
		slug = data.string("slug")
		parent = data.optional_string("parent-category")
		fields: Fields = {
			"name": data.string("name"),
			"slug": slug,
			"parent": self.context.gid("Category", parent) if parent else None,
			"description": data.optional_string("description"),
			"image": image_url(data.get("main-image")),
		}
		return Desired(self.kind, node.id(), fields, (f"{CATEGORY_PATH}{slug}",))


class ProductMapper:
	"""A product as an Item: one SKU without options; else a template with a variant per SKU, its options
	from the product's SKU properties. A compare-at price above the price is the price and the price the
	sale; stock is counted where the SKU's inventory is finite. Drafts are unpublished, archived products
	aren't kept; a subscription is a subscription plan (planned)."""

	kind = Kind.PRODUCT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("isArchived"):
			return None
		data = node.required_child("fieldData")
		properties = {
			prop.string("id"): (
				prop.string("name"),
				{choice.string("id"): choice.string("name") for choice in prop.children("enum")},
			)
			for prop in data.children("sku-properties")
		}
		skus = node.children("skus")
		if not skus:
			raise Unsupported("a product with no SKU")
		simple = not properties
		variants = [self._variant(sku, properties) for sku in skus]
		first = variants[0]
		slug = data.string("slug")
		shippable = data.optional_flag("shippable") is not False
		fields: Fields = {
			"type": "simple" if simple else "variable",
			"name": data.string("name"),
			"slug": slug,
			"description": data.optional_string("description"),
			"short_description": None,
			"sku": first["sku"] if simple else None,
			"barcode": None,
			"brand": None,
			"collections": [self.context.gid("Category", c) for c in data.strings("category")],
			"images": self._images(variants, data),
			"published": node.optional_flag("isDraft") is not True,
			"is_stock_item": shippable and any(v["stock_qty"] is not None for v in variants),
			"weight": None,
			"price": first["price"] if simple else None,
			"sale": first["sale"] if simple else None,
			"stock_qty": first["stock_qty"] if simple else None,
			"attributes": [] if simple else variant_attributes(variants),
			"specifications": [],
			"variants": [] if simple else list[Field](variants),
			"tags": [],
		}
		custom = custom_values(node.children("fields"), data, PRODUCT_FIELDS)
		return Desired(self.kind, node.id(), with_custom(fields, custom), (f"{PRODUCT_PATH}{slug}",))

	@staticmethod
	def _images(variants: list[Fields], data: Node) -> list[Field]:
		urls = list(dict.fromkeys(str(v["image"]) for v in variants if v["image"]))
		return [{"url": url, "alt": data.string("name")} for url in urls]

	def _variant(self, sku: Node, properties: dict[str, tuple[str, dict[str, str]]]) -> Fields:
		data = sku.required_child("fieldData")
		if data.optional_string("ec-sku-billing-method") == SUBSCRIPTION:
			raise Unsupported(f"a subscription SKU is a subscription plan ({PLANNED})")
		price = money(data.child("price"))
		compare_at = money(data.child("compare-at-price"))
		on_sale = compare_at is not None and price is not None and compare_at > price
		chosen = data.child("sku-values")
		attributes: dict[str, Field] = {}
		for prop_id, choice in chosen.items() if chosen else []:
			name, choices = properties.get(prop_id, (prop_id, {}))
			attributes[name] = choices.get(str(choice), str(choice))
		inventory = sku.child("inventory")
		finite = inventory is not None and inventory.optional_string("inventoryType") == FINITE
		return {
			"gid": self.context.gid("Sku", sku.string("id")),
			"sku": data.optional_string("sku"),
			"barcode": None,
			"attributes": attributes,
			"price": compare_at if on_sale else price,
			"sale": {"price": price, "from": None, "to": None} if on_sale else None,
			"stock_qty": inventory.decimal("quantity") if finite and inventory else None,
			"weight": None,
			"image": image_url(data.get("main-image")),
			"enabled": True,
		}


def address_of(node: Node | None, address_type: str) -> Fields | None:
	if node is None:
		return None
	line1, city = node.optional_string("line1"), node.optional_string("city")
	if not (line1 and city):
		return None
	return address_fields(
		Address(
			address_type,
			node.optional_string("addressee"),
			None,
			line1,
			node.optional_string("line2"),
			city,
			node.optional_string("state"),
			node.optional_string("postalCode"),
			country_code(node.optional_string("country")),
			None,
			None,
		)
	)


class CustomerMapper:
	"""A customer an order names (Webflow keeps no customer record): their name, email and billing address."""

	kind = Kind.CUSTOMER
	version = 1

	def map(self, node: Node) -> Desired | None:
		email = node.string("email")
		name = node.optional_string("fullName") or email
		address = address_of(node.child("billingAddress"), "Billing")
		first, _, last = name.partition(" ")
		party = Party(
			name=name,
			type="Individual",
			first_name=first,
			last_name=last or None,
			email=email,
			addresses=[address] if address else [],
		)
		return Desired(self.kind, node.id(), party_fields(party))


class OrderMapper:
	"""An order as an invoice: its items at their row totals, its discounts (on the order and on shipping)
	as its discount, shipping and tax as charges, which must make its total; a pending order is a draft, any
	other submitted with its payment. A refunded order (or a dispute lost) is refunded in full; a fulfilled one
	has its shipment. The customer's comment is its remarks."""

	kind = Kind.ORDER
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		status = node.string("status")
		totals = node.required_child("totals")
		total_node = totals.required_child("total")
		currency = currency_of(total_node) or ""
		order_id = node.string("source_id")
		submitted = status not in NOT_SALES
		lines = [self._line(order_id, item, currency) for item in node.children("purchasedItems")]
		discount = Decimal(0)
		charges: list[Charge] = []
		for extra in totals.children("extras"):
			amount = money(extra.child("price")) or Decimal(0)
			kind = extra.string("type")
			if kind in DISCOUNTS:
				discount += abs(amount)
			elif kind == "shipping":
				charges.append(Charge(SHIPPING, extra.optional_string("name") or "Shipping", amount))
			elif kind == "tax":
				charges.append(Charge(TAX, extra.optional_string("name") or "Tax", amount))
			else:
				raise Unsupported(f'an order total of type "{kind}"')
		info = node.child("customerInfo")
		email = (info.optional_string("email") if info else None) or ""
		accepted = node.time("acceptedOn")
		refunds: list[Field] = []
		if submitted and status in REFUNDED:
			refunds.append(self._refund(node, currency, accepted))
		fields: Fields = {
			"number": order_id,
			"currency": currency,
			"created_at": accepted,
			"submitted": submitted,
			"customer": self.context.gid("Customer", email.lower()) if email and "/" not in email else None,
			"guest": None
			if email
			else {"name": info.optional_string("fullName") if info else None, "email": None, "phone": None},
			"billing": address_of(node.child("billingAddress"), "Billing"),
			"shipping": address_of(node.child("shippingAddress"), "Shipping"),
			"lines": [line_fields(line) for line in lines],
			"discount": discount,
			"charges": [charge_fields(charge) for charge in charges if charge.amount],
			"total": money(total_node) or Decimal(0),
			"payment": {"method": self._method(node), "reference": self._reference(node), "date": accepted}
			if submitted
			else None,
			"refunds": refunds,
			"notes": self._notes(node, accepted),
			"shipments": [self._shipment(node, lines)]
			if node.has("fulfilledOn") or node.has("shippingTracking")
			else [],
			"remarks": node.optional_string("comment"),
		}
		return Desired(self.kind, node.id(), fields)

	def _line(self, order_id: str, item: Node, currency: str) -> OrderLine:
		variant = item.optional_string("variantId")
		product = item.optional_string("productId")
		name = item.optional_string("productName") or "Item"
		variant_name = item.optional_string("variantName")
		return order_line(
			self.context.gid("OrderItem", f"{order_id}:{variant or name}"),
			self.context.gid("Product", product) if product else None,
			self.context.gid("Sku", variant) if variant else None,
			f"{name} ({variant_name})" if variant_name and variant_name != name else name,
			item.decimal("count"),
			money(item.child("rowTotal")) or Decimal(0),
			currency,
		)

	def _refund(self, node: Node, currency: str, accepted: str) -> Field:
		"""A refund (or a dispute lost) of what the customer paid, as one adjustment."""
		gid = self.context.gid("Refund", node.string("source_id"))
		paid = money(node.child("customerPaid")) or Decimal(0)
		stripe = node.child("stripeDetails")
		balance = refund_balance(f"{gid}#adjustment", paid, [], [], currency)
		return {
			"gid": gid,
			"date": node.optional_time("refundedOn") or node.optional_time("disputeUpdatedOn") or accepted,
			"reason": stripe.optional_string("refundReason") if stripe else None,
			"amount": paid,
			"lines": [line_fields(balance)] if balance else [],
			"charges": [],
		}

	def _notes(self, node: Node, accepted: str) -> list[Field]:
		note = node.optional_string("orderComment")
		if not note:
			return []
		return [
			{
				"gid": self.context.gid("OrderComment", node.string("source_id")),
				"date": accepted,
				"author": "Webflow",
				"text": note,
				"customer_note": False,
			}
		]

	def _shipment(self, node: Node, lines: list[OrderLine]) -> Field:
		return shipment_fields(
			Shipment(
				gid=self.context.gid("Shipment", node.string("source_id")),
				date=node.optional_time("fulfilledOn") or node.time("acceptedOn"),
				carrier=node.optional_string("shippingProvider"),
				service=None,
				tracking_number=node.optional_string("shippingTracking"),
				tracking_url=node.optional_string("shippingTrackingURL"),
				delivered=False,
				contents=contents_of([(line.name, line.qty) for line in lines]),
			)
		)

	@staticmethod
	def _method(node: Node) -> str:
		card = node.child("stripeCard")
		brand = card.optional_string("brand") if card else None
		return brand or node.optional_string("paymentProcessor") or "Webflow"

	@staticmethod
	def _reference(node: Node) -> str | None:
		stripe, paypal = node.child("stripeDetails"), node.child("paypalDetails")
		return (stripe.optional_string("chargeId") if stripe else None) or (
			paypal.optional_string("captureId") if paypal else None
		)


class PageMapper:
	"""A static page as a Web Page: its content (text as its HTML, images from the site's assets) under its
	slug; a draft unpublished. A collection's template page has no content of its own (its items are the
	posts), and an archived page isn't kept; forms on a page are the site's to rebuild (planned)."""

	kind = Kind.PAGE
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("archived") or node.optional_string("collectionId"):
			return None
		assets = node.child("assets")
		parts: list[str] = []
		for content in node.children("nodes"):
			match content.string("type"):
				case "text":
					text = content.required_child("text")
					body = text.optional_string("html") or html.escape(text.optional_string("text") or "")
					if body:
						parts.append(body)
				case "image":
					image = content.required_child("image")
					url = assets.optional_string(image.string("assetId")) if assets else None
					if url:
						alt = html.escape(image.optional_string("alt") or "", quote=True)
						parts.append(f'<img src="{html.escape(url, quote=True)}" alt="{alt}">')
		body = "\n".join(parts)
		title = node.optional_string("title") or node.string("source_id")
		slug = node.optional_string("slug") or "home"
		fields: Fields = {
			"title": title,
			"slug": slug,
			"parent": None,
			"published": node.optional_flag("draft") is not True,
			"html": body,
			"images": list[Field](self.context.hosted(body)),
		}
		legacy = path_of(node.optional_string("publishedPath"))
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else ())


class CollectionMapper:
	"""A CMS collection as a Blog Category: its name and slug."""

	kind = Kind.BLOG
	version = 1

	def map(self, node: Node) -> Desired | None:
		fields: Fields = {
			"title": node.string("displayName"),
			"slug": node.optional_string("slug") or slugify(node.string("displayName")),
			"description": None,
		}
		return Desired(self.kind, node.id(), fields)


class ItemMapper:
	"""A CMS item as a Blog Post in its collection's category: its rich text as its body (and its image
	fields as images), its first plain text field as its introduction, its other fields in its Source Fields;
	a draft unpublished, an archived item not kept. Its old page (/<collection>/<slug>) redirects to it."""

	kind = Kind.ARTICLE
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		if node.optional_flag("isArchived"):
			return None
		data = node.required_child("fieldData")
		fields_def = node.children("fields")
		body: list[str] = []
		images: list[str] = []
		intro: str | None = None
		own = set(NAME_SLUG)
		for field in fields_def:
			slug = field.optional_string("slug") or ""
			value = data.get(slug)
			kind = field.string("type")
			if kind in BODY_TYPES:
				own.add(slug)
				if isinstance(value, str) and value:
					body.append(value)
			elif kind in IMAGE_TYPES:
				own.add(slug)
				found = value if isinstance(value, list) else [value]
				images += [url for item in found if (url := image_url(item))]
			elif kind == "PlainText" and intro is None and slug not in NAME_SLUG and isinstance(value, str):
				own.add(slug)
				intro = value or None
		html_body = "\n".join(
			[*body, *(f'<img src="{html.escape(url, quote=True)}" alt="">' for url in images)]
		)
		slug = data.string("slug")
		collection = node.string("collection")
		published_on = node.optional_time("lastPublished") or node.time("createdOn")
		fields: Fields = {
			"title": data.string("name"),
			"slug": slug,
			"category": self.context.gid("Collection", collection),
			"author": dict(AUTHOR),
			"published": node.optional_flag("isDraft") is not True,
			"published_on": published_on[:10],
			"html": html_body,
			"intro": intro,
			"images": list[Field](dict.fromkeys([*images, *self.context.hosted(html_body)])),
			"tags": [],
		}
		custom = custom_values(fields_def, data, frozenset(own))
		return Desired(self.kind, node.id(), with_custom(fields, custom))


class RedirectMapper:
	"""A site redirect as a Website Route Redirect: to a path on the site (which redirects on to wherever its
	object was mirrored) or to a full URL as it is. A pattern redirect (a regular expression) is Unsupported."""

	kind = Kind.URL_REDIRECT
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context

	def map(self, node: Node) -> Desired | None:
		source_url = node.string("fromUrl")
		if any(mark in source_url for mark in ("(", "*", "%")):
			raise Unsupported("a pattern redirect; Website Route Redirects match exact paths")
		source = path_of(source_url)
		target = redirect_target(node.string("toUrl"), self.context.store_host)
		if source is None or target is None:
			return None
		return Desired(self.kind, node.id(), {"source": source, "target": target, "status": 301})


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		AssetMapper(),
		CategoryMapper(context),
		ProductMapper(context),
		CustomerMapper(),
		OrderMapper(context),
		PageMapper(context),
		CollectionMapper(),
		ItemMapper(context),
		RedirectMapper(context),
	]
