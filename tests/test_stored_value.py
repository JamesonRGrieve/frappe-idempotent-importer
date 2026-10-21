# SPDX-License-Identifier: AGPL-3.0-or-later
"""Gift cards, store credit and loyalty mirrored into Square: the numbers, how a balance change is applied,
phone numbers, the Square client against the fake Square (tests/fixtures/square_stored_value, every request
body validated against Square's spec), and the sources' mappers (BigCommerce gift certificates,
WooCommerce Gift Cards, Wix Loyalty)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.source import Adapter, ConnectionConfig
from idempotent_importer.sources.bigcommerce.adapter import BigCommerceAdapter
from idempotent_importer.sources.wix.adapter import WixAdapter
from idempotent_importer.sources.woocommerce.adapter import WooCommerceAdapter
from idempotent_importer.sources.woocommerce.mapping import Context as WooContext
from idempotent_importer.sources.woocommerce.mapping import GiftCardMapper
from idempotent_importer.stored_value import numbers, plans
from idempotent_importer.stored_value.phone import e164
from idempotent_importer.stored_value.square import SquareAccount, SquareStoredValue, key
from tests.support.transport import FakeTransport, RecordingSleeper, rest_source

D = Decimal


def test_a_source_code_is_kept_when_square_can_take_it() -> None:
	assert numbers.from_source_code("4kz-x0o-uhi-jhc") == "4KZX0OUHIJHC"
	assert numbers.from_source_code("SHORT1") is None
	assert numbers.from_source_code("A" * 21) is None
	assert numbers.from_source_code("4111111111111111") is None, "a card BIN"
	assert numbers.from_source_code("7783320000001234") is None, "Square's digital gift card BIN"
	assert numbers.from_source_code("4KZ12345AB") == "4KZ12345AB"


def test_new_numbers_are_stable_keyed_and_the_same_as_the_wordpress_importers() -> None:
	gid = "gid://bigcommerce/GiftCertificate/902"
	assert numbers.generated(gid, "e2e-gift-card-key") == "9415999166351453", "wp-woo's GiftCardsTest value"
	assert numbers.generated(gid, "another") != numbers.generated(gid, "e2e-gift-card-key")
	assert numbers.from_source_code(numbers.generated(gid, "k")) is not None


def test_gift_card_steps() -> None:
	assert plans.gift_card_step(D(50), D(0), "PENDING", D(0), False) == plans.GiftCardStep(
		plans.ACTIVATE, D(50), D(50)
	)
	assert plans.gift_card_step(D(0), D(0), "PENDING", D(0), False).step is None
	drop = plans.gift_card_step(D(30), D(50), "ACTIVE", D(45), False)
	assert (drop.step, drop.amount, drop.loaded) == (plans.DECREMENT, D(20), D(30)), (
		"what was spent in Square stays spent"
	)
	held = plans.gift_card_step(D(60), D(50), "ACTIVE", D(50), False)
	assert (held.step, held.loaded) == (None, D(50)) and held.held and "load fee" in held.held
	assert plans.gift_card_step(D(60), D(50), "ACTIVE", D(50), True).step == plans.INCREMENT
	both = plans.gift_card_step(D(0), D(50), "ACTIVE", D(15), False)
	assert (both.step, both.amount, both.loaded) == (plans.DECREMENT, D(15), D(0))
	assert both.held and "35 was spent in both stores" in both.held
	blocked = plans.gift_card_step(D(30), D(50), "BLOCKED", D(50), True)
	assert blocked.step is None and blocked.held and "BLOCKED" in blocked.held


def test_points_steps() -> None:
	assert plans.points_step(120, 0, 0) == plans.PointsStep(120, 120)
	assert plans.points_step(70, 120, 200) == plans.PointsStep(-50, 70)
	short = plans.points_step(0, 120, 30)
	assert (short.points, short.loaded) == (-30, 0) and short.held and "90 were spent" in short.held


def test_phone_numbers() -> None:
	assert e164("+1 306 555 0177", "FR") == "+13065550177"
	assert e164("(306) 555-0177", "CA") == "+13065550177"
	assert e164("1-306-555-0177", "us") == "+13065550177"
	assert e164("06 12 34 56 78", "FR") is None
	assert e164("555-0177", "CA") is None


def square() -> SquareStoredValue:
	account = SquareAccount(
		"Sandbox", "LOCMAIN1", "e2e-square-token", "https://square_stored_value-fake:8080"
	)
	return SquareStoredValue(account, rest_source("square_stored_value", "v1"), RecordingSleeper())


def test_the_square_client_sends_exactly_what_square_documents() -> None:
	client = square()
	gid = "gid://bigcommerce/bigcommerce-c/GiftCertificate/901"
	assert client.card_by_gan("4KZX0OUHIJHC") is None, "not in Square yet"
	card = client.create_card("4KZX0OUHIJHC", key("card", gid))
	assert (card.string("id"), card.string("state")) == ("GC_BC901", "PENDING")
	activity = client.activity(
		plans.ACTIVATE, "GC_BC901", D(50), "CAD", gid, key(gid, plans.ACTIVATE, "0", "50")
	)
	assert activity.string("type") == "ACTIVATE"
	assert client.customer("ingrid@example.com", None, None, "x") == "SQC_INGRID"
	grace = "gid://bigcommerce/bigcommerce-c/GiftCertificate/902"
	assert client.customer("grace@example.com", "Grace", "Hopper", grace) == "SQC_GRACE", (
		"created when missing"
	)
	assert client.loyalty_program_id() == "LP_MAIN"


def test_square_must_be_configured() -> None:
	with pytest.raises(SourceError):
		SquareStoredValue(
			SquareAccount("Production", "", ""), rest_source("square_stored_value", "v1"), RecordingSleeper()
		)
	assert "token" not in repr(SquareAccount("Production", "L", "secret-token"))


def mapped(
	adapter: Adapter,
	config: ConnectionConfig,
	kind: Kind,
	transport: FakeTransport,
	mapper: Mapper | None = None,
) -> list[Values]:
	reader = adapter.reader(config, transport, RecordingSleeper())
	chosen = mapper or next(m for m in adapter.mappers(config) if m.kind == kind)
	found = []
	for node in reader.page(kind, None, None, "").nodes:
		desired = chosen.map(node)
		assert desired is not None
		found.append(desired.values())
	return found


BIGCOMMERCE = ConnectionConfig(
	"bigcommerce",
	"bigcommerce-c",
	"https://bigcommerce-fake:8080",
	api_secret="e2e-bigcommerce-token",
	account="bcfix123",
)


def test_bigcommerce_gift_certificates() -> None:
	active, pending = mapped(
		BigCommerceAdapter(), BIGCOMMERCE, Kind.GIFT_CARD, rest_source("bigcommerce", "v1")
	)
	assert (active.optional_string("code"), active.decimal("balance"), active.string("state")) == (
		"4KZ-X0O-UHI-JHC",
		D(50),
		"active",
	)
	assert active.optional_string("customer") == "gid://bigcommerce/bigcommerce-c/Customer/41"
	assert (active.optional_string("email"), active.optional_string("given_name")) == (
		"ingrid@example.com",
		"Ingrid",
	)
	[issue] = active.records("ledger")
	assert (issue.string("kind"), issue.optional_string("at"), issue.decimal("amount")) == (
		"issue",
		"2026-09-01T16:00:00Z",
		D(50),
	)
	assert pending.string("state") == "disabled"
	assert (active.flag("load_increases"), active.flag("new_numbers")) == (False, True)
	[spent, _] = mapped(BigCommerceAdapter(), BIGCOMMERCE, Kind.GIFT_CARD, rest_source("bigcommerce", "v2"))
	assert spent.decimal("balance") == D(30)
	assert [(e.string("kind"), e.decimal("amount")) for e in spent.records("ledger")] == [
		("issue", D(50)),
		("observed", D(-20)),
	]


WOO = ConnectionConfig(
	"woocommerce",
	"woo-a",
	"https://woo-a-fake:8080",
	api_user="e2e",
	api_secret="e2e-application-password",
	auth_method="Application Password",
)


def test_woocommerce_gift_cards() -> None:
	mapper = GiftCardMapper(WooContext(WOO), now=lambda: datetime(2026, 10, 20, tzinfo=UTC))
	redeemed, off = mapped(
		WooCommerceAdapter(), WOO, Kind.GIFT_CARD, rest_source("woocommerce", "v1"), mapper
	)
	assert (redeemed.optional_string("code"), redeemed.decimal("balance"), redeemed.string("currency")) == (
		"5QY5-E4AX-TAVR-LCPK",
		D(100),
		"CAD",
	)
	assert redeemed.optional_string("customer") == "gid://woocommerce/woo-a/Customer/201", (
		"redeemed into Ingrid's account"
	)
	assert off.string("state") == "disabled"
	[used] = mapped(WooCommerceAdapter(), WOO, Kind.GIFT_CARD, rest_source("woocommerce", "v2"), mapper)[:1]
	assert used.decimal("balance") == D(60)
	assert [(e.string("kind"), e.decimal("amount")) for e in used.records("ledger")] == [
		("issued", D(100)),
		("used", D(-40)),
	]


def test_an_expired_woocommerce_gift_card_carries_no_balance() -> None:
	later = GiftCardMapper(WooContext(WOO), now=lambda: datetime(2027, 1, 1, tzinfo=UTC))
	transport = rest_source("woocommerce", "v1")
	reader = WooCommerceAdapter().reader(WOO, transport, RecordingSleeper())
	node = reader.page(Kind.GIFT_CARD, None, None, "").nodes[0].with_fields(expire_date="2026-12-31 00:00:00")
	desired = later.map(node)
	assert desired is not None and desired.values().string("state") == "expired"


WIX = ConnectionConfig(
	"wix", "wix-x", "https://wix-fake:8080", api_secret="e2e-wix-key", account="e2e-wix-site"
)


def test_wix_loyalty_accounts() -> None:
	[account] = mapped(WixAdapter(), WIX, Kind.LOYALTY_ACCOUNT, rest_source("wix", "v1"))
	assert account.integer("points") == 120
	assert (
		account.optional_string("customer") == "gid://wix/wix-x/Contact/2c4e6a80-0001-4b3d-9e1f-5a6b7c8d0001"
	)
	assert account.optional_string("email") == "maya.thunderchild@example.com"
	assert [(e.string("kind"), e.integer("points")) for e in account.records("ledger")] == [
		("earn", 100),
		("adjust", 20),
	]
	[redeemed] = mapped(WixAdapter(), WIX, Kind.LOYALTY_ACCOUNT, rest_source("wix", "v2"))
	assert redeemed.integer("points") == 70
	assert (
		redeemed.records("ledger")[2].string("kind"),
		redeemed.records("ledger")[2].integer("points"),
	) == ("redeem", -50)
