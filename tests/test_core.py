# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pure core: identities, money, times, nodes, mapped fields and the change signal."""

from __future__ import annotations

from decimal import Decimal

import pytest

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.fields import Values
from idempotent_importer.core.hasher import canonical, content_hash, part_hash
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import SYNC_ORDER, Kind
from idempotent_importer.core.money import (
	decimal_of,
	decimal_text,
	exact_quotient,
	from_minor_units,
	is_exact_at,
	minor_units,
)
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import normalise, path_of, redirect_target, slugify
from idempotent_importer.core.times import iso_utc


class TestIdentity:
	def test_identities_carry_source_and_connection(self) -> None:
		gid = Identity.of("woocommerce", "woo-a", "Product", 101)
		assert gid == "gid://woocommerce/woo-a/Product/101"
		assert gid.startswith(Identity.prefix("woocommerce", "woo-a"))
		assert not gid.startswith(Identity.prefix("woocommerce", "woo-b"))

	def test_parse_round_trips(self) -> None:
		identity = Identity.parse("gid://shopify/shop-s/ProductVariant/3011")
		assert (identity.source, identity.connection, identity.type, identity.id) == (
			"shopify",
			"shop-s",
			"ProductVariant",
			"3011",
		)
		assert str(identity) == "gid://shopify/shop-s/ProductVariant/3011"

	@pytest.mark.parametrize(
		"gid", ["gid://shopify/Product/1", "https://x/y/z/1", "gid://a/b/C/", "gid://a/b/C/1/2"]
	)
	def test_rejects_what_is_not_an_identity(self, gid: str) -> None:
		with pytest.raises(SourceError):
			Identity.parse(gid)

	@pytest.mark.parametrize(("source", "connection"), [("Woo", "a"), ("woo", "a b"), ("woo", "")])
	def test_rejects_unsafe_hosts(self, source: str, connection: str) -> None:
		with pytest.raises(SourceError):
			Identity.of(source, connection, "Product", 1)

	def test_rejects_an_id_with_a_slash(self) -> None:
		with pytest.raises(SourceError):
			Identity.of("woo", "a", "Product", "1/2")


class TestMoney:
	@pytest.mark.parametrize(
		("value", "expected"),
		[(19.99, "19.99"), ("10.50", "10.50"), (3, "3"), ("1e2", "1E+2"), (0.1, "0.1"), ("-0.0", "-0.0")],
	)
	def test_reads_numbers_exactly(self, value: float | str, expected: str) -> None:
		assert decimal_of(value) == Decimal(expected)

	@pytest.mark.parametrize("value", ["", "abc", "1.2.3", "nan"])
	def test_rejects_non_numbers(self, value: str) -> None:
		with pytest.raises(ValueError, match="not a decimal"):
			decimal_of(value)

	def test_rejects_booleans(self) -> None:
		with pytest.raises(ValueError, match="boolean"):
			decimal_of(True)

	@pytest.mark.parametrize(
		("value", "text"),
		[("10.50", "10.5"), ("10.00", "10"), ("1E+2", "100"), ("-0", "0"), ("0.0015", "0.0015")],
	)
	def test_plain_text(self, value: str, text: str) -> None:
		assert decimal_text(Decimal(value)) == text

	def test_minor_units(self) -> None:
		assert minor_units("CAD") == 2
		assert minor_units("JPY") == 0
		assert minor_units("KWD") == 3
		assert from_minor_units(1999, "CAD") == Decimal("19.99")
		assert from_minor_units(500, "JPY") == Decimal(500)
		with pytest.raises(ValueError, match="minor unit"):
			minor_units("XAU")

	def test_exact_quotients_only(self) -> None:
		assert exact_quotient(Decimal("50.00"), Decimal(2), 2) == Decimal("25.00")
		assert exact_quotient(Decimal("-25.00"), Decimal(-1), 2) == Decimal("25.00")
		assert exact_quotient(Decimal(10), Decimal(3), 2) is None
		assert exact_quotient(Decimal("0.05"), Decimal(2), 2) is None
		assert exact_quotient(Decimal(1), Decimal(0), 2) is None
		assert is_exact_at(Decimal("1.10"), 2)
		assert not is_exact_at(Decimal("1.105"), 2)


class TestTimes:
	@pytest.mark.parametrize(
		("value", "utc"),
		[
			("2026-09-02T16:00:00", "2026-09-02T16:00:00Z"),
			("2026-09-02T16:00:00Z", "2026-09-02T16:00:00Z"),
			("2026-09-02T10:00:00-06:00", "2026-09-02T16:00:00Z"),
			("2026-09-02T16:00:00.123456+00:00", "2026-09-02T16:00:00Z"),
			("Wed, 02 Sep 2026 16:00:00 +0000", "2026-09-02T16:00:00Z"),
		],
	)
	def test_utc(self, value: str, utc: str) -> None:
		assert iso_utc(value) == utc

	def test_absent(self) -> None:
		assert iso_utc(None) is None
		assert iso_utc(" ") is None

	def test_rejects_garbage(self) -> None:
		with pytest.raises(SourceError):
			iso_utc("yesterday")


class TestNode:
	def test_typed_reads(self) -> None:
		node = Node(
			{
				"s": "x",
				"e": "",
				"i": 3,
				"si": "4",
				"b": True,
				"d": 19.99,
				"t": "2026-09-02T16:00:00",
				"l": ["a"],
				"n": {"nodes": [{"id": "1"}]},
			}
		)
		assert node.string("s") == "x"
		assert node.optional_string("e") is None
		assert node.text("missing") == ""
		assert node.integer("i") == 3
		assert node.integer("si") == 4
		assert node.flag("b") is True
		assert node.decimal("d") == Decimal("19.99")
		assert node.time("t") == "2026-09-02T16:00:00Z"
		assert node.strings("l") == ["a"]
		assert [child.id() for child in node.children("n")] == ["1"]

	def test_mismatches_name_the_path(self) -> None:
		node = Node({"items": [{"price": "free"}]}, "orders")
		with pytest.raises(SourceError, match=r"orders\.items\[0\]\.price is not a decimal"):
			node.children("items")[0].decimal("price")
		with pytest.raises(SourceError, match=r"orders\.id is not a string"):
			node.id()

	def test_an_empty_list_reads_as_an_empty_object(self) -> None:
		assert Node({"meta": []}).child("meta") is not None
		assert Node({"meta": None}).child("meta") is None

	def test_with_fields(self) -> None:
		node = Node({"id": "1"}).with_fields(extra=[1])
		assert node.get("extra") == [1]


class TestValues:
	def test_typed_reads(self) -> None:
		values = Values({"s": "x", "i": 1, "d": Decimal(2), "f": False, "r": {"k": "v"}, "rs": [{"k": "w"}]})
		assert values.string("s") == "x"
		assert values.integer("i") == 1
		assert values.decimal("d") == Decimal(2)
		assert values.flag("f") is False
		assert values.required_record("r").string("k") == "v"
		assert [record.string("k") for record in values.records("rs")] == ["w"]
		assert values.optional_string("missing") is None

	def test_wrong_types_are_programming_errors(self) -> None:
		with pytest.raises(TypeError, match='"i" is not a string'):
			Values({"i": 1}).string("i")
		with pytest.raises(TypeError, match="Decimal"):
			Values({"d": 1}).decimal("d")


class TestHasher:
	def test_key_order_does_not_matter(self) -> None:
		a = Desired(Kind.PAGE, "gid://a/b/Page/1", {"x": 1, "y": {"b": 2, "a": Decimal("1.50")}})
		b = Desired(Kind.PAGE, "gid://a/b/Page/1", {"y": {"a": Decimal("1.5"), "b": 2}, "x": 1})
		assert content_hash(a, 1) == content_hash(b, 1)

	def test_any_change_changes_the_hash(self) -> None:
		base = Desired(Kind.PAGE, "gid://a/b/Page/1", {"x": 1}, ("/old",))
		assert content_hash(base, 1) != content_hash(base, 2)
		assert content_hash(base, 1) != content_hash(Desired(Kind.PAGE, base.gid, {"x": 2}, ("/old",)), 1)
		assert content_hash(base, 1) != content_hash(Desired(Kind.PAGE, base.gid, {"x": 1}, ("/new",)), 1)

	def test_list_order_matters_and_decimals_are_tagged(self) -> None:
		assert part_hash([1, 2]) != part_hash([2, 1])
		assert canonical(Decimal("2.50")) == {"$decimal": "2.5"}
		assert part_hash({"v": Decimal(2)}) != part_hash({"v": "2"})


class TestPaths:
	def test_path_of(self) -> None:
		assert path_of("https://shop.example/Shop/Hoodie/?x=1") == "/shop/hoodie"
		assert path_of("/") is None
		assert path_of(None) is None

	def test_normalise(self) -> None:
		assert normalise("/a/b/") == "/a/b"

	def test_redirect_target(self) -> None:
		assert redirect_target("/shop/", "store.example") == "/shop"
		assert redirect_target("https://www.store.example/sale", "store.example") == "/sale"
		assert (
			redirect_target("https://outlet.example/sale", "store.example") == "https://outlet.example/sale"
		)
		assert redirect_target("https://store.example/", "store.example") is None

	def test_slugify(self) -> None:
		assert slugify("Wool Toque (Navy)") == "wool-toque-navy"


def test_sync_order_lists_every_synced_kind_once() -> None:
	assert len(SYNC_ORDER) == len(set(SYNC_ORDER))
	assert SYNC_ORDER.index(Kind.PRODUCT) < SYNC_ORDER.index(Kind.ORDER) < SYNC_ORDER.index(Kind.REVIEW)
	assert Kind.parse("product") is Kind.PRODUCT
	with pytest.raises(ValueError, match="Unknown kind"):
		Kind.parse("widget")
