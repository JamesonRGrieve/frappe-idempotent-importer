# SPDX-License-Identifier: AGPL-3.0-or-later
"""Square Online, through the Square API (pinned to API_VERSION): the main location's return policy, catalog
categories, items with their variations, options, images and stock at the main location, customers, and
orders at every location with their refunds and fulfillments. Square Online's site pages and blog have no
public API. The operator enters a personal access token (or an OAuth access token) with read scopes."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.square_online import mapping
from idempotent_importer.sources.square_online.reader import (
	API_BASE,
	SOURCE_ID,
	SquareAuthorizer,
	SquareOnlineReader,
)


class SquareOnlineAdapter:
	id = SOURCE_ID
	label = "Square Online"
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return (Kind.COLLECTION, Kind.PRODUCT, Kind.CUSTOMER, Kind.ORDER, Kind.POLICY)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		client = RestClient(
			"Square",
			config.endpoint_override or API_BASE,
			SquareAuthorizer(config.api_secret),
			transport,
			sleeper,
		)
		return SquareOnlineReader(config, client)

	def filter_hint(self) -> str | None:
		return None
