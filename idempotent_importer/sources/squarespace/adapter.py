# SPDX-License-Identifier: AGPL-3.0-or-later
"""Squarespace, through its Commerce and Profiles APIs: store pages, products with their variants, prices,
sales, stock and images, customer profiles, discounts (promo codes and automatic), and orders with their payments, refunds
and fulfillments. The operator creates an API key (Settings → Developer API keys) with read-only access to
Products, Inventory, Orders, Transactions and Profiles, and enters the site's URL and the key."""

from __future__ import annotations

from datetime import UTC, datetime

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.squarespace import mapping
from idempotent_importer.sources.squarespace.reader import (
	API_BASE,
	RATE_LIMIT,
	SOURCE_ID,
	SquarespaceAuthorizer,
	SquarespaceReader,
)


class SquarespaceAdapter:
	id = SOURCE_ID
	label = "Squarespace"
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return (Kind.COLLECTION, Kind.PRODUCT, Kind.CUSTOMER, Kind.DISCOUNT, Kind.ORDER)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		client = RestClient(
			"Squarespace",
			(config.endpoint_override or API_BASE).rstrip("/"),
			SquarespaceAuthorizer(config.api_secret),
			transport,
			sleeper,
			RATE_LIMIT,
		)
		return SquarespaceReader(config, client, datetime.now(UTC))

	def filter_hint(self) -> str | None:
		return None
