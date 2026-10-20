# SPDX-License-Identifier: AGPL-3.0-or-later
"""Dynamics 365 Finance & Operations (finance and operations apps), through one legal entity's OData data
entities: customers, released products and sales orders (Microsoft's entity reference documents their
fields), and vendors and purchase orders through the entities the connection's field map names (none is
documented). The operator registers an app in Microsoft Entra ID, adds it in the environment's "Microsoft
Entra applications" page mapped to a user with read access, and enters the environment's URL
(``https://<environment>.operations.dynamics.com``), ``<tenant>/<legal entity>`` as the account, the app's
client ID and secret, and the field map for what its environment names differently."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.dynamics.odata import RATE_LIMIT, OData, entra_authorizer
from idempotent_importer.sources.dynamics_finance_operations import mapping
from idempotent_importer.sources.dynamics_finance_operations.reader import (
	PAGE_SIZE,
	SOURCE_ID,
	FinanceOperationsReader,
	api_base,
	environment_of,
	tenant_and_legal_entity,
)

LABEL = "Dynamics 365 Finance & Operations"


class FinanceOperationsAdapter:
	id = SOURCE_ID
	label = LABEL
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return (Kind.PRODUCT, Kind.CUSTOMER, Kind.SUPPLIER, Kind.SALES_ORDER, Kind.PURCHASE_ORDER)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		tenant, _ = tenant_and_legal_entity(config)
		base = api_base(config)
		headers = {"Prefer": f"odata.maxpagesize={PAGE_SIZE}"}
		authorizer = entra_authorizer(LABEL, tenant, environment_of(config), config, transport, headers)
		client = RestClient(LABEL, base, authorizer, transport, sleeper, RATE_LIMIT)
		return FinanceOperationsReader(config, OData(client, base))

	def filter_hint(self) -> str | None:
		return None
