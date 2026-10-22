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
             total, remarks, taxes_included (as for an order: a proposal, never booked); optionally quoted
             ({kind, party}: a CRM's quote to a lead or a prospect rather than a customer, ``quoted_party``)
page         title, slug, parent (identity | None), published, html, images (source-hosted URLs it embeds)
blog         title, slug, description
article      title, slug, category (identity), author ({key, name}), published, published_on (date),
             html, intro, images, tags
delivery_profile  name, rules ([{gid, label, countries (ISO codes), amount, enabled}]): flat shipping rates
comment      article (identity), author_name, author_email, html, published_on (date)
review       product (identity), author_name, author_email, text, rating (0..1), published_on (date)
url_redirect source (path), target (path or URL), status (HTTP code)
gift_card    kind ("gift_card" | "store_credit"), code (the full code, None when the source hides it),
             code_hint, currency, balance, state ("active" | "disabled" | "expired"), customer (identity |
             None), email, given_name, family_name, ledger ([{gid, at, kind, amount, balance_after,
             reference, note}]: the source's history, oldest first, amounts signed), details (JSON text),
             load_increases, new_numbers (the connection's Square policies, so changing one rewrites
             every card): written to Square Gift Cards (``gift_card_fields``)
loyalty_account  points, customer (identity | None), email, given_name, family_name, ledger ([{gid, at,
             kind, points, note}]), details (JSON text): written to Square Loyalty (``loyalty_fields``)

The books and the business around them (ERP and HR sources; dates are ``YYYY-MM-DD``):

supplier     as a customer (``party_fields``: optionally tax_id, currency, contacts ([{gid, name, email,
             phone, job_title}]) and disabled), without a group
account      number, name, root_type, account_type, parent (identity | None), is_group, currency
cost_center  name, parent (identity | None), is_group, optionally disabled (``tree_fields``)
warehouse    name, parent, is_group, address (address | None)
project      name, status, customer, cost_center (identities | None), start, end, notes
lead         status, first_name, last_name, company, email, phone, mobile, job_title, website, notes,
             optionally disabled, crm_notes ([{gid, date, text}])
opportunity  party_kind ("lead" | "customer" | "prospect"), party, title, status, date, currency, amount,
             probability, expected_closing, notes; optionally opportunity_type (identity), sales_stage (the
             pipeline stage's name)
prospect     name, website, email, phone, addresses ([address]), notes, leads (identities); optionally
             crm_notes (as a lead's; an opportunity's too) and contacts (as a supplier's)
opportunity_type  name, description
communication  with ({kind, party}: a lead, customer, prospect or opportunity), medium, sent, date, subject,
             content, phone, email
event        subject, starts_on, ends_on (YYYY-MM-DD HH:MM:SS on the source's clock, or a UTC time), status,
             description, participants ([{kind, party}])
todo         description, status, date, about ({kind, party})

A product, article, lead, prospect, opportunity, communication, event, todo, customer or supplier optionally carries
``custom`` ({label: text}): the values of the fields the source's users defined themselves, by label.
department   name, parent (identity | None), is_group
designation  name
employee     first_name, middle_name, last_name, gender, date_of_birth, date_of_joining, relieving_date,
             status, employee_number, department, designation, reports_to (identities | None),
             company_email, personal_email, phone; optionally branch (identity)
branch       name (a place the company works from)
leave_type   name, paid
shift_assignment  employee, date, shift ({name, start_time, end_time}: HH:MM:SS on the source's clock),
             status, submitted
employee_checkin  employee, time, log_type ("IN" | "OUT"), device
attendance   employee, date, status, working_minutes, in_time, out_time, leave_type, submitted
leave_application  employee, leave_type, from_date, to_date, status, posting_date, half_day, description
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
timesheet    start (UTC), hours, description, billable, submitted, employee, customer, project (identities |
             None) (``timesheet_fields``): time worked, as a source logged it
tax_rate     name, rate (percent), number (the tax's registration number | None): a sales tax the source
             charges, as a template its documents can apply (``tax_rate_fields``)

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
#   free_shipping          {max_shipping_price?, methods?, zones?, countries?, items?, groups?, variants?}
#                                                     free shipping (only these products' share of it)
#   shipping_discount      {discount, value, cap?, methods?, source?, items?, groups?, variants?}  a percentage
#                                                     or amount off shipping (only these products' share)
#   shipping_limits        {methods?, countries?, source?}  only these shipping methods or destinations
#   schedule               {source}                   only on the source's days and times
#   combination            {exclusive?, stop?, source?}  how it combines with other discounts
#   customer_segments      {customers?, groups?, source?}  only customers in these segments: their members (these
#                                                     customers, and customers of these groups), or the source's
#                                                     own description of them
#   excluded_customer_groups {groups}                 never customers of these groups (identities)
#   several_customers      {customers}                only these customers (a code applies one rule)
#   several_customer_groups {groups}                  only customers of these groups (a code applies one rule)
#   customer_history       {min_orders?, new?, returning?}  only new customers (no order yet), returning ones (an
#                                                     order already) or those with at least so many orders
#   cap                    {amount}                   at most this much off an order, all its items together
#   spread_amount          {items?, groups?, variants?}  an amount once, spread across only these items in
#                                                     proportion to their prices
#   item_limit             {qty, order?}              on at most this many units: the cheapest or dearest first
#                                                     (``order``: "cheapest" | "dearest"), or of each line
#                                                     ("each_line")
#   item_choice            {strategy?, source?}       which of the qualifying items are discounted
#   item_matcher           {source}                   items chosen by the source's other criteria (brands,
#                                                     options, attributes, combinations of criteria)
#   rule_condition         {source}                   a condition beyond spend, quantity and items
#   fixed_price_set        {qty, price, items?, groups?, variants?, order?, repeat?}  so many qualifying units
#                                                     (the cheapest or dearest first) for one price, once or
#                                                     (``repeat``) as many sets as there are
#   further_rules          {source}                   the source's further rules, after the one written
#   channels               {channels}                 only the source's other sales channels
#   price_list             {source}                   the source's own price list
#   applied_by_staff       {}                         applied by staff at the point of sale, never by itself
# Any condition may carry ``unread`` ([what]): terms of it the importer doesn't read from the source (yet), or
# ``undocumented`` ([what]): what the source doesn't document about it; so whatever the fork enforces, it stays
# held.
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
	"several_customers": COUPON_LIMITS,
	"customer_segments": DISCOUNT_CAPS,
	"excluded_customer_groups": DISCOUNT_CAPS,
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


@dataclass(frozen=True)
class Enforcement:
	"""How the Webshop fork enforces a kind of condition: the terms it reads (a condition with any other term,
	such as the source's own description, ``unread`` or ``undocumented``, stays held), those it needs (all of
	``required``, and one of ``one_of`` when given), and whether it does so only for a coupon code's discount
	(``coded``) or only on a single Pricing Rule, not a Promotional Scheme (``single_rule``)."""

	terms: frozenset[str] = frozenset()
	coded: bool = False
	single_rule: bool = False
	required: frozenset[str] = frozenset()
	one_of: frozenset[str] = frozenset()


ITEM_TERMS = frozenset({"items", "groups", "variants"})
# What several sources leave out, as ``undocumented`` or ``unread`` terms that keep a condition held.
PER_ITEM_OR_TOTAL = "whether the amount is off each item or once across them"
CUSTOMER_GROUP_MEMBERS = "which customers are in each group (the source's groups aren't mirrored)"
CHEAPEST, DEAREST, EACH_LINE = "cheapest", "dearest", "each_line"
UNIT_ORDERS = frozenset({CHEAPEST, DEAREST})
# The conditions the Webshop fork enforces (card 405: Coupon Code and Pricing Rule fields the writer fills; card
# 407: Pricing Rule eligibility and discount limits; see ``erpnext.writers.discounts``). A discount with any
# other condition is held: written disabled, its codes not redeemable. When the fork enforces another, add it
# here; the next run enables the discounts it held. Not yet: a shipping discount limited to the source's shipping
# methods or zones (the fork limits one to Shipping Rules and countries, but the sources' methods and zones aren't
# mirrored as those); an item limit or fixed-price set without the order its units are taken in, segments without
# their members, and anything the source describes only in its own terms.
ENFORCED: dict[str, Enforcement] = {
	"once_per_customer": Enforcement(coded=True),
	"per_customer_limit": Enforcement(frozenset({"uses"}), coded=True),
	"emails": Enforcement(frozenset({"emails"}), coded=True),
	"several_customers": Enforcement(frozenset({"customers"}), coded=True),
	"free_shipping": Enforcement(
		frozenset({"max_shipping_price", "countries"}) | ITEM_TERMS, single_rule=True
	),
	"shipping_discount": Enforcement(frozenset({"discount", "value", "cap"}) | ITEM_TERMS, single_rule=True),
	"customer_segments": Enforcement(
		frozenset({"customers", "groups"}), single_rule=True, one_of=frozenset({"customers", "groups"})
	),
	"excluded_customer_groups": Enforcement(
		frozenset({"groups"}), single_rule=True, required=frozenset({"groups"})
	),
	"several_customer_groups": Enforcement(
		frozenset({"groups"}), single_rule=True, required=frozenset({"groups"})
	),
	"customer_history": Enforcement(
		frozenset({"min_orders", "new", "returning"}),
		single_rule=True,
		one_of=frozenset({"min_orders", "new", "returning"}),
	),
	"cap": Enforcement(frozenset({"amount"}), single_rule=True, required=frozenset({"amount"})),
	"spread_amount": Enforcement(ITEM_TERMS, single_rule=True, one_of=ITEM_TERMS),
	"item_limit": Enforcement(
		frozenset({"qty", "order"}), single_rule=True, required=frozenset({"qty", "order"})
	),
	"fixed_price_set": Enforcement(
		frozenset({"qty", "price", "order", "repeat"}) | ITEM_TERMS,
		single_rule=True,
		required=frozenset({"qty", "price", "order"}),
	),
}


def is_enforced(kind: str, terms: Fields, coded: bool, single_rule: bool) -> bool:
	"""Whether the fork enforces a condition of ``kind`` with ``terms`` (those set) on a discount with codes
	(``coded``) written as one Pricing Rule (``single_rule``)."""
	how = ENFORCED.get(kind)
	given = {key for key, value in terms.items() if value is not None}
	return (
		how is not None
		and given <= how.terms
		and how.required <= given
		and (not how.one_of or bool(how.one_of & given))
		and (coded or not how.coded)
		and (single_rule or not how.single_rule)
	)


def is_scheme(tiers: int, customers: int, customer_groups: int) -> bool:
	"""Whether ERPNext holds a discount as a Promotional Scheme (one Pricing Rule per tier and customer): it has
	tiers, or is for several customers or customer groups."""
	return bool(tiers) or customers > 1 or customer_groups > 1


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


def held_for(conditions: list[Condition], coded: bool, single_rule: bool) -> list[str]:
	"""The cards a discount (or code) with these conditions is held for: those of the conditions the fork
	doesn't enforce (yet) on such a discount (``is_enforced``)."""
	return sorted({c.card for c in conditions if not is_enforced(c.kind, c.terms, coded, single_rule)})


def enforced_conditions(conditions: list[Values], coded: bool, single_rule: bool) -> list[Values]:
	"""Of recorded conditions, those the fork enforces on such a discount: what the writer gives it."""
	return [
		c for c in conditions if is_enforced(c.string("kind"), recorded_terms(c).all(), coded, single_rule)
	]


def recorded_terms(condition: Values) -> Values:
	return condition.record("terms") or Values({})


def hold_label(cards: list[str]) -> str:
	return f"held: awaiting enforcement ({', '.join(cards)})"


def conditions_json(conditions: list[Values]) -> str | None:
	"""Recorded conditions as the record keeps them for the fork: JSON, keys sorted, amounts as decimal
	strings; None when there are none."""
	if not conditions:
		return None
	return json.dumps([c.all() for c in conditions], default=decimal_text, sort_keys=True)


def conditions_summary(conditions: list[Values], coded: bool, single_rule: bool) -> str:
	"""The conditions a held discount awaits, for its warning: "once_per_customer (card 405), cap (card 407)"."""
	enforced = enforced_conditions(conditions, coded, single_rule)
	return ", ".join(f"{c.string('kind')} ({c.string('card')})" for c in conditions if c not in enforced)


@dataclass(frozen=True)
class Code:
	"""What one code of a discount holds of its own, where the source gives a code its own limits: its
	use limit and dates (enforced by its Coupon Code), and conditions (recorded, and holding that code)."""

	max_uses: int | None = None
	valid_upto: str | None = None
	conditions: list[Condition] = field(default_factory=list)
	valid_from: str | None = None


CODE_ALONE = False


def code_fields(code: Code) -> Fields:
	"""A code's own fields. Its own conditions are enforced on its Coupon Code, not a Pricing Rule (the rule is
	every code's): only what the fork enforces on a code, not on a single rule (``CODE_ALONE``), releases it."""
	fields: Fields = {
		"max_uses": code.max_uses,
		"valid_from": code.valid_from,
		"valid_upto": code.valid_upto,
		"conditions": condition_fields(code.conditions) or None,
		"held": list[Field](held_for(code.conditions, coded=True, single_rule=CODE_ALONE)) or None,
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
		"held": list[Field](held_for(discount.conditions, bool(discount.codes), single_rule(discount)))
		or None,
		"code_limits": {
			code: own for code, limits in discount.code_limits.items() if (own := code_fields(limits))
		}
		or None,
		"currency": discount.currency,
	}
	return fields | {key: value for key, value in extra.items() if value is not None}


def single_rule(discount: Discount) -> bool:
	"""Whether ERPNext holds the discount as one Pricing Rule, not a Promotional Scheme."""
	return not is_scheme(len(discount.tiers), len(discount.customers), len(discount.customer_groups))


@dataclass(frozen=True)
class CouponLimits:
	"""What the fork enforces of a code's conditions (its discount's and its own): uses per customer, and the
	customers (identities) and email addresses (or ``*`` patterns) it is only for."""

	per_customer: int | None = None
	customers: list[str] = field(default_factory=list)
	emails: list[str] = field(default_factory=list)


def coupon_limits(conditions: list[Values]) -> CouponLimits:
	"""The limits of enforced conditions: the fewest uses per customer (once per customer: 1); the customers and
	the emails of each list limiting them (with two lists of a kind, only what both hold)."""
	uses: list[int] = []
	customers: list[list[str]] = []
	emails: list[list[str]] = []
	for condition in conditions:
		terms = recorded_terms(condition)
		match condition.string("kind"):
			case "once_per_customer":
				uses.append(1)
			case "per_customer_limit":
				uses.append(terms.integer("uses"))
			case "several_customers":
				customers.append(terms.strings("customers"))
			case "emails":
				emails.append([email.lower() for email in terms.strings("emails")])
	return CouponLimits(min(uses, default=None), common(customers), common(emails))


def common(lists: list[list[str]]) -> list[str]:
	"""The entries every list holds, in the first list's order; none without lists."""
	if not lists:
		return []
	return [entry for entry in dict.fromkeys(lists[0]) if all(entry in other for other in lists[1:])]


FREE, PERCENTAGE, AMOUNT = "free", "percentage", "amount"


@dataclass(frozen=True)
class ShippingDiscount:
	"""A discount off a document's shipping charge, as the fork gives it: free (``FREE``), a percentage or an
	amount off; at most ``cap``; only on a charge of at most ``max_charge``; to ``countries`` (ISO codes); only
	the share of these products, variants and collections (identities)."""

	discount: str
	value: Decimal = Decimal(0)
	cap: Decimal | None = None
	max_charge: Decimal | None = None
	countries: list[str] = field(default_factory=list)
	items: list[str] = field(default_factory=list)
	variants: list[str] = field(default_factory=list)
	groups: list[str] = field(default_factory=list)


def shipping_discount(conditions: list[Values]) -> ShippingDiscount | None:
	"""The shipping discount of enforced conditions (free shipping, or a shipping discount), if any. The fork gives
	one per discount: a discount with two is Unsupported."""
	found = [c for c in conditions if c.string("kind") in ("free_shipping", "shipping_discount")]
	if not found:
		return None
	if len(found) > 1:
		raise Unsupported("a discount giving two shipping discounts: the Webshop fork gives one per rule")
	condition = found[0]
	terms = recorded_terms(condition)
	free = condition.string("kind") == "free_shipping"
	return ShippingDiscount(
		discount=FREE
		if free
		else one_of(terms.string("discount"), frozenset({PERCENTAGE, AMOUNT}), "discount"),
		value=Decimal(0) if free else terms.decimal("value"),
		cap=terms.optional_decimal("cap"),
		max_charge=terms.optional_decimal("max_shipping_price"),
		countries=[code.upper() for code in terms.strings("countries")],
		items=terms.strings("items"),
		variants=terms.strings("variants"),
		groups=terms.strings("groups"),
	)


@dataclass(frozen=True)
class Targets:
	"""The products, variants and collections (identities) a limit is about."""

	items: list[str] = field(default_factory=list)
	variants: list[str] = field(default_factory=list)
	groups: list[str] = field(default_factory=list)


def targets_of(terms: Values) -> Targets:
	return Targets(terms.strings("items"), terms.strings("variants"), terms.strings("groups"))


@dataclass(frozen=True)
class FixedPriceSet:
	"""``qty`` units (the cheapest or dearest first) for ``price`` together; once, or as many sets as there are."""

	qty: Decimal
	price: Decimal
	order: str
	repeat: bool
	targets: Targets


@dataclass(frozen=True)
class RuleLimits:
	"""What the fork enforces of a discount's conditions on its Pricing Rule (card 407): who it is for (a segment
	of ``customers`` and customers of ``groups``; never ``excluded_groups``; ``new_customers`` only, or those with
	``min_orders``), and how much it gives (at most ``cap``; an amount ``spread`` once across these targets; on at
	most ``max_qty`` units in ``qty_order``; or ``fixed_set``)."""

	customers: list[str] = field(default_factory=list)
	groups: list[str] = field(default_factory=list)
	excluded_groups: list[str] = field(default_factory=list)
	new_customers: bool = False
	min_orders: int = 0
	cap: Decimal | None = None
	spread: Targets | None = None
	max_qty: Decimal | None = None
	qty_order: str | None = None
	fixed_set: FixedPriceSet | None = None


def rule_limits(conditions: list[Values]) -> RuleLimits:
	"""The rule limits of enforced conditions. Two of a kind hold together: the smaller cap and quantity, the
	groups both lists hold; what the fork can't combine on one rule is Unsupported."""
	found: dict[str, list[Values]] = {}
	for condition in conditions:
		found.setdefault(condition.string("kind"), []).append(recorded_terms(condition))
	segments, several = found.get("customer_segments", []), found.get("several_customer_groups", [])
	if len(segments) + bool(several) > 1:
		raise Unsupported("a discount for two segments, or a segment and customer groups: the fork reads one")
	for kind in ("spread_amount", "item_limit", "fixed_price_set"):
		if len(found.get(kind, [])) > 1:
			raise Unsupported(f"a discount with two of {kind}: the fork reads one")
	spread = found.get("spread_amount", [])
	item_limit = next(iter(found.get("item_limit", [])), None)
	fixed = next(iter(found.get("fixed_price_set", [])), None)
	if fixed is not None and (item_limit is not None or spread):
		raise Unsupported(
			"a fixed-price set with an item limit or a spread amount: the fork gives a set alone"
		)
	history = found.get("customer_history", [])
	caps = [terms.decimal("amount") for terms in found.get("cap", [])]
	return RuleLimits(
		customers=segments[0].strings("customers") if segments else [],
		groups=segments[0].strings("groups") if segments else common([t.strings("groups") for t in several]),
		excluded_groups=list(
			dict.fromkeys(
				g for terms in found.get("excluded_customer_groups", []) for g in terms.strings("groups")
			)
		),
		new_customers=new_customers(history),
		min_orders=max(
			(h.integer("min_orders") if h.has("min_orders") else 1 for h in history if not h.has("new")),
			default=0,
		),
		cap=min(caps) if caps else None,
		spread=targets_of(spread[0]) if spread else None,
		max_qty=item_limit.decimal("qty") if item_limit else None,
		qty_order=one_of(item_limit.string("order"), UNIT_ORDERS | {EACH_LINE}, "order")
		if item_limit
		else None,
		fixed_set=FixedPriceSet(
			qty=fixed.decimal("qty"),
			price=fixed.decimal("price"),
			order=one_of(fixed.string("order"), UNIT_ORDERS, "order"),
			repeat=fixed.has("repeat") and fixed.flag("repeat"),
			targets=targets_of(fixed),
		)
		if fixed
		else None,
	)


def new_customers(history: list[Values]) -> bool:
	"""Whether customer histories ask for new customers only; with one asking for earlier orders, Unsupported."""
	new = any(h.has("new") and h.flag("new") for h in history)
	if new and any(not (h.has("new") and h.flag("new")) for h in history):
		raise Unsupported("a discount for new customers that asks for earlier orders")
	return new


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
# Who an opportunity is with.
OPPORTUNITY_PARTIES = frozenset({Kind.LEAD, Kind.CUSTOMER, Kind.PROSPECT})
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


def with_custom(fields: Fields, custom: dict[str, str]) -> Fields:
	"""A record's fields with the values of its source's own fields (``custom``, by label), only when it has
	any, so a record mapped before they were mirrored keeps its change signal."""
	if not custom:
		return fields
	return fields | {"custom": {label: custom[label] for label in sorted(custom)}}


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
	custom: dict[str, str] = field(default_factory=dict)


def people_fields(people: list[ContactPerson]) -> list[Field]:
	return [
		{
			"gid": person.gid,
			"name": person.name,
			"email": person.email,
			"phone": person.phone,
			"job_title": person.job_title,
		}
		for person in people
	]


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
		"contacts": people_fields(party.contacts) or None,
		"disabled": party.disabled or None,
	}
	return with_custom(
		fields | {key: value for key, value in extra.items() if value is not None}, party.custom
	)


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


@dataclass(frozen=True)
class TimeLog:
	"""Time worked: when it started (UTC), how many hours, what was done, whether it is billable, and who
	(an employee identity), for whom (a customer) and on what (a project), each None where the source keeps
	none; ``submitted`` once the source logged it (a running timer is a draft)."""

	start: str
	hours: Decimal
	description: str | None = None
	billable: bool = False
	submitted: bool = True
	employee: str | None = None
	customer: str | None = None
	project: str | None = None


def timesheet_fields(log: TimeLog) -> Fields:
	if log.hours <= 0:
		raise Unsupported(f"{decimal_text(log.hours)} hours of time can't be booked")
	return {
		"start": checked_time(log.start, "the time's start"),
		"hours": log.hours,
		"description": log.description,
		"billable": log.billable,
		"submitted": log.submitted,
		"employee": log.employee,
		"customer": log.customer,
		"project": log.project,
	}


def tax_rate_fields(name: str, rate: Decimal, number: str | None = None) -> Fields:
	"""A sales tax as a template: its name, its rate (percent) and its registration number."""
	if rate < 0:
		raise Unsupported(f"a tax at {decimal_text(rate)}%")
	return {"name": name, "rate": rate, "number": number}


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
	crm_notes: list[Note] = field(default_factory=list)
	custom: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Note:
	"""One of the notes a source keeps on a lead: its identity, when it was written (UTC) and its text."""

	gid: str
	date: str | None
	text: str


def noted(notes: list[Note]) -> Fields:
	"""A record's notes of its own (``crm_notes``), only when it has any, so a record mapped before they were
	mirrored keeps its change signal."""
	return {"crm_notes": note_fields(notes)} if notes else {}


def note_fields(notes: list[Note]) -> list[Field]:
	return [
		{"gid": note.gid, "date": checked_time(note.date, "a note's time"), "text": note.text}
		for note in notes
	]


def lead_fields(lead: Lead) -> Fields:
	"""A ``lead``'s fields; ``disabled`` (a lead the source has closed, kept for the opportunities that
	name it) and its notes of their own (``crm_notes``) only when it has them."""
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
	if lead.crm_notes:
		fields["crm_notes"] = note_fields(lead.crm_notes)
	return with_custom(fields | {"disabled": True} if lead.disabled else fields, lead.custom)


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
	# The source's pipeline (an ``opportunity_type`` identity), where it keeps several.
	opportunity_type: str | None = None
	# The pipeline stage it is at, by name (an ERPNext Sales Stage).
	sales_stage: str | None = None
	custom: dict[str, str] = field(default_factory=dict)
	crm_notes: list[Note] = field(default_factory=list)


def opportunity_fields(opportunity: Opportunity) -> Fields:
	"""An ``opportunity``'s fields; its type only when the source names one, so an opportunity mapped before
	types existed keeps its change signal."""
	if opportunity.party_kind not in OPPORTUNITY_PARTIES:
		raise ValueError(
			f"an opportunity is with a lead, a customer or a prospect, not a {opportunity.party_kind}"
		)
	typed: Fields = {"opportunity_type": opportunity.opportunity_type} if opportunity.opportunity_type else {}
	if opportunity.sales_stage:
		typed["sales_stage"] = opportunity.sales_stage
	return with_custom(
		typed
		| {
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
		| noted(opportunity.crm_notes),
		opportunity.custom,
	)


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
	branch: str | None = None


def employee_fields(employee: Employee) -> Fields:
	"""An ``employee``'s fields; its branch only when the source keeps one, so an employee mapped before
	branches existed keeps its change signal."""
	fields: Fields = {
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
	return fields | {"branch": employee.branch} if employee.branch else fields


# -- Time and leave (HR sources) -------------------------------------------------------------------------

UTC_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
WALL_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}$")
SHIFT_STATUSES = frozenset({"Active", "Inactive"})
LOG_TYPES = frozenset({"IN", "OUT"})
ATTENDANCE_STATUSES = frozenset({"Present", "Absent", "On Leave", "Half Day", "Work From Home"})
LEAVE_STATUSES = frozenset({"Open", "Approved", "Rejected", "Cancelled"})
# A leave the source approved or refused is decided (submitted); an open or cancelled one is not.
DECIDED_LEAVE = frozenset({"Approved", "Rejected"})


def checked_time(value: str | None, what: str) -> str | None:
	"""A UTC ``YYYY-MM-DDTHH:MM:SSZ`` time, or None; anything else is a mapper's mistake."""
	if value is not None and not UTC_TIME.match(value):
		raise ValueError(f"{what} is not a UTC time: {value!r}")
	return value


def wall_time(value: str, what: str) -> str:
	"""A time of day as the source's clock shows it (``HH:MM:SS``, no zone)."""
	if not WALL_TIME.match(value):
		raise ValueError(f"{what} is not an HH:MM:SS time of day: {value!r}")
	return value


def shift_name(start_time: str, end_time: str) -> str:
	"""The Shift Type a shift's hours are kept as, named by them: 08:00-16:30."""
	return f"{start_time[:5]}-{end_time[:5]}"


@dataclass(frozen=True)
class ShiftAssignment:
	"""A shift an employee (an identity) is scheduled for: the day it starts, and its hours as the source's
	clock shows them (the Shift Type of those hours; one ending before it starts runs past midnight).
	``submitted`` once the source has published it; ``status`` "Inactive" for one the employee won't work."""

	employee: str
	date: str
	start_time: str
	end_time: str
	submitted: bool = True
	status: str = "Active"


def shift_assignment_fields(shift: ShiftAssignment) -> Fields:
	start = wall_time(shift.start_time, "a shift's start")
	end = wall_time(shift.end_time, "a shift's end")
	return {
		"employee": shift.employee,
		"date": required_date(shift.date, "a shift's date"),
		"shift": {"name": shift_name(start, end), "start_time": start, "end_time": end},
		"status": one_of(shift.status, SHIFT_STATUSES, "shift status"),
		"submitted": shift.submitted,
	}


@dataclass(frozen=True)
class Checkin:
	"""One punch of the time clock: the employee (an identity), when (UTC), in or out, and the device."""

	employee: str
	time: str
	log_type: str
	device: str | None = None


def checkin_fields(checkin: Checkin) -> Fields:
	return {
		"employee": checkin.employee,
		"time": checked_time(checkin.time, "a punch's time"),
		"log_type": one_of(checkin.log_type, LOG_TYPES, "log type"),
		"device": checkin.device,
	}


@dataclass(frozen=True)
class Attendance:
	"""An employee's (an identity) attendance on a day: its status, the minutes worked, the first punch in
	and last punch out (UTC), the leave type (an identity) of a day on leave, and ``submitted`` once the
	source has approved the day's time."""

	employee: str
	date: str
	status: str
	working_minutes: int
	in_time: str | None = None
	out_time: str | None = None
	leave_type: str | None = None
	submitted: bool = True


def attendance_fields(attendance: Attendance) -> Fields:
	if attendance.working_minutes < 0:
		raise Unsupported(f"{attendance.working_minutes} minutes worked can't be booked")
	return {
		"employee": attendance.employee,
		"date": required_date(attendance.date, "an attendance's date"),
		"status": one_of(attendance.status, ATTENDANCE_STATUSES, "attendance status"),
		"working_minutes": attendance.working_minutes,
		"in_time": checked_time(attendance.in_time, "an attendance's first punch"),
		"out_time": checked_time(attendance.out_time, "an attendance's last punch"),
		"leave_type": attendance.leave_type,
		"submitted": attendance.submitted,
	}


def leave_type_fields(name: str, paid: bool) -> Fields:
	"""A kind of leave: its name, and whether it is paid (an unpaid one is leave without pay)."""
	return {"name": name, "paid": paid}


@dataclass(frozen=True)
class LeaveApplication:
	"""A leave an employee (an identity) asked for: its type (an identity), first and last day, status
	(LEAVE_STATUSES), whether it is half a day, the employee's reason and the day it was asked on."""

	employee: str
	leave_type: str
	from_date: str
	to_date: str
	status: str
	posting_date: str
	half_day: bool = False
	description: str | None = None


def leave_application_fields(leave: LeaveApplication) -> Fields:
	start = required_date(leave.from_date, "a leave's first day")
	end = required_date(leave.to_date, "a leave's last day")
	if end < start:
		raise Unsupported(f"a leave from {start} ending before it starts ({end})")
	return {
		"employee": leave.employee,
		"leave_type": leave.leave_type,
		"from_date": start,
		"to_date": end,
		"status": one_of(leave.status, LEAVE_STATUSES, "leave status"),
		"posting_date": required_date(leave.posting_date, "a leave's posting date"),
		"half_day": leave.half_day,
		"description": leave.description,
	}


# -- The sales pipeline's organisations, conversations, appointments and tasks (CRM sources) ---------------

COMMUNICATION_MEDIUMS = frozenset({"Email", "Chat", "Phone", "SMS", "Other"})
EVENT_STATUSES = frozenset({"Open", "Completed", "Closed", "Cancelled"})
TODO_STATUSES = frozenset({"Open", "Closed", "Cancelled"})
# Who or what a conversation, an appointment or a task is about: a lead, a customer, an organisation (a
# prospect) or a deal (an opportunity).
CRM_PARTIES = frozenset({Kind.LEAD, Kind.CUSTOMER, Kind.PROSPECT, Kind.OPPORTUNITY})
LOCAL_TIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def crm_party(kind: Kind, gid: str) -> Fields:
	if kind not in CRM_PARTIES:
		raise ValueError(f"a lead, customer, prospect or opportunity, not a {kind}")
	return {"kind": kind.value, "party": gid}


QUOTED_PARTIES = frozenset({Kind.LEAD, Kind.PROSPECT})


def quoted_party(kind: Kind, gid: str) -> Fields:
	"""A draft order's ``quoted``: a CRM's quote to a lead or an organisation (a prospect) that isn't a
	customer."""
	if kind not in QUOTED_PARTIES:
		raise ValueError(f"a quote is to a lead or a prospect, not a {kind}")
	return {"kind": kind.value, "party": gid}


def local_time(value: str | None, what: str) -> str | None:
	"""A date and time as the source's clock shows it (``YYYY-MM-DD HH:MM:SS``, no zone), or None."""
	if value is not None and not LOCAL_TIME.match(value):
		raise ValueError(f"{what} is not a YYYY-MM-DD HH:MM:SS time: {value!r}")
	return value


def event_time(value: str | None, what: str) -> str | None:
	"""An event's time: as the source's clock shows it, or (for a source that keeps them in UTC) a UTC time,
	which the writer shows on the site's clock."""
	if value is not None and UTC_TIME.match(value):
		return value
	return local_time(value, what)


@dataclass(frozen=True)
class Prospect:
	"""An organisation the sales pipeline works with: its name, website, how to reach it, its address
	(``address_fields``), a description, and its people: leads (lead identities), and the contacts a CRM keeps
	at it (``contacts``, as a party's other people)."""

	name: str
	website: str | None = None
	email: str | None = None
	phone: str | None = None
	address: Fields | None = None
	notes: str | None = None
	leads: list[str] = field(default_factory=list)
	custom: dict[str, str] = field(default_factory=dict)
	crm_notes: list[Note] = field(default_factory=list)
	contacts: list[ContactPerson] = field(default_factory=list)


def prospect_fields(prospect: Prospect) -> Fields:
	fields: Fields = {
		"name": prospect.name,
		"website": prospect.website,
		"email": prospect.email,
		"phone": prospect.phone,
		"addresses": [prospect.address] if prospect.address else [],
		"notes": prospect.notes,
		"leads": list[Field](prospect.leads),
	}
	if prospect.contacts:
		fields["contacts"] = people_fields(prospect.contacts)
	return with_custom(fields | noted(prospect.crm_notes), prospect.custom)


def opportunity_type_fields(name: str, description: str | None = None) -> Fields:
	"""A kind of opportunity (a source's sales pipeline): its name and description."""
	return {"name": name, "description": description}


@dataclass(frozen=True)
class Communication:
	"""One message of a conversation with a lead or a customer: its medium (COMMUNICATION_MEDIUMS), whether
	it was sent or received, when (UTC), its subject and content, and the phone number or email address it
	went to or came from."""

	party_kind: Kind
	party: str
	medium: str
	sent: bool
	date: str
	subject: str
	content: str
	phone: str | None = None
	email: str | None = None
	custom: dict[str, str] = field(default_factory=dict)


def communication_fields(message: Communication) -> Fields:
	return with_custom(
		{
			"with": crm_party(message.party_kind, message.party),
			"medium": one_of(message.medium, COMMUNICATION_MEDIUMS, "communication medium"),
			"sent": message.sent,
			"date": checked_time(message.date, "a message's time"),
			"subject": message.subject,
			"content": message.content,
			"phone": message.phone,
			"email": message.email,
		},
		message.custom,
	)


@dataclass(frozen=True)
class Event:
	"""An appointment: its subject, start and end as the source's clock shows them, status
	(EVENT_STATUSES), description, and who it is with (lead or customer identities)."""

	subject: str
	starts_on: str
	status: str
	ends_on: str | None = None
	description: str | None = None
	participants: list[tuple[Kind, str]] = field(default_factory=list)
	custom: dict[str, str] = field(default_factory=dict)


def event_fields(event: Event) -> Fields:
	return with_custom(
		{
			"subject": event.subject,
			"starts_on": event_time(event.starts_on, f"{event.subject}'s start"),
			"ends_on": event_time(event.ends_on, f"{event.subject}'s end"),
			"status": one_of(event.status, EVENT_STATUSES, "event status"),
			"description": event.description,
			"participants": [crm_party(kind, gid) for kind, gid in event.participants],
		},
		event.custom,
	)


@dataclass(frozen=True)
class ToDo:
	"""A task about a lead or a customer: what to do, its status (TODO_STATUSES) and due date."""

	description: str
	status: str
	party_kind: Kind
	party: str
	date: str | None = None
	custom: dict[str, str] = field(default_factory=dict)


def todo_fields(todo: ToDo) -> Fields:
	return with_custom(
		{
			"description": todo.description,
			"status": one_of(todo.status, TODO_STATUSES, "task status"),
			"date": checked_date(todo.date, "a task's due date"),
			"about": crm_party(todo.party_kind, todo.party),
		},
		todo.custom,
	)


# ── gift cards, store credit and loyalty (Square)────────────────────────────────────────────────────

GIFT_CARD, STORE_CREDIT = "gift_card", "store_credit"
ACTIVE, DISABLED, EXPIRED = "active", "disabled", "expired"
CARD_STATES = frozenset({ACTIVE, DISABLED, EXPIRED})


@dataclass(frozen=True)
class LedgerEntry:
	"""One line of a card's or account's source history: ``amount`` signed (a redemption is negative), in
	the card's currency, or points for a loyalty account."""

	gid: str
	at: str | None
	kind: str
	amount: Decimal | int
	balance_after: Decimal | None = None
	reference: str | None = None
	note: str | None = None


def ledger_fields(entry: LedgerEntry, amount_key: str = "amount") -> Fields:
	return {
		"gid": entry.gid,
		"at": entry.at,
		"kind": entry.kind,
		amount_key: entry.amount,
		"balance_after": entry.balance_after,
		"reference": entry.reference,
		"note": entry.note,
	}


@dataclass(frozen=True)
class GiftCard:
	kind: str
	code: str | None
	code_hint: str | None
	currency: str
	balance: Decimal
	state: str
	customer: str | None
	email: str | None
	given_name: str | None
	family_name: str | None
	ledger: list[LedgerEntry]
	details: dict[str, JsonValue]
	load_increases: bool
	new_numbers: bool


def gift_card_fields(card: GiftCard) -> Fields:
	return {
		"kind": one_of(card.kind, frozenset({GIFT_CARD, STORE_CREDIT}), "gift card kind"),
		"code": card.code,
		"code_hint": card.code_hint,
		"currency": card.currency.upper(),
		"balance": card.balance,
		"state": one_of(card.state, CARD_STATES, "gift card state"),
		"customer": card.customer,
		"email": card.email.lower() if card.email else None,
		"given_name": card.given_name,
		"family_name": card.family_name,
		"ledger": [ledger_fields(entry) for entry in card.ledger],
		"details": json.dumps(card.details, sort_keys=True, default=str),
		"load_increases": card.load_increases,
		"new_numbers": card.new_numbers,
	}


@dataclass(frozen=True)
class LoyaltyAccount:
	points: int
	customer: str | None
	email: str | None
	given_name: str | None
	family_name: str | None
	ledger: list[LedgerEntry]
	details: dict[str, JsonValue]


def loyalty_fields(account: LoyaltyAccount) -> Fields:
	return {
		"points": account.points,
		"customer": account.customer,
		"email": account.email.lower() if account.email else None,
		"given_name": account.given_name,
		"family_name": account.family_name,
		"ledger": [
			{k: v for k, v in ledger_fields(entry, "points").items() if k != "balance_after"}
			for entry in account.ledger
		],
		"details": json.dumps(account.details, sort_keys=True, default=str),
	}
