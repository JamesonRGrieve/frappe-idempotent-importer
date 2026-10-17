# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared field contract between every source's mappers and the ERPNext writers. Mappers translate
their platform's vocabulary into these fields; writers read only these. Per kind (``Desired.fields``):

asset        url, alt (a file of the store's own, as a File)
collection   name, slug, parent (identity | None), description (HTML | None), image (URL | None)
product      type ("simple" | "variable"), name, slug, description, short_description, sku, barcode,
             brand, collections (identities; the first is the Item Group), images ([{url, alt}]),
             published, is_stock_item, weight ({value, unit} | None), price (Decimal | None),
             sale ({price, from, to} | None), stock_qty (Decimal | None), attributes ([{name, values}]),
             specifications ([{label, value}]), variants ([{gid, sku, barcode, attributes {name: value},
             price, sale, stock_qty, weight, image, enabled}]), tags
customer_group  name (a group of customers a source's discounts are for)
customer     name, type ("Individual" | "Company"), first_name, last_name, company, email, phone,
             addresses ([address]); optionally group (customer_group identity | None)
discount     codes, description, discount ("percentage" | "amount"), applies ("transaction" | "items" |
             "groups"), value, items, groups (identities), min_amount, valid_from, valid_upto (dates),
             max_uses
order        number, currency, created_at, submitted, customer (identity | None), guest ({name, email,
             phone} | None), billing, shipping (address | None), lines ([line]), discount, charges
             ([charge]), total, payment ({method, reference, date} | None), refunds ([{gid, date, reason,
             amount, lines, charges}]), notes ([{gid, date, author, text, customer_note}]), remarks,
             shipments ([{gid, date, carrier, service, tracking_number, tracking_url, delivered,
             contents}]: its fulfilments);
             optionally taxes_included (line prices include the "included_tax" charges; when the lines'
             rates differ each line carries its own, taxes ([{title, rate}])), and for an order
             in another currency than the company's, exchange_rate (company currency per order currency,
             as the source recorded it), base_total when the source booked the total in the company's
             currency too (checked exactly), and the source's base_currency when it names it (the writer
             refuses one that isn't the company's)
draft_order  number, currency, created_at, customer, guest, billing, shipping, lines, discount, charges,
             total, remarks, taxes_included (as for an order: a proposal, never booked)
page         title, slug, parent (identity | None), published, html, images (source-hosted URLs it embeds)
blog         title, slug, description
article      title, slug, category (identity), author ({key, name}), published, published_on (date),
             html, intro, images, tags
delivery_profile  name, rules ([{gid, label, countries (ISO codes), amount, enabled}]): flat shipping rates
comment      article (identity), author_name, author_email, html, published_on (date)
review       product (identity), author_name, author_email, text, rating (0..1), published_on (date)
url_redirect source (path), target (path or URL), status (HTTP code)

Times are UTC ``YYYY-MM-DDTHH:MM:SSZ``; money and quantities are exact Decimals."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from decimal import Decimal

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Field, Fields, Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_text, exact_quotient, minor_units


@dataclass(frozen=True)
class Address:
	address_type: str
	name: str | None
	company: str | None
	line1: str
	line2: str | None
	city: str
	state: str | None
	postcode: str | None
	country_code: str | None
	email: str | None
	phone: str | None


def address_fields(address: Address) -> Fields:
	return {
		"address_type": address.address_type,
		"name": address.name,
		"company": address.company,
		"line1": address.line1,
		"line2": address.line2,
		"city": address.city,
		"state": address.state,
		"postcode": address.postcode,
		"country_code": address.country_code.upper() if address.country_code else None,
		"email": address.email,
		"phone": address.phone,
	}


@dataclass(frozen=True)
class OrderLine:
	"""One sold (or, in a refund, returned: negative quantity) line, before order-level discounts. In an
	order whose lines include tax at different rates, ``taxes`` is this line's own rates (percent, by tax
	title; see ``included_taxes``)."""

	source_line: str
	product: str | None
	variant: str | None
	name: str
	qty: Decimal
	rate: Decimal
	taxes: tuple[tuple[str, Decimal], ...] | None = None


def order_line(
	source_line: str,
	product: str | None,
	variant: str | None,
	name: str,
	qty: Decimal,
	amount: Decimal,
	currency: str,
) -> OrderLine:
	"""A line from its quantity and amount. ERPNext holds a line as quantity x rate, so the rate must be
	exact in the currency's minor unit; a line whose amount doesn't divide exactly is Unsupported."""
	rate = exact_quotient(amount, qty, minor_units(currency)) if qty else None
	if rate is None:
		raise Unsupported(f'line "{name}" ({amount} for {qty}) has no exact unit price in {currency}')
	return OrderLine(source_line, product, variant, name, qty, rate)


def line_fields(line: OrderLine) -> Fields:
	fields: Fields = {
		"source_line": line.source_line,
		"product": line.product,
		"variant": line.variant,
		"name": line.name,
		"qty": line.qty,
		"rate": line.rate,
	}
	if line.taxes is not None:
		fields["taxes"] = [{"title": title, "rate": rate} for title, rate in line.taxes]
	return fields


@dataclass(frozen=True)
class Charge:
	"""An order-level amount on top of its lines: a tax, a shipping line or a fee. In an order whose line
	prices include tax, a tax already in them is ``included_tax`` (``rate`` percent of the lines' net
	amount, or no rate when each line carries its own: see ``included_taxes``), and a tax on shipping is
	``shipping_tax`` (``rate`` percent of the shipping charge just before it); either comes to ``amount``,
	which ERPNext must compute exactly."""

	kind: str
	description: str
	amount: Decimal
	rate: Decimal | None = None


TAX, SHIPPING, FEE = "tax", "shipping", "fee"
INCLUDED_TAX, SHIPPING_TAX = "included_tax", "shipping_tax"

type LineRates = dict[str, Decimal]


def included_taxes(
	lines: list[OrderLine], rates: list[LineRates], amounts: dict[str, Decimal]
) -> tuple[list[OrderLine], list[Charge]]:
	"""The taxes included in an order's line prices: each tax (by title) one ``included_tax`` charge of the
	source's amount, ``rates`` being each line's rates (percent, by title). When every line is taxed at the
	same rates, each charge carries its rate and applies to every line. When they differ (a tax-exempt
	line, a reduced rate, a tax only some lines bear) the charges carry none and each line carries its own
	rates, which ERPNext applies to that line alone (an Item Tax Template). A tax that comes to nothing is
	left out, and so is its rate."""
	if len(rates) != len(lines):
		raise ValueError("included_taxes needs one set of rates per line")
	titles = sorted(title for title, amount in amounts.items() if amount != 0)
	own = [{title: rate[title] for title in titles if rate.get(title)} for rate in rates]
	if all(line_rates == own[0] for line_rates in own) and set(own[0] if own else ()) == set(titles):
		common = own[0] if own else {}
		return lines, [Charge(INCLUDED_TAX, title, amounts[title], common[title]) for title in titles]
	taxed = [
		replace(line, taxes=tuple((title, line_rates[title]) for title in titles if title in line_rates))
		for line, line_rates in zip(lines, own, strict=True)
	]
	return taxed, [Charge(INCLUDED_TAX, title, amounts[title]) for title in titles]


def taxed_charge(charge: Charge, taxes: list[tuple[str, Decimal, Decimal]]) -> list[Charge]:
	"""A charge before tax (shipping, a fee) in an order whose prices include tax, then each of its taxes
	(title, rate percent, amount) at its rate of the charge; nothing when the charge is nothing."""
	if charge.amount == 0:
		return []
	return [
		charge,
		*(
			Charge(SHIPPING_TAX, f"{title} ({charge.description})", amount, rate)
			for title, rate, amount in taxes
			if amount
		),
	]


def charge_fields(charge: Charge) -> Fields:
	fields: Fields = {"kind": charge.kind, "description": charge.description, "amount": charge.amount}
	if charge.rate is not None:
		fields["rate"] = charge.rate
	return fields


def order_remarks(values: Values) -> str:
	"""An order's number, customer note, guest and addresses as text (an invoice has no home for a guest's
	details or an order's own addresses)."""
	parts = [f"Order {values.string('number')}"]
	if values.has("remarks"):
		parts.append(f"Note: {values.string('remarks')}")
	guest = values.record("guest")
	if guest is not None:
		contact = ", ".join(
			v
			for v in (
				guest.optional_string("name"),
				guest.optional_string("email"),
				guest.optional_string("phone"),
			)
			if v
		)
		if contact:
			parts.append(f"Guest: {contact}")
	for label in ("billing", "shipping"):
		address = values.record(label)
		if address is not None:
			fields = ("name", "company", "line1", "line2", "city", "state", "postcode", "country_code")
			parts.append(
				f"{label.title()}: " + ", ".join(v for f in fields if (v := address.optional_string(f)))
			)
	return "\n".join(parts)


def booked(fields: Fields) -> Fields:
	"""What the books hold of an invoice or credit note, for its change signal: its lines without the
	products they refer to. A product deleted at the source (which some sources then stop naming on its
	old orders) leaves the lines it sold as they were booked."""
	lines = fields.get("lines")
	if not isinstance(lines, list):
		return fields
	return {
		**fields,
		"lines": [
			{k: v for k, v in line.items() if k not in ("product", "variant")}
			if isinstance(line, dict)
			else line
			for line in lines
		],
	}


@dataclass(frozen=True)
class Shipment:
	"""A fulfilment of an order: when it shipped, by which carrier and service, its tracking number and
	link, whether it was delivered, and what it held ("2 x Toque, 1 x Scarf")."""

	gid: str
	date: str
	carrier: str | None
	service: str | None
	tracking_number: str | None
	tracking_url: str | None
	delivered: bool
	contents: str | None


def shipment_fields(shipment: Shipment) -> Fields:
	return {
		"gid": shipment.gid,
		"date": shipment.date,
		"carrier": shipment.carrier,
		"service": shipment.service,
		"tracking_number": shipment.tracking_number,
		"tracking_url": shipment.tracking_url,
		"delivered": shipment.delivered,
		"contents": shipment.contents,
	}


def contents_of(items: list[tuple[str, Decimal]]) -> str | None:
	"""What a shipment held, from its (name, quantity) items: "2 x Toque, 1 x Scarf"."""
	return ", ".join(f"{decimal_text(qty)} x {name}" for name, qty in items if qty) or None


REFUND_ADJUSTMENT = "Refund adjustment"


def refund_balance(
	key: str, amount: Decimal, lines: list[OrderLine], charges: list[Charge], currency: str
) -> OrderLine | None:
	"""What a refund of ``amount`` returned beyond its returned lines and refunded charges (shipping, a
	goodwill amount) as one adjustment line, so a credit note's lines and charges add up to the refund."""
	returned = sum((line.qty * line.rate for line in lines), Decimal(0))
	charged = sum((charge.amount for charge in charges), Decimal(0))
	beyond = amount + returned + charged
	if beyond == 0:
		return None
	return order_line(key, None, None, REFUND_ADJUSTMENT, Decimal(-1), -beyond, currency)


@dataclass(frozen=True)
class FreeItem:
	"""What a product discount gives: ``qty`` of an item (``product``, with its ``variant``; neither for
	the same item bought) at ``rate`` (free when None), for every ``recurse_for`` bought when recursive."""

	product: str | None
	variant: str | None
	qty: Decimal
	rate: Decimal | None = None
	recursive: bool = False
	recurse_for: Decimal | None = None


@dataclass(frozen=True)
class Tier:
	"""One tier of a tiered discount: from a quantity (or spend) up to the next, its discount."""

	discount: str
	value: Decimal
	min_qty: Decimal | None = None
	max_qty: Decimal | None = None
	min_amount: Decimal | None = None
	max_amount: Decimal | None = None


@dataclass(frozen=True)
class Discount:
	"""A discount as ERPNext's pricing holds it: codes (none when automatic); a percentage, an amount or a
	fixed rate (``discount``: "percentage" | "amount" | "rate") off the transaction, some products (and
	single ``variants``, as (product, variant) identities), some collections, or every item
	(``applies``: "transaction" | "items" | "groups" | "all_items"), or an item given (``free``); with its
	minimum and maximum quantity (across the items together when ``mixed``) and spend, dates and use limit;
	for some ``customers`` or ``customer_groups`` (identities); on other items than those bought
	(``other``: ("items" | "groups", identity)); in ``tiers``. What the source's discount holds beyond
	that is ``planned`` (each a condition and the companion card that enforces it): the rest is written."""

	codes: list[str]
	description: str | None
	discount: str
	applies: str
	value: Decimal
	items: list[str]
	groups: list[str]
	min_amount: Decimal | None
	valid_from: str | None
	valid_upto: str | None
	max_uses: int | None
	automatic: bool = False
	variants: list[tuple[str | None, str]] = field(default_factory=list)
	min_qty: Decimal | None = None
	max_qty: Decimal | None = None
	max_amount: Decimal | None = None
	mixed: bool = False
	customers: list[str] = field(default_factory=list)
	customer_groups: list[str] = field(default_factory=list)
	free: FreeItem | None = None
	other: tuple[str, str] | None = None
	tiers: list[Tier] = field(default_factory=list)
	planned: list[str] = field(default_factory=list)


def discount_fields(discount: Discount) -> Fields:
	"""The discount's fields; those it doesn't use are left out, so a discount mapped before they existed
	keeps its change signal."""
	fields: Fields = {
		"codes": list[Field](discount.codes),
		"description": discount.description,
		"discount": discount.discount,
		"applies": discount.applies,
		"value": discount.value,
		"items": list[Field](discount.items),
		"groups": list[Field](discount.groups),
		"min_amount": discount.min_amount,
		"valid_from": discount.valid_from,
		"valid_upto": discount.valid_upto,
		"max_uses": discount.max_uses,
	}
	extra: Fields = {
		"automatic": discount.automatic or None,
		"variants": [{"product": p, "variant": v} for p, v in discount.variants] or None,
		"min_qty": discount.min_qty,
		"max_qty": discount.max_qty,
		"max_amount": discount.max_amount,
		"mixed": discount.mixed or None,
		"customers": list[Field](discount.customers) or None,
		"customer_groups": list[Field](discount.customer_groups) or None,
		"free": free_fields(discount.free) if discount.free else None,
		"other": {"applies": discount.other[0], "target": discount.other[1]} if discount.other else None,
		"tiers": [tier_fields(tier) for tier in discount.tiers] or None,
		"planned": list[Field](discount.planned) or None,
	}
	return fields | {key: value for key, value in extra.items() if value is not None}


def free_fields(free: FreeItem) -> Fields:
	fields: Fields = {
		"item": free.product,
		"variant": free.variant,
		"qty": free.qty,
		"rate": free.rate,
		"recursive": free.recursive,
		"recurse_for": free.recurse_for,
	}
	return {key: value for key, value in fields.items() if value is not None}


def tier_fields(tier: Tier) -> Fields:
	fields: Fields = {
		"discount": tier.discount,
		"value": tier.value,
		"min_qty": tier.min_qty,
		"max_qty": tier.max_qty,
		"min_amount": tier.min_amount,
		"max_amount": tier.max_amount,
	}
	return {key: value for key, value in fields.items() if value is not None}


def discount_desired(gid: str, discount: Discount) -> Desired:
	"""A discount's desired state; what it holds that is planned companion work is reported as warnings."""
	return Desired(Kind.DISCOUNT, gid, discount_fields(discount), warnings=tuple(discount.planned))


# The companion cards that enforce what ERPNext's pricing doesn't (the frappe-webshop fork's).
COUPON_LIMITS = "card 405"
DISCOUNT_CAPS = "card 407"


def planned(condition: str, card: str) -> str:
	"""A discount condition ERPNext's pricing doesn't enforce, and the companion card that will."""
	return f"{condition} (planned: {card})"


def date_of(time: str | None) -> str | None:
	"""A contract time's date (``YYYY-MM-DD``), or None."""
	return time[:10] if time else None


def variant_attributes(variants: list[Fields]) -> list[Field]:
	"""A product's attributes from its mapped variants: each attribute name once, in the order the variants
	first use it, with its values in the order they first appear."""
	values: dict[str, list[Field]] = {}
	for variant in variants:
		chosen = variant["attributes"]
		if isinstance(chosen, dict):
			for name, value in chosen.items():
				if value not in values.setdefault(name, []):
					values[name].append(value)
	return [{"name": name, "values": taken} for name, taken in values.items()]


def rating_fraction(rating: int, scale: int) -> Decimal:
	"""A review's stars as ERPNext's Rating fraction (4 of 5 → 0.8)."""
	if not 0 <= rating <= scale:
		raise Unsupported(f"a rating of {rating} is outside 0..{scale}")
	return Decimal(rating) / Decimal(scale)
