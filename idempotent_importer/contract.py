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
customer     name, type ("Individual" | "Company"), first_name, last_name, company, email, phone,
             addresses ([address])
discount     codes, description, discount ("percentage" | "amount"), applies ("transaction" | "items" |
             "groups"), value, items, groups (identities), min_amount, valid_from, valid_upto (dates),
             max_uses
order        number, currency, created_at, submitted, customer (identity | None), guest ({name, email,
             phone} | None), billing, shipping (address | None), lines ([line]), discount, charges
             ([charge]), total, payment ({method, reference, date} | None), refunds ([{gid, date, reason,
             amount, lines, charges}]), notes ([{gid, date, author, text, customer_note}]), remarks;
             optionally taxes_included (line prices include the "included_tax" charges), and for an order
             in another currency than the company's, exchange_rate (company currency per order currency)
             and base_total (the total in the company's currency, as the source booked it)
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

from dataclasses import dataclass
from decimal import Decimal

from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Fields, Values
from idempotent_importer.core.money import exact_quotient, minor_units


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
	"""One sold (or, in a refund, returned: negative quantity) line, before order-level discounts."""

	source_line: str
	product: str | None
	variant: str | None
	name: str
	qty: Decimal
	rate: Decimal


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
	return {
		"source_line": line.source_line,
		"product": line.product,
		"variant": line.variant,
		"name": line.name,
		"qty": line.qty,
		"rate": line.rate,
	}


@dataclass(frozen=True)
class Charge:
	"""An order-level amount on top of its lines: a tax, a shipping line or a fee. In an order whose line
	prices include tax, a tax already in them is ``included_tax`` (``rate`` percent of the lines' net
	amount), and a tax on shipping is ``shipping_tax`` (``rate`` percent of the shipping charge just
	before it); either comes to ``amount``, which ERPNext must compute exactly."""

	kind: str
	description: str
	amount: Decimal
	rate: Decimal | None = None


TAX, SHIPPING, FEE = "tax", "shipping", "fee"
INCLUDED_TAX, SHIPPING_TAX = "included_tax", "shipping_tax"


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


def rating_fraction(rating: int, scale: int) -> Decimal:
	"""A review's stars as ERPNext's Rating fraction (4 of 5 → 0.8)."""
	if not 0 <= rating <= scale:
		raise Unsupported(f"a rating of {rating} is outside 0..{scale}")
	return Decimal(rating) / Decimal(scale)
