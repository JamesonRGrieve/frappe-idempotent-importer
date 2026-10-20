# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Business Central adapter over its fixtures (tests/fixtures/dynamics_business_central, written by
tools/bc_fixtures.py and proven against tests/schemas/dynamics_business_central.json): Microsoft Entra ID's
client credentials, OData paging and the company's root, and every mapper."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import CredentialsRejected, SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.dynamics.common import date_of, noon_of, required_date
from idempotent_importer.sources.dynamics.odata import OData, quoted
from idempotent_importer.sources.dynamics_business_central.adapter import BusinessCentralAdapter
from idempotent_importer.sources.dynamics_business_central.reader import BusinessCentralReader, company_base
from tests.support.dynamics import BC_COMPANY, BC_CONFIG, ENTRA_TOKEN
from tests.support.transport import FakeTransport, RecordingSleeper, entra, scripted

ADAPTER = BusinessCentralAdapter()
GID = "gid://dynamics_business_central/business-central-b"


def guid(n: int) -> str:
	return f"{n:08x}-0000-4000-8000-{n:012x}"


def reader(state: str = "v1") -> tuple[BusinessCentralReader, FakeTransport]:
	transport = entra("dynamics_business_central", state)
	built = ADAPTER.reader(BC_CONFIG, transport, RecordingSleeper())
	assert isinstance(built, BusinessCentralReader)
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


def key(type_: str, n: int) -> str:
	return f"{type_}/{guid(n)}"


def mapper_of(kind: Kind) -> Mapper:
	return {m.kind: m for m in ADAPTER.mappers(BC_CONFIG)}[kind]


def mapped(kind: Kind, type_: str, n: int, state: str = "v1") -> Desired | None:
	return mapper_of(kind).map(nodes(kind, state)[key(type_, n)])


def values(kind: Kind, type_: str, n: int, state: str = "v1") -> Values:
	desired = mapped(kind, type_, n, state)
	assert desired is not None
	return desired.values()


class NoHeaders:
	def headers(self) -> dict[str, str]:
		return {}

	def renew(self) -> bool:
		return False


class TestReader:
	def test_the_app_reads_with_its_own_token(self) -> None:
		source, transport = reader()
		source.page(Kind.ACCOUNT, None, None, "")
		grant, first = transport.requests[0], transport.requests[1]
		assert grant.url == "https://login.microsoftonline.com/e2e-tenant/oauth2/v2.0/token"
		form = parse_qs(grant.body.decode())
		assert (form["grant_type"], form["scope"], form["client_id"]) == (
			["client_credentials"],
			["https://api.businesscentral.dynamics.com/.default"],
			["e2e-entra-client"],
		)
		assert first.headers["Authorization"] == f"Bearer {ENTRA_TOKEN}"
		assert first.headers["Prefer"] == "odata.maxpagesize=200"
		assert urlsplit(first.url).path.endswith(f"/companies({BC_COMPANY})/accounts")
		assert any(request.url.endswith("/companyInformation") for request in transport.requests), (
			"its currency"
		)

	def test_rejected_client_credentials_stop_the_run(self) -> None:
		config = ConnectionConfig(
			BC_CONFIG.source,
			BC_CONFIG.connection,
			BC_CONFIG.store_url,
			"e2e-entra-client",
			"wrong",
			account=BC_COMPANY,
		)
		source = ADAPTER.reader(config, entra("dynamics_business_central", "v1"), RecordingSleeper())
		with pytest.raises(CredentialsRejected, match="refused the token request"):
			source.page(Kind.ACCOUNT, None, None, "")

	def test_the_environment_url_and_company_are_checked(self) -> None:
		with pytest.raises(SourceError, match="environment's API URL"):
			company_base(
				ConnectionConfig(
					"dynamics_business_central",
					"b",
					"https://businesscentral.dynamics.com/x",
					account=BC_COMPANY,
				)
			)
		with pytest.raises(SourceError, match="company's id"):
			company_base(
				ConnectionConfig("dynamics_business_central", "b", BC_CONFIG.store_url, account="CRONUS")
			)
		with pytest.raises(SourceError, match="client ID and client secret"):
			ADAPTER.reader(
				ConnectionConfig("dynamics_business_central", "b", BC_CONFIG.store_url, account=BC_COMPANY),
				scripted(HttpResponse(200, b"{}")),
				RecordingSleeper(),
			)

	def test_lists_follow_the_next_link(self) -> None:
		source, _ = reader()
		first = source.page(Kind.CUSTOMER, None, None, "")
		assert [node.string("number") for node in first.nodes] == ["10000"]
		assert first.end_cursor == "customers|/customers?$skiptoken=eyJza2lwIjoxfQ"
		second = source.page(Kind.CUSTOMER, first.end_cursor, None, "")
		assert ([node.string("number") for node in second.nodes], second.has_next_page) == (["20000"], False)

	def test_a_kind_of_two_entity_sets_reads_one_then_the_other(self) -> None:
		assert sorted(nodes(Kind.SALES_INVOICE)) == sorted(
			[key("SalesInvoice", 421), key("SalesInvoice", 431), key("SalesCreditMemo", 441)]
		)
		assert sorted(nodes(Kind.COST_CENTER)) == sorted(
			[key("Dimension", 111), key("DimensionValue", 112), key("DimensionValue", 113)]
		)

	def test_incremental_kinds_filter_by_modification_time(self) -> None:
		source, transport = reader()
		assert source.is_incremental(Kind.CUSTOMER)
		source.page(Kind.SUPPLIER, None, datetime(2026, 3, 1, tzinfo=UTC), "")
		listing = next(request for request in transport.requests if "/vendors" in request.url)
		query = parse_qs(urlsplit(listing.url).query)
		assert query["$filter"] == ["lastModifiedDateTime gt 2026-03-01T00:00:00Z"]
		source.page(Kind.COST_CENTER, None, datetime(2026, 3, 1, tzinfo=UTC), "")
		dimensions = next(request for request in transport.requests if "/dimensions" in request.url)
		assert "$filter" not in parse_qs(urlsplit(dimensions.url).query), "dimensions aren't filtered"

	def test_documents_arrive_with_their_lines_and_opportunities_with_their_contact(self) -> None:
		invoice = nodes(Kind.SALES_INVOICE)[key("SalesInvoice", 421)]
		assert [line.string("lineType") for line in invoice.children("salesInvoiceLines")] == [
			"Item",
			"Account",
		]
		assert invoice.string("company_currency") == "CAD"
		opportunity = nodes(Kind.OPPORTUNITY)[key("Opportunity", 351)]
		assert opportunity.string("contact") == f"{GID}/Contact/{guid(342)}"

	def test_single_records(self) -> None:
		source, _ = reader()
		with pytest.raises(SourceError, match="Not a Business Central"):
			source.node(Kind.CUSTOMER, f"{GID}/Customer/10000")
		with pytest.raises(SourceError, match="has no"):
			source.page(Kind.REVIEW, None, None, "")

	def test_a_next_link_must_stay_on_the_service(self) -> None:
		base = company_base(BC_CONFIG)
		odata = OData(
			RestClient("BC", base, NoHeaders(), scripted(HttpResponse(200, b"{}")), RecordingSleeper()), base
		)
		with pytest.raises(SourceError, match="outside its service"):
			odata.relative("https://elsewhere.example/other/path?x=1")
		assert (
			odata.relative(f"https://other-host.example{urlsplit(base).path}/items?$skiptoken=1")
			== "/items?$skiptoken=1"
		)
		assert quoted("O'Neil") == "'O''Neil'"


class TestMappers:
	def test_accounts(self) -> None:
		with pytest.raises(Unsupported, match="Heading"):
			mapped(Kind.ACCOUNT, "Account", 101)
		bank = values(Kind.ACCOUNT, "Account", 102)
		assert (bank.string("number"), bank.string("root_type"), bank.string("account_type")) == (
			"10100",
			"Asset",
			"Bank",
		)
		assert values(Kind.ACCOUNT, "Account", 106).string("account_type") == "Cost of Goods Sold"
		with pytest.raises(SourceError, match="unknown account category"):
			mapper_of(Kind.ACCOUNT).map(
				Node(
					{
						"id": f"{GID}/Account/x",
						"accountType": "Posting",
						"category": "Odd",
						"displayName": "x",
					}
				)
			)

	def test_dimensions_locations_and_categories(self) -> None:
		assert values(Kind.COST_CENTER, "Dimension", 111).flag("is_group")
		assert (
			values(Kind.COST_CENTER, "DimensionValue", 112).string("parent") == f"{GID}/Dimension/{guid(111)}"
		)
		assert values(Kind.WAREHOUSE, "Location", 121).required_record("address").string("city") == "Edmonton"
		assert values(Kind.COLLECTION, "ItemCategory", 131).string("name") == "Office Furniture"

	def test_items(self) -> None:
		desk = values(Kind.PRODUCT, "Item", 201)
		assert (
			desk.string("sku"),
			desk.decimal("price"),
			desk.decimal("stock_qty"),
			desk.flag("is_stock_item"),
		) == ("1896-S", Decimal(1000), Decimal(4), True)
		chair = mapped(Kind.PRODUCT, "Item", 202)
		assert chair is not None and chair.warnings, "no stock per variant"
		black, red = chair.values().records("variants")
		assert (black.string("sku"), red.required_record("attributes").all()) == (
			"1900-S-BLACK",
			{"Variant": "RED"},
		)
		installation = values(Kind.PRODUCT, "Item", 205)
		assert installation.flag("is_stock_item") is False
		assert installation.strings("collections") == [], "the empty GUID names no category"
		assert values(Kind.PRODUCT, "Item", 201, "v2").decimal("price") == Decimal(1050)

	def test_parties_people_and_projects(self) -> None:
		adatum = values(Kind.CUSTOMER, "Customer", 301)
		assert (
			adatum.string("type"),
			adatum.string("tax_id"),
			adatum.records("addresses")[0].string("country_code"),
		) == ("Company", "CA123456789", "CA")
		assert values(Kind.CUSTOMER, "Customer", 302, "v2").flag("disabled")
		assert values(Kind.SUPPLIER, "Vendor", 311).string("name") == "Fabrikam, Inc."
		alicia = values(Kind.EMPLOYEE, "Employee", 321)
		assert (
			alicia.string("first_name"),
			alicia.optional_string("date_of_birth"),
			alicia.string("employee_number"),
		) == ("Alicia", None, "AH")
		gone = values(Kind.EMPLOYEE, "Employee", 321, "v2")
		assert (gone.string("status"), gone.string("relieving_date")) == ("Left", "2026-03-31")
		with pytest.raises(SourceError, match="unknown employee status"):
			mapper_of(Kind.EMPLOYEE).map(
				Node({"id": f"{GID}/Employee/x", "status": "Odd", "displayName": "x"})
			)
		assert values(Kind.PROJECT, "Project", 331).string("name") == "JOB00010 Office fit-out"

	def test_contacts_and_opportunities(self) -> None:
		assert values(Kind.LEAD, "Contact", 341).string("status") == "Converted"
		robin = values(Kind.LEAD, "Contact", 342)
		assert (robin.string("first_name"), robin.string("company"), robin.string("mobile")) == (
			"Robin Lead",
			"Northwind Interiors",
			"+1 587 555 0144",
		)
		deal = values(Kind.OPPORTUNITY, "Opportunity", 351)
		assert (deal.string("party_kind"), deal.decimal("probability"), deal.string("status")) == (
			"lead",
			Decimal(50),
			"Open",
		)
		assert values(Kind.OPPORTUNITY, "Opportunity", 351, "v2").string("status") == "Converted"
		orphan = nodes(Kind.OPPORTUNITY)[key("Opportunity", 351)].with_fields(contact=None)
		with pytest.raises(Unsupported, match="isn't in the API"):
			mapper_of(Kind.OPPORTUNITY).map(orphan)

	def test_quotes_and_orders(self) -> None:
		quote = values(Kind.DRAFT_ORDER, "SalesQuote", 401)
		assert (quote.string("created_at"), quote.decimal("total")) == ("2026-02-25T12:00:00Z", Decimal(2100))
		order = values(Kind.SALES_ORDER, "SalesOrder", 411)
		assert (
			order.flag("submitted"),
			order.string("due_date"),
			order.string("reference"),
			len(order.records("lines")),
		) == (True, "2026-03-20", "AD-PO-77", 1)
		purchase = values(Kind.PURCHASE_ORDER, "PurchaseOrder", 451)
		assert (purchase.string("party"), purchase.string("due_date")) == (
			f"{GID}/Vendor/{guid(311)}",
			"2026-03-06",
		)

	def test_invoices_and_credit_memos(self) -> None:
		invoice = values(Kind.SALES_INVOICE, "SalesInvoice", 421)
		desk, installation = invoice.records("lines")
		assert (
			desk.string("product"),
			installation.optional_string("product"),
			installation.string("account"),
		) == (
			f"{GID}/Item/{guid(201)}",
			None,
			f"{GID}/Account/{guid(105)}",
		)
		assert [tax.decimal("amount") for tax in invoice.records("taxes")] == [Decimal(55)]
		assert (invoice.flag("submitted"), invoice.decimal("total")) == (True, Decimal(1155))
		draft = values(Kind.SALES_INVOICE, "SalesInvoice", 431)
		assert (draft.flag("submitted"), draft.records("lines")[0].string("variant")) == (
			False,
			f"{GID}/ItemVariant/{guid(204)}",
		)
		assert values(Kind.SALES_INVOICE, "SalesInvoice", 431, "v2").flag("submitted")
		credit = values(Kind.SALES_INVOICE, "SalesCreditMemo", 441)
		assert (credit.flag("is_return"), credit.string("return_against"), credit.decimal("total")) == (
			True,
			f"{GID}/SalesInvoice/{guid(421)}",
			Decimal(-105),
		)
		bill = values(Kind.PURCHASE_INVOICE, "PurchaseInvoice", 461)
		assert (bill.decimal("total"), bill.string("reference")) == (Decimal(3150), "FAB-9001")
		cancelled = nodes(Kind.SALES_INVOICE)[key("SalesInvoice", 421)].with_fields(status="Canceled")
		assert mapper_of(Kind.SALES_INVOICE).map(cancelled) is None
		foreign = nodes(Kind.SALES_INVOICE)[key("SalesInvoice", 421)].with_fields(currencyCode="USD")
		with pytest.raises(Unsupported, match="no exchange rate"):
			mapper_of(Kind.SALES_INVOICE).map(foreign)

	def test_a_resource_line_books_to_the_fallback_item(self) -> None:
		invoice = nodes(Kind.SALES_INVOICE)[key("SalesInvoice", 421)]
		raw = invoice.raw()
		lines = raw["salesInvoiceLines"]
		assert isinstance(lines, list) and isinstance(lines[0], dict)
		lines[0] = {**lines[0], "lineType": "Resource", "itemId": None}
		desired = mapper_of(Kind.SALES_INVOICE).map(Node(raw))
		assert desired is not None and "fallback item" in desired.warnings[0]
		assert desired.values().records("lines")[0].optional_string("product") is None


def test_blank_dates_and_noon() -> None:
	node = Node({"a": "0001-01-01", "b": "2026-03-05T10:00:00Z", "c": None})
	assert (date_of(node, "a"), date_of(node, "b"), date_of(node, "c")) == (None, "2026-03-05", None)
	assert noon_of("2026-03-05") == "2026-03-05T12:00:00Z"
	with pytest.raises(SourceError):
		required_date(node, "a")


def test_the_token_is_fetched_once_and_again_when_rejected() -> None:
	grant = HttpResponse(
		200, json.dumps({"access_token": "t1", "expires_in": 3599, "token_type": "Bearer"}).encode()
	)
	listing = HttpResponse(200, json.dumps({"value": [{"currencyCode": "CAD"}]}).encode())
	transport = scripted(grant, HttpResponse(401, b"{}"), grant, listing)
	source = ADAPTER.reader(BC_CONFIG, transport, RecordingSleeper())
	assert isinstance(source, BusinessCentralReader)
	assert source.company_currency() == "CAD"
	assert [request.method for request in transport.requests] == ["POST", "GET", "POST", "GET"]
