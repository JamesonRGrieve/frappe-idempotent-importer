# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Dynamics 365 Sales adapter over its fixtures (tests/fixtures/dynamics_sales, written by
tools/dataverse_fixtures.py and proven against tests/schemas/dynamics_sales.json): the Dataverse Web API
requests (columns, expansions, annotations, paging), and every mapper."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.dynamics_sales.adapter import DynamicsSalesAdapter
from idempotent_importer.sources.dynamics_sales.mapping import address_of, discount_of
from idempotent_importer.sources.dynamics_sales.reader import DataverseReader, api_base
from tests.support.dynamics import ENTRA_TOKEN, SALES_CONFIG
from tests.support.transport import FakeTransport, RecordingSleeper, entra

ADAPTER = DynamicsSalesAdapter()
GID = "gid://dynamics_sales/dynamics-sales-d"


def guid(n: int) -> str:
	return f"d{n:07x}-0000-4000-8000-{n:012x}"


def reader(state: str = "v1") -> tuple[DataverseReader, FakeTransport]:
	transport = entra("dynamics_sales", state)
	built = ADAPTER.reader(SALES_CONFIG, transport, RecordingSleeper())
	assert isinstance(built, DataverseReader)
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
	return {m.kind: m for m in ADAPTER.mappers(SALES_CONFIG)}[kind]


def mapped(kind: Kind, type_: str, n: int, state: str = "v1") -> Desired | None:
	return mapper_of(kind).map(nodes(kind, state)[f"{type_}/{guid(n)}"])


def values(kind: Kind, type_: str, n: int, state: str = "v1") -> Values:
	desired = mapped(kind, type_, n, state)
	assert desired is not None
	return desired.values()


class TestReader:
	def test_requests_select_their_columns_with_the_apps_token(self) -> None:
		source, transport = reader()
		source.page(Kind.LEAD, None, None, "")
		grant = parse_qs(transport.requests[0].body.decode())
		assert grant["scope"] == ["https://harbourpine.crm3.dynamics.com/.default"]
		listing = next(request for request in transport.requests if "/leads" in request.url)
		assert listing.headers["Authorization"] == f"Bearer {ENTRA_TOKEN}"
		assert listing.headers["Prefer"].startswith("odata.maxpagesize=200,odata.include-annotations=")
		query = parse_qs(urlsplit(listing.url).query)
		assert query["$select"][0].startswith("leadid,subject,")

	def test_customers_are_accounts_then_contacts_in_pages(self) -> None:
		found = nodes(Kind.CUSTOMER)
		assert sorted(found) == sorted(
			[f"Account/{guid(21)}", f"Account/{guid(22)}", f"Contact/{guid(31)}", f"Contact/{guid(32)}"]
		)
		contoso = found[f"Account/{guid(21)}"]
		assert [person.string("fullname") for person in contoso.children("contact_customer_accounts")] == [
			"Yvonne McKay"
		]
		assert contoso.string("currency") == "CAD" and contoso.string("company_currency") == "CAD"

	def test_incremental_by_modification_time(self) -> None:
		source, transport = reader()
		assert source.is_incremental(Kind.SALES_INVOICE)
		source.page(Kind.PRODUCT, None, datetime(2026, 3, 1, tzinfo=UTC), "")
		listing = next(request for request in transport.requests if "/products" in request.url)
		assert parse_qs(urlsplit(listing.url).query)["$filter"] == ["modifiedon gt 2026-03-01T00:00:00Z"]

	def test_configuration_and_single_records(self) -> None:
		with pytest.raises(SourceError, match="environment's URL"):
			api_base(ConnectionConfig("dynamics_sales", "d", "https://harbourpine.example.com"))
		source, _ = reader()
		with pytest.raises(SourceError, match="Not a Dynamics 365 Sales"):
			source.node(Kind.CUSTOMER, f"{GID}/Lead/{guid(41)}")
		with pytest.raises(SourceError, match="has no"):
			source.page(Kind.REVIEW, None, None, "")


class TestMappers:
	def test_products(self) -> None:
		desk = values(Kind.PRODUCT, "Product", 11)
		assert (desk.string("sku"), desk.decimal("price"), desk.decimal("stock_qty")) == (
			"SD-100",
			Decimal(800),
			Decimal(6),
		)
		assert values(Kind.PRODUCT, "Product", 12).flag("is_stock_item") is False
		with pytest.raises(Unsupported, match="family or bundle"):
			mapped(Kind.PRODUCT, "Product", 13)
		assert values(Kind.PRODUCT, "Product", 11, "v2").decimal("price") == Decimal(850)

	def test_customers(self) -> None:
		contoso = values(Kind.CUSTOMER, "Account", 21)
		assert (contoso.string("type"), contoso.records("addresses")[0].string("country_code")) == (
			"Company",
			"CA",
		)
		assert [person.string("name") for person in contoso.records("contacts")] == ["Yvonne McKay"]
		fourth = mapped(Kind.CUSTOMER, "Account", 22)
		assert fourth is not None and "isn't an ISO code" in fourth.warnings[0]
		assert values(Kind.CUSTOMER, "Account", 22, "v2").flag("disabled")
		susan = values(Kind.CUSTOMER, "Contact", 32)
		assert (susan.string("type"), susan.string("first_name"), susan.string("phone")) == (
			"Individual",
			"Susan",
			"+1 587 555 0132",
		)

	def test_leads_and_opportunities(self) -> None:
		assert values(Kind.LEAD, "Lead", 41).string("status") == "Open"
		assert values(Kind.LEAD, "Lead", 42).string("status") == "Converted"
		contoso = values(Kind.OPPORTUNITY, "Opportunity", 51)
		assert (contoso.string("party_kind"), contoso.string("party"), contoso.decimal("probability")) == (
			"customer",
			f"{GID}/Account/{guid(21)}",
			Decimal(60),
		)
		assert values(Kind.OPPORTUNITY, "Opportunity", 52).string("party") == f"{GID}/Lead/{guid(41)}"
		assert values(Kind.OPPORTUNITY, "Opportunity", 51, "v2").string("status") == "Converted"
		orphan = nodes(Kind.OPPORTUNITY)[f"Opportunity/{guid(52)}"].with_fields(_originatingleadid_value=None)
		with pytest.raises(Unsupported, match="neither a customer nor a lead"):
			mapper_of(Kind.OPPORTUNITY).map(orphan)

	def test_quotes_orders_and_invoices(self) -> None:
		quote = values(Kind.DRAFT_ORDER, "Quote", 61)
		line = quote.records("lines")[0]
		assert (line.decimal("qty"), line.decimal("rate"), quote.decimal("total")) == (
			Decimal(2),
			Decimal("750.00"),
			Decimal(1625),
		)
		assert [charge.string("kind") for charge in quote.records("charges")] == ["shipping", "tax"]
		order = values(Kind.SALES_ORDER, "SalesOrder", 71)
		assert (order.decimal("discount"), order.flag("submitted"), order.string("due_date")) == (
			Decimal(90),
			True,
			"2026-03-20",
		)
		desk, setup = order.records("lines")
		assert (desk.string("product"), setup.optional_string("product")) == (
			f"{GID}/Product/{guid(11)}",
			None,
		)
		assert mapped(Kind.SALES_ORDER, "SalesOrder", 71, "v2") is None, "cancelled"
		usd = values(Kind.SALES_INVOICE, "Invoice", 81)
		assert (
			usd.string("currency"),
			usd.decimal("exchange_rate"),
			usd.decimal("base_total"),
			usd.string("party"),
		) == (
			"USD",
			Decimal("1.37"),
			Decimal("863.1"),
			f"{GID}/Contact/{guid(32)}",
		)
		paid = values(Kind.SALES_INVOICE, "Invoice", 83)
		assert (paid.flag("submitted"), [tax.decimal("amount") for tax in paid.records("taxes")]) == (
			True,
			[Decimal("7.5")],
		)
		cancelled = nodes(Kind.SALES_INVOICE)[f"Invoice/{guid(83)}"].with_fields(statecode=3)
		assert mapper_of(Kind.SALES_INVOICE).map(cancelled) is None
		nobody = nodes(Kind.SALES_INVOICE)[f"Invoice/{guid(83)}"].with_fields(_customerid_value=None)
		with pytest.raises(Unsupported, match="no account or contact"):
			mapper_of(Kind.SALES_INVOICE).map(nobody)
		closed = nodes(Kind.DRAFT_ORDER)[f"Quote/{guid(61)}"].with_fields(statecode=2)
		assert mapper_of(Kind.DRAFT_ORDER).map(closed) is None

	def test_helpers(self) -> None:
		assert discount_of(
			Node({"discountamount": 5, "totallineitemamount": 200, "discountpercentage": 10})
		) == Decimal(25)
		assert address_of(Node({"address1_line1": "1 Main"}), "Billing", None) == (None, None)
