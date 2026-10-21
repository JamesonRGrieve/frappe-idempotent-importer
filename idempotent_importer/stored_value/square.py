# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Square account gift cards, store credit and loyalty live in, whatever the source: Square Gift Cards,
Square Loyalty and Square customers, through the Square API at the version the Square Online adapter speaks.
Every write carries an idempotency key derived from what it writes, so a retried run never applies it twice.
The connection holds the access token (GIFTCARDS_READ, GIFTCARDS_WRITE, LOYALTY_READ, LOYALTY_WRITE,
CUSTOMERS_READ, CUSTOMERS_WRITE), the location activities are recorded at, and the environment."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import quote

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.money import minor_units
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import RestClient
from idempotent_importer.stored_value.plans import ACTIVATE, DECREMENT, INCREMENT

API_VERSION = "2026-09-16"
BASE_URLS = {"Production": "https://connect.squareup.com", "Sandbox": "https://connect.squareupsandbox.com"}
LABEL = "Square"
IDEMPOTENCY_KEY_LENGTH = 45
# Why the importer adjusts a balance: Square's reason closest to "the card's other store changed it".
ADJUSTMENT_REASON = "SUPPORT_ISSUE"
POINTS_REASON = "Moved from the previous store"
DETAILS = {
	ACTIVATE: "activate_activity_details",
	INCREMENT: "adjust_increment_activity_details",
	DECREMENT: "adjust_decrement_activity_details",
}


@dataclass(frozen=True)
class SquareAccount:
	"""Where a connection's gift cards and loyalty go. Secrets are never rendered."""

	environment: str
	location_id: str
	access_token: str = field(repr=False)
	endpoint_override: str | None = None

	@property
	def configured(self) -> bool:
		return bool(self.access_token and self.location_id)


class SquareAuthorizer:
	def __init__(self, access_token: str) -> None:
		self._token = access_token

	def headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self._token}", "Square-Version": API_VERSION}

	def renew(self) -> bool:
		return False

	def __repr__(self) -> str:
		return "SquareAuthorizer(access_token=[redacted])"


def key(*parts: str) -> str:
	"""An idempotency key for one write: a hash of what it is (Square takes up to 45 characters)."""
	return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:IDEMPOTENCY_KEY_LENGTH]


def to_minor_units(amount: Decimal, currency: str) -> int:
	"""An exact amount in the currency's smallest unit (19.99 CAD → 1999); finer amounts are refused."""
	scaled = amount.scaleb(minor_units(currency))
	if scaled != scaled.to_integral_value():
		raise SourceError(f"{amount} {currency} is finer than the currency's smallest unit.")
	return int(scaled)


def money(amount: Decimal, currency: str) -> dict[str, JsonValue]:
	return {"amount": to_minor_units(amount, currency), "currency": currency}


class SquareStoredValue:
	def __init__(self, account: SquareAccount, transport: HttpTransport, sleeper: Sleeper) -> None:
		if not account.configured:
			raise SourceError(
				"Square gift cards and loyalty are not configured: enter the Square access token and location."
			)
		base = account.endpoint_override or BASE_URLS[account.environment]
		self.location_id = account.location_id
		self._client = RestClient(LABEL, base, SquareAuthorizer(account.access_token), transport, sleeper)

	def _write(self, path: str, body: dict[str, JsonValue]) -> Node:
		result = self._client.query(path, body)
		if result is None:
			raise SourceError(f"Square has no {path}.")
		return result.node

	# ── gift cards ───────────────────────────────────────────────────────────────────────────────

	def card(self, card_id: str) -> Node | None:
		result = self._client.get(f"/v2/gift-cards/{quote(card_id, safe='')}")
		return result.node.child("gift_card") if result else None

	def card_by_gan(self, gan: str) -> Node | None:
		result = self._client.query("/v2/gift-cards/from-gan", {"gan": gan})
		return result.node.child("gift_card") if result else None

	def create_card(self, gan: str, idempotency_key: str) -> Node:
		"""A new digital card with a custom GAN (PENDING, no balance)."""
		return self._write(
			"/v2/gift-cards",
			{
				"idempotency_key": idempotency_key,
				"location_id": self.location_id,
				"gift_card": {"type": "DIGITAL", "gan_source": "OTHER", "gan": gan},
			},
		).required_child("gift_card")

	def activity(
		self, type_: str, card_id: str, amount: Decimal, currency: str, source_gid: str, idempotency_key: str
	) -> Node:
		"""An ACTIVATE, ADJUST_INCREMENT or ADJUST_DECREMENT. Activating outside Square's Orders API takes the
		buyer's payment instruments for Square's compliance checks; a migrated card has none, so the source
		card's identity stands in for them."""
		details: dict[str, JsonValue] = (
			{
				"amount_money": money(amount, currency),
				"buyer_payment_instrument_ids": [source_gid],
				"reference_id": source_gid,
			}
			if type_ == ACTIVATE
			else {"amount_money": money(amount, currency), "reason": ADJUSTMENT_REASON}
		)
		return self._write(
			"/v2/gift-cards/activities",
			{
				"idempotency_key": idempotency_key,
				"gift_card_activity": {
					"type": type_,
					"location_id": self.location_id,
					"gift_card_id": card_id,
					DETAILS[type_]: details,
				},
			},
		).required_child("gift_card_activity")

	def link_customer(self, card_id: str, customer_id: str) -> Node:
		return self._write(
			f"/v2/gift-cards/{quote(card_id, safe='')}/link-customer", {"customer_id": customer_id}
		).required_child("gift_card")

	# ── customers ────────────────────────────────────────────────────────────────────────────────

	def customer(self, email: str, given_name: str | None, family_name: str | None, reference_id: str) -> str:
		"""The Square customer with this email, created when there is none; returns its id."""
		result = self._client.query(
			"/v2/customers/search", {"limit": 1, "query": {"filter": {"email_address": {"exact": email}}}}
		)
		found = result.node.children("customers") if result else []
		if found:
			return found[0].string("id")
		values: dict[str, JsonValue] = {
			"idempotency_key": key("customer", reference_id),
			"given_name": given_name,
			"family_name": family_name,
			"email_address": email,
			"reference_id": reference_id,
		}
		return (
			self._write("/v2/customers", {k: v for k, v in values.items() if v})
			.required_child("customer")
			.string("id")
		)

	# ── loyalty ──────────────────────────────────────────────────────────────────────────────────

	def loyalty_program_id(self) -> str:
		result = self._client.get("/v2/loyalty/programs/main")
		program = result.node.child("program") if result else None
		if program is None:
			raise SourceError(
				"The Square seller has no loyalty program (Square Loyalty needs its own subscription)."
			)
		return program.string("id")

	def loyalty_account(self, account_id: str) -> Node | None:
		result = self._client.get(f"/v2/loyalty/accounts/{quote(account_id, safe='')}")
		return result.node.child("loyalty_account") if result else None

	def loyalty_account_by_phone(self, phone: str) -> Node | None:
		result = self._client.query(
			"/v2/loyalty/accounts/search", {"query": {"mappings": [{"phone_number": phone}]}, "limit": 1}
		)
		found = result.node.children("loyalty_accounts") if result else []
		return found[0] if found else None

	def create_loyalty_account(
		self, program_id: str, phone: str, customer_id: str | None, idempotency_key: str
	) -> Node:
		account: dict[str, JsonValue] = {"program_id": program_id, "mapping": {"phone_number": phone}}
		if customer_id:
			account["customer_id"] = customer_id
		return self._write(
			"/v2/loyalty/accounts", {"loyalty_account": account, "idempotency_key": idempotency_key}
		).required_child("loyalty_account")

	def adjust_points(self, account_id: str, points: int, idempotency_key: str) -> Node:
		"""Adds (or, negative, removes) points outside a purchase; returns the ADJUST_POINTS event."""
		return self._write(
			f"/v2/loyalty/accounts/{quote(account_id, safe='')}/adjust",
			{
				"idempotency_key": idempotency_key,
				"adjust_points": {"points": points, "reason": POINTS_REASON},
			},
		).required_child("event")
