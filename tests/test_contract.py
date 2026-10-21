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
	Attendance,
	Charge,
	Checkin,
	Code,
	Communication,
	Condition,
	Discount,
	Employee,
	Event,
	JournalLine,
	Lead,
	LeaveApplication,
	LedgerAccount,
	Note,
	Opportunity,
	OrderLine,
	Party,
	Payment,
	Prospect,
	ShiftAssignment,
	ShippingDiscount,
	Tier,
	ToDo,
	TradeDocument,
	TradeLine,
	TradeTax,
	account_fields,
	attendance_fields,
	booked,
	checkin_fields,
	communication_fields,
	conditions_json,
	conditions_summary,
	coupon_limits,
	date_of,
	discount_fields,
	employee_fields,
	enforced_conditions,
	event_fields,
	included_taxes,
	is_enforced,
	journal_fields,
	lead_fields,
	leave_application_fields,
	leave_type_fields,
	line_fields,
	opportunity_fields,
	opportunity_type_fields,
	order_line,
	order_remarks,
	party_fields,
	payment_fields,
	project_fields,
	prospect_fields,
	rating_fraction,
	refund_balance,
	required_date,
	shift_assignment_fields,
	shipping_discount,
	todo_fields,
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
			Condition("item_limit", {"qty": Decimal("2.5"), "unused": None}),
			Condition("once_per_customer"),
		],
		code_limits={"B": Code(max_uses=1, conditions=[Condition("per_customer_limit", {"uses": 1})])},
	)
	fields = discount_fields(discount)
	assert fields["conditions"] == [
		{"kind": "item_limit", "card": "card 407", "terms": {"qty": Decimal("2.5")}},
		{"kind": "once_per_customer", "card": "card 405", "terms": {}},
	]
	assert fields["held"] == ["card 407"], (
		"the fork enforces once per customer: only the item limit, in no order, holds it"
	)
	assert fields["code_limits"] == {
		"B": {
			"max_uses": 1,
			"conditions": [{"kind": "per_customer_limit", "card": "card 405", "terms": {"uses": 1}}],
		}
	}
	recorded = Values(fields).records("conditions")
	assert conditions_json(recorded) == (
		'[{"card": "card 407", "kind": "item_limit", "terms": {"qty": "2.5"}}, '
		'{"card": "card 405", "kind": "once_per_customer", "terms": {}}]'
	)
	assert conditions_json([]) is None
	assert conditions_summary(recorded, coded=True, single_rule=True) == "item_limit (card 407)"
	assert conditions_summary(recorded, coded=False, single_rule=True) == (
		"item_limit (card 407), once_per_customer (card 405)"
	), "without a code, nothing counts a customer's uses"
	monkeypatch.setattr(contract, "ENFORCED", {})
	assert discount_fields(discount)["held"] == ["card 405", "card 407"], "nothing enforced: both hold it"
	monkeypatch.setattr(
		contract,
		"ENFORCED",
		{kind: contract.Enforcement(frozenset({"qty"})) for kind in contract.CONDITIONS},
	)
	assert "held" not in discount_fields(discount), "every condition enforced: the discount is enabled"
	with pytest.raises(ValueError, match="not a discount condition"):
		Condition("anything")


class TestEnforcement:
	"""What the Webshop fork enforces (card 405), and what it is given."""

	def test_the_fork_enforces_known_terms_only(self) -> None:
		assert is_enforced("per_customer_limit", {"uses": 2}, coded=True, single_rule=True)
		assert not is_enforced("per_customer_limit", {"uses": 2}, coded=False, single_rule=True), "automatic"
		assert is_enforced("free_shipping", {"max_shipping_price": None}, coded=False, single_rule=True)
		assert not is_enforced("free_shipping", {}, coded=False, single_rule=False), "a Promotional Scheme"
		assert not is_enforced("free_shipping", {"unread": ["destinations"]}, coded=True, single_rule=True)
		assert not is_enforced("free_shipping", {"methods": ["2"]}, coded=True, single_rule=True)
		assert not is_enforced("shipping_discount", {"discount": "amount", "source": {}}, True, True)
		assert contract.CONDITIONS["several_customers"] == contract.COUPON_LIMITS

	def test_the_fork_enforces_rule_limits(self) -> None:
		"""Card 407: on one Pricing Rule, coded or automatic, with every term it needs and none it doesn't read."""
		assert is_enforced("cap", {"amount": Decimal(5)}, coded=False, single_rule=True)
		assert not is_enforced("cap", {"amount": Decimal(5)}, coded=True, single_rule=False), "a scheme"
		assert not is_enforced("cap", {"amount": Decimal(5), "undocumented": ["scope"]}, True, True)
		assert is_enforced("item_limit", {"qty": Decimal(2), "order": "cheapest"}, False, True)
		assert not is_enforced("item_limit", {"qty": Decimal(2)}, False, True), "in no known order"
		assert is_enforced(
			"fixed_price_set", {"qty": 3, "price": 10, "order": "dearest", "items": ["p"]}, True, True
		)
		assert not is_enforced("fixed_price_set", {"qty": 3, "price": 10, "items": ["p"]}, True, True)
		assert is_enforced("customer_segments", {"groups": ["g"]}, coded=False, single_rule=True)
		assert not is_enforced("customer_segments", {}, coded=False, single_rule=True), "no members"
		assert not is_enforced("customer_segments", {"source": {"id": ["s"]}}, False, True)
		assert is_enforced("customer_history", {"new": True}, coded=True, single_rule=True)
		assert not is_enforced("customer_history", {}, coded=True, single_rule=True)
		assert is_enforced("spread_amount", {"items": ["p"]}, coded=True, single_rule=True)
		assert not is_enforced("spread_amount", {"items": ["p"], "undocumented": ["x"]}, True, True)
		for kind in ("item_choice", "item_matcher", "rule_condition"):
			assert not is_enforced(kind, {"source": {}}, coded=True, single_rule=True), kind

	def test_rule_limits(self) -> None:
		limits = contract.rule_limits(
			self.recorded(
				Condition("several_customer_groups", {"groups": ["g1", "g2"]}),
				Condition("several_customer_groups", {"groups": ["g2", "g3"]}),
				Condition("excluded_customer_groups", {"groups": ["g9"]}),
				Condition("customer_history", {"min_orders": 3}),
				Condition("customer_history", {"returning": True}),
				Condition("cap", {"amount": Decimal(30)}),
				Condition("cap", {"amount": Decimal(20)}),
				Condition("spread_amount", {"items": ["p1"], "variants": ["v1"]}),
				Condition("item_limit", {"qty": Decimal(4), "order": "dearest"}),
			)
		)
		assert limits == contract.RuleLimits(
			groups=["g2"],
			excluded_groups=["g9"],
			min_orders=3,
			cap=Decimal(20),
			spread=contract.Targets(items=["p1"], variants=["v1"]),
			max_qty=Decimal(4),
			qty_order="dearest",
		)
		assert contract.rule_limits([]) == contract.RuleLimits()
		segment = contract.rule_limits(
			self.recorded(
				Condition("customer_segments", {"customers": ["c1"], "groups": ["g1"]}),
				Condition("customer_history", {"new": True}),
			)
		)
		assert (segment.customers, segment.groups, segment.new_customers) == (["c1"], ["g1"], True)
		fixed = contract.rule_limits(
			self.recorded(
				Condition(
					"fixed_price_set",
					{"qty": Decimal(3), "price": Decimal(10), "order": "cheapest", "repeat": True},
				)
			)
		).fixed_set
		assert fixed == contract.FixedPriceSet(Decimal(3), Decimal(10), "cheapest", True, contract.Targets())

	@pytest.mark.parametrize(
		("conditions", "message"),
		[
			(
				[
					Condition("customer_segments", {"groups": ["g1"]}),
					Condition("several_customer_groups", {"groups": ["g1"]}),
				],
				"segment and customer groups",
			),
			([Condition("item_limit", {"qty": 1, "order": "x"})] * 2, "two of item_limit"),
			(
				[
					Condition("fixed_price_set", {"qty": 3, "price": 10, "order": "cheapest"}),
					Condition("spread_amount", {"items": ["p"]}),
				],
				"a set alone",
			),
			(
				[
					Condition("customer_history", {"new": True}),
					Condition("customer_history", {"min_orders": 2}),
				],
				"earlier orders",
			),
		],
	)
	def test_rule_limits_the_fork_cant_combine(self, conditions: list[Condition], message: str) -> None:
		with pytest.raises(Unsupported, match=message):
			contract.rule_limits(self.recorded(*conditions))

	def test_held_by_scheme_or_automatic(self) -> None:
		shipping = Condition("free_shipping")
		tiered = Discount(
			[], None, "percentage", "transaction", Decimal(5), [], [], None, None, None, None,
			automatic=True,
			tiers=[Tier("percentage", Decimal(5), min_qty=Decimal(2))],
			conditions=[shipping],
		)  # fmt: skip
		assert discount_fields(tiered)["held"] == ["card 405"], "the fork gives shipping discounts on a rule"
		assert "held" not in discount_fields(replace(tiered, tiers=[])), "on one Pricing Rule it does"
		once = replace(tiered, tiers=[], conditions=[Condition("once_per_customer")])
		assert discount_fields(once)["held"] == ["card 405"], "an automatic discount has no uses to count"

	def recorded(self, *conditions: Condition) -> list[Values]:
		return [Values(c) for c in contract.condition_fields(list(conditions)) if isinstance(c, dict)]

	def test_coupon_limits(self) -> None:
		conditions = self.recorded(
			Condition("per_customer_limit", {"uses": 3}),
			Condition("once_per_customer"),
			Condition("several_customers", {"customers": ["c1", "c2", "c3"]}),
			Condition("several_customers", {"customers": ["c3", "c2"]}),
			Condition("emails", {"emails": ["A@Example.com", "*@example.org"]}),
			Condition("item_matcher", {"source": {"brands": [1]}}),
		)
		limits = coupon_limits(conditions)
		assert (limits.per_customer, limits.customers, limits.emails) == (
			1,
			["c2", "c3"],
			["a@example.com", "*@example.org"],
		)
		assert coupon_limits([]) == contract.CouponLimits()
		enforced = enforced_conditions(conditions, coded=True, single_rule=True)
		assert [c.string("kind") for c in enforced][-1] == "emails", (
			"a source's own item criteria aren't the fork's"
		)

	def test_shipping_discounts(self) -> None:
		free = self.recorded(
			Condition(
				"free_shipping",
				{"max_shipping_price": Decimal(20), "countries": ["ca"], "items": ["p1"], "groups": ["g1"]},
			)
		)
		assert shipping_discount(free) == ShippingDiscount(
			"free", max_charge=Decimal(20), countries=["CA"], items=["p1"], groups=["g1"]
		)
		off = self.recorded(
			Condition(
				"shipping_discount", {"discount": "percentage", "value": Decimal(50), "cap": Decimal(8)}
			)
		)
		assert shipping_discount(off) == ShippingDiscount("percentage", Decimal(50), cap=Decimal(8))
		assert shipping_discount(self.recorded(Condition("once_per_customer"))) is None
		with pytest.raises(Unsupported, match="two shipping discounts"):
			shipping_discount(free + off)


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
		assert "discount" not in trade_fields(self.document("10.50")), (
			"absent extras keep older change signals"
		)

	def test_a_document_discount_and_freight(self) -> None:
		freight = TradeTax("Freight", Decimal("5.00"), role="shipping")
		document = replace(
			self.document("14.50"), taxes=[*self.document("0").taxes, freight], discount=Decimal("1.00")
		)
		fields = trade_fields(document)
		assert (fields["discount"], fields["taxes"]) == (
			Decimal("1.00"),
			[
				{"description": "GST 5%", "amount": Decimal("0.50"), "account": "gid://x/c/Account/220"},
				{"description": "Freight", "amount": Decimal("5.00"), "account": None, "role": "shipping"},
			],
		)
		with pytest.raises(ValueError, match="charge role"):
			trade_fields(replace(document, taxes=[TradeTax("Odd", Decimal("5.50"), role="duty")]))
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

	def test_the_pipelines_organisations_conversations_appointments_and_tasks(self) -> None:
		lead = Lead(
			"Lead", "Riley", crm_notes=[Note("gid://x/c/Note/1", "2026-09-02T14:30:00Z", "Call back")]
		)
		assert lead_fields(lead)["crm_notes"] == [
			{"gid": "gid://x/c/Note/1", "date": "2026-09-02T14:30:00Z", "text": "Call back"}
		]
		assert "crm_notes" not in lead_fields(Lead("Lead", "Riley")), "a lead without notes keeps its signal"
		deal = Opportunity(Kind.LEAD, "gid://x/c/Lead/1", "Rebrand", "Open", "2026-09-05")
		assert "opportunity_type" not in opportunity_fields(deal)
		typed = opportunity_fields(replace(deal, opportunity_type="gid://x/c/Pipeline/1"))
		assert typed["opportunity_type"] == "gid://x/c/Pipeline/1"
		assert prospect_fields(Prospect("River Co", leads=["gid://x/c/Lead/1"]))["addresses"] == []
		assert opportunity_type_fields("Sales") == {"name": "Sales", "description": None}
		message = Communication(
			Kind.LEAD, "gid://x/c/Lead/1", "SMS", True, "2026-09-01T12:05:00Z", "Hi", "Hi there"
		)
		assert communication_fields(message)["with"] == {"kind": "lead", "party": "gid://x/c/Lead/1"}
		with pytest.raises(ValueError, match="communication medium"):
			communication_fields(replace(message, medium="Fax"))
		with pytest.raises(ValueError, match="a lead, customer, prospect or opportunity"):
			communication_fields(replace(message, party_kind=Kind.SUPPLIER))
		about_deal = communication_fields(replace(message, party_kind=Kind.OPPORTUNITY))
		assert about_deal["with"] == {"kind": "opportunity", "party": "gid://x/c/Lead/1"}
		call = Event(
			"Discovery", "2026-10-22 10:00:00", "Open", participants=[(Kind.LEAD, "gid://x/c/Lead/1")]
		)
		assert event_fields(call)["participants"] == [{"kind": "lead", "party": "gid://x/c/Lead/1"}]
		assert event_fields(replace(call, starts_on="2026-10-22T10:00:00Z"))["starts_on"] == (
			"2026-10-22T10:00:00Z"
		), "a source keeping UTC times gives them as such"
		with pytest.raises(ValueError, match="HH:MM:SS"):
			event_fields(replace(call, starts_on="2026-10-22T10:00:00"))
		task = ToDo("Send samples", "Open", Kind.CUSTOMER, "gid://x/c/Customer/1", "2026-10-30")
		assert todo_fields(task)["about"] == {"kind": "customer", "party": "gid://x/c/Customer/1"}
		with pytest.raises(ValueError, match="task status"):
			todo_fields(replace(task, status="Done"))

	def test_crm_and_projects(self) -> None:
		assert project_fields("Fit-out", "Open", start="2026-01-01")["start"] == "2026-01-01"
		with pytest.raises(ValueError, match="project status"):
			project_fields("Fit-out", "Running")
		assert lead_fields(Lead("Open", company="Acme"))["company"] == "Acme"
		with pytest.raises(Unsupported, match="no name, organisation or email"):
			lead_fields(Lead("Open"))
		opportunity = Opportunity(
			Kind.LEAD, "gid://x/c/Lead/1", "Fit-out", "Open", "2026-02-01", "CAD", Decimal(500)
		)
		assert opportunity_fields(opportunity)["party_kind"] == "lead"
		with pytest.raises(ValueError, match="a lead, a customer or a prospect"):
			opportunity_fields(replace(opportunity, party_kind=Kind.SUPPLIER))
		with pytest.raises(ValueError, match="opportunity status"):
			opportunity_fields(replace(opportunity, status="Won"))

	def test_pipeline_stages_and_the_fields_users_add(self) -> None:
		deal = Opportunity(Kind.PROSPECT, "gid://x/c/Company/1", "Fit-out", "Open", "2026-02-01")
		plain = opportunity_fields(deal)
		assert "sales_stage" not in plain and "custom" not in plain, "an unstaged deal keeps its signal"
		staged = opportunity_fields(replace(deal, sales_stage="Qualified", custom={"b": "2", "a": "1"}))
		assert staged["sales_stage"] == "Qualified" and staged["party_kind"] == "prospect"
		custom = staged["custom"]
		assert isinstance(custom, dict) and list(custom) == ["a", "b"], "by label, in order"
		assert lead_fields(Lead("Lead", "Riley", custom={"Tier": "Gold"}))["custom"] == {"Tier": "Gold"}
		assert "custom" not in party_fields(Party("Acme", "Company"))
		assert party_fields(Party("Acme", "Company", custom={"Tier": "A"}))["custom"] == {"Tier": "A"}
		assert prospect_fields(Prospect("Acme", custom={"Size": "50"}))["custom"] == {"Size": "50"}
		assert contract.quoted_party(Kind.PROSPECT, "gid://x/c/Company/1") == {
			"kind": "prospect",
			"party": "gid://x/c/Company/1",
		}
		with pytest.raises(ValueError, match="a lead or a prospect"):
			contract.quoted_party(Kind.CUSTOMER, "gid://x/c/Company/1")
		noted = opportunity_fields(
			replace(deal, crm_notes=[Note("gid://x/c/Note/1", "2026-09-02T14:30:00Z", "Call back")])
		)
		assert noted["crm_notes"] == [
			{"gid": "gid://x/c/Note/1", "date": "2026-09-02T14:30:00Z", "text": "Call back"}
		]
		task = ToDo("Call", "Open", Kind.OPPORTUNITY, "gid://x/c/Deal/1", custom={"Kind": "Call"})
		assert todo_fields(task)["custom"] == {"Kind": "Call"}

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
		assert "branch" not in fields, "an employee without a branch keeps its change signal"
		assert employee_fields(replace(employee, branch="gid://x/c/Site/1"))["branch"] == "gid://x/c/Site/1"

	def test_shifts_punches_attendance_and_leave(self) -> None:
		shift = ShiftAssignment("gid://x/c/Member/1", "2026-10-05", "22:00:00", "06:00:00")
		assert shift_assignment_fields(shift)["shift"] == {
			"name": "22:00-06:00",
			"start_time": "22:00:00",
			"end_time": "06:00:00",
		}
		with pytest.raises(ValueError, match="time of day"):
			shift_assignment_fields(replace(shift, start_time="22:00"))
		with pytest.raises(ValueError, match="shift status"):
			shift_assignment_fields(replace(shift, status="Draft"))
		checkin = Checkin("gid://x/c/Member/1", "2026-10-05T11:58:12Z", "IN")
		assert checkin_fields(checkin)["log_type"] == "IN"
		with pytest.raises(ValueError, match="UTC time"):
			checkin_fields(replace(checkin, time="2026-10-05 07:58"))
		with pytest.raises(ValueError, match="log type"):
			checkin_fields(replace(checkin, log_type="BREAK"))
		day = Attendance("gid://x/c/Member/1", "2026-10-05", "Present", 450, "2026-10-05T12:00:00Z")
		assert attendance_fields(day)["working_minutes"] == 450
		with pytest.raises(Unsupported, match="minutes worked"):
			attendance_fields(replace(day, working_minutes=-1))
		with pytest.raises(ValueError, match="attendance status"):
			attendance_fields(replace(day, status="Late"))
		assert leave_type_fields("Vacation", True) == {"name": "Vacation", "paid": True}
		leave = LeaveApplication(
			"gid://x/c/Member/1",
			"gid://x/c/LeaveType/1",
			"2026-10-09",
			"2026-10-10",
			"Approved",
			"2026-09-01",
		)
		assert leave_application_fields(leave)["to_date"] == "2026-10-10"
		with pytest.raises(Unsupported, match="ending before it starts"):
			leave_application_fields(replace(leave, to_date="2026-10-08"))
		with pytest.raises(ValueError, match="leave status"):
			leave_application_fields(replace(leave, status="Pending"))
