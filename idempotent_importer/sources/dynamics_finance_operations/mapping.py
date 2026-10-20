# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure Finance & Operations → desired ERPNext state, read through each record's field-map roles (a role's
field matched case-insensitively). What the documented entities don't hold (an order's taxes and
confirmation, a product's stock) is reported as planned, never invented."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from idempotent_importer.contract import (
	Address,
	Party,
	TradeDocument,
	TradeLine,
	address_fields,
	order_line,
	party_fields,
	trade_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Fields as Mapped
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_of
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.paths import slugify
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.dynamics.common import BLANK_DATES, DATE_LENGTH
from idempotent_importer.sources.dynamics_finance_operations.fieldmap import Fields
from idempotent_importer.sources.dynamics_finance_operations.reader import TYPES, key_of

PERSON = "Person"
PRODUCT_MASTER = "ProductMaster"
ISO_COUNTRY_LENGTH = 2


@dataclass(frozen=True)
class Context:
	config: ConnectionConfig

	def gid(self, kind: Kind, value: JsonValue) -> str | None:
		return None if value in (None, "") else self.config.identity(TYPES[kind], key_of(value))


def roles(node: Node, field: str = "roles") -> dict[str, str]:
	given = node.get(field)
	return {k: v for k, v in given.items() if isinstance(v, str)} if isinstance(given, dict) else {}


def text(fields: Fields, role: str) -> str | None:
	value = fields.get(role)
	if value is None or value == "":
		return None
	if not isinstance(value, str):
		raise SourceError(f"Finance & Operations' {role} field isn't text")
	return value.strip() or None


def number(fields: Fields, role: str) -> Decimal:
	value = fields.get(role)
	if value is None or value == "":
		return Decimal(0)
	if isinstance(value, bool) or not isinstance(value, int | float | str):
		raise SourceError(f"Finance & Operations' {role} field isn't a number")
	return decimal_of(value)


def date(fields: Fields, role: str) -> str | None:
	value = text(fields, role)
	day = value[:DATE_LENGTH] if value else None
	return None if day is None or day in BLANK_DATES else day


class PartyMapper:
	"""A customer (or a mapped vendor) as a Customer (Supplier): an organisation or a person, its currency,
	its address when the map names a street and the country is an ISO code (Finance & Operations' country
	ids are usually three letters: a warning)."""

	version = 1

	def __init__(self, kind: Kind) -> None:
		self.kind = kind

	def map(self, node: Node) -> Desired | None:
		fields = Fields(node, roles(node))
		person = text(fields, "party_type") == PERSON
		names = [text(fields, role) for role in ("first_name", "middle_name", "last_name")]
		name = (" ".join(part for part in names if part) if person else None) or text(fields, "name")
		if name is None:
			raise Unsupported("it has no name")
		address, warning = self._address(fields, name)
		party = Party(
			name=name,
			type="Individual" if person else "Company",
			first_name=names[0] if person else None,
			last_name=names[2] if person else None,
			company=None if person else name,
			email=text(fields, "email"),
			phone=text(fields, "phone"),
			addresses=[address] if address else [],
			currency=text(fields, "currency"),
		)
		return Desired(self.kind, node.id(), party_fields(party), warnings=(warning,) if warning else ())

	def _address(self, fields: Fields, name: str) -> tuple[Mapped | None, str | None]:
		street, city, country = text(fields, "street"), text(fields, "city"), text(fields, "country")
		if not (street and city and country):
			return None, None if street else "its street isn't in the field map (an address needs one)"
		if len(country) != ISO_COUNTRY_LENGTH:
			return None, f'its country "{country}" isn\'t a two-letter ISO code (planned: card 386)'
		return (
			address_fields(
				Address(
					"Billing",
					name,
					None,
					street,
					None,
					city,
					text(fields, "state"),
					text(fields, "postcode"),
					country,
					None,
					None,
				)
			),
			None,
		)


class ProductMapper:
	"""A released product as an Item at its sales price. A product master (with variants) is planned; on-hand
	stock lives in its own entities (planned), so the item isn't counted as a stock item yet."""

	kind = Kind.PRODUCT
	version = 1

	def map(self, node: Node) -> Desired | None:
		fields = Fields(node, roles(node))
		if text(fields, "subtype") == PRODUCT_MASTER:
			raise Unsupported("a product master and its variants (planned: card 386)")
		name = text(fields, "name") or node.string("source_id")
		mapped: Mapped = {
			"type": "simple",
			"name": name,
			"slug": slugify(name),
			"description": None,
			"short_description": None,
			"sku": text(fields, "key"),
			"barcode": None,
			"brand": None,
			"collections": [],
			"images": [],
			"published": False,
			"is_stock_item": False,
			"weight": None,
			"price": number(fields, "price"),
			"sale": None,
			"stock_qty": None,
			"attributes": [],
			"specifications": [],
			"variants": [],
			"tags": [],
		}
		return Desired(self.kind, node.id(), mapped, warnings=("its on-hand stock is planned (card 386)",))


class OrderMapper:
	"""A sales (or mapped purchase) order as a draft Sales (Purchase) Order: its lines at their line amounts.
	The documented entities hold no tax or confirmation status, so the order is a draft totalling its lines
	(planned: its taxes and confirmation, card 386)."""

	version = 1

	def __init__(self, kind: Kind, context: Context) -> None:
		self.kind = kind
		self._context = context
		self._party_role, self._party_kind = (
			("customer", Kind.CUSTOMER) if kind is Kind.SALES_ORDER else ("supplier", Kind.SUPPLIER)
		)

	def map(self, node: Node) -> Desired | None:
		fields = Fields(node, roles(node))
		currency = text(fields, "currency")
		party = self._context.gid(self._party_kind, fields.get(self._party_role))
		day = date(fields, "date")
		if not (currency and party and day):
			raise Unsupported(
				f"it has no {'currency' if not currency else self._party_role if not party else 'date'}"
			)
		line_roles = roles(node, "line_roles")
		lines: list[TradeLine] = []
		for index, raw in enumerate(node.children("lines")):
			line = Fields(raw, line_roles)
			item = self._context.gid(Kind.PRODUCT, line.get("item"))
			lines.append(
				TradeLine(
					order_line(
						f"{node.id()}#line:{index + 1}",
						item,
						None,
						text(line, "item") or "Line",
						number(line, "quantity"),
						number(line, "amount"),
						currency,
					)
				)
			)
		total = sum((line.line.qty * line.line.rate for line in lines), Decimal(0))
		document = TradeDocument(
			number=node.string("source_id"),
			date=day,
			party=party,
			currency=currency,
			lines=lines,
			taxes=[],
			total=total,
			submitted=False,
			due_date=date(fields, "delivery_date"),
		)
		return Desired(
			self.kind,
			node.id(),
			trade_fields(document),
			warnings=("its taxes and confirmation are planned (card 386)",),
		)


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		ProductMapper(),
		PartyMapper(Kind.CUSTOMER),
		PartyMapper(Kind.SUPPLIER),
		OrderMapper(Kind.SALES_ORDER, context),
		OrderMapper(Kind.PURCHASE_ORDER, context),
	]
