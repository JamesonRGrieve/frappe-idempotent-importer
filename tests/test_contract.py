# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared field contract's helpers and the naming rules writers apply."""

from __future__ import annotations

from decimal import Decimal

import pytest

from idempotent_importer.contract import (
	Charge,
	booked,
	order_line,
	order_remarks,
	rating_fraction,
	refund_balance,
)
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Fields, Values
from idempotent_importer.naming import (
	item_code,
	named_after,
	plain_text,
	route,
	safe_name,
	suffixed,
	unique_abbr,
)


class TestOrderLines:
	def test_rates_are_exact(self) -> None:
		line = order_line("1", "p", None, "Toque", Decimal(2), Decimal("50.00"), "CAD")
		assert line.rate == Decimal("25.00")
		returned = order_line("1", "p", None, "Toque", Decimal(-1), Decimal("-25.00"), "CAD")
		assert returned.rate == Decimal("25.00")

	def test_an_amount_with_no_exact_unit_price_is_unsupported(self) -> None:
		with pytest.raises(Unsupported, match="no exact unit price"):
			order_line("1", "p", None, "Pens", Decimal(3), Decimal("10.00"), "CAD")
		assert order_line("1", "p", None, "Yen", Decimal(2), Decimal(1000), "JPY").rate == Decimal(500)

	def test_a_refund_balances_with_an_adjustment_line(self) -> None:
		lines = [order_line("1", "p", None, "Toque", Decimal(-1), Decimal("-25.00"), "CAD")]
		charges = [Charge("tax", "GST", Decimal("-1.25"))]
		assert refund_balance("r", Decimal("26.25"), lines, charges, "CAD") is None
		extra = refund_balance("r", Decimal("36.25"), lines, charges, "CAD")
		assert extra is not None
		assert (extra.qty, extra.rate, extra.product) == (Decimal(-1), Decimal("10.00"), None)
		amount_only = refund_balance("r", Decimal("5.00"), [], [], "CAD")
		assert amount_only is not None
		assert amount_only.qty * amount_only.rate == Decimal("-5.00")


def test_booked_ignores_which_product_a_line_named() -> None:
	invoice: Fields = {"total": Decimal(1), "lines": [{"product": "a", "variant": "b", "qty": Decimal(1)}]}
	assert booked(invoice) == {"total": Decimal(1), "lines": [{"qty": Decimal(1)}]}
	assert booked({"total": Decimal(1)}) == {"total": Decimal(1)}


def test_order_remarks() -> None:
	remarks = order_remarks(
		Values(
			{
				"number": "501",
				"remarks": "Gift wrap",
				"guest": {"name": "Ana", "email": "ana@example.test", "phone": None},
				"billing": {"name": "Ana", "line1": "1 Main", "city": "Calgary", "country_code": "CA"},
				"shipping": None,
			}
		)
	)
	assert (
		remarks
		== "Order 501\nNote: Gift wrap\nGuest: Ana, ana@example.test\nBilling: Ana, 1 Main, Calgary, CA"
	)


def test_ratings() -> None:
	assert rating_fraction(4, 5) == Decimal("0.8")
	with pytest.raises(Unsupported):
		rating_fraction(6, 5)


class TestNaming:
	def test_item_codes(self) -> None:
		assert item_code("WA", "TOQUE-1", "gid://woocommerce/woo-a/Product/101") == "WA-TOQUE-1"
		assert (
			item_code("WA", None, "gid://woocommerce/woo-a/ProductVariation/1022")
			== "WA-ProductVariation-1022"
		)
		assert item_code("WA", "A/B", "gid://woocommerce/woo-a/Product/1") == "WA-A-B"
		assert len(item_code("WA", "x" * 300, "gid://woocommerce/woo-a/Product/1")) == 140

	def test_names_and_routes(self) -> None:
		assert suffixed("Hoodies", "WA") == "Hoodies - WA"
		assert safe_name('Say "hi" 50%') == "Say -hi- 50-"
		assert route("store-a", "/about-us/") == "store-a/about-us"
		assert route("", "about-us") == "about-us"
		assert named_after("store-a/about-us") == "store-a-about-us"

	def test_unique_abbreviations(self) -> None:
		taken: set[str] = set()
		assert unique_abbr("Navy blue", taken) == "NAVYBLUE"
		assert unique_abbr("Navy-blue", taken) == "NAVYBLUE2"
		assert unique_abbr("!!", taken) == "V"

	def test_plain_text(self) -> None:
		assert plain_text("<p>So <b>warm</b> &amp; soft.</p>") == "So warm & soft."
		assert plain_text(None) == ""
