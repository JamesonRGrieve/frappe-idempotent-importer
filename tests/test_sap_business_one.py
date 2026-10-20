# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SAP Business One adapter over the recorded company (tests/fixtures/sap_business_one, proven against
the OpenAPI documents generated from the Service Layer's $metadata): the session login, entity sets read in
key order and through next links, the incremental condition, the reader's annotations, and every mapper."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError, Unsupported
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.sap_business_one.adapter import SapBusinessOneAdapter
from idempotent_importer.sources.sap_business_one.mapping import FieldMap
from idempotent_importer.sources.sap_business_one.reader import (
	KINDS,
	SETS,
	ServiceLayerReader,
	key_literal,
	key_text,
	service_url,
	top_of,
)
from tests.support.sap_business_one import (
	B1_COMPANY_DB,
	B1_COOKIE,
	B1_PASSWORD,
	B1_USER,
)
from tests.support.transport import FakeTransport, RecordingSleeper, rest_source, scripted

ADAPTER = SapBusinessOneAdapter()
ENDPOINT = "http://sap_business_one-fake:8080"
ROOTS = '{"root_types": {"1": "Asset", "2": "Liability", "3": "Equity"}}'
LOGIN = HttpResponse(200, b'{"SessionId":"s1","Version":"1000201","SessionTimeout":30}')


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="sap_business_one",
		connection="sap-b",
		store_url="https://b1.example.test:50000",
		api_user=B1_USER,
		api_secret=B1_PASSWORD,
		account=B1_COMPANY_DB,
		field_map=ROOTS,
		endpoint_override=ENDPOINT,
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[ServiceLayerReader, FakeTransport]:
	fake = rest_source("sap_business_one", state)
	built = ADAPTER.reader(config, fake, RecordingSleeper())
	assert isinstance(built, ServiceLayerReader)
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


def fields(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> dict[str, Any]:
	desired = mapped(config, kind, nodes(config, kind, state)[key])
	assert desired is not None
	return desired.fields


def fields_of(desired: Desired | None) -> dict[str, Any]:
	assert desired is not None
	return desired.fields


def changed(node: Node, **values: Any) -> Node:
	return Node({**node.raw(), **values}, node.path)


def editable(node: Node, field: str) -> list[dict[str, Any]]:
	copied: list[dict[str, Any]] = json.loads(json.dumps(node.raw()[field]))
	return copied


def gid(type_: str, key: str) -> str:
	return f"gid://sap_business_one/sap-b/{type_}/{key}"


def listing(*entities: dict[str, Any], next_link: str | None = None) -> HttpResponse:
	body: dict[str, Any] = {"value": list(entities)}
	if next_link:
		body["odata.nextLink"] = next_link
	return HttpResponse(200, json.dumps(body).encode())


class TestSession:
	def test_the_login_opens_a_session_sent_as_cookies(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.WAREHOUSE, None, None, "")
		login = fake.requests[0]
		assert login.url == ENDPOINT + "/b1s/v1/Login"
		assert json.loads(login.body) == {
			"CompanyDB": B1_COMPANY_DB,
			"UserName": B1_USER,
			"Password": B1_PASSWORD,
		}
		assert fake.requests[1].headers["Cookie"] == B1_COOKIE
		assert fake.requests[1].headers["Prefer"] == "odata.maxpagesize=100"

	def test_a_lapsed_session_logs_in_again_once(self, config: ConnectionConfig) -> None:
		fake = scripted(LOGIN, HttpResponse(401, b"{}"), LOGIN, listing())
		ADAPTER.reader(config, fake, RecordingSleeper()).page(Kind.PROJECT, None, None, "")
		assert [urlsplit(r.url).path for r in fake.requests] == [
			"/b1s/v1/Login",
			"/b1s/v1/Projects",
			"/b1s/v1/Login",
			"/b1s/v1/Projects",
		]
		assert fake.requests[3].headers["Cookie"] == "B1SESSION=s1"

	def test_a_refused_login_stops_the_run_and_trouble_pauses_it(self, config: ConnectionConfig) -> None:
		refused = scripted(HttpResponse(401, b'{"error":{"code":100000027}}'))
		with pytest.raises(CredentialsRejected, match="company database"):
			ADAPTER.reader(config, refused, RecordingSleeper()).page(Kind.PROJECT, None, None, "")
		with pytest.raises(RetryLater):
			ADAPTER.reader(config, scripted(HttpResponse(503, b"")), RecordingSleeper()).page(
				Kind.PROJECT, None, None, ""
			)
		with pytest.raises(SourceError, match="no session"):
			ADAPTER.reader(config, scripted(HttpResponse(200, b"[]")), RecordingSleeper()).page(
				Kind.PROJECT, None, None, ""
			)

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="company database"):
			ADAPTER.reader(replace(config, account=""), scripted(), RecordingSleeper())
		with pytest.raises(SourceError, match="Service Layer's address"):
			service_url(replace(config, endpoint_override=None, store_url="b1 server"))
		bare = replace(config, endpoint_override=None)
		assert service_url(bare) == "https://b1.example.test:50000/b1s/v1"
		assert service_url(replace(bare, store_url="https://b1:50000/b1s/v1/")) == "https://b1:50000/b1s/v1"
		assert "password" not in repr(ADAPTER.reader(config, scripted(), RecordingSleeper()))
		assert ADAPTER.filter_hint() is None and ADAPTER.kinds() == KINDS


class TestReader:
	def test_a_set_is_read_in_key_order_with_the_kinds_condition(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		page = source.page(Kind.CUSTOMER, None, None, "")
		assert [node.string("CardCode") for node in page.nodes] == ["C001", "C002"]
		query = parse_qs(urlsplit(fake.requests[1].url).query)
		assert query == {"$orderby": ["CardCode"], "$filter": ["CardType eq 'cCustomer'"]}

	def test_an_incremental_read_asks_from_the_day_before(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.SUPPLIER, None, datetime(2026, 10, 1, 9, tzinfo=UTC), "")
		query = parse_qs(urlsplit(fake.requests[1].url).query)
		assert query["$filter"] == ["CardType eq 'cSupplier' and UpdateDate ge '2026-09-30'"]
		assert source.is_incremental(Kind.SALES_INVOICE)
		assert not source.is_incremental(Kind.ACCOUNT) and not source.is_incremental(Kind.PAYMENT_ENTRY)
		source.page(Kind.ACCOUNT, None, datetime(2026, 10, 1, tzinfo=UTC), "")
		assert "$filter" not in parse_qs(urlsplit(fake.requests[-1].url).query)

	def test_next_links_and_the_next_set(self, config: ConnectionConfig) -> None:
		fake = scripted(
			LOGIN,
			listing({"DocEntry": 1, "DocNum": 1}, next_link="Invoices?$orderby=DocEntry&$skip=100"),
			listing({"DocEntry": 2, "DocNum": 2}),
			listing(),
		)
		source = ADAPTER.reader(config, fake, RecordingSleeper())
		first = source.page(Kind.SALES_INVOICE, None, None, "")
		assert first.end_cursor == "0|/Invoices?$orderby=DocEntry&$skip=100"
		second = source.page(Kind.SALES_INVOICE, first.end_cursor, None, "")
		assert unquote(fake.requests[2].url) == ENDPOINT + "/b1s/v1/Invoices?$orderby=DocEntry&$skip=100"
		assert (second.has_next_page, second.end_cursor) == (True, "1|")
		last = source.page(Kind.SALES_INVOICE, second.end_cursor, None, "")
		assert urlsplit(fake.requests[3].url).path == "/b1s/v1/CreditNotes"
		assert (last.has_next_page, last.end_cursor) == (False, None)
		with pytest.raises(SourceError, match="cursor"):
			source.page(Kind.SALES_INVOICE, "7|", None, "")
		with pytest.raises(SourceError, match="no page"):
			source.page(Kind.PAGE, None, None, "")

	def test_only_manual_journal_entries_are_read(self, config: ConnectionConfig) -> None:
		journals = nodes(config, Kind.JOURNAL_ENTRY)
		assert list(journals) == ["JournalEntry/10"]
		card_types = [
			line.optional_string("card_type")
			for line in journals["JournalEntry/10"].children("JournalEntryLines")
		]
		assert card_types == [None, "cSupplier"]

	def test_accounts_know_their_drawer(self, config: ConnectionConfig) -> None:
		accounts = nodes(config, Kind.ACCOUNT)
		assert accounts["Account/1100"].string("drawer") == "1"
		assert accounts["Account/4"].string("drawer") == "4"
		assert top_of("c", {"c": "b", "b": "c"}) == "b"

	def test_one_entity_is_looked_up_by_key(self, config: ConnectionConfig) -> None:
		source, _ = reader(config)
		found = source.node(Kind.CUSTOMER, gid("BusinessPartner", "C001"))
		assert found is not None and found.string("CardName") == "Bayview Bistro"
		with pytest.raises(SourceError, match="Not a SAP Business One customer"):
			source.node(Kind.CUSTOMER, gid("Invoice", "4"))

	def test_keys(self) -> None:
		assert key_text("A/B 1") == "A%2FB 1" and key_text(12) == "12"
		with pytest.raises(SourceError):
			key_text(None)
		partners, invoices = SETS[Kind.CUSTOMER][0], SETS[Kind.SALES_INVOICE][0]
		assert key_literal(partners, "O%27Neil") == "'O''Neil'"
		assert key_literal(invoices, "4") == "4"
		with pytest.raises(SourceError):
			key_literal(invoices, "x")


class TestBooks:
	def test_accounts_take_their_root_type_from_their_type_or_drawer(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.ACCOUNT, "Account/1100") == {
			"number": "1100",
			"name": "Chequing",
			"root_type": "Asset",
			"account_type": "Cash",
			"parent": gid("Account", "1"),
			"is_group": False,
			"currency": None,
		}
		assert fields(config, Kind.ACCOUNT, "Account/4100")["root_type"] == "Income"
		assert fields(config, Kind.ACCOUNT, "Account/5")["is_group"] is True
		assert fields(config, Kind.ACCOUNT, "Account/5200", "v2")["name"] == "Office Supplies & Postage"
		frozen = changed(nodes(config, Kind.ACCOUNT)["Account/5100"], FrozenFor="tYES")
		assert fields_of(mapped(config, Kind.ACCOUNT, frozen))["disabled"] is True

	def test_an_other_account_without_its_drawers_root_type_is_unsupported(
		self, config: ConnectionConfig
	) -> None:
		bare = replace(config, field_map="")
		with pytest.raises(Unsupported, match="drawer 1"):
			mapped(bare, Kind.ACCOUNT, nodes(bare, Kind.ACCOUNT)["Account/1200"])

	def test_the_field_map(self) -> None:
		assert FieldMap.of("") == FieldMap()
		assert FieldMap.of('{"price_list": 2}').price_list == 2
		for bad in ("[", '{"root_types": []}', '{"root_types": {"1": "Assets"}}', '{"price_list": true}'):
			with pytest.raises(SourceError, match="Field Map"):
				FieldMap.of(bad)

	def test_profit_centers_of_the_first_dimension(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.COST_CENTER, "ProfitCenter/OPS") == {
			"name": "Operations",
			"parent": None,
			"is_group": False,
		}
		with pytest.raises(Unsupported, match="dimension 2"):
			mapped(config, Kind.COST_CENTER, nodes(config, Kind.COST_CENTER)["ProfitCenter/REGION-W"])
		inactive = changed(nodes(config, Kind.COST_CENTER)["ProfitCenter/OPS"], Active="tNO")
		assert fields_of(mapped(config, Kind.COST_CENTER, inactive))["disabled"] is True

	def test_warehouses(self, config: ConnectionConfig) -> None:
		main = fields(config, Kind.WAREHOUSE, "Warehouse/01")
		assert (main["address"]["line1"], main["address"]["country_code"]) == ("200 Granville St", "CA")
		assert fields(config, Kind.WAREHOUSE, "Warehouse/02", "v2")["disabled"] is True

	def test_projects(self, config: ConnectionConfig) -> None:
		project = fields(config, Kind.PROJECT, "Project/P01")
		assert (project["name"], project["status"], project["end"]) == (
			"Bayview Bistro Refit",
			"Open",
			"2026-11-30",
		)
		closed = changed(nodes(config, Kind.PROJECT)["Project/P01"], Active="tNO")
		assert fields_of(mapped(config, Kind.PROJECT, closed))["status"] == "Completed"


class TestCatalog:
	def test_items(self, config: ConnectionConfig) -> None:
		mug = fields(config, Kind.PRODUCT, "Item/A1000")
		assert (mug["sku"], mug["barcode"], mug["price"], mug["stock_qty"], mug["collections"]) == (
			"A1000",
			"0627843001234",
			Decimal(18),
			Decimal(40),
			[gid("ItemGroup", "100")],
		)
		assert fields(config, Kind.PRODUCT, "Item/S100")["is_stock_item"] is False
		assert mapped(config, Kind.PRODUCT, nodes(config, Kind.PRODUCT, "v2")["Item/K200"]) is None
		second = replace(config, field_map='{"price_list": 2}')
		assert fields(second, Kind.PRODUCT, "Item/A1000")["price"] == Decimal(15)
		assert fields(second, Kind.PRODUCT, "Item/S100")["price"] is None

	def test_item_groups(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.COLLECTION, "ItemGroup/100")["name"] == "Mugs"


class TestParties:
	def test_a_customer_with_its_addresses_and_contact_persons(self, config: ConnectionConfig) -> None:
		cafe = fields(config, Kind.CUSTOMER, "BusinessPartner/C001")
		assert (cafe["type"], cafe["tax_id"], cafe["currency"]) == ("Company", "123456789RT0001", "CAD")
		assert [(a["address_type"], a["line1"]) for a in cafe["addresses"]] == [
			("Billing", "12 Lakeshore Rd"),
			("Shipping", "14 Lakeshore Rd"),
		]
		assert cafe["contacts"] == [
			{
				"gid": gid("ContactEmployee", "11"),
				"name": "Lena Park",
				"email": "lena@bayview.example",
				"phone": "+1 250 555 0112",
				"job_title": "Head Chef",
			}
		]
		assert fields(config, Kind.CUSTOMER, "BusinessPartner/C001", "v2")["phone"] == "+1 604 555 0199"

	def test_a_private_customer_and_a_frozen_one(self, config: ConnectionConfig) -> None:
		maya = fields(config, Kind.CUSTOMER, "BusinessPartner/C002")
		assert (maya["type"], maya["company"], maya["currency"]) == ("Individual", None, "USD")
		frozen = changed(nodes(config, Kind.CUSTOMER)["BusinessPartner/C002"], Frozen="tYES", Currency="##")
		desired = mapped(config, Kind.CUSTOMER, frozen)
		assert desired is not None and desired.fields["disabled"] is True and "currency" not in desired.fields

	def test_suppliers_and_leads(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.SUPPLIER, "BusinessPartner/V001")["name"] == "Coastal Paper Ltd"
		harbor = fields(config, Kind.LEAD, "BusinessPartner/L001")
		assert (harbor["status"], harbor["company"], harbor["notes"]) == (
			"Lead",
			"Summit Pilates",
			"Met at the spring trade show",
		)
		person = changed(
			nodes(config, Kind.LEAD)["BusinessPartner/L001"], CompanyPrivate="cPrivate", CardName="Ana Ruiz"
		)
		desired = mapped(config, Kind.LEAD, person)
		assert desired is not None and (desired.fields["first_name"], desired.fields["last_name"]) == (
			"Ana",
			"Ruiz",
		)
		assert "BusinessPartner/L001" in nodes(config, Kind.CUSTOMER, "v2")
		assert "BusinessPartner/L001" not in nodes(config, Kind.LEAD, "v2")


class TestPeople:
	def test_employees(self, config: ConnectionConfig) -> None:
		jordan = fields(config, Kind.EMPLOYEE, "Employee/2")
		assert (jordan["department"], jordan["designation"], jordan["reports_to"], jordan["gender"]) == (
			gid("Department", "2"),
			gid("Position", "2"),
			gid("Employee", "1"),
			"Female",
		)
		left = fields(config, Kind.EMPLOYEE, "Employee/2", "v2")
		assert (left["status"], left["relieving_date"]) == ("Left", "2026-10-01")
		inactive = changed(nodes(config, Kind.EMPLOYEE)["Employee/1"], Active="tNO")
		assert fields_of(mapped(config, Kind.EMPLOYEE, inactive))["status"] == "Inactive"

	def test_departments_and_positions(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.DEPARTMENT, "Department/2")["name"] == "Operations"
		assert fields(config, Kind.DESIGNATION, "Position/1") == {"name": "Sales Manager"}


class TestSales:
	def test_an_opportunity_with_a_lead(self, config: ConnectionConfig) -> None:
		opportunity = fields(config, Kind.OPPORTUNITY, "Opportunity/1")
		assert (
			opportunity["party_kind"],
			opportunity["party"],
			opportunity["status"],
			opportunity["amount"],
		) == (
			"lead",
			gid("BusinessPartner", "L002"),
			"Open",
			Decimal(900),
		)
		node = nodes(config, Kind.OPPORTUNITY)["Opportunity/1"]
		won = mapped(config, Kind.OPPORTUNITY, changed(node, card_type="cCustomer", Status="sos_Sold"))
		assert won is not None and (won.fields["party_kind"], won.fields["status"]) == (
			"customer",
			"Converted",
		)
		with pytest.raises(Unsupported, match="cSupplier"):
			mapped(config, Kind.OPPORTUNITY, changed(node, card_type="cSupplier"))

	def test_a_quotation_is_a_draft_quotation(self, config: ConnectionConfig) -> None:
		quotation = fields(config, Kind.DRAFT_ORDER, "Quotation/1")
		assert (quotation["number"], quotation["created_at"], quotation["total"]) == (
			"1001",
			"2026-09-10T12:00:00Z",
			Decimal("37.8"),
		)
		assert quotation["charges"] == [{"kind": "tax", "description": "GST", "amount": Decimal("1.8")}]

	def test_an_order_charges_freight_as_shipping(self, config: ConnectionConfig) -> None:
		order = fields(config, Kind.SALES_ORDER, "Order/2")
		assert order["taxes"] == [
			{"description": "GST", "amount": Decimal("7.05"), "account": None},
			{"description": "Freight", "amount": Decimal(10), "account": None, "role": "shipping"},
		]
		assert (order["due_date"], order["total"]) == ("2026-09-30", Decimal("148.05"))

	def test_an_invoice(self, config: ConnectionConfig) -> None:
		invoice = fields(config, Kind.SALES_INVOICE, "Invoice/4")
		assert (invoice["discount"], invoice["total"], invoice["reference"]) == (
			Decimal(11),
			Decimal(126),
			"PO-7781",
		)
		second = invoice["lines"][1]
		assert (second["cost_center"], second["project"], second["account"]) == (
			gid("ProfitCenter", "OPS"),
			gid("Project", "P01"),
			gid("Account", "4100"),
		)
		assert fields(config, Kind.SALES_ORDER, "Order/2", "v2")["remarks"] == "Deliver to the kitchen door"

	def test_an_invoice_in_another_currency_is_in_its_own(self, config: ConnectionConfig) -> None:
		invoice = fields(config, Kind.SALES_INVOICE, "Invoice/5")
		assert (invoice["currency"], invoice["exchange_rate"], invoice["total"], invoice["base_total"]) == (
			"USD",
			Decimal("1.35"),
			Decimal(23),
			Decimal("31.05"),
		)
		assert invoice["lines"][0]["rate"] == Decimal(23)

	def test_a_cancelled_invoice_and_its_cancellation_are_gone(self, config: ConnectionConfig) -> None:
		invoices = nodes(config, Kind.SALES_INVOICE, "v2")
		assert mapped(config, Kind.SALES_INVOICE, invoices["Invoice/5"]) is None
		assert mapped(config, Kind.SALES_INVOICE, invoices["Invoice/12"]) is None
		assert fields(config, Kind.SALES_INVOICE, "Invoice/13", "v2")["total"] == Decimal("94.5")

	def test_a_credit_note_returns_against_its_invoice(self, config: ConnectionConfig) -> None:
		note = fields(config, Kind.SALES_INVOICE, "CreditNote/6")
		assert (note["is_return"], note["return_against"], note["total"]) == (
			True,
			gid("Invoice", "4"),
			Decimal("-18.9"),
		)
		assert note["lines"][0]["qty"] == Decimal(-1)

	def test_rounding_and_down_payments(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.SALES_INVOICE)["Invoice/4"]
		rounded = fields_of(
			mapped(config, Kind.SALES_INVOICE, changed(node, RoundingDiffAmount=0.01, DocTotal=126.01))
		)
		assert rounded["taxes"][-1] == {
			"description": "Rounding",
			"amount": Decimal("0.01"),
			"account": None,
			"role": "fee",
		}
		with pytest.raises(Unsupported, match="down payment"):
			mapped(config, Kind.SALES_INVOICE, changed(node, DownPaymentAmount=10.0))
		with pytest.raises(Unsupported, match="not its total"):
			mapped(config, Kind.SALES_INVOICE, changed(node, DocTotal=1.0))

	def test_tax_codes_that_dont_make_the_vat_are_one_tax(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.SALES_INVOICE)["Invoice/4"]
		lines = editable(node, "DocumentLines")
		lines[0]["TaxTotal"] = 0.0
		desired = mapped(config, Kind.SALES_INVOICE, changed(node, DocumentLines=lines))
		assert desired is not None and desired.fields["taxes"] == [
			{"description": "Tax", "amount": Decimal(6), "account": None}
		]


class TestPurchases:
	def test_a_purchase_invoice_with_a_service_line(self, config: ConnectionConfig) -> None:
		bill = fields(config, Kind.PURCHASE_INVOICE, "PurchaseInvoice/7")
		assert bill["reference"] == "NP-3321"
		paper = bill["lines"][1]
		assert (paper["product"], paper["account"], paper["cost_center"], paper["qty"]) == (
			None,
			gid("Account", "5200"),
			gid("ProfitCenter", "OPS"),
			Decimal(1),
		)
		assert fields(config, Kind.PURCHASE_ORDER, "PurchaseOrder/3")["total"] == Decimal(90)

	def test_a_purchase_credit_note_and_purchase_rounding(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.PURCHASE_INVOICE)["PurchaseInvoice/7"]
		credit = changed(node, id=gid("PurchaseCreditNote", "7"))
		desired = mapped(config, Kind.PURCHASE_INVOICE, Node(credit.raw(), credit.path))
		assert desired is not None and desired.fields["is_return"] is True
		with pytest.raises(Unsupported, match="rounding"):
			mapped(config, Kind.PURCHASE_INVOICE, changed(node, RoundingDiffAmount=0.01))


class TestPayments:
	def test_an_incoming_transfer(self, config: ConnectionConfig) -> None:
		payment = fields(config, Kind.PAYMENT_ENTRY, "IncomingPayment/8")
		assert (payment["payment_type"], payment["amount"], payment["account"], payment["reference"]) == (
			"Receive",
			Decimal("107.1"),
			gid("Account", "1100"),
			"EFT-7001",
		)
		assert payment["allocations"] == [
			{"kind": "sales_invoice", "document": gid("Invoice", "4"), "amount": Decimal("107.1")}
		]

	def test_a_vendor_cheque(self, config: ConnectionConfig) -> None:
		payment = fields(config, Kind.PAYMENT_ENTRY, "VendorPayment/9")
		assert (payment["payment_type"], payment["party_type"], payment["account"]) == (
			"Pay",
			"Supplier",
			gid("Account", "1100"),
		)
		assert payment["allocations"][0]["kind"] == "purchase_invoice"

	def test_payments_that_cant_be_payment_entries(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.PAYMENT_ENTRY)["IncomingPayment/8"]
		cases: dict[str, dict[str, Any]] = {
			"on account": {"DocType": "rAccount"},
			"another currency": {"DocRate": 1.35},
			"2 means": {"CashSum": 5.0, "CashAccount": "1100"},
			"credit note": {
				"PaymentInvoices": [{"DocEntry": 6, "SumApplied": -18.9, "InvoiceType": "it_CredItnote"}]
			},
			"it_JournalEntry": {
				"PaymentInvoices": [{"DocEntry": 10, "SumApplied": 5.0, "InvoiceType": "it_JournalEntry"}]
			},
		}
		for message, values in cases.items():
			with pytest.raises(Unsupported, match=message):
				mapped(config, Kind.PAYMENT_ENTRY, changed(node, **values))
		assert mapped(config, Kind.PAYMENT_ENTRY, changed(node, Cancelled="tYES")) is None
		card = changed(
			node,
			TransferSum=0.0,
			PaymentCreditCards=[{"LineNum": 0, "CreditAcct": "1100", "CreditSum": 107.1}],
		)
		assert fields_of(mapped(config, Kind.PAYMENT_ENTRY, card))["account"] == gid("Account", "1100")


class TestJournals:
	def test_a_manual_journal_entry(self, config: ConnectionConfig) -> None:
		journal = fields(config, Kind.JOURNAL_ENTRY, "JournalEntry/10")
		assert (journal["number"], journal["date"], journal["remarks"]) == (
			"10",
			"2026-09-30",
			"September rent",
		)
		assert [
			(line["debit"], line["party_type"], line["party"], line["cost_center"])
			for line in journal["lines"]
		] == [
			(Decimal(1500), None, None, gid("ProfitCenter", "OPS")),
			(Decimal(0), "Supplier", gid("BusinessPartner", "V001"), None),
		]

	def test_journals_that_cant_be_journal_entries(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.JOURNAL_ENTRY)["JournalEntry/10"]
		lines = editable(node, "JournalEntryLines")
		lines[1]["card_type"] = "cLid"
		with pytest.raises(Unsupported, match="cLid"):
			mapped(config, Kind.JOURNAL_ENTRY, changed(node, JournalEntryLines=lines))
		with pytest.raises(Unsupported, match="doesn't balance"):
			mapped(config, Kind.JOURNAL_ENTRY, changed(node, JournalEntryLines=lines[:1]))
