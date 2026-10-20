# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Acumatica adapter over the recorded tenant (tests/fixtures/acumatica, proven against the Default
endpoint 24.200.001's entities as the official REST client generates them): OAuth, the paging and
incremental condition, the reader's annotations, and every mapper."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.oauth import TokenGrant
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.acumatica.adapter import GRANT_PATH, AcumaticaAdapter
from idempotent_importer.sources.acumatica.reader import (
	ENDPOINT_PATH,
	KINDS,
	PAGE_SIZE,
	AcumaticaReader,
	derived_key,
	instance_url,
)
from tests.support.transport import (
	OAUTH_ACCESS_TOKEN,
	OAUTH_ROTATED_REFRESH_TOKEN,
	FakeTransport,
	RecordingSleeper,
	oauth_source,
	scripted,
)

ADAPTER = AcumaticaAdapter()
ENDPOINT = "http://acumatica-fake:8080"
INSTANCE = "https://summitgear.acumatica.com"
TOKEN = HttpResponse(
	200, b'{"access_token":"t","refresh_token":"r2","token_type":"Bearer","expires_in":3600}'
)


@pytest.fixture
def saved() -> list[str]:
	return []


@pytest.fixture
def config(saved: list[str]) -> ConnectionConfig:
	return ConnectionConfig(
		source="acumatica",
		connection="acu-a",
		store_url=INSTANCE,
		api_user="0E5B…@Summit",
		api_secret="e2e-acumatica-secret",
		refresh_token="e2e-acumatica-refresh",
		save_refresh_token=saved.append,
		endpoint_override=ENDPOINT,
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[AcumaticaReader, FakeTransport]:
	fake = oauth_source("acumatica", state, GRANT_PATH)
	built = ADAPTER.reader(config, fake, RecordingSleeper())
	assert isinstance(built, AcumaticaReader)
	return built, fake


def nodes(config: ConnectionConfig, kind: Kind, state: str = "v1") -> dict[str, Node]:
	source, _ = reader(config, state)
	found: dict[str, Node] = {}
	cursor: str | None = None
	while True:
		page = source.page(kind, cursor, None, "")
		found |= {node.id().split("/", 4)[-1]: node for node in page.nodes}
		if not page.has_next_page:
			return found
		cursor = page.end_cursor


def mapped(config: ConnectionConfig, kind: Kind, node: Node) -> Desired | None:
	return {m.kind: m for m in ADAPTER.mappers(config)}[kind].map(node)


def fields_of(desired: Desired | None) -> dict[str, Any]:
	assert desired is not None
	return desired.fields


def fields(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> dict[str, Any]:
	return fields_of(mapped(config, kind, nodes(config, kind, state)[key]))


def changed(node: Node, **values: Any) -> Node:
	"""The node with fields replaced, each given as its value (wrapped as the API sends it)."""
	wrapped = {name: {"value": v} if not isinstance(v, dict | list) else v for name, v in values.items()}
	return Node({**node.raw(), **wrapped}, node.path)


def gid(type_: str, key: str) -> str:
	return f"gid://acumatica/acu-a/{type_}/{key}"


def query_of(url: str) -> dict[str, list[str]]:
	return parse_qs(urlsplit(url).query)


class TestOAuth:
	def test_the_approval_goes_to_the_instances_identity_server(self, config: ConnectionConfig) -> None:
		url = ADAPTER.oauth(config, scripted()).authorization_url("https://erp/cb", "s", "v")
		assert url.startswith(INSTANCE + "/identity/connect/authorize?")
		query = query_of(url)
		assert (query["scope"], query["client_id"]) == (["api offline_access"], ["0E5B…@Summit"])
		assert ADAPTER.connected(config, scripted(), TokenGrant("t"), {}) == {}

	def test_refreshes_send_the_secret_and_keep_the_new_token(
		self, config: ConnectionConfig, saved: list[str]
	) -> None:
		source, fake = reader(config)
		source.page(Kind.COST_CENTER, None, None, "")
		grant = fake.requests[0]
		assert grant.url == ENDPOINT + GRANT_PATH
		form = parse_qs(grant.body.decode())
		assert (form["grant_type"], form["client_secret"]) == (["refresh_token"], ["e2e-acumatica-secret"])
		assert saved == [OAUTH_ROTATED_REFRESH_TOKEN]
		assert fake.requests[1].headers["Authorization"] == f"Bearer {OAUTH_ACCESS_TOKEN}"

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="client ID and secret"):
			ADAPTER.oauth(replace(config, api_secret=""), scripted())
		with pytest.raises(SourceError, match="refresh token"):
			ADAPTER.oauth(replace(config, save_refresh_token=None), scripted())
		with pytest.raises(SourceError, match="instance's address"):
			instance_url(replace(config, endpoint_override=None, store_url="acumatica"))
		assert instance_url(replace(config, endpoint_override=None)) == INSTANCE
		assert ADAPTER.filter_hint() is None and ADAPTER.kinds() == KINDS


class TestReader:
	def test_a_page_with_its_expansions(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		page = source.page(Kind.CUSTOMER, None, None, "")
		assert [node.string("source_key") for node in page.nodes] == ["ALPINE", "RIVERS"]
		query = query_of(fake.requests[1].url)
		assert urlsplit(fake.requests[1].url).path == ENDPOINT_PATH + "/Customer"
		assert (query["$top"], query["$skip"]) == (["100"], ["0"])
		assert query["$expand"][0].startswith("MainContact,MainContact/Address")
		assert page.nodes[0].string("entity_id") == "00000000-0000-0000-0000-000000000033"

	def test_an_incremental_page_asks_from_the_day_before(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.SALES_ORDER, None, datetime(2026, 10, 1, 9, 30, tzinfo=UTC), "")
		assert query_of(fake.requests[1].url)["$filter"] == [
			"OrderType ne 'QT' and LastModifiedDateTime gt datetimeoffset'2026-09-30T09:30:00'"
		]
		assert source.is_incremental(Kind.SALES_ORDER) and not source.is_incremental(Kind.COST_CENTER)

	def test_full_pages_continue_and_the_next_entity_follows(self, config: ConnectionConfig) -> None:
		full = [{"id": str(n), "InventoryID": {"value": f"I{n}"}} for n in range(PAGE_SIZE)]
		fake = scripted(
			TOKEN,
			HttpResponse(200, json.dumps(full).encode()),
			HttpResponse(200, b"[]"),
			HttpResponse(200, b"[]"),
		)
		source = ADAPTER.reader(config, fake, RecordingSleeper())
		first = source.page(Kind.PRODUCT, None, None, "")
		assert (first.has_next_page, first.end_cursor) == (True, "0|100")
		second = source.page(Kind.PRODUCT, first.end_cursor, None, "")
		assert query_of(fake.requests[2].url)["$skip"] == ["100"]
		assert (second.has_next_page, second.end_cursor) == (True, "1|0")
		last = source.page(Kind.PRODUCT, second.end_cursor, None, "")
		assert urlsplit(fake.requests[3].url).path == ENDPOINT_PATH + "/NonStockItem"
		assert not last.has_next_page
		with pytest.raises(SourceError, match="cursor"):
			source.page(Kind.PRODUCT, "5|0", None, "")
		with pytest.raises(SourceError, match="no page"):
			source.page(Kind.PAGE, None, None, "")

	def test_invoices_know_their_customers_currency(self, config: ConnectionConfig) -> None:
		invoices = nodes(config, Kind.SALES_INVOICE)
		assert invoices["Invoice/Invoice:AR000002"].string("customer_currency") == "USD"
		assert invoices["Invoice/Invoice:AR000001"].optional_flag("currency_override") is False

	def test_only_general_ledger_batches_are_journal_entries(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		page = source.page(Kind.JOURNAL_ENTRY, None, None, "")
		assert query_of(fake.requests[1].url)["$filter"] == ["Module eq 'GL'"]
		assert page.nodes[0].string("ledger_currency") == "CAD"

	def test_departments_and_job_titles_come_from_the_employees(self, config: ConnectionConfig) -> None:
		assert list(nodes(config, Kind.DEPARTMENT)) == [f"Department/{derived_key('RETAIL')}"]
		titles = nodes(config, Kind.DESIGNATION)
		assert sorted(node.string("name") for node in titles.values()) == ["Fitter", "Store Manager"]
		source, _ = reader(config)
		found = source.node(Kind.DESIGNATION, gid("Designation", derived_key("Fitter")))
		assert found is not None and found.string("name") == "Fitter"

	def test_one_record_is_looked_up_by_key(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		found = source.node(Kind.CUSTOMER, gid("Customer", "ALPINE"))
		assert found is not None and found.string("source_key") == "ALPINE"
		with pytest.raises(SourceError, match="Not an Acumatica customer"):
			source.node(Kind.CUSTOMER, gid("Vendor", "LEATHERCO"))
		with pytest.raises(SourceError, match="key"):
			source.node(Kind.SALES_INVOICE, gid("Invoice", "AR000001"))


class TestBooks:
	def test_accounts(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.ACCOUNT, "Account/10100") == {
			"number": "10100",
			"name": "Operating Bank",
			"root_type": "Asset",
			"account_type": "Bank",
			"parent": None,
			"is_group": False,
			"currency": "CAD",
		}
		assert fields(config, Kind.ACCOUNT, "Account/40000")["account_type"] == "Income Account"
		assert fields(config, Kind.ACCOUNT, "Account/60000", "v2")["disabled"] is True
		assert fields(config, Kind.ACCOUNT, "Account/61000", "v2")["name"] == "Office & Packing Supplies"
		with pytest.raises(Unsupported, match="Statistical"):
			mapped(
				config,
				Kind.ACCOUNT,
				changed(nodes(config, Kind.ACCOUNT)["Account/10100"], Type="Statistical"),
			)

	def test_subaccounts_warehouses_and_item_classes(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.COST_CENTER, "Subaccount/OPS-000") == {
			"name": "Operations",
			"parent": None,
			"is_group": False,
		}
		assert fields(config, Kind.WAREHOUSE, "Warehouse/RETAIL", "v2")["disabled"] is True
		assert fields(config, Kind.COLLECTION, "ItemClass/BOOTS")["name"] == "Boots"


class TestCatalog:
	def test_items(self, config: ConnectionConfig) -> None:
		boot = fields(config, Kind.PRODUCT, "Item/HIKER-BOOT")
		assert (boot["is_stock_item"], boot["stock_qty"], boot["price"], boot["collections"]) == (
			True,
			Decimal(15),
			Decimal(120),
			[gid("ItemClass", "BOOTS")],
		)
		fitting = fields(config, Kind.PRODUCT, "Item/FITTING")
		assert (fitting["is_stock_item"], fitting["stock_qty"]) == (False, None)
		assert mapped(config, Kind.PRODUCT, nodes(config, Kind.PRODUCT, "v2")["Item/TRAIL-SOCK"]) is None


class TestParties:
	def test_a_customer_with_its_addresses_and_contacts(self, config: ConnectionConfig) -> None:
		alpine = fields(config, Kind.CUSTOMER, "Customer/ALPINE")
		assert (alpine["type"], alpine["email"], alpine["tax_id"], alpine["currency"]) == (
			"Company",
			"buying@alpine.example",
			"987654321RT0001",
			"CAD",
		)
		assert [(a["address_type"], a["line1"]) for a in alpine["addresses"]] == [
			("Billing", "400 Bow Valley Trail"),
			("Shipping", "12 Railway Ave"),
		]
		assert alpine["contacts"] == [
			{
				"gid": gid("Contact", "101"),
				"name": "Noor Haddad",
				"email": "noor@alpine.example",
				"phone": "+1 403 555 0102",
				"job_title": "Buyer",
			}
		]
		assert fields(config, Kind.CUSTOMER, "Customer/ALPINE", "v2")["phone"] == "+1 403 555 0199"

	def test_an_individual_an_inactive_customer_and_a_vendor(self, config: ConnectionConfig) -> None:
		rivers = fields(config, Kind.CUSTOMER, "Customer/RIVERS")
		assert (rivers["type"], rivers["company"], rivers["addresses"]) == ("Individual", None, [])
		inactive = changed(nodes(config, Kind.CUSTOMER)["Customer/RIVERS"], Status="Inactive")
		assert fields_of(mapped(config, Kind.CUSTOMER, inactive))["disabled"] is True
		vendor = fields(config, Kind.SUPPLIER, "Vendor/LEATHERCO")
		assert (vendor["name"], vendor["addresses"][0]["city"]) == ("Prairie Leather Co", "Saskatoon")

	def test_leads(self, config: ConnectionConfig) -> None:
		ivy = fields(config, Kind.LEAD, "Lead/201")
		assert (ivy["status"], ivy["company"], ivy["notes"]) == (
			"Lead",
			"Moss Guiding",
			"Asked about group boot fittings",
		)
		assert fields(config, Kind.LEAD, "Lead/201", "v2")["status"] == "Converted"
		closed = changed(nodes(config, Kind.LEAD)["Lead/202"], Active=False, Status="Disqualified")
		assert fields_of(mapped(config, Kind.LEAD, closed))["status"] == "Do Not Contact"


class TestPeople:
	def test_employees(self, config: ConnectionConfig) -> None:
		theo = fields(config, Kind.EMPLOYEE, "Employee/EP002")
		assert (theo["department"], theo["designation"], theo["reports_to"], theo["status"]) == (
			gid("Department", derived_key("RETAIL")),
			gid("Designation", derived_key("Fitter")),
			gid("Employee", "EP001"),
			"Active",
		)
		assert fields(config, Kind.EMPLOYEE, "Employee/EP001")["date_of_birth"] == "1984-02-11"
		assert fields(config, Kind.EMPLOYEE, "Employee/EP002", "v2")["status"] == "Inactive"
		assert fields(config, Kind.DEPARTMENT, f"Department/{derived_key('RETAIL')}")["name"] == "RETAIL"

	def test_projects(self, config: ConnectionConfig) -> None:
		project = fields(config, Kind.PROJECT, "Project/FITOUT")
		assert (project["status"], project["customer"]) == ("Open", gid("Customer", "ALPINE"))
		completed = changed(nodes(config, Kind.PROJECT)["Project/FITOUT"], Status="Completed")
		assert fields_of(mapped(config, Kind.PROJECT, completed))["status"] == "Completed"


class TestSales:
	def test_an_opportunity_with_a_lead_or_a_customer(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.OPPORTUNITY)["Opportunity/OP000001"]
		opportunity = fields_of(mapped(config, Kind.OPPORTUNITY, node))
		assert (opportunity["party_kind"], opportunity["party"], opportunity["amount"]) == (
			"lead",
			gid("Lead", "202"),
			Decimal(2400),
		)
		won = fields_of(
			mapped(config, Kind.OPPORTUNITY, changed(node, BusinessAccount="ALPINE", Status="Won"))
		)
		assert (won["party_kind"], won["status"]) == ("customer", "Converted")
		bare = Node({k: v for k, v in node.raw().items() if k != "ContactID"}, node.path)
		with pytest.raises(Unsupported, match="neither"):
			mapped(config, Kind.OPPORTUNITY, bare)

	def test_a_quotation_and_a_sales_order(self, config: ConnectionConfig) -> None:
		quote = fields(config, Kind.DRAFT_ORDER, "SalesOrder/QT:QT000001")
		assert (quote["number"], quote["total"], quote["charges"]) == (
			"QT QT000001",
			Decimal(252),
			[{"kind": "tax", "description": "GST", "amount": Decimal(12)}],
		)
		order = fields(config, Kind.SALES_ORDER, "SalesOrder/SO:SO000001")
		assert (order["discount"], order["total"], order["reference"]) == (
			Decimal(10),
			Decimal("283.5"),
			"PO-5521",
		)
		assert order["taxes"][-1] == {
			"description": "Freight",
			"amount": Decimal(10),
			"account": None,
			"role": "shipping",
		}
		node = nodes(config, Kind.SALES_ORDER)["SalesOrder/SO:SO000001"]
		assert (
			fields_of(mapped(config, Kind.SALES_ORDER, changed(node, Status="On Hold")))["submitted"] is False
		)
		assert mapped(config, Kind.SALES_ORDER, changed(node, Status="Cancelled")) is None

	def test_an_invoice_with_its_document_discount(self, config: ConnectionConfig) -> None:
		invoice = fields(config, Kind.SALES_INVOICE, "Invoice/Invoice:AR000001")
		assert (invoice["discount"], invoice["total"], invoice["submitted"]) == (
			Decimal(30),
			Decimal("262.5"),
			True,
		)
		assert invoice["lines"][0]["cost_center"] == gid("Subaccount", "OPS-000")
		assert fields(config, Kind.SALES_INVOICE, "Invoice/Invoice:AR000004", "v2")["submitted"] is False
		assert (
			mapped(
				config,
				Kind.SALES_INVOICE,
				nodes(config, Kind.SALES_INVOICE, "v2")["Invoice/Invoice:AR000002"],
			)
			is None
		)

	def test_a_credit_memo_and_a_cash_sale(self, config: ConnectionConfig) -> None:
		memo = fields(config, Kind.SALES_INVOICE, "Invoice/Credit Memo:AR000003")
		assert (memo["is_return"], memo["return_against"], memo["total"]) == (
			True,
			gid("Invoice", "Invoice:AR000001"),
			Decimal(-42),
		)
		sale = fields(config, Kind.SALES_INVOICE, "CashSale/Cash Sale:CS000001")
		assert (sale["total"], sale["return_settles"], sale["reference"]) == (Decimal("31.5"), False, "POS-1")

	def test_currencies_the_default_endpoint_cant_tell(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.SALES_INVOICE)["Invoice/Invoice:AR000001"]
		with pytest.raises(Unsupported, match="several currencies"):
			mapped(config, Kind.SALES_INVOICE, Node({**node.raw(), "currency_override": True}, node.path))
		with pytest.raises(SourceError, match="no currency"):
			mapped(config, Kind.SALES_INVOICE, Node({**node.raw(), "customer_currency": None}, node.path))


class TestPurchases:
	def test_a_purchase_order_and_a_bill(self, config: ConnectionConfig) -> None:
		order = fields(config, Kind.PURCHASE_ORDER, "PurchaseOrder/Normal:PO000001")
		assert (order["total"], order["reference"], order["lines"][0]["rate"]) == (
			Decimal(600),
			"PL-881",
			Decimal(60),
		)
		bill = fields(config, Kind.PURCHASE_INVOICE, "Bill/Bill:AP000001")
		assert (bill["total"], bill["reference"], bill["lines"][1]["account"]) == (
			Decimal(651),
			"PL-881-INV",
			gid("Account", "61000"),
		)

	def test_a_debit_adjustment_is_a_return_and_a_prepayment_is_planned(
		self, config: ConnectionConfig
	) -> None:
		node = nodes(config, Kind.PURCHASE_INVOICE)["Bill/Bill:AP000001"]
		debit = fields_of(mapped(config, Kind.PURCHASE_INVOICE, changed(node, Type="Debit Adj.")))
		assert (debit["is_return"], debit["total"]) == (True, Decimal(-651))
		with pytest.raises(Unsupported, match="Prepayment"):
			mapped(config, Kind.PURCHASE_INVOICE, changed(node, Type="Prepayment"))


class TestPayments:
	def test_a_customer_payment_applied_to_its_invoice(self, config: ConnectionConfig) -> None:
		payment = fields(config, Kind.PAYMENT_ENTRY, "Payment/Payment:PM000001")
		assert (payment["payment_type"], payment["amount"], payment["account"], payment["reference"]) == (
			"Receive",
			Decimal("220.5"),
			None,
			"EFT-3301",
		)
		assert payment["allocations"] == [
			{
				"kind": "sales_invoice",
				"document": gid("Invoice", "Invoice:AR000001"),
				"amount": Decimal("220.5"),
			}
		]

	def test_a_check_and_a_cash_sales_money(self, config: ConnectionConfig) -> None:
		check = fields(config, Kind.PAYMENT_ENTRY, "Check/Payment:CHK00001")
		assert (check["payment_type"], check["allocations"][0]["document"]) == (
			"Pay",
			gid("Bill", "Bill:AP000001"),
		)
		sale = fields(config, Kind.PAYMENT_ENTRY, "CashSale/Cash Sale:CS000001")
		assert sale["allocations"] == [
			{
				"kind": "sales_invoice",
				"document": gid("CashSale", "Cash Sale:CS000001"),
				"amount": Decimal("31.5"),
			}
		]
		node = nodes(config, Kind.PAYMENT_ENTRY)["CashSale/Cash Sale:CS000001"]
		assert (
			fields_of(mapped(config, Kind.PAYMENT_ENTRY, changed(node, Type="Cash Return")))["payment_type"]
			== "Pay"
		)

	def test_payments_that_cant_be_payment_entries(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.PAYMENT_ENTRY)["Payment/Payment:PM000001"]
		with pytest.raises(Unsupported, match="Voided Payment"):
			mapped(config, Kind.PAYMENT_ENTRY, changed(node, Type="Voided Payment"))
		memo = [
			{
				"AdjustedDocType": {"value": "Credit Memo"},
				"AdjustedRefNbr": {"value": "AR000003"},
				"AmountPaid": {"value": 5},
			}
		]
		with pytest.raises(Unsupported, match="Credit Memo"):
			mapped(config, Kind.PAYMENT_ENTRY, changed(node, ApplicationHistory=memo))
		assert mapped(config, Kind.PAYMENT_ENTRY, changed(node, Status="Voided")) is None


class TestJournals:
	def test_a_general_ledger_batch(self, config: ConnectionConfig) -> None:
		batch = fields(config, Kind.JOURNAL_ENTRY, "JournalTransaction/GL:GL000001")
		assert (batch["number"], batch["date"], batch["submitted"]) == ("GL GL000001", "2026-09-30", True)
		assert [(line["account"], line["debit"], line["cost_center"]) for line in batch["lines"]] == [
			(gid("Account", "60000"), Decimal(1800), gid("Subaccount", "OPS-000")),
			(gid("Account", "10100"), Decimal(0), gid("Subaccount", "000-000")),
		]

	def test_a_batch_in_another_currency_is_planned(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.JOURNAL_ENTRY)["JournalTransaction/GL:GL000001"]
		with pytest.raises(Unsupported, match="USD"):
			mapped(config, Kind.JOURNAL_ENTRY, changed(node, CurrencyID="USD"))
