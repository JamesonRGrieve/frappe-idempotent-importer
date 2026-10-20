# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Odoo adapter over its fixtures (tests/fixtures/odoo, written by tools/odoo_fixtures.py from an Odoo 19
database model and proven against tests/schemas/odoo.json): the JSON-2 and JSON-RPC clients, the reader's
paging, filters and assembly, and every mapper."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import CredentialsRejected, KindUnavailable, SourceError, Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.odoo.adapter import JSONRPC, OdooAdapter, api_of
from idempotent_importer.sources.odoo.client import ApiKeyAuthorizer, JsonRpcApi, NoHeaders, OdooRestClient
from idempotent_importer.sources.odoo.mapping import Context, analytic_center, foreign, text, time_of
from idempotent_importer.sources.odoo.reader import PAGE_SIZE, OdooReader, company_of, m2o_id, m2o_name
from tests.support.odoo import ODOO_CONFIG
from tests.support.transport import FakeTransport, RecordingSleeper, rest_source, scripted

ADAPTER = OdooAdapter()
CONFIG = ODOO_CONFIG
GID = "gid://odoo/odoo-o"


def reader(state: str = "v1") -> tuple[OdooReader, FakeTransport]:
	transport = rest_source("odoo", state)
	built = ADAPTER.reader(CONFIG, transport, RecordingSleeper())
	assert isinstance(built, OdooReader)
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
	return {m.kind: m for m in ADAPTER.mappers(CONFIG)}[kind]


def mapped(kind: Kind, key: str, state: str = "v1") -> Desired | None:
	return mapper_of(kind).map(nodes(kind, state)[key])


def values(kind: Kind, key: str, state: str = "v1") -> Values:
	desired = mapped(kind, key, state)
	assert desired is not None
	return desired.values()


def ok(body: object) -> HttpResponse:
	return HttpResponse(200, json.dumps(body).encode())


def rpc(result: object) -> HttpResponse:
	return ok({"jsonrpc": "2.0", "id": 1, "result": result})


def rpc_error(name: str) -> HttpResponse:
	return ok(
		{
			"jsonrpc": "2.0",
			"id": 1,
			"error": {"code": 200, "message": "Odoo Server Error", "data": {"name": name}},
		}
	)


class TestClients:
	def test_json2_sends_named_parameters_with_the_key_and_database(self) -> None:
		source, transport = reader()
		source.page(Kind.ACCOUNT, None, None, "")
		request = transport.requests[0]
		assert request.url == "http://odoo-fake:8080/json/2/account.account/fields_get"
		assert (request.headers["Authorization"], request.headers["X-Odoo-Database"]) == (
			"bearer e2e-odoo-key",
			"harbour",
		)
		listing = json.loads(next(r for r in transport.requests if r.url.endswith("search_read")).body)
		assert (listing["offset"], listing["limit"], listing["order"], listing["context"]) == (
			0,
			PAGE_SIZE,
			"id asc",
			{"active_test": False},
		)
		assert "deprecated" not in listing["fields"], "only fields the database has are asked for"
		with pytest.raises(SourceError, match="API key"):
			ApiKeyAuthorizer("", "db")
		assert "e2e-odoo-key" not in repr(ApiKeyAuthorizer("e2e-odoo-key", ""))
		assert "X-Odoo-Database" not in ApiKeyAuthorizer("k", "").headers()

	def test_json2_errors(self) -> None:
		forbidden = HttpResponse(403, json.dumps({"name": "odoo.exceptions.AccessError"}).encode())
		api = api_of(CONFIG, scripted(forbidden), RecordingSleeper())
		with pytest.raises(KindUnavailable, match="may not read"):
			api.call("hr.employee", "fields_get", None, {})
		with pytest.raises(CredentialsRejected):
			api_of(CONFIG, scripted(HttpResponse(401, b"{}")), RecordingSleeper()).call(
				"res.partner", "read", [1], {}
			)
		with pytest.raises(CredentialsRejected):
			api_of(CONFIG, scripted(HttpResponse(403, b"not json")), RecordingSleeper()).call(
				"x", "read", [1], {}
			)
		with pytest.raises(KindUnavailable, match="isn't installed"):
			api_of(CONFIG, scripted(HttpResponse(404, b"{}")), RecordingSleeper()).call(
				"crm.lead", "fields_get", None, {}
			)

	def test_json_rpc_authenticates_once_and_calls_execute_kw(self) -> None:
		config = ConnectionConfig("odoo", "o", "https://odoo.example", "admin", "key-1", JSONRPC, "harbour")
		transport = scripted(rpc(2), rpc([{"id": 7, "name": "Deco Addict"}]), rpc([]))
		api = api_of(config, transport, RecordingSleeper())
		assert api.call("res.partner", "read", [7], {"fields": ["name"]}) == [
			{"id": 7, "name": "Deco Addict"}
		]
		assert api.call("res.partner", "search_read", None, {"domain": []}) == []
		login, first, second = (json.loads(request.body) for request in transport.requests)
		assert login["params"] == {
			"service": "common",
			"method": "authenticate",
			"args": ["harbour", "admin", "key-1", {}],
		}
		assert first["params"]["args"] == [
			"harbour",
			2,
			"key-1",
			"res.partner",
			"read",
			[[7]],
			{"fields": ["name"]},
		]
		assert second["params"]["args"][5] == [], "an @api.model method takes no ids"
		assert transport.requests[0].url == "https://odoo.example/jsonrpc"
		assert "Authorization" not in transport.requests[0].headers and NoHeaders().renew() is False

	@pytest.mark.parametrize(
		("answers", "error"),
		[
			((rpc(False),), CredentialsRejected),
			((rpc_error("odoo.exceptions.AccessDenied"),), CredentialsRejected),
			((rpc(2), rpc_error("odoo.exceptions.AccessError")), KindUnavailable),
			((rpc(2), rpc_error("builtins.KeyError")), KindUnavailable),
			((rpc(2), rpc_error("odoo.exceptions.UserError")), SourceError),
			((rpc(2), ok({"jsonrpc": "2.0", "id": 1})), SourceError),
		],
	)
	def test_json_rpc_errors(self, answers: tuple[HttpResponse, ...], error: type[Exception]) -> None:
		config = ConnectionConfig("odoo", "o", "https://odoo.example", "admin", "key-1", JSONRPC, "harbour")
		with pytest.raises(error):
			api_of(config, scripted(*answers), RecordingSleeper()).call("res.partner", "read", [1], {})

	def test_configuration_is_checked(self) -> None:
		with pytest.raises(SourceError, match="URL"):
			api_of(
				ConnectionConfig("odoo", "o", "odoo.example", api_secret="k"),
				scripted(ok([])),
				RecordingSleeper(),
			)
		with pytest.raises(SourceError, match="JSON-RPC needs"):
			api_of(
				ConnectionConfig("odoo", "o", "https://o.example", api_secret="k", auth_method=JSONRPC),
				scripted(ok([])),
				RecordingSleeper(),
			)
		with pytest.raises(SourceError, match="authentication"):
			api_of(
				ConnectionConfig("odoo", "o", "https://o.example", api_secret="k", auth_method="Password"),
				scripted(ok([])),
				RecordingSleeper(),
			)
		assert company_of(ConnectionConfig("odoo", "o", "x", account="harbour:3")) == ("harbour", 3)
		with pytest.raises(SourceError, match="company id"):
			company_of(ConnectionConfig("odoo", "o", "x", account="harbour:main"))

	def test_a_json_rpc_client_needs_its_credentials(self) -> None:
		client = OdooRestClient(
			"Odoo", "https://o.example", NoHeaders(), scripted(ok({})), RecordingSleeper()
		)
		assert isinstance(client, RestClient)
		with pytest.raises(SourceError):
			JsonRpcApi(client, "", "admin", "key")


class TestReader:
	def test_customers_and_suppliers(self) -> None:
		customers = nodes(Kind.CUSTOMER)
		assert sorted(customers) == ["Partner/16", "Partner/7"], "not the company's or its users' partners"
		assert sorted(nodes(Kind.SUPPLIER)) == ["Partner/14"]
		deco = customers["Partner/7"]
		assert [child.string("name") for child in deco.children("children")] == [
			"Douglas Fletcher",
			"Deco Addict, Warehouse",
		]
		assert deco.string("company_currency") == "CAD"

	def test_products_arrive_with_variants_and_their_attribute_values(self) -> None:
		desk = nodes(Kind.PRODUCT)["Product/11"]
		assert [variant.integer("id") for variant in desk.children("variants")] == [21, 22]
		assert desk.children("variants")[1].children("attribute_values")[0].string("name") == "Walnut"

	def test_moves_arrive_with_their_lines_and_what_they_settle(self) -> None:
		invoice = nodes(Kind.SALES_INVOICE)["Move/31"]
		receivable = next(line for line in invoice.children("lines") if line.integer("id") == 313)
		assert sorted(settled.integer("move") for settled in receivable.children("settles")) == [32, 51]
		assert receivable.string("account_type") == "asset_receivable"
		assert sorted(nodes(Kind.JOURNAL_ENTRY)) == ["Move/34", "Move/35"], (
			"a partner's payment isn't a journal"
		)
		payment = nodes(Kind.PAYMENT_ENTRY)["Payment/42"]
		assert [(s.integer("move"), s.string("move_type")) for s in payment.children("settles")] == [
			(33, "in_invoice")
		]

	def test_orders_and_leads_know_their_commercial_partner(self) -> None:
		order = nodes(Kind.SALES_ORDER)["SaleOrder/1"]
		assert (order.integer("party_id"), order.get("party_is_customer")) == (7, True)
		assert nodes(Kind.PURCHASE_ORDER)["PurchaseOrder/1"].get("party_is_supplier") is True
		assert nodes(Kind.LEAD)["CrmLead/3"].get("party_id") is None

	def test_every_kind_is_incremental_by_write_date(self) -> None:
		source, transport = reader()
		assert all(source.is_incremental(kind) for kind in ADAPTER.kinds())
		source.page(Kind.CUSTOMER, None, datetime(2026, 3, 1, 12, tzinfo=UTC), "")
		listing = json.loads(
			next(
				r
				for r in reversed(transport.requests)
				if "res.partner/search_read" in r.url and b"customer_rank" in r.body
			).body
		)
		assert ["write_date", ">=", "2026-03-01 12:00:00"] in listing["domain"]

	def test_pages_follow_by_offset(self) -> None:
		source, _ = reader()
		page = source.page(Kind.ACCOUNT, None, None, "")
		assert (page.has_next_page, page.end_cursor) == (False, None)
		answers: dict[str, object] = {
			"fields_get": {"name": {"type": "char"}, "write_date": {"type": "datetime"}},
			"search_read": [{"id": i, "name": f"C{i}"} for i in range(PAGE_SIZE)],
		}

		def route(_method: str, url: str, _body: bytes, _headers: dict[str, str]) -> HttpResponse:
			if "res.company" in url:
				return ok(
					[{"id": 1, "name": "C", "currency_id": [1, "CAD"], "partner_id": [1, "C"]}]
					if url.endswith("search_read")
					else {"name": {}, "currency_id": {}, "partner_id": {}}
				)
			return ok(answers[url.rsplit("/", 1)[-1]])

		full = OdooReader(CONFIG, api_of(CONFIG, FakeTransport(route), RecordingSleeper()))
		first = full.page(Kind.COLLECTION, None, None, "")
		assert (len(first.nodes), first.end_cursor) == (PAGE_SIZE, str(PAGE_SIZE))

	def test_single_records(self) -> None:
		source, _ = reader()
		with pytest.raises(SourceError, match="Not an Odoo identity"):
			source.node(Kind.CUSTOMER, f"{GID}/Partner/x")
		with pytest.raises(SourceError, match="has no"):
			source.page(Kind.REVIEW, None, None, "")

	def test_a_user_who_sees_several_companies_must_name_one(self) -> None:
		def route(_method: str, url: str, _body: bytes, _headers: dict[str, str]) -> HttpResponse:
			if url.endswith("fields_get"):
				return ok({"name": {}, "currency_id": {}, "partner_id": {}})
			return ok([{"id": 1, "name": "A"}, {"id": 2, "name": "B"}])

		several = OdooReader(CONFIG, api_of(CONFIG, FakeTransport(route), RecordingSleeper()))
		with pytest.raises(SourceError, match="several companies"):
			several.company_currency()

	def test_many2one_values(self) -> None:
		node = Node({"a": [3, "Three"], "b": False, "c": "x"})
		assert (m2o_id(node, "a"), m2o_name(node, "a"), m2o_id(node, "b"), m2o_name(node, "b")) == (
			3,
			"Three",
			None,
			None,
		)
		with pytest.raises(SourceError, match="many2one"):
			m2o_id(node, "c")


class TestMappers:
	def test_accounts(self) -> None:
		receivable = values(Kind.ACCOUNT, "Account/101")
		assert (
			receivable.string("number"),
			receivable.string("root_type"),
			receivable.string("account_type"),
		) == (
			"121000",
			"Asset",
			"Receivable",
		)
		assert values(Kind.ACCOUNT, "Account/108").string("account_type") == "Tax", "a tax's account"
		assert values(Kind.ACCOUNT, "Account/104").string("account_type") == "Bank", "outstanding receipts"
		assert values(Kind.ACCOUNT, "Account/105").optional_string("account_type") == "Bank"
		with pytest.raises(Unsupported, match="off-balance"):
			mapped(Kind.ACCOUNT, "Account/110")
		with pytest.raises(SourceError, match="unknown account type"):
			mapper_of(Kind.ACCOUNT).map(Node({"id": f"{GID}/Account/1", "account_type": "odd", "name": "x"}))

	def test_cost_centers_warehouses_and_categories(self) -> None:
		assert values(Kind.COST_CENTER, "AnalyticAccount/201").string("name") == "PRJ Office Design"
		warehouse = values(Kind.WAREHOUSE, "Warehouse/1")
		assert warehouse.required_record("address").string("city") == "Edmonton"
		assert values(Kind.COLLECTION, "Category/2").string("parent") == f"{GID}/Category/1"

	def test_products(self) -> None:
		chair = values(Kind.PRODUCT, "Product/10")
		assert (
			chair.string("type"),
			chair.string("sku"),
			chair.decimal("price"),
			chair.decimal("stock_qty"),
		) == (
			"simple",
			"CHAIR",
			Decimal("150.0"),
			Decimal("12.0"),
		)
		assert chair.flag("published") and chair.required_record("weight").all() == {
			"value": Decimal("9.5"),
			"unit": "kg",
		}
		desk = values(Kind.PRODUCT, "Product/11")
		oak, walnut = desk.records("variants")
		assert (oak.string("gid"), walnut.decimal("price"), walnut.required_record("attributes").all()) == (
			f"{GID}/Variant/21",
			Decimal("450.0"),
			{"Wood": "Walnut"},
		)
		assert desk.records("attributes")[0].all() == {"name": "Wood", "values": ["Oak", "Walnut"]}
		service = values(Kind.PRODUCT, "Product/12")
		assert (service.flag("is_stock_item"), service.optional_decimal("stock_qty")) == (False, None)
		assert values(Kind.PRODUCT, "Product/10", "v2").decimal("price") == Decimal("155.0")

	def test_partners(self) -> None:
		deco = values(Kind.CUSTOMER, "Partner/7")
		assert (
			deco.string("type"),
			deco.string("tax_id"),
			[a.string("address_type") for a in deco.records("addresses")],
		) == (
			"Company",
			"CA123456789",
			["Billing", "Shipping"],
		)
		assert [person.string("name") for person in deco.records("contacts")] == ["Douglas Fletcher"]
		lumber = values(Kind.CUSTOMER, "Partner/16")
		assert (lumber.string("type"), lumber.has("disabled")) == ("Individual", False)
		assert values(Kind.CUSTOMER, "Partner/16", "v2").flag("disabled"), "an archived partner is disabled"
		assert values(Kind.SUPPLIER, "Partner/14").string("name") == "Wood Corner"

	def test_people(self) -> None:
		assert values(Kind.DEPARTMENT, "Department/2").string("parent") == f"{GID}/Department/1"
		assert values(Kind.DESIGNATION, "Job/1").string("name") == "Sales Manager"
		admin = values(Kind.EMPLOYEE, "Employee/1")
		assert (admin.string("gender"), admin.string("date_of_birth"), admin.string("date_of_joining")) == (
			"Male",
			"1980-02-14",
			"2020-01-06",
		)
		marc = values(Kind.EMPLOYEE, "Employee/2")
		assert (marc.optional_string("gender"), marc.string("reports_to"), marc.string("designation")) == (
			None,
			f"{GID}/Employee/1",
			f"{GID}/Job/1",
		)
		gone = values(Kind.EMPLOYEE, "Employee/2", "v2")
		assert (gone.string("status"), gone.string("relieving_date")) == ("Left", "2026-03-31")

	def test_projects_leads_and_opportunities(self) -> None:
		project = values(Kind.PROJECT, "Project/1")
		assert (project.string("status"), project.string("customer"), project.string("cost_center")) == (
			"Open",
			f"{GID}/Partner/7",
			f"{GID}/AnalyticAccount/201",
		)
		assert values(Kind.LEAD, "CrmLead/1").string("status") == "Open"
		assert values(Kind.LEAD, "CrmLead/2").string("status") == "Opportunity"
		assert mapped(Kind.OPPORTUNITY, "CrmLead/1") is None, "a lead is no opportunity"
		with_customer = values(Kind.OPPORTUNITY, "CrmLead/2")
		assert (with_customer.string("party_kind"), with_customer.decimal("amount")) == (
			"customer",
			Decimal("4500.0"),
		)
		with_lead = values(Kind.OPPORTUNITY, "CrmLead/3")
		assert (with_lead.string("party_kind"), with_lead.string("party")) == ("lead", f"{GID}/CrmLead/3")
		assert values(Kind.OPPORTUNITY, "CrmLead/3", "v2").string("status") == "Lost"

	def test_quotations_and_orders(self) -> None:
		assert (
			mapped(Kind.DRAFT_ORDER, "SaleOrder/1") is None
			and mapped(Kind.SALES_ORDER, "SaleOrder/2") is None
		)
		order = values(Kind.SALES_ORDER, "SaleOrder/1")
		assert (
			order.string("party"),
			order.decimal("total"),
			order.string("reference"),
			order.flag("submitted"),
		) == (
			f"{GID}/Partner/7",
			Decimal("315.0"),
			"DA-PO-77",
			True,
		)
		assert len(order.records("lines")) == 1, "a note line lays the order out"
		quotation = values(Kind.DRAFT_ORDER, "SaleOrder/2")
		assert [line.optional_string("variant") for line in quotation.records("lines")] == [
			f"{GID}/Variant/21",
			f"{GID}/Variant/22",
		]
		assert mapped(Kind.DRAFT_ORDER, "SaleOrder/2", "v2") is None, "confirmed: the order's"
		assert values(Kind.SALES_ORDER, "SaleOrder/2", "v2").decimal("total") == Decimal("892.5")
		purchase = values(Kind.PURCHASE_ORDER, "PurchaseOrder/1")
		assert (purchase.string("party"), purchase.string("due_date")) == (f"{GID}/Partner/14", "2026-03-06")
		assert mapped(Kind.PURCHASE_ORDER, "PurchaseOrder/1", "v2") is None, "cancelled"

	def test_invoices_and_credit_notes(self) -> None:
		invoice = values(Kind.SALES_INVOICE, "Move/31")
		line = invoice.records("lines")[0]
		assert (
			line.decimal("qty"),
			line.decimal("rate"),
			line.string("account"),
			line.string("cost_center"),
		) == (
			Decimal("2.0"),
			Decimal("150.00"),
			f"{GID}/Account/106",
			f"{GID}/AnalyticAccount/201",
		)
		assert [(tax.string("description"), tax.decimal("amount")) for tax in invoice.records("taxes")] == [
			("GST 5%", Decimal("15.0"))
		]
		assert (invoice.string("party_account"), invoice.decimal("total")) == (
			f"{GID}/Account/101",
			Decimal("315.0"),
		)
		credit = values(Kind.SALES_INVOICE, "Move/32")
		assert (credit.flag("is_return"), credit.string("return_against"), credit.decimal("total")) == (
			True,
			f"{GID}/Move/31",
			Decimal("-157.5"),
		)
		assert credit.records("lines")[0].decimal("qty") == Decimal("-1.0")
		assert credit.flag("return_settles"), "reconciled with the invoice it reverses"
		bill = values(Kind.PURCHASE_INVOICE, "Move/33")
		assert (bill.decimal("total"), bill.string("reference"), bill.string("party_account")) == (
			Decimal("840.0"),
			"WC-INV-9001",
			f"{GID}/Account/102",
		)
		draft = values(Kind.SALES_INVOICE, "Move/36", "v2")
		assert (draft.flag("submitted"), draft.string("number")) == (False, "Draft 36")

	def test_payments_and_journals(self) -> None:
		received = values(Kind.PAYMENT_ENTRY, "Payment/41")
		assert (
			received.string("payment_type"),
			received.string("account"),
			received.string("party_account"),
		) == (
			"Receive",
			f"{GID}/Account/104",
			f"{GID}/Account/101",
		)
		assert [a.all() for a in received.records("allocations")] == [
			{"kind": "sales_invoice", "document": f"{GID}/Move/31", "amount": Decimal("157.5")}
		]
		paid = values(Kind.PAYMENT_ENTRY, "Payment/42")
		assert (paid.string("payment_type"), paid.string("party_type")) == ("Pay", "Supplier")
		rent = values(Kind.JOURNAL_ENTRY, "Move/34")
		assert [(line.decimal("debit"), line.decimal("credit")) for line in rent.records("lines")] == [
			(Decimal("1000.0"), Decimal(0)),
			(Decimal(0), Decimal("1000.0")),
		]
		assert values(Kind.JOURNAL_ENTRY, "Move/34", "v2").records("lines")[0].decimal("debit") == Decimal(
			"1100.0"
		)

	def test_a_journal_line_settling_invoices_is_split(self) -> None:
		node = Node(
			{
				"id": f"{GID}/Move/9",
				"source_id": "9",
				"state": "posted",
				"name": "BNK/9",
				"date": "2026-03-01",
				"ref": False,
				"narration": False,
				"lines": [
					{
						"id": 1,
						"account_id": [103, "Bank"],
						"debit": 100.0,
						"credit": 0.0,
						"partner_id": False,
						"account_type": "asset_cash",
						"settles": [],
					},
					{
						"id": 2,
						"account_id": [101, "Receivable"],
						"debit": 0.0,
						"credit": 100.0,
						"partner_id": [7, "Deco"],
						"account_type": "asset_receivable",
						"settles": [
							{"move": 31, "move_type": "out_invoice", "amount": 60.0},
							{"move": 77, "move_type": "entry", "amount": 10.0},
						],
					},
				],
			}
		)
		desired = mapper_of(Kind.JOURNAL_ENTRY).map(node)
		assert desired is not None
		receivable = [
			line for line in desired.values().records("lines") if line.string("account").endswith("/101")
		]
		settled = [
			(line.decimal("credit"), (line.record("against") or Values({})).all()) for line in receivable
		]
		assert settled == [
			(Decimal("60.0"), {"kind": "sales_invoice", "document": f"{GID}/Move/31"}),
			(Decimal("40.0"), {}),
		], "the reconciled share settles its invoice; the rest (and a journal's share) stays on the account"
		without_partner = node.raw()
		lines = without_partner["lines"]
		assert isinstance(lines, list) and isinstance(lines[1], dict)
		lines[1]["partner_id"] = False
		with pytest.raises(Unsupported, match="names no partner"):
			mapper_of(Kind.JOURNAL_ENTRY).map(Node(without_partner))

	def test_cancelled_documents_have_no_copy(self) -> None:
		for kind in (Kind.SALES_INVOICE, Kind.JOURNAL_ENTRY):
			assert mapper_of(kind).map(Node({"id": f"{GID}/Move/1", "state": "cancel"})) is None
		assert (
			mapper_of(Kind.PAYMENT_ENTRY).map(Node({"id": f"{GID}/Payment/1", "state": "canceled"})) is None
		)

	def test_helpers(self) -> None:
		context = Context(CONFIG)
		assert (
			analytic_center(context, Node({"analytic_distribution": {"201": 100.0}}))
			== f"{GID}/AnalyticAccount/201"
		)
		assert analytic_center(context, Node({"analytic_distribution": '{"201": 50, "202": 50}'})) is None
		assert analytic_center(context, Node({"analytic_distribution": {"201,5": 100}})) is None
		assert analytic_center(context, Node({"analytic_distribution": False})) is None
		assert time_of(Node({"t": "2026-03-01 10:00:00"}), "t") == "2026-03-01T10:00:00Z"
		with pytest.raises(SourceError):
			time_of(Node({"t": False}), "t")
		with pytest.raises(SourceError, match="not text"):
			text(Node({"t": 3}), "t")
		usd = Node({"company_currency": "CAD", "amount_total": 100.0, "amount_total_signed": 137.5})
		assert foreign("USD", usd) == (Decimal("1.375000000"), Decimal("137.5"))
		assert foreign("CAD", usd) == (None, None)
		assert foreign(
			"USD", Node({"company_currency": "CAD", "amount_total": 0, "amount_total_signed": 0})
		) == (None, None)

	def test_a_document_to_an_unmirrored_partner_is_unsupported(self) -> None:
		order = nodes(Kind.SALES_ORDER)["SaleOrder/1"].with_fields(party_is_customer=False)
		with pytest.raises(Unsupported, match="isn't mirrored as a customer"):
			mapper_of(Kind.SALES_ORDER).map(order)
