# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gift cards, store credit and loyalty accounts → the Square account they live in (Square Gift Cards and
Square Loyalty: one balance, spent online and at the Square counter), each kept in ERPNext as an Importer
Gift Card with its ledger (the source history, which Square can't backdate, and every write made in
Square).

A gift card becomes one digital Square card numbered with its own code where Square takes it, else a new
keyed number (when the connection allows it). Its balance is loaded on activation; afterwards only what the
source changes is applied (``stored_value.plans``), so money spent in Square is never put back. It is linked
to the customer's Square profile (found by email, created when missing). A loyalty account becomes the Square
Loyalty account for its customer's phone number (Square enrols by phone), created when missing; a customer
without a phone Square can enrol is deferred (reported in the run) and retried. Gone at the source: the record
stays (marked retired) and nothing is deactivated in Square. A write in Square can't be rolled back, so
what Square holds after each one is committed at once (``settle``), and the next run never repeats it."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import frappe
from frappe.utils.password import get_encryption_key

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import SourceError, UnresolvedDependency
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import SystemSleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_text, from_minor_units
from idempotent_importer.core.node import Node
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.core.transport import RequestsTransport
from idempotent_importer.erpnext.writers.base import Writer
from idempotent_importer.stored_value import numbers, plans
from idempotent_importer.stored_value.phone import e164
from idempotent_importer.stored_value.square import SquareAccount, SquareStoredValue, key

CARD = "Importer Gift Card"
CONNECTION = "Importer Connection"
LOYALTY = "loyalty"
POINTS = "PTS"
RETIRED = "retired"
SOURCE, SQUARE = "source", "square"
# Test harness only (site config): where a fake Square answers for a connection, as "<connection>:square".
TEST_ENDPOINTS = "idempotent_importer_test_endpoints"
# The key new gift card numbers are derived with (site config); the site's encryption key when unset.
NUMBER_KEY = "idempotent_importer_gift_card_number_key"


def square_account(connection: str) -> SquareAccount:
	doc = frappe.get_doc(CONNECTION, connection)
	endpoints = frappe.conf.get(TEST_ENDPOINTS) or {}
	return SquareAccount(
		environment=str(doc.get("square_environment") or "Production"),
		location_id=str(doc.get("square_location_id") or ""),
		access_token=str(doc.get_password("square_access_token", raise_exception=False) or ""),
		endpoint_override=endpoints.get(f"{connection}:square"),
	)


def text(value: object) -> str:
	"""An amount as the ledger and the idempotency keys write it: plain, without trailing zeros."""
	return decimal_text(value) if isinstance(value, Decimal) else str(value)


def balance_of(node: Node, currency: str, field: str = "balance_money") -> Decimal:
	money = node.child(field)
	amount = money.optional_integer("amount") if money else None
	return from_minor_units(amount, currency) if amount is not None else Decimal(0)


class StoredValueWriter(Writer):
	"""What gift cards and loyalty accounts share: the Square account and the Importer Gift Card record."""

	def square(self) -> SquareStoredValue:
		return SquareStoredValue(
			square_account(self.context.connection), RequestsTransport(), SystemSleeper()
		)

	def record(self, gid: str, link: Link | None) -> Any:
		existing = self.existing(link)
		if existing is not None:
			return existing
		name = frappe.db.get_value(CARD, {"connection": self.context.connection, "gid": gid})
		return frappe.get_doc(CARD, name) if name else frappe.new_doc(CARD)

	def customer_of(self, values: Values) -> str | None:
		gid = values.optional_string("customer")
		return self.dependency(Kind.CUSTOMER, gid) if gid else None

	@staticmethod
	def source_history(doc: Any, entries: list[Values], amount_key: str) -> None:
		"""Replaces the record's source history; the importer's own Square entries stay, after it."""
		square = [row.as_dict() for row in doc.get("entries") or [] if row.origin == SQUARE]
		rows = [
			{
				"entry_gid": entry.string("gid"),
				"origin": SOURCE,
				"occurred_at": (entry.optional_string("at") or "").replace("T", " ").rstrip("Z") or None,
				"entry_kind": entry.string("kind"),
				"amount": text(entry.all().get(amount_key)),
				"balance_after": text(entry.all()["balance_after"])
				if entry.all().get("balance_after") is not None
				else None,
				"reference": entry.optional_string("reference"),
				"note": entry.optional_string("note"),
			}
			for entry in entries
		]
		keep = (
			"entry_gid",
			"origin",
			"occurred_at",
			"entry_kind",
			"amount",
			"balance_after",
			"reference",
			"note",
		)
		doc.set("entries", rows + [{k: row.get(k) for k in keep} for row in square])

	@staticmethod
	def square_entry(
		doc: Any, entry_gid: str, at: str | None, kind: str, amount: str, balance_after: str | None
	) -> None:
		doc.append(
			"entries",
			{
				"entry_gid": entry_gid,
				"origin": SQUARE,
				"occurred_at": (at or "").replace("T", " ").rstrip("Z") or None,
				"entry_kind": kind,
				"amount": amount,
				"balance_after": balance_after,
				"reference": entry_gid.removeprefix("square:"),
				"note": "Written in Square by the importer",
			},
		)

	def settle(self, doc: Any, loaded: object, held: str | None) -> None:
		"""Records what Square now holds, committed at once: a write in Square can't be rolled back with the
		object's transaction, so the next run must know it happened (else it would apply it again)."""
		doc.loaded_balance = text(loaded)
		doc.held = held
		self.save(doc)
		self.commit()

	def commit(self) -> None:
		frappe.db.commit()

	def retire(self, link: Link) -> None:
		frappe.db.set_value(CARD, link.target.name, "state", RETIRED)


class GiftCardWriter(StoredValueWriter):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		currency = values.string("currency")
		doc = self.record(desired.gid, link)
		doc.update(
			{
				"connection": self.context.connection,
				"gid": desired.gid,
				"kind": values.string("kind"),
				"customer": self.customer_of(values),
				"gan": doc.get("gan") or self.number(desired.gid, values),
				"state": values.string("state"),
				"currency": currency,
				"source_balance": text(values.decimal("balance")),
				"loaded_balance": doc.get("loaded_balance") or "0",
				"details": values.string("details"),
			}
		)
		self.source_history(doc, values.records("ledger"), "amount")
		self.save(doc)
		target = values.decimal("balance") if values.string("state") == "active" else Decimal(0)
		if not doc.square_id and target == 0:
			# Nothing to spend yet (store credit at zero): no Square card until there is.
			return Target(CARD, doc.name)
		square = self.square()
		card = (
			(square.card(doc.square_id) if doc.square_id else None)
			or square.card_by_gan(doc.gan)
			or square.create_card(doc.gan, key("card", desired.gid))
		)
		doc.square_id = card.string("id")
		self.settle(doc, doc.loaded_balance, doc.get("held"))
		loaded = Decimal(doc.loaded_balance)
		step = plans.gift_card_step(
			target, loaded, card.string("state"), balance_of(card, currency), values.flag("load_increases")
		)
		if step.step is not None:
			activity = square.activity(
				step.step,
				doc.square_id,
				step.amount,
				currency,
				desired.gid,
				key(desired.gid, step.step, text(loaded), text(step.loaded)),
			)
			signed = -step.amount if step.step == plans.DECREMENT else step.amount
			self.square_entry(
				doc,
				"square:" + activity.string("id"),
				activity.optional_string("created_at"),
				step.step.lower(),
				text(signed),
				text(balance_of(activity, currency, "gift_card_balance_money")),
			)
		self.settle(doc, step.loaded, step.held)
		email = values.optional_string("email")
		if email:
			customer_id = square.customer(
				email,
				values.optional_string("given_name"),
				values.optional_string("family_name"),
				doc.customer or desired.gid,
			)
			if customer_id not in card.strings("customer_ids"):
				square.link_customer(doc.square_id, customer_id)
		return Target(CARD, doc.name)

	def number(self, gid: str, values: Values) -> str:
		code = values.optional_string("code")
		gan = numbers.from_source_code(code) if code else None
		if gan:
			return gan
		if not values.flag("new_numbers"):
			raise SourceError(
				f"{gid}: the source doesn't reveal the card's code, and new gift card numbers are switched off."
				if code is None
				else f"{gid}: Square can't use the card's code as its number, and new gift card numbers are switched off."
			)
		return numbers.generated(gid, str(frappe.conf.get(NUMBER_KEY) or get_encryption_key()))


class LoyaltyWriter(StoredValueWriter):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = self.record(desired.gid, link)
		customer = self.customer_of(values)
		doc.update(
			{
				"connection": self.context.connection,
				"gid": desired.gid,
				"kind": LOYALTY,
				"customer": customer,
				"state": "active",
				"currency": POINTS,
				"source_balance": str(values.integer("points")),
				"loaded_balance": doc.get("loaded_balance") or "0",
				"details": values.string("details"),
			}
		)
		self.source_history(doc, values.records("ledger"), "points")
		phone = self.phone(customer)
		if phone is None:
			# Deferred (logged in the run, retried every run) until the customer has one.
			raise UnresolvedDependency(
				f"{desired.gid}: its customer has no phone number Square Loyalty can enrol."
			)
		self.save(doc)
		square = self.square()
		account = (
			square.loyalty_account(doc.square_id) if doc.square_id else None
		) or square.loyalty_account_by_phone(phone)
		if account is None:
			email = values.optional_string("email")
			account = square.create_loyalty_account(
				square.loyalty_program_id(),
				phone,
				square.customer(
					email,
					values.optional_string("given_name"),
					values.optional_string("family_name"),
					customer or desired.gid,
				)
				if email
				else None,
				key("loyalty", desired.gid),
			)
		doc.square_id = account.string("id")
		self.settle(doc, doc.loaded_balance, doc.get("held"))
		loaded = int(doc.loaded_balance)
		step = plans.points_step(values.integer("points"), loaded, account.optional_integer("balance") or 0)
		if step.points:
			event = square.adjust_points(
				doc.square_id, step.points, key(desired.gid, "adjust", str(loaded), str(step.loaded))
			)
			self.square_entry(
				doc,
				"square:" + event.string("id"),
				event.optional_string("created_at"),
				"adjust_points",
				str(step.points),
				None,
			)
		self.settle(doc, step.loaded, step.held)
		return Target(CARD, doc.name)

	def phone(self, customer: str | None) -> str | None:
		if not customer:
			return None
		number = frappe.db.get_value("Customer", customer, "mobile_no")
		contact = frappe.db.get_value("Customer", customer, "customer_primary_contact")
		if not number and contact:
			number = frappe.db.get_value("Contact", contact, "mobile_no") or frappe.db.get_value(
				"Contact", contact, "phone"
			)
		country = frappe.db.get_value("Company", self.context.company, "country")
		code = frappe.db.get_value("Country", country, "code") if country else None
		return e164(str(number), str(code or "")) if number else None
