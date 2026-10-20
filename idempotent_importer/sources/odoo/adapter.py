# SPDX-License-Identifier: AGPL-3.0-or-later
"""Odoo 17, 18 and 19 (Community or Enterprise, Odoo Online, Odoo.sh or self-hosted), through its external
API: the chart of accounts, analytic accounts, warehouses, product categories and products with variants
and stock, customers and suppliers with their contacts and addresses, departments, job positions and
employees, projects, CRM leads and opportunities, quotations, sale and purchase orders, customer invoices
and vendor bills with their credit notes, payments and journal entries. The operator creates an API key for
a user who can read those apps (Preferences → Account Security → New API Key) and enters the database's URL,
the database name (the account; ``<database>:<company id>`` for one company of several), and the key as the
secret; for the JSON-RPC API (Odoo 17 and 18) also the user's login. Odoo 19 and later use the JSON-2 API."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.odoo import mapping
from idempotent_importer.sources.odoo.client import (
	ApiKeyAuthorizer,
	Json2Api,
	JsonRpcApi,
	NoHeaders,
	OdooApi,
	OdooRestClient,
)
from idempotent_importer.sources.odoo.reader import SOURCE_ID, OdooReader, company_of

JSON2, JSONRPC = "JSON-2 API Key", "JSON-RPC API Key"
LABEL = "Odoo"


def api_of(config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> OdooApi:
	base = (config.endpoint_override or config.store_url).strip().rstrip("/")
	if not base.startswith(("https://", "http://")):
		raise SourceError("Odoo is not configured: enter the database's URL (https://mycompany.odoo.com).")
	database, _ = company_of(config)
	if config.auth_method == JSONRPC:
		client = OdooRestClient(LABEL, base, NoHeaders(), transport, sleeper)
		return JsonRpcApi(client, database, config.api_user.strip(), config.api_secret)
	if config.auth_method not in ("", JSON2):
		raise SourceError(f"Odoo has no {config.auth_method} authentication.")
	return Json2Api(
		OdooRestClient(LABEL, base, ApiKeyAuthorizer(config.api_secret, database), transport, sleeper)
	)


class OdooAdapter:
	id = SOURCE_ID
	label = LABEL
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return (
			Kind.ACCOUNT,
			Kind.COST_CENTER,
			Kind.WAREHOUSE,
			Kind.COLLECTION,
			Kind.PRODUCT,
			Kind.CUSTOMER,
			Kind.SUPPLIER,
			Kind.DEPARTMENT,
			Kind.DESIGNATION,
			Kind.EMPLOYEE,
			Kind.PROJECT,
			Kind.LEAD,
			Kind.OPPORTUNITY,
			Kind.DRAFT_ORDER,
			Kind.SALES_ORDER,
			Kind.PURCHASE_ORDER,
			Kind.SALES_INVOICE,
			Kind.PURCHASE_INVOICE,
			Kind.PAYMENT_ENTRY,
			Kind.JOURNAL_ENTRY,
		)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		return OdooReader(config, api_of(config, transport, sleeper))

	def filter_hint(self) -> str | None:
		return None
