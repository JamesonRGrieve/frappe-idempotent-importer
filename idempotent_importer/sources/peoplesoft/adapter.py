# SPDX-License-Identifier: AGPL-3.0-or-later
"""PeopleSoft (HCM and Financials), through the Integration Broker's delivered REST Query Access Service:
departments, jobs and employees into HRMS; customers, suppliers, the chart of accounts, AR invoices, AP
vouchers, payments and journals into the books, each through a PeopleSoft Query the connection's field map
names (every installation's records are its own). The operator gives a PeopleSoft user the QAS REST
service operations and query access, builds or chooses a query per kind, and enters the web server's
address as the Store URL, the user ID and password, and the field map (``fieldmap``)."""

from __future__ import annotations

from urllib.parse import urlsplit

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import SYNC_ORDER, Kind
from idempotent_importer.core.rest import BasicAuthorizer, RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.peoplesoft import mapping
from idempotent_importer.sources.peoplesoft.fieldmap import KINDS
from idempotent_importer.sources.peoplesoft.reader import RATE_LIMIT, SOURCE_ID, PeopleSoftReader

LABEL = "PeopleSoft"


def gateway_url(config: ConnectionConfig) -> str:
	"""The web server the Integration Broker gateway answers on (the connection's Store URL)."""
	if config.endpoint_override:
		return config.endpoint_override
	parts = urlsplit(config.store_url)
	if parts.scheme not in ("https", "http") or not parts.netloc:
		raise SourceError(
			"PeopleSoft's Store URL is its web server's address, e.g. https://hcm.example.com:8443."
		)
	return f"{parts.scheme}://{parts.netloc}"


class PeopleSoftAdapter:
	id = SOURCE_ID
	label = LABEL
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return tuple(kind for kind in SYNC_ORDER if kind in KINDS)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		authorizer = BasicAuthorizer(
			config.api_user,
			config.api_secret,
			"PeopleSoft is not configured: enter the PeopleSoft user ID and password.",
		)
		client = RestClient(LABEL, gateway_url(config), authorizer, transport, sleeper, RATE_LIMIT)
		return PeopleSoftReader(config, client)

	def filter_hint(self) -> str | None:
		return None
