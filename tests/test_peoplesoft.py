# SPDX-License-Identifier: AGPL-3.0-or-later
"""The PeopleSoft adapter over the recorded installation (tests/fixtures/peoplesoft, proven against the Query
Access Service's REST answer as PeopleBooks documents it): the field map and what it refuses, the query
request, paging by key, the change prompt, HTTP Basic, and every mapper through the map's roles."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from idempotent_importer.core.errors import CredentialsRejected, KindUnavailable, SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.peoplesoft.adapter import PeopleSoftAdapter, gateway_url
from idempotent_importer.sources.peoplesoft.fieldmap import field_map
from idempotent_importer.sources.peoplesoft.reader import PeopleSoftReader, key_text
from tests.support.peoplesoft import FIELD_MAP, FIELD_MAP_TEXT, PS_PASSWORD, PS_USER
from tests.support.transport import FakeTransport, RecordingSleeper, rest_source, scripted

ADAPTER = PeopleSoftAdapter()


def gid(type_: str, key: str) -> str:
	return f"gid://peoplesoft/peoplesoft-p/{type_}/{key}"


@pytest.fixture
def config() -> ConnectionConfig:
	return ConnectionConfig(
		source="peoplesoft",
		connection="peoplesoft-p",
		store_url="https://hcm.example.test:8443",
		api_user=PS_USER,
		api_secret=PS_PASSWORD,
		field_map=FIELD_MAP_TEXT,
		endpoint_override="http://peoplesoft-fake:8080",
	)


def reader(config: ConnectionConfig, state: str = "v1") -> tuple[PeopleSoftReader, FakeTransport]:
	fake = rest_source("peoplesoft", state)
	built = ADAPTER.reader(config, fake, RecordingSleeper())
	assert isinstance(built, PeopleSoftReader)
	return built, fake


def nodes(config: ConnectionConfig, kind: Kind, state: str = "v1") -> dict[str, Node]:
	source, _ = reader(config, state)
	found: dict[str, Node] = {}
	cursor: str | None = None
	while True:
		page = source.page(kind, cursor, None, "")
		found |= {node.id(): node for node in page.nodes}
		if not page.has_next_page:
			return found
		cursor = page.end_cursor


def desired(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> object:
	mapper = {m.kind: m for m in ADAPTER.mappers(config)}[kind]
	return mapper.map(nodes(config, kind, state)[key])


def values(config: ConnectionConfig, kind: Kind, key: str, state: str = "v1") -> Values:
	mapped = desired(config, kind, key, state)
	assert mapped is not None and hasattr(mapped, "values")
	result = mapped.values()
	assert isinstance(result, Values)
	return result


def with_map(config: ConnectionConfig, **changes: Any) -> ConnectionConfig:
	return replace(config, field_map=json.dumps(FIELD_MAP | changes))


def answer(rows: list[dict[str, Any]]) -> HttpResponse:
	numbered = [{"attr:rownumber": n, **row} for n, row in enumerate(rows, 1)]
	body = {"status": "success", "data": {"query": {"numrows": len(rows), "rows": numbered}}}
	return HttpResponse(200, json.dumps(body).encode())


class TestFieldMap:
	def test_the_map_names_the_node_and_each_kinds_query(self) -> None:
		parsed = field_map(FIELD_MAP_TEXT)
		assert (parsed.node, parsed.owner, parsed.max_rows) == ("PSFT_HR", "PUBLIC", 500)
		employee = parsed.queries(Kind.EMPLOYEE)[0]
		assert (employee.query, employee.key, employee.after_prompt) == (
			"ERP_EMPLOYEES",
			("A.EMPLID",),
			"AFTER_EMPLID",
		)
		assert [q.type for q in parsed.queries(Kind.PAYMENT_ENTRY)] == ["ApPayment", "ArPayment"]
		assert parsed.type_of(Kind.SALES_INVOICE) == "ArInvoice"
		assert field_map('{"node": "N"}').type_of(Kind.EMPLOYEE) == "Employee", "an unmapped kind's default"

	@pytest.mark.parametrize(
		("text", "message"),
		[
			("", "naming its Integration Broker node"),
			("{", "isn't JSON"),
			('{"kinds": {}}', "no Integration Broker"),
			('{"node": "N", "owner": "ANYONE"}', "PUBLIC or PRIVATE"),
			('{"node": "N", "max_rows": 0}', "positive whole number"),
			('{"node": "N", "kinds": {"lead": {}}}', "can't map lead"),
			('{"node": "N", "kinds": {"designation": {"key": "J"}}}', "names no query"),
			('{"node": "N", "kinds": {"designation": {"query": "Q", "key": "J"}}}', "to name name"),
			(
				'{"node": "N", "kinds": {"designation": {"query": "Q", "key": "J", "fields": {"name": "D", "pay": "P"}}}}',
				"no role pay",
			),
			(
				'{"node": "N", "kinds": {"designation": {"query": "Q", "fields": {"name": "D"}}}}',
				"key is a column",
			),
			(
				'{"node": "N", "kinds": {"designation": {"query": "Q", "key": ["A", "B"], "fields": {"name": "D"}, "after_prompt": "P"}}}',
				"one column",
			),
			(
				'{"node": "N", "kinds": {"designation": [{"query": "Q", "key": "J", "fields": {"name": "D"}}, {"query": "R", "key": "J", "fields": {"name": "D"}}]}}',
				"a type of their own",
			),
		],
	)
	def test_what_the_map_refuses(self, text: str, message: str) -> None:
		with pytest.raises(SourceError, match=message):
			field_map(text)

	def test_a_kind_the_map_doesnt_name_is_unavailable(self, config: ConnectionConfig) -> None:
		source, _ = reader(with_map(config, kinds={}))
		with pytest.raises(KindUnavailable, match="field map"):
			source.page(Kind.EMPLOYEE, None, None, "")


class TestReader:
	def test_a_query_runs_through_the_query_access_service(self, config: ConnectionConfig) -> None:
		source, fake = reader(config)
		source.page(Kind.DESIGNATION, None, None, "")
		request = fake.requests[0]
		parts = urlsplit(request.url)
		assert parts.path == (
			"/PSIGW/RESTListeningConnector/PSFT_HR/ExecuteQuery.v1/PUBLIC/ERP_JOBCODES/JSON/NONFILE"
		)
		assert parse_qs(parts.query) == {"isconnectedquery": ["N"], "maxrows": ["500"], "json_resp": ["true"]}
		assert request.headers["Authorization"].startswith("Basic ")

	def test_the_store_url_is_the_web_server(self, config: ConnectionConfig) -> None:
		assert gateway_url(replace(config, endpoint_override=None)) == "https://hcm.example.test:8443"
		with pytest.raises(SourceError, match="web server"):
			gateway_url(replace(config, store_url="hcm", endpoint_override=None))
		with pytest.raises(SourceError, match="user ID and password"):
			ADAPTER.reader(replace(config, api_secret=""), scripted(), RecordingSleeper())

	def test_prompts_page_by_key_and_narrow_by_change(self, config: ConnectionConfig) -> None:
		small = with_map(config, max_rows=3)
		rows = [{"A.EMPLID": key, "FIRST_NAME": "X", "EMPL_STATUS": "A"} for key in ("K1", "K2", "K3")]
		fake = scripted(answer(rows))
		source = ADAPTER.reader(small, fake, RecordingSleeper())
		page = source.page(Kind.EMPLOYEE, None, datetime(2026, 10, 1, 8, tzinfo=UTC), "")
		assert [node.string("source_id") for node in page.nodes] == ["K1", "K2"], "the last key may continue"
		assert page.has_next_page and page.end_cursor == "0:K2"
		sent = parse_qs(urlsplit(fake.requests[0].url).query, keep_blank_values=True)
		assert sent["prompt_uniquepromptname"] == ["CHANGED_SINCE", "AFTER_EMPLID"]
		assert sent["prompt_fieldvalue"] == ["2026-10-01", ""]
		source.page(Kind.EMPLOYEE, page.end_cursor, None, "")
		assert parse_qs(urlsplit(fake.requests[1].url).query)["prompt_fieldvalue"] == ["K2"]
		assert source.is_incremental(Kind.EMPLOYEE) and not source.is_incremental(Kind.DESIGNATION)

	def test_a_full_query_without_paging_is_refused(self, config: ConnectionConfig) -> None:
		small = with_map(config, max_rows=1)
		source = ADAPTER.reader(
			small, scripted(answer([{"JOBCODE": "J1", "DESCR": "A"}])), RecordingSleeper()
		)
		with pytest.raises(SourceError, match="after_prompt"):
			source.page(Kind.DESIGNATION, None, None, "")
		one_key = scripted(answer([{"A.EMPLID": "K1", "FIRST_NAME": "X", "EMPL_STATUS": "A"}]))
		with pytest.raises(SourceError, match="one key"):
			ADAPTER.reader(small, one_key, RecordingSleeper()).page(Kind.EMPLOYEE, None, None, "")

	def test_refusals(self, config: ConnectionConfig) -> None:
		failed = HttpResponse(200, b'{"status": "fail", "data": "Query not found."}')
		with pytest.raises(SourceError, match="Query not found"):
			ADAPTER.reader(config, scripted(failed), RecordingSleeper()).page(
				Kind.DESIGNATION, None, None, ""
			)
		with pytest.raises(CredentialsRejected):
			ADAPTER.reader(config, scripted(HttpResponse(401, b"")), RecordingSleeper()).page(
				Kind.DESIGNATION, None, None, ""
			)
		with pytest.raises(SourceError, match="HTTP 404"):
			ADAPTER.reader(config, scripted(HttpResponse(404, b"")), RecordingSleeper()).page(
				Kind.DESIGNATION, None, None, ""
			)
		keyless = scripted(answer([{"JOBCODE": None, "DESCR": "A"}]))
		with pytest.raises(SourceError, match="no JOBCODE"):
			ADAPTER.reader(config, keyless, RecordingSleeper()).page(Kind.DESIGNATION, None, None, "")
		source, _ = reader(config)
		with pytest.raises(SourceError, match="read whole"):
			source.node(Kind.EMPLOYEE, gid("Employee", "KU0001"))

	def test_documents_gather_their_rows_and_kinds_their_queries(self, config: ConnectionConfig) -> None:
		invoice = nodes(config, Kind.SALES_INVOICE)[gid("ArInvoice", "US001:INV0000101")]
		assert len(invoice.children("rows")) == 2
		assert set(nodes(config, Kind.PAYMENT_ENTRY, "v2")) == {
			gid("ApPayment", "RBC01:0000000451"),
			gid("ArPayment", "US001:DEP0001:1"),
		}
		assert key_text(["A/B", 1, None]) == "A%2FB:1:"

	def test_every_kind_has_a_mapper(self, config: ConnectionConfig) -> None:
		assert {mapper.kind for mapper in ADAPTER.mappers(config)} == set(ADAPTER.kinds())


class TestMappers:
	def test_departments_jobs_and_employees(self, config: ConnectionConfig) -> None:
		field = values(config, Kind.DEPARTMENT, gid("Department", "10100"))
		assert (field.string("name"), field.optional_string("parent")) == (
			"Field Services",
			gid("Department", "10000"),
		)
		assert values(config, Kind.DEPARTMENT, gid("Department", "10100"), "v2").all()["disabled"] is True
		assert values(config, Kind.DESIGNATION, gid("Job", "KC001")).string("name") == "Field Technician"
		avery = values(config, Kind.EMPLOYEE, gid("Employee", "KU0002"))
		assert (
			avery.string("gender"),
			avery.optional_string("date_of_joining"),
			avery.optional_string("department"),
			avery.optional_string("designation"),
			avery.optional_string("reports_to"),
			avery.string("status"),
		) == (
			"Male",
			"2021-09-13",
			gid("Department", "10100"),
			gid("Job", "KC001"),
			gid("Employee", "KU0001"),
			"Active",
		)
		left = values(config, Kind.EMPLOYEE, gid("Employee", "KU0002"), "v2")
		assert (left.string("status"), left.optional_string("relieving_date")) == ("Left", "2026-10-09")

	def test_parties_and_accounts(self, config: ConnectionConfig) -> None:
		vendor = values(config, Kind.SUPPLIER, gid("Supplier", "VEND01"), "v2")
		assert vendor.string("name") == "Northern Tools & Supply Inc"
		assert vendor.records("addresses")[0].string("country_code") == "CA", "the map's country codes"
		customer = values(config, Kind.CUSTOMER, gid("Customer", "CUST01"))
		assert (customer.string("type"), customer.optional_string("email")) == (
			"Company",
			"ap@harbour.example",
		)
		revenue = values(config, Kind.ACCOUNT, gid("Account", "400000"))
		assert (revenue.string("root_type"), revenue.optional_string("number")) == ("Income", "400000")

	def test_invoices_and_vouchers(self, config: ConnectionConfig) -> None:
		invoice = values(config, Kind.SALES_INVOICE, gid("ArInvoice", "US001:INV0000101"))
		assert [(line.decimal("qty"), line.decimal("rate")) for line in invoice.records("lines")] == [
			(Decimal(2), Decimal(150)),
			(Decimal(1), Decimal(300)),
		]
		assert invoice.records("lines")[0].optional_string("account") == gid("Account", "400000")
		assert (invoice.decimal("total"), invoice.flag("submitted")) == (Decimal(640), True)
		voucher = gid("Voucher", "US001:00000201")
		assert not values(config, Kind.PURCHASE_INVOICE, voucher).flag("submitted")
		posted = values(config, Kind.PURCHASE_INVOICE, voucher, "v2")
		assert (posted.flag("submitted"), posted.optional_string("reference")) == (True, "NT-7781")

	def test_payments_and_journals(self, config: ConnectionConfig) -> None:
		paid = values(config, Kind.PAYMENT_ENTRY, gid("ApPayment", "RBC01:0000000451"), "v2")
		assert (paid.string("payment_type"), paid.string("party_type"), paid.string("party")) == (
			"Pay",
			"Supplier",
			gid("Supplier", "VEND01"),
		)
		assert [a.all() for a in paid.records("allocations")] == [
			{"kind": "purchase_invoice", "document": gid("Voucher", "US001:00000201"), "amount": Decimal(450)}
		]
		received = values(config, Kind.PAYMENT_ENTRY, gid("ArPayment", "US001:DEP0001:1"))
		assert (received.string("payment_type"), received.optional_string("reference")) == (
			"Receive",
			"CHK-5521",
		)
		journal = values(config, Kind.JOURNAL_ENTRY, gid("Journal", "US001:JRN0000301:2026-09-30"))
		assert [(line.decimal("debit"), line.decimal("credit")) for line in journal.records("lines")] == [
			(Decimal(125), Decimal(0)),
			(Decimal(0), Decimal(125)),
		]
		assert journal.string("number") == "JRN0000301"

	def test_what_the_values_cant_make(self, config: ConnectionConfig) -> None:
		employees = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.EMPLOYEE]
		row: dict[str, JsonValue] = {"EMPLID": "K9", "FIRST_NAME": None, "EMPL_STATUS": "A"}
		with pytest.raises(Unsupported, match="first_name is empty"):
			employees.map(
				Node({"id": gid("Employee", "K9"), "source_id": "K9", "type": "Employee", "rows": [row]})
			)
		departments = {m.kind: m for m in ADAPTER.mappers(config)}[Kind.DEPARTMENT]
		odd: dict[str, JsonValue] = {"DEPTID": "1", "DESCR": "X", "EFF_STATUS": "Q"}
		with pytest.raises(SourceError, match="not Y or N"):
			departments.map(
				Node({"id": gid("Department", "1"), "source_id": "1", "type": "Department", "rows": [odd]})
			)
		with pytest.raises(SourceError, match="no designation query of type Position"):
			{m.kind: m for m in ADAPTER.mappers(config)}[Kind.DESIGNATION].map(
				Node({"id": "x", "source_id": "x", "type": "Position", "rows": [{}]})
			)
