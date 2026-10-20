# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the accounting sources share (sources/ledger.py, the multi-collection cursor in sources/common.py)
and the XML Schema reader QuickBooks' fixtures are proven with (tools/xsd.py)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import Page
from idempotent_importer.sources.common import chained
from idempotent_importer.sources.ledger import book_item, country_code, day, midday, required_day
from tools.openapi import OpenApiValidator
from tools.xsd import REF_PREFIX, XsdSchemas

SCHEMA = """<?xml version="1.0"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns="urn:t" targetNamespace="urn:t">
	<xs:simpleType name="Colour"><xs:restriction base="xs:string">
		<xs:enumeration value="Red"/><xs:enumeration value="Blue"/>
	</xs:restriction></xs:simpleType>
	<xs:complexType name="Ref"><xs:simpleContent><xs:extension base="xs:string">
		<xs:attribute name="name" type="xs:string"/>
	</xs:extension></xs:simpleContent></xs:complexType>
	<xs:complexType name="Entity"><xs:sequence>
		<xs:element name="Id" type="xs:string" minOccurs="0"/>
	</xs:sequence><xs:attribute name="domain" type="xs:string"/></xs:complexType>
	<xs:complexType name="Thing"><xs:complexContent><xs:extension base="Entity"><xs:sequence>
		<xs:element name="Amount" type="xs:decimal" minOccurs="0"/>
		<xs:element name="Count" type="xs:int" minOccurs="0"/>
		<xs:element name="Paid" type="xs:boolean" minOccurs="0"/>
		<xs:element name="Colour" type="Colour" minOccurs="0"/>
		<xs:element name="OwnerRef" type="Ref" minOccurs="0"/>
		<xs:element name="Line" type="Entity" minOccurs="0" maxOccurs="unbounded"/>
		<xs:choice><xs:element name="A" type="xs:string"/><xs:element name="B" type="xs:string"/></xs:choice>
	</xs:sequence></xs:extension></xs:complexContent></xs:complexType>
	<xs:complexType name="Open"><xs:sequence><xs:any minOccurs="0" maxOccurs="unbounded"/></xs:sequence></xs:complexType>
	<xs:element name="Object" type="Entity" abstract="true"/>
	<xs:element name="Thing" type="Thing" substitutionGroup="Object"/>
	<xs:complexType name="Response"><xs:sequence>
		<xs:element ref="Object" minOccurs="0" maxOccurs="unbounded"/>
	</xs:sequence></xs:complexType>
</xs:schema>
"""


@pytest.fixture
def validator(tmp_path: Path) -> OpenApiValidator:
	(tmp_path / "t.xsd").write_text(SCHEMA)
	schemas = XsdSchemas([tmp_path / "t.xsd"]).schemas()
	return OpenApiValidator(schemas, REF_PREFIX, False)


def errors(validator: OpenApiValidator, value: object, type_: str = "Thing") -> list[str]:
	return validator.validate(value, {"$ref": REF_PREFIX + type_})


class TestXsd:
	def test_elements_attributes_and_derived_types(self, validator: OpenApiValidator) -> None:
		good = {
			"Id": "1",
			"domain": "QBO",
			"Amount": 1.5,
			"Count": 2,
			"Paid": True,
			"Colour": "Red",
			"OwnerRef": {"value": "7", "name": "Amy"},
			"Line": [{"Id": "2"}],
			"A": "x",
		}
		assert errors(validator, good) == []

	def test_strictness(self, validator: OpenApiValidator) -> None:
		assert errors(validator, {"Nope": 1}) == ["$.Nope is not in the spec"]
		assert errors(validator, {"Count": "2"}) == ["$.Count should be integer, got str"]
		assert errors(validator, {"Colour": "Green"})
		assert errors(validator, {"Line": {"Id": "2"}}) == ["$.Line should be array, got dict"]

	def test_substitution_groups_and_any(self, validator: OpenApiValidator) -> None:
		assert errors(validator, {"Thing": [{"Id": "1"}]}, "Response") == []
		assert errors(validator, {"Whatever": 1}, "Open") == []


class TestLedger:
	def test_countries(self) -> None:
		assert country_code("Canada") == "CA"
		assert country_code("ca") == "CA"
		assert country_code("United States of America") == "US"
		assert country_code("Atlantis") is None and country_code(None) is None

	def test_dates(self) -> None:
		assert day("2026-02-10T09:00:00-0500") == "2026-02-10"
		assert day("") is None
		with pytest.raises(SourceError):
			day("10/02/2026")
		with pytest.raises(SourceError, match="no date"):
			required_day(None, "x")
		assert midday("2026-02-10") == "2026-02-10T12:00:00Z"

	def test_book_items(self) -> None:
		item = book_item("Lamp", "L-1", None, Decimal(45), True, Decimal(3), ["gid://a/b/C/1"])
		assert (item["type"], item["sku"], item["published"], item["stock_qty"]) == (
			"simple",
			"L-1",
			False,
			Decimal(3),
		)
		variable = book_item("Jacket", "J", None, Decimal(1), True, variants=[{"gid": "v"}])
		assert (variable["type"], variable["sku"], variable["price"]) == ("variable", None, None)


def test_chained_reads_each_feed_in_turn() -> None:
	pages = {
		(0, None): Page([], True, "2"),
		(0, "2"): Page([Node({})], False, None),
		(1, None): Page([], False, None),
	}
	first = chained(2, None, lambda feed, position: pages[(feed, position)])
	assert (first.has_next_page, first.end_cursor) == (True, "0:2")
	second = chained(2, first.end_cursor, lambda feed, position: pages[(feed, position)])
	assert (second.has_next_page, second.end_cursor) == (True, "1:")
	last = chained(2, second.end_cursor, lambda feed, position: pages[(feed, position)])
	assert (last.has_next_page, last.end_cursor) == (False, None)
