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
             max_uses; optionally automatic, variants, min_qty, max_qty, max_amount, mixed, customers,
             customer_groups, free, other, tiers, conditions ([{kind, card, terms}]: see ``CONDITIONS``),
             held (the cards it awaits), code_limits ({code: {max_uses, valid_upto, conditions, held}}),
             currency (only orders in it; the company's when absent)
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

The books and the business around them (ERP and HR sources; dates are ``YYYY-MM-DD``):

supplier     as a customer (``party_fields``: optionally tax_id, currency, contacts ([{gid, name, email,
             phone, job_title}]) and disabled), without a group
account      number, name, root_type, account_type, parent (identity | None), is_group, currency
cost_center  name, parent (identity | None), is_group, optionally disabled (``tree_fields``)
warehouse    name, parent, is_group, address (address | None)
project      name, status, customer, cost_center (identities | None), start, end, notes
lead         status, first_name, last_name, company, email, phone, mobile, job_title, website, notes,
             optionally disabled
opportunity  party_kind ("lead" | "customer"), party, title, status, date, currency, amount, probability,
             expected_closing, notes
department   name, parent (identity | None), is_group
designation  name
employee     first_name, middle_name, last_name, gender, date_of_birth, date_of_joining, relieving_date,
             status, employee_number, department, designation, reports_to (identities | None),
             company_email, personal_email, phone
sales_order, purchase_order, sales_invoice, purchase_invoice
             number, date, due_date (an order's delivery date), party (customer or supplier identity),
             currency, exchange_rate, lines ([line + account, cost_center, project]), taxes
             ([{description, amount, account}]), total, submitted, is_return, return_against, reference,
             remarks, party_account (identity | None), base_total, return_settles, optionally a
             discount on the net total and a tax row's role ("shipping" | "fee") (``trade_fields``:
             lines and taxes, less the discount, make the total exactly)
payment_entry  payment_type, party_type, party, date, amount, currency, exchange_rate, account,
             party_account, submitted, reference, remarks, allocations ([{kind, document, amount}])
journal_entry  number, date, remarks, submitted, lines ([{account, debit, credit, party_type, party,
             cost_center, project, against ({kind, document} | None)}]: balanced exactly)

Times are UTC ``YYYY-MM-DDTHH:MM:SSZ``; money and quantities are exact Decimals."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from decimal import Decimal

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Field, Fields, Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_text, exact_quotient, minor_units
from idempotent_importer.core.node import JsonValue


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


# The companion cards that enforce what ERPNext's pricing doesn't (the frappe-webshop fork's), and the hold
# of a condition no card covers yet: it waits for the operator to choose its home.
COUPON_LIMITS = "card 405"
DISCOUNT_CAPS = "card 407"
OPERATOR_DECISION = "an operator decision"

# Every discount condition ERPNext's pricing doesn't enforce, and the card that will. A condition's terms are
# the source's, in the contract's vocabulary (identities, Decimals, ISO codes; ``source`` is the source's own
# description of it, as it sent it); the fork reads them:
#   once_per_customer      {}                         one use per customer
#   per_customer_limit     {uses}                     uses per customer
#   shared_use_limit       {uses}                     one use limit across all of a discount's codes, or of a
#                                                     discount with no code
#   emails                 {emails}                   only these customers' email addresses
#   excluded_items         {items?, variants?}        never these products or variants (identities)
#   excluded_groups        {groups}                   never these collections (identities)
#   excluded_sale_items    {}                         never items on sale
#   groups_beside_items    {groups}                   these collections qualify beside the rule's products
#   free_shipping          {max_shipping_price?, methods?, zones?, countries?}  free shipping
#   shipping_discount      {discount, value, cap?, methods?}  a percentage or amount off shipping
#   shipping_limits        {methods?, countries?, source?}  only these shipping methods or destinations
#   schedule               {source}                   only on the source's days and times
#   combination            {exclusive?, stop?, source?}  how it combines with other discounts
#   customer_segments      {segments?}                only customers in these segments
#   excluded_customer_groups {groups}                 never customers of these groups (identities)
#   several_customers      {customers}                only these customers (a code applies one rule)
#   several_customer_groups {groups}                  only customers of these groups (a code applies one rule)
#   customer_history       {min_orders?, new?, returning?}  only new or returning customers
#   cap                    {amount}                   at most this much off
#   spread_amount          {items?, groups?, variants?}  an amount once, spread across only these items
#   item_limit             {qty}                      on at most this many items
#   item_choice            {strategy?, source?}       which of the qualifying items are discounted
#   item_matcher           {source}                   items chosen by the source's other criteria (brands,
#                                                     options, attributes, combinations of criteria)
#   rule_condition         {source}                   a condition beyond spend, quantity and items
#   fixed_price_set        {qty, price, items?, groups?}  so many qualifying items for one price
#   further_rules          {source}                   the source's further rules, after the one written
#   channels               {channels}                 only the source's other sales channels
#   price_list             {source}                   the source's own price list
#   applied_by_staff       {}                         applied by staff at the point of sale, never by itself
CONDITIONS: dict[str, str] = {
	"once_per_customer": COUPON_LIMITS,
	"per_customer_limit": COUPON_LIMITS,
	"shared_use_limit": COUPON_LIMITS,
	"emails": COUPON_LIMITS,
	"excluded_items": COUPON_LIMITS,
	"excluded_groups": COUPON_LIMITS,
	"excluded_sale_items": COUPON_LIMITS,
	"groups_beside_items": COUPON_LIMITS,
	"free_shipping": COUPON_LIMITS,
	"shipping_discount": COUPON_LIMITS,
	"shipping_limits": COUPON_LIMITS,
	"schedule": COUPON_LIMITS,
	"combination": COUPON_LIMITS,
	"customer_segments": DISCOUNT_CAPS,
	"excluded_customer_groups": DISCOUNT_CAPS,
	"several_customers": DISCOUNT_CAPS,
	"several_customer_groups": DISCOUNT_CAPS,
	"customer_history": DISCOUNT_CAPS,
	"cap": DISCOUNT_CAPS,
	"spread_amount": DISCOUNT_CAPS,
	"item_limit": DISCOUNT_CAPS,
	"item_choice": DISCOUNT_CAPS,
	"item_matcher": DISCOUNT_CAPS,
	"rule_condition": DISCOUNT_CAPS,
	"fixed_price_set": DISCOUNT_CAPS,
	"further_rules": OPERATOR_DECISION,
	"channels": OPERATOR_DECISION,
	"price_list": OPERATOR_DECISION,
	"applied_by_staff": OPERATOR_DECISION,
}
# The conditions the Webshop fork enforces. A discount with any other condition is held: written disabled,
# its codes not redeemable. When the fork enforces a condition, add it here; the next run enables the
# discounts it held.
ENFORCED: frozenset[str] = frozenset()


def source_terms(value: JsonValue) -> Field:
	"""A source's own description of a condition (a JSON value) as recorded terms: numbers that aren't whole
	are exact Decimals, as the source wrote them."""
	if isinstance(value, float):
		return Decimal(str(value))
	if isinstance(value, list):
		return [source_terms(item) for item in value]
	if isinstance(value, dict):
		return {key: source_terms(item) for key, item in value.items()}
	return value


@dataclass(frozen=True)
class Condition:
	"""A condition of a source's discount ERPNext's pricing doesn't enforce, recorded on the discount for
	the companion card that will (``CONDITIONS``): its kind and the source's terms."""

	kind: str
	terms: Fields = field(default_factory=dict)

	def __post_init__(self) -> None:
		if self.kind not in CONDITIONS:
			raise ValueError(f"{self.kind} is not a discount condition")

	@property
	def card(self) -> str:
		return CONDITIONS[self.kind]


def condition_fields(conditions: list[Condition]) -> list[Field]:
	return [
		{"kind": c.kind, "card": c.card, "terms": {k: v for k, v in c.terms.items() if v is not None}}
		for c in conditions
	]


def held_for(conditions: list[Condition]) -> list[str]:
	"""The cards a discount (or code) with these conditions is held for: those of the conditions the fork
	doesn't enforce yet."""
	return sorted({c.card for c in conditions if c.kind not in ENFORCED})


def hold_label(cards: list[str]) -> str:
	return f"held: awaiting enforcement ({', '.join(cards)})"


def conditions_json(conditions: list[Values]) -> str | None:
	"""Recorded conditions as the record keeps them for the fork: JSON, keys sorted, amounts as decimal
	strings; None when there are none."""
	if not conditions:
		return None
	return json.dumps([c.all() for c in conditions], default=decimal_text, sort_keys=True)


def conditions_summary(conditions: list[Values]) -> str:
	"""The conditions a held discount awaits, for its warning: "once_per_customer (card 405), cap (card 407)"."""
	return ", ".join(
		f"{c.string('kind')} ({c.string('card')})" for c in conditions if c.string("kind") not in ENFORCED
	)


@dataclass(frozen=True)
class Code:
	"""What one code of a discount holds of its own, where the source gives a code its own limits: its
	use limit and dates (enforced by its Coupon Code), and conditions (recorded, and holding that code)."""

	max_uses: int | None = None
	valid_upto: str | None = None
	conditions: list[Condition] = field(default_factory=list)
	valid_from: str | None = None


def code_fields(code: Code) -> Fields:
	fields: Fields = {
		"max_uses": code.max_uses,
		"valid_from": code.valid_from,
		"valid_upto": code.valid_upto,
		"conditions": condition_fields(code.conditions) or None,
		"held": list[Field](held_for(code.conditions)) or None,
	}
	return {key: value for key, value in fields.items() if value is not None}


@dataclass(frozen=True)
class Discount:
	"""A discount as ERPNext's pricing holds it: codes (none when automatic); a percentage, an amount or a
	fixed rate (``discount``: "percentage" | "amount" | "rate") off the transaction, some products (and
	single ``variants``, as (product, variant) identities), some collections, or every item
	(``applies``: "transaction" | "items" | "groups" | "all_items"), or an item given (``free``); with its
	minimum and maximum quantity (across the items together when ``mixed``) and spend, dates and use limit
	(of each code); for some ``customers`` or ``customer_groups`` (identities); on other items than those
	bought (``other``: ("items" | "groups", identity)); in ``tiers``; a code's own limits in ``code_limits``;
	only in one ``currency`` (the company's when None).
	What the source's discount holds beyond that is in ``conditions``: recorded on the discount, which is
	held (disabled, its codes not redeemable) until the fork enforces each of them."""

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
	conditions: list[Condition] = field(default_factory=list)
	code_limits: dict[str, Code] = field(default_factory=dict)
	currency: str | None = None


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
		"conditions": condition_fields(discount.conditions) or None,
		"held": list[Field](held_for(discount.conditions)) or None,
		"code_limits": {
			code: own for code, limits in discount.code_limits.items() if (own := code_fields(limits))
		}
		or None,
		"currency": discount.currency,
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
	return Desired(Kind.DISCOUNT, gid, discount_fields(discount))


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


# -- The books and the business around them (ERP and HR sources) ---------------------------------------

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ROOT_TYPES = frozenset({"Asset", "Liability", "Equity", "Income", "Expense"})
CUSTOMER_PARTY, SUPPLIER_PARTY, EMPLOYEE_PARTY = "Customer", "Supplier", "Employee"
# The party types a journal line or a payment can name, and the kind each is mirrored as.
PARTY_KINDS: dict[str, Kind] = {
	CUSTOMER_PARTY: Kind.CUSTOMER,
	SUPPLIER_PARTY: Kind.SUPPLIER,
	EMPLOYEE_PARTY: Kind.EMPLOYEE,
}
RECEIVE, PAY = "Receive", "Pay"
PROJECT_STATUSES = frozenset({"Open", "On hold", "Completed", "Cancelled"})
LEAD_STATUSES = frozenset(
	{
		"Lead",
		"Open",
		"Replied",
		"Opportunity",
		"Quotation",
		"Lost Quotation",
		"Interested",
		"Converted",
		"Do Not Contact",
	}
)
OPPORTUNITY_STATUSES = frozenset({"Open", "Quotation", "Converted", "Lost", "Replied", "Closed"})
EMPLOYEE_STATUSES = frozenset({"Active", "Inactive", "Suspended", "Left"})
# ERPNext's own Gender records (setup's install fixtures).
GENDERS = frozenset(
	{"Male", "Female", "Transgender", "Genderqueer", "Non-Conforming", "Prefer not to say", "Other"}
)


def checked_date(value: str | None, what: str) -> str | None:
	"""A ``YYYY-MM-DD`` date, or None; anything else is a mapper's mistake."""
	if value is not None and not DATE.match(value):
		raise ValueError(f"{what} is not a YYYY-MM-DD date: {value!r}")
	return value


def required_date(value: str | None, what: str) -> str:
	checked = checked_date(value, what)
	if checked is None:
		raise ValueError(f"{what} is missing")
	return checked


def one_of(value: str, allowed: frozenset[str], what: str) -> str:
	if value not in allowed:
		raise ValueError(f"{what} {value!r} is not one of {sorted(allowed)}")
	return value


@dataclass(frozen=True)
class ContactPerson:
	"""One of a party's people (beyond its main contact): their identity, name, email, phone and job."""

	gid: str
	name: str
	email: str | None = None
	phone: str | None = None
	job_title: str | None = None


@dataclass(frozen=True)
class Party:
	"""A customer or a supplier: its name and type ("Individual" | "Company"), the person to contact,
	its addresses (``address_fields``), and its tax id and currency; its other people (``contacts``); and
	whether the source has archived it (``disabled``: it stays a live record, disabled, so the history that
	names it still books)."""

	name: str
	type: str
	first_name: str | None = None
	last_name: str | None = None
	company: str | None = None
	email: str | None = None
	phone: str | None = None
	addresses: list[Fields] = field(default_factory=list)
	tax_id: str | None = None
	currency: str | None = None
	contacts: list[ContactPerson] = field(default_factory=list)
	disabled: bool = False


def party_fields(party: Party) -> Fields:
	"""The ``customer`` (and ``supplier``) fields; tax id, currency, contacts and ``disabled`` only when
	the source has them, so a customer mapped before they existed keeps its change signal."""
	fields: Fields = {
		"name": party.name,
		"type": one_of(party.type, frozenset({"Individual", "Company"}), "party type"),
		"first_name": party.first_name,
		"last_name": party.last_name,
		"company": party.company,
		"email": party.email,
		"phone": party.phone,
		"addresses": list[Field](party.addresses),
	}
	extra: Fields = {
		"tax_id": party.tax_id,
		"currency": party.currency,
		"contacts": [
			{
				"gid": person.gid,
				"name": person.name,
				"email": person.email,
				"phone": person.phone,
				"job_title": person.job_title,
			}
			for person in party.contacts
		]
		or None,
		"disabled": party.disabled or None,
	}
	return fields | {key: value for key, value in extra.items() if value is not None}


@dataclass(frozen=True)
class LedgerAccount:
	"""An account of the source's chart: its number and name, root type, ERPNext account type (None for an
	ordinary one), parent (an account identity; None for a top-level one, which goes under the company's
	root of its type), whether it is a group, and its currency (None for the company's)."""

	number: str | None
	name: str
	root_type: str
	account_type: str | None = None
	parent: str | None = None
	is_group: bool = False
	currency: str | None = None


def account_fields(account: LedgerAccount) -> Fields:
	return {
		"number": account.number,
		"name": account.name,
		"root_type": one_of(account.root_type, ROOT_TYPES, "root type"),
		"account_type": account.account_type,
		"parent": account.parent,
		"is_group": account.is_group,
		"currency": account.currency,
	}


def tree_fields(name: str, parent: str | None, is_group: bool = False, disabled: bool = False) -> Fields:
	"""A node of a tree the company keeps (a cost center, a warehouse, a department): its name, its parent
	(an identity of the same kind; None for one under the company's root), whether it is a group, and
	whether the source has archived it (only when it has: a disabled node stays live for what names it)."""
	fields: Fields = {"name": name, "parent": parent, "is_group": is_group}
	return fields | {"disabled": True} if disabled else fields


def warehouse_fields(name: str, parent: str | None, is_group: bool, address: Fields | None) -> Fields:
	return tree_fields(name, parent, is_group) | {"address": address}


@dataclass(frozen=True)
class TradeLine:
	"""One line of an order or an invoice: what was sold or bought (``line``, exact), and where it books
	(an account, cost center and project identity, each None for the document's defaults)."""

	line: OrderLine
	account: str | None = None
	cost_center: str | None = None
	project: str | None = None


@dataclass(frozen=True)
class TradeTax:
	"""A tax (or another charge) on a document, as an amount: its description, amount and the account it
	books to (an account identity; None for the connection's account of its ``role``: TAX, or on a sale
	SHIPPING (freight) or FEE)."""

	description: str
	amount: Decimal
	account: str | None = None
	role: str = TAX


@dataclass(frozen=True)
class TradeDocument:
	"""A sales or purchase order or invoice: its number, date (and due or delivery date), party (a customer
	or supplier identity), currency and the rate the source recorded for another than the company's, its
	lines and taxes, and its total, which they must make exactly. ``submitted`` when the source has
	confirmed or posted it; a return (credit or debit note: negative quantities) against the identity it
	returns, when the source names one. ``reference`` is the party's own number (a supplier's bill).
	``party_account`` is the receivable or payable account (an identity) an invoice books its party's
	balance to, when the source keeps its own (else the party's default in ERPNext); in another currency,
	``base_total`` is the total in the company's currency as the source booked it (checked exactly). A return
	against an invoice settles that invoice's balance (``return_settles``), unless the source settled it on
	its own (a refund paid out), when it keeps its own balance."""

	number: str
	date: str
	party: str
	currency: str
	lines: list[TradeLine]
	taxes: list[TradeTax]
	total: Decimal
	submitted: bool
	due_date: str | None = None
	exchange_rate: Decimal | None = None
	is_return: bool = False
	return_against: str | None = None
	reference: str | None = None
	remarks: str | None = None
	party_account: str | None = None
	base_total: Decimal | None = None
	return_settles: bool = True
	discount: Decimal | None = None


def trade_line_fields(line: TradeLine) -> Fields:
	return line_fields(line.line) | {
		"account": line.account,
		"cost_center": line.cost_center,
		"project": line.project,
	}


def trade_tax_fields(tax: TradeTax) -> Fields:
	fields: Fields = {"description": tax.description, "amount": tax.amount, "account": tax.account}
	if tax.role != TAX:
		fields["role"] = one_of(tax.role, frozenset({SHIPPING, FEE}), "charge role")
	return fields


def trade_fields(document: TradeDocument) -> Fields:
	"""A ``sales_order`` / ``purchase_order`` / ``sales_invoice`` / ``purchase_invoice``'s fields. Its lines
	and taxes must add up to its total exactly, or it can't be booked as the source has it (Unsupported)."""
	made = (
		sum((item.line.qty * item.line.rate for item in document.lines), Decimal(0))
		+ sum((tax.amount for tax in document.taxes), Decimal(0))
		- (document.discount or Decimal(0))
	)
	if made != document.total:
		raise Unsupported(
			f"{document.number}'s lines and taxes come to {decimal_text(made)}, not its total "
			f"{decimal_text(document.total)}"
		)
	fields: Fields = {
		"number": document.number,
		"date": required_date(document.date, f"{document.number}'s date"),
		"due_date": checked_date(document.due_date, f"{document.number}'s due date"),
		"party": document.party,
		"currency": document.currency,
		"exchange_rate": document.exchange_rate,
		"lines": [trade_line_fields(line) for line in document.lines],
		"taxes": [trade_tax_fields(tax) for tax in document.taxes],
		"total": document.total,
		"submitted": document.submitted,
		"is_return": document.is_return,
		"return_against": document.return_against,
		"reference": document.reference,
		"remarks": document.remarks,
		"party_account": document.party_account,
		"base_total": document.base_total,
		"return_settles": document.return_settles,
	}
	if document.discount:
		fields["discount"] = document.discount
	return fields


@dataclass(frozen=True)
class Allocation:
	"""Part of a payment settling one document: its kind (``sales_invoice``, ``purchase_invoice``, or a
	storefront ``order``), its identity, and the amount allocated to it."""

	kind: Kind
	document: str
	amount: Decimal


@dataclass(frozen=True)
class Payment:
	"""A payment received from or paid to a party (``payment_type``: "Receive" | "Pay"; ``party_type`` a
	key of PARTY_KINDS), on a date, of an amount in its currency (at the rate the source recorded, for
	another than the company's), into or out of an account (an identity; None for the connection's deposit
	account), allocated to the documents it settles; anything unallocated is an advance. ``party_account``
	is the receivable or payable account it settles on (an identity; None for the party's default).
	``submitted`` once the source has posted it (a draft payment stays a draft)."""

	payment_type: str
	party_type: str
	party: str
	date: str
	amount: Decimal
	currency: str
	allocations: list[Allocation] = field(default_factory=list)
	exchange_rate: Decimal | None = None
	account: str | None = None
	reference: str | None = None
	remarks: str | None = None
	party_account: str | None = None
	submitted: bool = True


def payment_fields(payment: Payment) -> Fields:
	"""A ``payment_entry``'s fields. It can't allocate more than it is, or a negative amount."""
	allocated = sum((allocation.amount for allocation in payment.allocations), Decimal(0))
	if payment.amount <= 0 or allocated > payment.amount or any(a.amount <= 0 for a in payment.allocations):
		raise Unsupported(
			f"a payment of {decimal_text(payment.amount)} allocated {decimal_text(allocated)} can't be booked"
		)
	return {
		"payment_type": one_of(payment.payment_type, frozenset({RECEIVE, PAY}), "payment type"),
		"party_type": one_of(payment.party_type, frozenset(PARTY_KINDS), "party type"),
		"party": payment.party,
		"date": required_date(payment.date, "the payment's date"),
		"amount": payment.amount,
		"currency": payment.currency,
		"exchange_rate": payment.exchange_rate,
		"account": payment.account,
		"party_account": payment.party_account,
		"submitted": payment.submitted,
		"reference": payment.reference,
		"remarks": payment.remarks,
		"allocations": [
			{"kind": allocation.kind.value, "document": allocation.document, "amount": allocation.amount}
			for allocation in payment.allocations
		],
	}


@dataclass(frozen=True)
class JournalLine:
	"""One line of a journal entry: an account identity, its debit or credit, and the party, cost center
	and project (identities) it is for; on a receivable or payable account, the invoice it settles
	(``against``: its kind and identity), when the source reconciled it with one."""

	account: str
	debit: Decimal
	credit: Decimal
	party_type: str | None = None
	party: str | None = None
	cost_center: str | None = None
	project: str | None = None
	against: tuple[Kind, str] | None = None


def journal_fields(
	number: str, date: str, lines: list[JournalLine], submitted: bool, remarks: str | None = None
) -> Fields:
	"""A ``journal_entry``'s fields: it must balance exactly, and every line must be a debit or a credit."""
	debits = sum((line.debit for line in lines), Decimal(0))
	credits = sum((line.credit for line in lines), Decimal(0))
	if not lines or debits != credits:
		raise Unsupported(
			f"journal entry {number} doesn't balance (debits {decimal_text(debits)}, credits "
			f"{decimal_text(credits)})"
		)
	if any(line.debit < 0 or line.credit < 0 or (line.debit and line.credit) for line in lines):
		raise Unsupported(f"journal entry {number} has a line that is both a debit and a credit, or negative")
	for line in lines:
		if line.party_type is not None:
			one_of(line.party_type, frozenset(PARTY_KINDS), "party type")
	return {
		"number": number,
		"date": required_date(date, f"journal entry {number}'s date"),
		"remarks": remarks,
		"submitted": submitted,
		"lines": [
			{
				"account": line.account,
				"debit": line.debit,
				"credit": line.credit,
				"party_type": line.party_type,
				"party": line.party,
				"cost_center": line.cost_center,
				"project": line.project,
				"against": {"kind": line.against[0].value, "document": line.against[1]}
				if line.against
				else None,
			}
			for line in lines
			if line.debit or line.credit
		],
	}


def project_fields(
	name: str,
	status: str,
	customer: str | None = None,
	start: str | None = None,
	end: str | None = None,
	notes: str | None = None,
	cost_center: str | None = None,
) -> Fields:
	"""A ``project``: its name, status (PROJECT_STATUSES), customer, cost center (identities), expected
	start and end dates, and notes."""
	return {
		"name": name,
		"status": one_of(status, PROJECT_STATUSES, "project status"),
		"customer": customer,
		"cost_center": cost_center,
		"start": checked_date(start, f"{name}'s start"),
		"end": checked_date(end, f"{name}'s end"),
		"notes": notes,
	}


@dataclass(frozen=True)
class Lead:
	"""A sales lead: the person and their organisation, how to reach them, and its status
	(LEAD_STATUSES)."""

	status: str
	first_name: str | None = None
	last_name: str | None = None
	company: str | None = None
	email: str | None = None
	phone: str | None = None
	mobile: str | None = None
	job_title: str | None = None
	website: str | None = None
	notes: str | None = None
	disabled: bool = False


def lead_fields(lead: Lead) -> Fields:
	"""A ``lead``'s fields; ``disabled`` (a lead the source has closed, kept for the opportunities that
	name it) only when it is."""
	if not (lead.first_name or lead.last_name or lead.company or lead.email):
		raise Unsupported("a lead with no name, organisation or email")
	fields: Fields = {
		"status": one_of(lead.status, LEAD_STATUSES, "lead status"),
		"first_name": lead.first_name,
		"last_name": lead.last_name,
		"company": lead.company,
		"email": lead.email,
		"phone": lead.phone,
		"mobile": lead.mobile,
		"job_title": lead.job_title,
		"website": lead.website,
		"notes": lead.notes,
	}
	return fields | {"disabled": True} if lead.disabled else fields


@dataclass(frozen=True)
class Opportunity:
	"""A sales opportunity with a lead or a customer (``party_kind`` and its identity): its title, status
	(OPPORTUNITY_STATUSES), date, expected closing, amount in its currency and probability (percent)."""

	party_kind: Kind
	party: str
	title: str
	status: str
	date: str
	currency: str | None = None
	amount: Decimal | None = None
	probability: Decimal | None = None
	expected_closing: str | None = None
	notes: str | None = None


def opportunity_fields(opportunity: Opportunity) -> Fields:
	if opportunity.party_kind not in (Kind.LEAD, Kind.CUSTOMER):
		raise ValueError(f"an opportunity is with a lead or a customer, not a {opportunity.party_kind}")
	return {
		"party_kind": opportunity.party_kind.value,
		"party": opportunity.party,
		"title": opportunity.title,
		"status": one_of(opportunity.status, OPPORTUNITY_STATUSES, "opportunity status"),
		"date": required_date(opportunity.date, f"{opportunity.title}'s date"),
		"currency": opportunity.currency,
		"amount": opportunity.amount,
		"probability": opportunity.probability,
		"expected_closing": checked_date(opportunity.expected_closing, f"{opportunity.title}'s closing"),
		"notes": opportunity.notes,
	}


@dataclass(frozen=True)
class Employee:
	"""A person employed: their names, gender (GENDERS), dates of birth, joining and leaving, status
	(EMPLOYEE_STATUSES), the source's employee number, department and designation and manager
	(identities), and their work and personal email and phone. A date or gender the source doesn't keep is
	None: ERPNext requires them, so the record is saved without them, never with an invented value."""

	first_name: str
	status: str
	last_name: str | None = None
	middle_name: str | None = None
	gender: str | None = None
	date_of_birth: str | None = None
	date_of_joining: str | None = None
	relieving_date: str | None = None
	employee_number: str | None = None
	department: str | None = None
	designation: str | None = None
	reports_to: str | None = None
	company_email: str | None = None
	personal_email: str | None = None
	phone: str | None = None


def employee_fields(employee: Employee) -> Fields:
	return {
		"first_name": employee.first_name,
		"middle_name": employee.middle_name,
		"last_name": employee.last_name,
		"gender": one_of(employee.gender, GENDERS, "gender") if employee.gender else None,
		"date_of_birth": checked_date(employee.date_of_birth, "a date of birth"),
		"date_of_joining": checked_date(employee.date_of_joining, "a date of joining"),
		"relieving_date": checked_date(employee.relieving_date, "a relieving date"),
		"status": one_of(employee.status, EMPLOYEE_STATUSES, "employee status"),
		"employee_number": employee.employee_number,
		"department": employee.department,
		"designation": employee.designation,
		"reports_to": employee.reports_to,
		"company_email": employee.company_email,
		"personal_email": employee.personal_email,
		"phone": employee.phone,
	}
