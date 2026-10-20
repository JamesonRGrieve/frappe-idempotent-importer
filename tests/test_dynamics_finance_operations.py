# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Finance & Operations adapter over its fixtures (tests/fixtures/dynamics_finance_operations, written by
tools/fo_fixtures.py and proven against tests/schemas/dynamics_finance_operations.json): the field map and
its documented defaults, the legal entity's records, case-insensitive fields, and every mapper."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import KindUnavailable, SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.dynamics_finance_operations.adapter import FinanceOperationsAdapter
from idempotent_importer.sources.dynamics_finance_operations.fieldmap import Fields, entity_maps, usable
from idempotent_importer.sources.dynamics_finance_operations.reader import (
	FinanceOperationsReader,
	api_base,
	key_of,
	tenant_and_legal_entity,
)
from tests.support.dynamics import FO_CONFIG, FO_ENVIRONMENT
from tests.support.transport import FakeTransport, RecordingSleeper, entra

ADAPTER = FinanceOperationsAdapter()
GID = "gid://dynamics_finance_operations/finance-operations-f"


def reader(
	state: str = "v1", config: ConnectionConfig = FO_CONFIG
) -> tuple[FinanceOperationsReader, FakeTransport]:
	transport = entra("dynamics_finance_operations", state)
	built = ADAPTER.reader(config, transport, RecordingSleeper())
	assert isinstance(built, FinanceOperationsReader)
	return built, transport


def nodes(kind: Kind, state: str = "v1") -> dict[str, Node]:
	source, _ = reader(state)
	found: dict[str, Node] = {}
	cursor: str | None = None
	while True:
		page = source.page(kind, cursor, None, "")
		found |= {node.id().removeprefix(f"{GID}/"): node for node in page.nodes}
		if not page.has_next_page:
			return found
		cursor = page.end_cursor


def mapper_of(kind: Kind) -> Mapper:
	return {m.kind: m for m in ADAPTER.mappers(FO_CONFIG)}[kind]


def mapped(kind: Kind, key: str, state: str = "v1") -> Desired | None:
	return mapper_of(kind).map(nodes(kind, state)[key])


def values(kind: Kind, key: str, state: str = "v1") -> Values:
	desired = mapped(kind, key, state)
	assert desired is not None
	return desired.values()


class TestFieldMap:
	def test_documented_defaults_and_the_connections_map(self) -> None:
		maps = entity_maps(FO_CONFIG.field_map)
		assert maps[Kind.CUSTOMER].entity == "CustomersV3"
		assert maps[Kind.SALES_ORDER].fields["date"] == "ORDERDATE"
		assert maps[Kind.SALES_ORDER].fields["customer"] == "ORDERINGCUSTOMERACCOUNTNUMBER"
		with pytest.raises(KindUnavailable, match="date"):
			usable(entity_maps(""), Kind.SALES_ORDER)
		with pytest.raises(KindUnavailable, match="entity"):
			usable(entity_maps(""), Kind.SUPPLIER)
		vendors = entity_maps(
			'{"supplier": {"entity": "VendorsV2", "fields": {"key": "VendorAccountNumber", "name": "VendorOrganizationName"}}}'
		)
		assert usable(vendors, Kind.SUPPLIER).entity == "VendorsV2"

	@pytest.mark.parametrize(
		"field_map", ["not json", "[]", '{"review": {}}', '{"customer": {"fields": {"key": 3}}}']
	)
	def test_a_bad_field_map_is_refused(self, field_map: str) -> None:
		with pytest.raises((SourceError, ValueError)):
			entity_maps(field_map)

	def test_fields_match_whatever_their_case(self) -> None:
		fields = Fields(Node({"CustomerAccount": "US-001"}), {"key": "CUSTOMERACCOUNT", "name": "NAME"})
		assert (fields.get("key"), fields.name("key"), fields.get("name"), fields.get("email")) == (
			"US-001",
			"CustomerAccount",
			None,
			None,
		)


class TestReader:
	def test_the_legal_entitys_records_with_the_apps_token(self) -> None:
		source, transport = reader()
		source.page(Kind.CUSTOMER, None, None, "")
		grant = parse_qs(transport.requests[0].body.decode())
		assert grant["scope"] == [f"{FO_ENVIRONMENT}/.default"]
		query = parse_qs(urlsplit(transport.requests[1].url).query)
		assert (query["cross-company"], query["$filter"]) == (["true"], ["dataAreaId eq 'hpt'"])
		assert not source.is_incremental(Kind.CUSTOMER)

	def test_lists_page_and_orders_carry_their_lines(self) -> None:
		assert sorted(nodes(Kind.CUSTOMER)) == ["Customer/US-001", "Customer/US-027"]
		order = nodes(Kind.SALES_ORDER)["SalesOrder/000701"]
		assert [Fields(line, {"amount": "LINEAMOUNT"}).get("amount") for line in order.children("lines")] == [
			360.0
		]

	def test_configuration(self) -> None:
		assert tenant_and_legal_entity(FO_CONFIG) == ("e2e-tenant", "hpt")
		with pytest.raises(SourceError, match="tenant"):
			tenant_and_legal_entity(
				ConnectionConfig("dynamics_finance_operations", "f", FO_ENVIRONMENT, account="hpt")
			)
		with pytest.raises(SourceError, match="environment's URL"):
			api_base(
				ConnectionConfig("dynamics_finance_operations", "f", "https://example.com", account="t/hpt")
			)
		assert key_of("A/1") == "A%2F1"
		with pytest.raises(SourceError, match="no key"):
			key_of("")
		source, _ = reader()
		with pytest.raises(SourceError, match="has no"):
			source.page(Kind.REVIEW, None, None, "")
		found = source.node(Kind.CUSTOMER, f"{GID}/Customer/US-027")
		assert found is not None and found.string("source_id") == "US-027"
		assert source.node(Kind.CUSTOMER, f"{GID}/Customer/XX") is None


class TestMappers:
	def test_customers(self) -> None:
		contoso = mapped(Kind.CUSTOMER, "Customer/US-001")
		assert contoso is not None
		assert (contoso.values().string("type"), contoso.values().string("currency")) == ("Company", "CAD")
		assert "street" in contoso.warnings[0], "no documented street: no address"
		birch = values(Kind.CUSTOMER, "Customer/US-027")
		assert (birch.string("type"), birch.string("name"), birch.string("last_name")) == (
			"Individual",
			"Birch Lake",
			"Lake",
		)

	def test_a_mapped_street_with_a_three_letter_country(self) -> None:
		mapper = mapper_of(Kind.CUSTOMER)
		node = Node(
			{
				"id": f"{GID}/Customer/US-001",
				"CUSTOMERACCOUNT": "US-001",
				"ORGANIZATIONNAME": "Contoso",
				"ADDRESSSTREET": "1 Main St",
				"ADDRESSCITY": "Calgary",
				"ADDRESSCOUNTRYREGIONID": "CAN",
				"roles": {
					"key": "CUSTOMERACCOUNT",
					"name": "ORGANIZATIONNAME",
					"street": "ADDRESSSTREET",
					"city": "ADDRESSCITY",
					"country": "ADDRESSCOUNTRYREGIONID",
				},
			}
		)
		desired = mapper.map(node)
		assert desired is not None and "two-letter ISO code" in desired.warnings[0]
		desired = mapper.map(node.with_fields(ADDRESSCOUNTRYREGIONID="CA"))
		assert desired is not None and desired.values().records("addresses")[0].string("line1") == "1 Main St"
		with pytest.raises(Unsupported, match="no name"):
			mapper.map(node.with_fields(ORGANIZATIONNAME=""))

	def test_products(self) -> None:
		speaker = values(Kind.PRODUCT, "Item/D0001")
		assert (speaker.string("name"), speaker.decimal("price"), speaker.flag("is_stock_item")) == (
			"MidRangeSpeaker",
			Decimal("180.0"),
			False,
		)
		with pytest.raises(Unsupported, match="product master"):
			mapped(Kind.PRODUCT, "Item/D0004")
		assert values(Kind.PRODUCT, "Item/D0001", "v2").decimal("price") == Decimal("189.0")

	def test_sales_orders(self) -> None:
		order = values(Kind.SALES_ORDER, "SalesOrder/000701")
		line = order.records("lines")[0]
		assert (
			order.string("party"),
			order.string("date"),
			order.string("due_date"),
			order.flag("submitted"),
		) == (
			f"{GID}/Customer/US-001",
			"2026-03-02",
			"2026-03-20",
			False,
		)
		assert (
			line.string("product"),
			line.decimal("qty"),
			line.decimal("rate"),
			order.decimal("total"),
		) == (
			f"{GID}/Item/D0001",
			Decimal("2.0"),
			Decimal("180.00"),
			Decimal("360.00"),
		)
		assert values(Kind.SALES_ORDER, "SalesOrder/000702").optional_string("due_date") is None, (
			"the blank date"
		)
		dateless = nodes(Kind.SALES_ORDER)["SalesOrder/000701"].with_fields(ORDERDATE="")
		with pytest.raises(Unsupported, match="no date"):
			mapper_of(Kind.SALES_ORDER).map(dateless)
		assert "SalesOrder/000702" not in nodes(Kind.SALES_ORDER, "v2")
