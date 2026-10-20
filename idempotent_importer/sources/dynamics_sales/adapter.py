# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dynamics 365 Sales, through the Dataverse Web API (v9.2): products, accounts with their contacts and
contacts, leads, opportunities, quotes, orders and invoices. The operator registers an app in Microsoft
Entra ID, adds it to the environment as an application user with a read-only security role over those
tables, and enters the environment's URL (``https://<org>.crm.dynamics.com``), the tenant (its id or domain)
as the account, and the app's client ID and secret."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.dynamics.odata import RATE_LIMIT, OData, entra_authorizer
from idempotent_importer.sources.dynamics_sales import mapping
from idempotent_importer.sources.dynamics_sales.reader import (
	ANNOTATIONS,
	PAGE_SIZE,
	SOURCE_ID,
	DataverseReader,
	api_base,
	organisation_of,
)

LABEL = "Dynamics 365 Sales"


class DynamicsSalesAdapter:
	id = SOURCE_ID
	label = LABEL
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return (
			Kind.PRODUCT,
			Kind.CUSTOMER,
			Kind.LEAD,
			Kind.OPPORTUNITY,
			Kind.DRAFT_ORDER,
			Kind.SALES_ORDER,
			Kind.SALES_INVOICE,
		)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		base = api_base(config)
		headers = {
			"Prefer": f"odata.maxpagesize={PAGE_SIZE},{ANNOTATIONS}",
			"OData-MaxVersion": "4.0",
			"OData-Version": "4.0",
		}
		authorizer = entra_authorizer(
			LABEL, config.account.strip(), organisation_of(config), config, transport, headers
		)
		client = RestClient(LABEL, base, authorizer, transport, sleeper, RATE_LIMIT)
		return DataverseReader(config, OData(client, base))

	def filter_hint(self) -> str | None:
		return None
