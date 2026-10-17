# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared field contract's helpers and the naming rules writers apply."""

from __future__ import annotations

from decimal import Decimal
from typing import ClassVar

import pytest

from idempotent_importer.contract import (
	Charge,
	Discount,
	OrderLine,
	booked,
	date_of,
	discount_fields,
	included_taxes,
	line_fields,
	order_line,
	order_remarks,
	rating_fraction,
	refund_balance,
	variant_attributes,
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


class TestIncludedTaxes:
	LINES: ClassVar[list[OrderLine]] = [
		order_line("1", "p", None, "Toque", Decimal(1), Decimal("21.00"), "CAD"),
		order_line("2", "q", None, "Bread", Decimal(1), Decimal("10.50"), "CAD"),
	]

	def test_one_set_of_rates_is_carried_by_the_charges(self) -> None:
		rates = [{"GST": Decimal(5)}, {"GST": Decimal(5)}]
		lines, charges = included_taxes(self.LINES, rates, {"GST": Decimal("1.50")})
		assert lines == self.LINES
		assert charges == [Charge("included_tax", "GST", Decimal("1.50"), Decimal(5))]
		assert "taxes" not in line_fields(lines[0])

	def test_lines_taxed_differently_carry_their_own_rates(self) -> None:
		rates = [{"GST": Decimal(5), "PST": Decimal(7)}, {"GST": Decimal(5)}]
		amounts = {"GST": Decimal("1.50"), "PST": Decimal("1.40"), "HST": Decimal(0)}
		lines, charges = included_taxes(self.LINES, rates, amounts)
		assert charges == [
			Charge("included_tax", "GST", Decimal("1.50")),
			Charge("included_tax", "PST", Decimal("1.40")),
		]
		assert [line_fields(line)["taxes"] for line in lines] == [
			[{"title": "GST", "rate": Decimal(5)}, {"title": "PST", "rate": Decimal(7)}],
			[{"title": "GST", "rate": Decimal(5)}],
		]

	def test_a_tax_exempt_line_has_no_rates(self) -> None:
		lines, charges = included_taxes(self.LINES, [{"GST": Decimal(5)}, {}], {"GST": Decimal("1.00")})
		assert [line.taxes for line in lines] == [(("GST", Decimal(5)),), ()]
		assert charges[0].rate is None

	def test_rates_are_one_per_line(self) -> None:
		with pytest.raises(ValueError, match="one set of rates per line"):
			included_taxes(self.LINES, [], {})


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


def test_variant_attributes_keep_first_use_order() -> None:
	variants: list[Fields] = [
		{"attributes": {"Colour": "Red", "Size": "S"}},
		{"attributes": {"Colour": "Navy", "Size": "S"}},
		{"attributes": {"Colour": "Red", "Size": "M"}},
	]
	assert variant_attributes(variants) == [
		{"name": "Colour", "values": ["Red", "Navy"]},
		{"name": "Size", "values": ["S", "M"]},
	]


def test_discount_fields_and_dates() -> None:
	discount = Discount(
		["A", "B"], None, "amount", "items", Decimal(5), ["gid://x/c/P/1"], [], None, None, None, 3
	)
	fields = discount_fields(discount)
	assert (fields["codes"], fields["items"], fields["max_uses"]) == (["A", "B"], ["gid://x/c/P/1"], 3)
	assert date_of("2026-09-01T10:00:00Z") == "2026-09-01"
	assert date_of(None) is None


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
