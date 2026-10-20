# SPDX-License-Identifier: AGPL-3.0-or-later
"""SAP Business One, through its Service Layer (OData v3 at ``/b1s/v1``): the chart of accounts, profit
centers, warehouses, item groups and items, business partners (customers, suppliers and leads) with their
addresses and contact persons, departments, positions and employees, projects, sales opportunities,
quotations, sales and purchase orders, A/R and A/P invoices and credit notes, incoming and outgoing
payments, and manual journal entries.

The operator enters the Service Layer's address (``https://<server>:50000``) as the Store URL, the company
database as the account, and a SAP Business One user's name and password; the user needs read access to
these objects. The Field Map names the root type of the chart's "other" drawers (see mapping)."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.sap_business_one import mapping
from idempotent_importer.sources.sap_business_one.reader import (
	KINDS,
	RATE_LIMIT,
	SOURCE_ID,
	ServiceLayerReader,
	service_url,
)
from idempotent_importer.sources.sap_business_one.session import ServiceLayerSession

LABEL = "SAP Business One"


class SapBusinessOneAdapter:
	id = SOURCE_ID
	label = LABEL
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return KINDS

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		base = service_url(config)
		session = ServiceLayerSession(
			base, config.account.strip(), config.api_user.strip(), config.api_secret, transport
		)
		return ServiceLayerReader(config, RestClient(LABEL, base, session, transport, sleeper, RATE_LIMIT))

	def filter_hint(self) -> str | None:
		return None
