# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Sage Intacct adapter over the recorded company (tests/fixtures/sage_intacct, proven against the
gateway's 2.1 response DTD): the gateway's requests, credentials and errors, ``get_list`` paging and the
incremental filter, the Field Map, every mapper, and the DTD validator itself."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError, Unsupported
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.sage_intacct.adapter import SageIntacctAdapter
from idempotent_importer.sources.sage_intacct.gateway import GATEWAY_PATH, xml_value
from idempotent_importer.sources.sage_intacct.reader import KINDS, FieldMap, IntacctReader, derived_key
from tests.support.fixtures import intacct_request
from tests.support.sage_intacct import (
	INTACCT_COMPANY,
	INTACCT_PASSWORD,
	INTACCT_SENDER,
	INTACCT_SENDER_PASSWORD,
	INTACCT_USER,
)
from tests.support.transport import FakeTransport, RecordingSleeper, rest_source, scripted
from tools.dtd import Dtd

ADAPTER = SageIntacctAdapter()
ENDPOINT = "http://sage_intacct-fake:8080"


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="sage_intacct",
		connection="intacct-s",
		store_url="https://www.intacct.com",
		api_user=INTACCT_USER,
		api_secret=INTACCT_PASSWORD,
		account=INTACCT_COMPANY,
		key_id=INTACCT_SENDER,
		sender_secret=INTACCT_SENDER_PASSWORD,
		field_map='{"root_types": {"3000": "Equity"}}',
		endpoint_override=ENDPOINT,
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[IntacctReader, FakeTransport]:
	fake = rest_source("sage_intacct", state)
	built = ADAPTER.reader(config, fake, RecordingSleeper())
	assert isinstance(built, IntacctReader)
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
	return Node({**node.raw(), **values}, node.path)


def gid(type_: str, key: str) -> str:
	return f"gid://sage_intacct/intacct-s/{type_}/{key}"


def answer(body: str) -> HttpResponse:
	return HttpResponse(200, body.encode())


FAILED_CONTROL = (
	"<response><control><status>failure</status><senderid>s</senderid><controlid>c</controlid></control>"
	"<errormessage><error><errorno>XL03000006</errorno><description2>Incorrect sender</description2></error>"
	"</errormessage></response>"
)
FAILED_LOGIN = (
	"<response><control><status>success</status><senderid>s</senderid><controlid>c</controlid></control>"
	"<operation><authentication><status>failure</status><userid>u</userid><companyid>c</companyid>"
	"</authentication><errormessage><error><errorno>XL03000006</errorno></error></errormessage></operation>"
	"</response>"
)


class TestGateway:
	def test_a_request_carries_the_sender_and_the_login(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.WAREHOUSE, None, None, "")
		request = fake.requests[0]
		assert request.url == ENDPOINT + GATEWAY_PATH
		assert request.headers["Content-Type"] == "x-intacct-xml-request"
		decoded = intacct_request(request.body)
		assert {k: decoded[k] for k in ("sender", "sender_password", "company", "user", "password")} == {
			"sender": INTACCT_SENDER,
			"sender_password": INTACCT_SENDER_PASSWORD,
			"company": INTACCT_COMPANY,
			"user": INTACCT_USER,
			"password": INTACCT_PASSWORD,
		}
		assert (decoded["function"], decoded["object"], decoded["start"]) == ("get_list", "warehouse", "0")
		assert INTACCT_PASSWORD not in repr(source)

	def test_refused_credentials_stop_the_run(self, config: ConnectionConfig) -> None:
		for body, message in ((FAILED_CONTROL, "sender"), (FAILED_LOGIN, "login")):
			source = ADAPTER.reader(config, scripted(answer(body)), RecordingSleeper())
			with pytest.raises(CredentialsRejected, match=message):
				source.page(Kind.ACCOUNT, None, None, "")

	def test_trouble_pauses_and_nonsense_fails(self, config: ConnectionConfig) -> None:
		with pytest.raises(RetryLater):
			ADAPTER.reader(config, scripted(HttpResponse(503, b"")), RecordingSleeper()).page(
				Kind.ACCOUNT, None, None, ""
			)
		with pytest.raises(SourceError, match="isn't XML"):
			ADAPTER.reader(config, scripted(answer("{}")), RecordingSleeper()).page(
				Kind.ACCOUNT, None, None, ""
			)
		refused = (
			FAILED_LOGIN.replace("<status>failure</status><userid>", "<status>success</status><userid>")
			.replace(
				"<errormessage>",
				"<result><status>failure</status><function>get_list</function><controlid>f1</controlid><errormessage>",
			)
			.replace("</errormessage></operation>", "</errormessage></result></operation>")
		)
		with pytest.raises(SourceError, match="refused the request: XL03000006"):
			ADAPTER.reader(config, scripted(answer(refused)), RecordingSleeper()).page(
				Kind.ACCOUNT, None, None, ""
			)

	def test_configuration(self, config: ConnectionConfig) -> None:
		with pytest.raises(SourceError, match="the sender password"):
			ADAPTER.reader(replace(config, sender_secret=""), scripted(), RecordingSleeper())
		assert ADAPTER.kinds() == KINDS and ADAPTER.filter_hint() is None

	def test_xml_values(self) -> None:
		element = ET.fromstring('<a x="1"><b>2</b><b>3</b><c/><d>t</d></a>')  # noqa: S314 - a literal
		assert xml_value(element) == {"@x": "1", "b": ["2", "3"], "c": "", "d": "t"}


class TestReader:
	def test_an_incremental_list_filters_from_the_day_before(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.CUSTOMER, None, datetime(2026, 10, 1, 9, 30, tzinfo=UTC), "")
		assert intacct_request(fake.requests[0].body)["filter"] == [
			["whenmodified", ">=", "09/30/2026 09:30:00"]
		]
		assert source.is_incremental(Kind.PAYMENT_ENTRY) and not source.is_incremental(Kind.WAREHOUSE)

	def test_pages_follow_the_list_total(self, config: ConnectionConfig) -> None:
		def page(start: int, end: int, total: int) -> HttpResponse:
			items = "".join(
				f"<warehouse><warehouseid>W{n}</warehouseid></warehouse>" for n in range(start, end + 1)
			)
			return answer(
				"<response><control><status>success</status></control><operation><authentication><status>success"
				"</status></authentication><result><status>success</status><function>get_list</function>"
				f'<listtype start="{start}" end="{end}" total="{total}">warehouse</listtype><data>{items}</data>'
				"</result></operation></response>"
			)

		fake = scripted(page(0, 99, 150), page(100, 149, 150))
		source = ADAPTER.reader(config, fake, RecordingSleeper())
		first = source.page(Kind.WAREHOUSE, None, None, "")
		assert (len(first.nodes), first.end_cursor) == (100, "0|100")
		second = source.page(Kind.WAREHOUSE, first.end_cursor, None, "")
		assert intacct_request(fake.requests[1].body)["start"] == "100"
		assert not second.has_next_page
		with pytest.raises(SourceError, match="cursor"):
			source.page(Kind.WAREHOUSE, "9|0", None, "")
		with pytest.raises(SourceError, match="no page"):
			source.page(Kind.PAGE, None, None, "")

	def test_payments_read_both_ledgers(self, config: ConnectionConfig) -> None:
		payments = nodes(config, Kind.PAYMENT_ENTRY)
		assert sorted(payments) == ["APPayment/61", "ARPayment/51"]
		assert payments["ARPayment/51"].string("customer_currency") == "CAD"

	def test_the_field_map_picks_transactions_and_journals(self, config: ConnectionConfig) -> None:
		assert list(nodes(config, Kind.SALES_ORDER)) == ["SOTransaction/12"]
		assert list(nodes(config, Kind.DRAFT_ORDER)) == ["SOTransaction/11"]
		assert list(nodes(config, Kind.JOURNAL_ENTRY)) == ["GLTransaction/GJ:7"]
		custom = replace(
			config, field_map='{"sales_orders": ["Sales Order", "Sales Invoice"], "journals": ["GJ", "ARJ"]}'
		)
		assert sorted(nodes(custom, Kind.SALES_ORDER)) == ["SOTransaction/12", "SOTransaction/13"]
		assert len(nodes(custom, Kind.JOURNAL_ENTRY)) == 2

	def test_the_field_maps_shape(self) -> None:
		assert FieldMap.of("").lists["journals"] == ("GJ",)
		for bad in ("[", "[]", '{"journals": "GJ"}', '{"root_types": []}'):
			with pytest.raises(SourceError, match="Field Map"):
				FieldMap.of(bad)

	def test_designations_and_lookups(self, config: ConnectionConfig) -> None:
		titles = nodes(config, Kind.DESIGNATION)
		assert sorted(node.string("name") for node in titles.values()) == ["Partner", "Tax Analyst"]
		source, _ = reader(config)
		assert source.node(Kind.DESIGNATION, gid("Designation", derived_key("Partner"))) is not None
		found = source.node(Kind.CUSTOMER, gid("Customer", "C-NORTH"))
		assert found is not None and found.text("name") == "Northgate Dental"
		with pytest.raises(SourceError, match="Not a Sage Intacct customer"):
			source.node(Kind.CUSTOMER, gid("Vendor", "V-SOFT"))


class TestBooks:
	def test_accounts(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.ACCOUNT, "GLAccount/1000")["root_type"] == "Asset"
		assert fields(config, Kind.ACCOUNT, "GLAccount/2000")["root_type"] == "Liability"
		assert fields(config, Kind.ACCOUNT, "GLAccount/3000")["root_type"] == "Equity"
		assert fields(config, Kind.ACCOUNT, "GLAccount/4000")["account_type"] == "Income Account"
		assert fields(config, Kind.ACCOUNT, "GLAccount/6000", "v2")["disabled"] is True
		assert fields(config, Kind.ACCOUNT, "GLAccount/6100", "v2")["name"] == "Software Subscriptions"
		with pytest.raises(Unsupported, match="statistical"):
			mapped(
				config,
				Kind.ACCOUNT,
				changed(nodes(config, Kind.ACCOUNT)["GLAccount/1000"], accounttype="statistical"),
			)

	def test_departments_warehouses_and_items(self, config: ConnectionConfig) -> None:
		assert fields(config, Kind.COST_CENTER, "Department/ADV-TAX")["parent"] is None
		assert fields(config, Kind.DEPARTMENT, "Department/ADV-TAX")["parent"] == gid("Department", "ADV")
		assert fields(config, Kind.WAREHOUSE, "Warehouse/WH1")["address"]["city"] == "Toronto"
		guide = fields(config, Kind.PRODUCT, "Item/GUIDE")
		assert (guide["is_stock_item"], guide["barcode"], guide["price"]) == (True, "0627843009991", None)
		assert (
			mapped(
				config, Kind.PRODUCT, changed(nodes(config, Kind.PRODUCT)["Item/AUDIT"], status="inactive")
			)
			is None
		)


class TestParties:
	def test_customers_and_vendors(self, config: ConnectionConfig) -> None:
		north = fields(config, Kind.CUSTOMER, "Customer/C-NORTH")
		assert (north["email"], north["tax_id"], north["addresses"][0]["line1"]) == (
			"office@northgatedental.example",
			"445566778RT0001",
			"10 Front St",
		)
		assert north["contacts"] == [
			{
				"gid": gid("Contact", derived_key("Paul Okafor")),
				"name": "Paul Okafor",
				"email": None,
				"phone": None,
				"job_title": "Accounting",
			}
		]
		assert fields(config, Kind.CUSTOMER, "Customer/C-NORTH", "v2")["phone"] == "+1 613 555 0199"
		inactive = changed(nodes(config, Kind.SUPPLIER)["Vendor/V-SOFT"], status="inactive")
		assert fields_of(mapped(config, Kind.SUPPLIER, inactive))["disabled"] is True


class TestPeople:
	def test_employees_and_projects(self, config: ConnectionConfig) -> None:
		grace = fields(config, Kind.EMPLOYEE, "Employee/E-01")
		assert (grace["gender"], grace["date_of_birth"], grace["designation"]) == (
			"Female",
			"1979-04-02",
			gid("Designation", derived_key("Partner")),
		)
		omar = fields(config, Kind.EMPLOYEE, "Employee/E-02", "v2")
		assert (omar["status"], omar["relieving_date"], omar["reports_to"]) == (
			"Left",
			"2026-10-01",
			gid("Employee", "E-01"),
		)
		project = fields(config, Kind.PROJECT, "Project/P-100")
		assert (project["customer"], project["end"]) == (gid("Customer", "C-NORTH"), "2026-12-31")
		done = changed(nodes(config, Kind.PROJECT)["Project/P-100"], projectstatus="Completed")
		assert fields_of(mapped(config, Kind.PROJECT, done))["status"] == "Completed"


class TestDocuments:
	def test_quotes_orders_and_purchase_orders(self, config: ConnectionConfig) -> None:
		quote = fields(config, Kind.DRAFT_ORDER, "SOTransaction/11")
		assert (quote["number"], quote["total"]) == ("Q001", Decimal(4500))
		order = fields(config, Kind.SALES_ORDER, "SOTransaction/12")
		assert (order["total"], order["submitted"], order["reference"]) == (Decimal(4620), False, "PO-88")
		assert (
			fields(config, Kind.SALES_ORDER, "SOTransaction/12", "v2")["remarks"]
			== "Deliver guides to reception"
		)
		purchase = fields(config, Kind.PURCHASE_ORDER, "POTransaction/21")
		assert (purchase["total"], purchase["reference"]) == (Decimal(250), "LL-5501")

	def test_invoices_and_bills(self, config: ConnectionConfig) -> None:
		invoice = fields(config, Kind.SALES_INVOICE, "Invoice/31")
		assert (invoice["total"], invoice["lines"][0]["account"], invoice["lines"][0]["cost_center"]) == (
			Decimal(4620),
			gid("GLAccount", "4000"),
			gid("Department", "ADV"),
		)
		assert (
			mapped(config, Kind.SALES_INVOICE, nodes(config, Kind.SALES_INVOICE, "v2")["Invoice/32"]) is None
		)
		assert fields(config, Kind.PURCHASE_INVOICE, "Bill/42", "v2")["total"] == Decimal(90)
		credit = changed(
			nodes(config, Kind.SALES_INVOICE)["Invoice/32"],
			totalamount="-800",
			invoiceitems={"lineitem": {"amount": "-800", "glaccountno": "4000"}},
		)
		assert fields_of(mapped(config, Kind.SALES_INVOICE, credit))["is_return"] is True

	def test_a_document_in_another_currency(self, config: ConnectionConfig) -> None:
		node = nodes(config, Kind.SALES_INVOICE)["Invoice/32"]
		usd = changed(
			node,
			currency="USD",
			totalamount="750",
			trx_totalamount="600",
			invoiceitems={"lineitem": {"amount": "750", "trx_amount": "600", "glaccountno": "4000"}},
		)
		document = fields_of(mapped(config, Kind.SALES_INVOICE, usd))
		assert (document["currency"], document["total"], document["base_total"]) == (
			"USD",
			Decimal(600),
			Decimal(750),
		)
		inexact = changed(usd, totalamount="700")
		with pytest.raises(Unsupported, match="exchange rate"):
			mapped(config, Kind.SALES_INVOICE, inexact)


class TestPaymentsAndJournals:
	def test_payments(self, config: ConnectionConfig) -> None:
		received = fields(config, Kind.PAYMENT_ENTRY, "ARPayment/51")
		assert (
			received["payment_type"],
			received["currency"],
			received["account"],
			received["allocations"],
		) == (
			"Receive",
			"CAD",
			gid("GLAccount", "1000"),
			[{"kind": "sales_invoice", "document": gid("Invoice", "31"), "amount": Decimal(4620)}],
		)
		paid = fields(config, Kind.PAYMENT_ENTRY, "APPayment/61")
		assert (paid["payment_type"], paid["allocations"][0]["document"]) == ("Pay", gid("Bill", "41"))
		node = nodes(config, Kind.PAYMENT_ENTRY)["ARPayment/51"]
		with pytest.raises(Unsupported, match="no currency"):
			mapped(config, Kind.PAYMENT_ENTRY, changed(node, customer_currency=None))
		assert mapped(config, Kind.PAYMENT_ENTRY, changed(node, state="Reversed")) is None

	def test_journals(self, config: ConnectionConfig) -> None:
		journal = fields(config, Kind.JOURNAL_ENTRY, "GLTransaction/GJ:7")
		assert (journal["number"], journal["date"]) == ("GJ 7", "2026-09-30")
		assert [(line["debit"], line["credit"]) for line in journal["lines"]] == [
			(Decimal(2200), Decimal(0)),
			(Decimal(0), Decimal(2200)),
		]
		node = nodes(replace(config, field_map='{"journals": ["ARJ"]}'), Kind.JOURNAL_ENTRY)[
			"GLTransaction/ARJ:8"
		]
		assert fields_of(mapped(config, Kind.JOURNAL_ENTRY, node))["lines"][0]["party_type"] == "Customer"


class TestDtd:
	def test_content_models(self) -> None:
		dtd = Dtd(
			'<!ENTITY % NAME "(#PCDATA)"><!ELEMENT a (b, c?, d*)><!ELEMENT b %NAME;><!ELEMENT c EMPTY>'
			"<!ELEMENT d (#PCDATA | b)*><!ELEMENT e ANY><!-- <!ELEMENT z EMPTY> -->"
		)
		assert dtd.order("a") == ["b", "c", "d"]
		assert list(dtd.errors("<a><b>x</b><c/><d>t<b>y</b></d></a>")) == []
		errors = list(dtd.errors("<a><c>x</c><b><c/></b><z/></a>"))
		assert any("don't match" in e for e in errors)
		assert any("must be empty" in e for e in errors)
		assert any("only text" in e for e in errors)
		assert any("z is not declared" in e for e in errors)
		assert list(dtd.errors("<d><c/></d>")) == ["/d may not hold c"]
		assert list(dtd.errors("<a>text<b>x</b></a>")) == ["/a holds text, but only elements are declared"]
