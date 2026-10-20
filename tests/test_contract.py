# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared field contract's helpers and the naming rules writers apply."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import ClassVar

import pytest

from idempotent_importer import contract
from idempotent_importer.contract import (
	RECEIVE,
	Allocation,
	Charge,
	Code,
	Condition,
	Discount,
	Employee,
	JournalLine,
	Lead,
	LedgerAccount,
	Opportunity,
	OrderLine,
	Party,
	Payment,
	TradeDocument,
	TradeLine,
	TradeTax,
	account_fields,
	booked,
	conditions_json,
	conditions_summary,
	date_of,
	discount_fields,
	employee_fields,
	included_taxes,
	journal_fields,
	lead_fields,
	line_fields,
	opportunity_fields,
	order_line,
	order_remarks,
	party_fields,
	payment_fields,
	project_fields,
	rating_fraction,
	refund_balance,
	required_date,
	trade_fields,
	tree_fields,
	variant_attributes,
	warehouse_fields,
)
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Fields, Values
from idempotent_importer.core.kind import Kind
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
	assert "held" not in fields and "conditions" not in fields


def test_conditions_are_recorded_and_hold_their_discount(monkeypatch: pytest.MonkeyPatch) -> None:
	discount = Discount(
		["A", "B"],
		None,
		"percentage",
		"transaction",
		Decimal(10),
		[],
		[],
		None,
		None,
		None,
		None,
		conditions=[
			Condition("cap", {"amount": Decimal("25.50"), "unused": None}),
			Condition("once_per_customer"),
		],
		code_limits={"B": Code(max_uses=1, conditions=[Condition("per_customer_limit", {"uses": 1})])},
	)
	fields = discount_fields(discount)
	assert fields["conditions"] == [
		{"kind": "cap", "card": "card 407", "terms": {"amount": Decimal("25.50")}},
		{"kind": "once_per_customer", "card": "card 405", "terms": {}},
	]
	assert fields["held"] == ["card 405", "card 407"]
	assert fields["code_limits"] == {
		"B": {
			"max_uses": 1,
			"conditions": [{"kind": "per_customer_limit", "card": "card 405", "terms": {"uses": 1}}],
			"held": ["card 405"],
		}
	}
	recorded = Values(fields).records("conditions")
	assert conditions_json(recorded) == (
		'[{"card": "card 407", "kind": "cap", "terms": {"amount": "25.5"}}, '
		'{"card": "card 405", "kind": "once_per_customer", "terms": {}}]'
	)
	assert conditions_json([]) is None
	assert conditions_summary(recorded) == "cap (card 407), once_per_customer (card 405)"
	monkeypatch.setattr(contract, "ENFORCED", frozenset({"once_per_customer"}))
	assert discount_fields(discount)["held"] == ["card 407"], "an enforced condition no longer holds it"
	assert conditions_summary(recorded) == "cap (card 407)"
	monkeypatch.setattr(contract, "ENFORCED", frozenset(contract.CONDITIONS))
	assert "held" not in discount_fields(discount), "every condition enforced: the discount is enabled"
	with pytest.raises(ValueError, match="not a discount condition"):
		Condition("anything")


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


class TestBooks:
	"""The ERP and HR sources' contract: documents that must add up, payments that can't over-allocate,
	journals that must balance, and the closed vocabularies writers rely on."""

	def document(self, total: str, date: str = "2026-03-01", due_date: str | None = None) -> TradeDocument:
		line = TradeLine(
			order_line("1", "gid://x/c/Product/1", None, "Bolts", Decimal(4), Decimal("10.00"), "CAD"),
			account="gid://x/c/Account/400",
		)
		return TradeDocument(
			number="INV/1",
			date=date,
			due_date=due_date,
			party="gid://x/c/Partner/7",
			currency="CAD",
			lines=[line],
			taxes=[TradeTax("GST 5%", Decimal("0.50"), "gid://x/c/Account/220")],
			total=Decimal(total),
			submitted=True,
		)

	def test_a_trade_document_must_make_its_total(self) -> None:
		fields = trade_fields(self.document("10.50"))
		assert fields["total"] == Decimal("10.50")
		lines = fields["lines"]
		assert isinstance(lines, list) and isinstance(lines[0], dict)
		assert (lines[0]["rate"], lines[0]["account"], lines[0]["cost_center"]) == (
			Decimal("2.50"),
			"gid://x/c/Account/400",
			None,
		)
		assert fields["taxes"] == [
			{"description": "GST 5%", "amount": Decimal("0.50"), "account": "gid://x/c/Account/220"}
		]
		with pytest.raises(Unsupported, match=r"come to 10\.5, not its total 11"):
			trade_fields(self.document("11"))

	def test_a_trade_document_needs_real_dates(self) -> None:
		with pytest.raises(ValueError, match="date"):
			trade_fields(self.document("10.50", date="01/03/2026"))
		assert trade_fields(self.document("10.50", due_date="2026-04-01"))["due_date"] == "2026-04-01"
		assert trade_fields(self.document("10.50"))["return_settles"] is True
		with pytest.raises(ValueError, match="missing"):
			required_date(None, "a date")

	def test_payments_allocate_no_more_than_they_are(self) -> None:
		allocation = Allocation(Kind.SALES_INVOICE, "gid://x/c/Move/1", Decimal("10.50"))
		payment = Payment(
			RECEIVE, "Customer", "gid://x/c/Partner/7", "2026-03-02", Decimal(20), "CAD", [allocation]
		)
		fields = payment_fields(payment)
		assert fields["allocations"] == [
			{"kind": "sales_invoice", "document": "gid://x/c/Move/1", "amount": Decimal("10.50")}
		]
		with pytest.raises(Unsupported, match=r"allocated 10\.5"):
			payment_fields(replace(payment, amount=Decimal(5)))
		with pytest.raises(Unsupported):
			payment_fields(replace(payment, amount=Decimal(0), allocations=[]))
		with pytest.raises(ValueError, match="party type"):
			payment_fields(replace(payment, party_type="Vendor"))
		with pytest.raises(ValueError, match="payment type"):
			payment_fields(replace(payment, payment_type="Refund"))
		assert payment_fields(replace(payment, submitted=False))["submitted"] is False

	def test_journals_balance(self) -> None:
		lines = [
			JournalLine("gid://x/c/Account/1", Decimal(5), Decimal(0), "Supplier", "gid://x/c/Partner/9"),
			JournalLine("gid://x/c/Account/2", Decimal(0), Decimal(5)),
			JournalLine("gid://x/c/Account/3", Decimal(0), Decimal(0)),
		]
		lines[1] = replace(lines[1], against=(Kind.PURCHASE_INVOICE, "gid://x/c/Move/4"))
		fields = journal_fields("MISC/1", "2026-03-03", lines, True, "Accrual")
		assert isinstance(fields["lines"], list) and len(fields["lines"]) == 2, "an empty line is left out"
		assert isinstance(fields["lines"][1], dict)
		assert fields["lines"][1]["against"] == {"kind": "purchase_invoice", "document": "gid://x/c/Move/4"}
		with pytest.raises(Unsupported, match="doesn't balance"):
			journal_fields("MISC/2", "2026-03-03", lines[:1], True)
		with pytest.raises(Unsupported, match="both a debit and a credit"):
			journal_fields(
				"MISC/3",
				"2026-03-03",
				[JournalLine("a", Decimal(5), Decimal(5)), JournalLine("b", Decimal(0), Decimal(0))],
				True,
			)
		with pytest.raises(ValueError, match="party type"):
			journal_fields(
				"MISC/4",
				"2026-03-03",
				[
					JournalLine("a", Decimal(1), Decimal(0), "Vendor", "p"),
					JournalLine("b", Decimal(0), Decimal(1)),
				],
				True,
			)

	def test_parties_and_accounts(self) -> None:
		party = Party("Acme Ltd", "Company", company="Acme Ltd", tax_id="123")
		assert party_fields(party)["tax_id"] == "123"
		assert "currency" not in party_fields(party), "absent extras keep older change signals"
		with pytest.raises(ValueError, match="party type"):
			party_fields(Party("Acme", "Partnership"))
		account = account_fields(LedgerAccount("4000", "Sales", "Income", "Income Account"))
		assert (account["root_type"], account["is_group"], account["parent"]) == ("Income", False, None)
		with pytest.raises(ValueError, match="root type"):
			account_fields(LedgerAccount(None, "Odd", "Revenue"))
		assert tree_fields("Main", None) == {"name": "Main", "parent": None, "is_group": False}
		assert warehouse_fields("WH", "gid://x/c/Warehouse/1", True, None)["address"] is None

	def test_crm_and_projects(self) -> None:
		assert project_fields("Fit-out", "Open", start="2026-01-01")["start"] == "2026-01-01"
		with pytest.raises(ValueError, match="project status"):
			project_fields("Fit-out", "Running")
		assert lead_fields(Lead("Open", company="Acme"))["company"] == "Acme"
		with pytest.raises(Unsupported, match="no name"):
			lead_fields(Lead("Open"))
		opportunity = Opportunity(
			Kind.LEAD, "gid://x/c/Lead/1", "Fit-out", "Open", "2026-02-01", "CAD", Decimal(500)
		)
		assert opportunity_fields(opportunity)["party_kind"] == "lead"
		with pytest.raises(ValueError, match="lead or a customer"):
			opportunity_fields(replace(opportunity, party_kind=Kind.SUPPLIER))
		with pytest.raises(ValueError, match="opportunity status"):
			opportunity_fields(replace(opportunity, status="Won"))

	def test_employees(self) -> None:
		employee = Employee(
			"Avery", "Active", last_name="Writer", gender="Female", date_of_joining="2024-05-01"
		)
		fields = employee_fields(employee)
		assert (fields["gender"], fields["date_of_birth"]) == ("Female", None), "an unknown date stays empty"
		with pytest.raises(ValueError, match="gender"):
			employee_fields(replace(employee, gender="F"))
		with pytest.raises(ValueError, match="employee status"):
			employee_fields(replace(employee, status="Gone"))
