# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dynamics 365 Business Central (online), through API v2.0: the chart of accounts, dimensions and their
values, locations, item categories and items with their variants and inventory, customers, vendors,
employees, projects, contacts and opportunities, sales quotes and orders, purchase orders, and sales and
purchase invoices and credit memos. The operator registers an app in Microsoft Entra ID with Business
Central's API.ReadWrite.All application permission (Business Central's APIs need it to read), adds it in
Business Central's "Microsoft Entra Applications" page with read permission sets, and enters the
environment's API URL (``https://api.businesscentral.dynamics.com/v2.0/<tenant>/<environment>``), the
company's id as the account, and the app's client ID and secret."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.dynamics.odata import RATE_LIMIT, OData, entra_authorizer
from idempotent_importer.sources.dynamics_business_central import mapping
from idempotent_importer.sources.dynamics_business_central.reader import (
	API_HOST,
	PAGE_SIZE,
	SOURCE_ID,
	BusinessCentralReader,
	company_base,
	environment_of,
)

LABEL = "Dynamics 365 Business Central"


class BusinessCentralAdapter:
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
			Kind.EMPLOYEE,
			Kind.PROJECT,
			Kind.LEAD,
			Kind.OPPORTUNITY,
			Kind.DRAFT_ORDER,
			Kind.SALES_ORDER,
			Kind.PURCHASE_ORDER,
			Kind.SALES_INVOICE,
			Kind.PURCHASE_INVOICE,
		)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		tenant, _ = environment_of(config)
		base = company_base(config)
		authorizer = entra_authorizer(
			LABEL, tenant, API_HOST, config, transport, {"Prefer": f"odata.maxpagesize={PAGE_SIZE}"}
		)
		client = RestClient(LABEL, base, authorizer, transport, sleeper, RATE_LIMIT)
		return BusinessCentralReader(config, OData(client, base))

	def filter_hint(self) -> str | None:
		return None
