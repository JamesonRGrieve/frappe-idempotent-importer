# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ecwid by Lightspeed, through its REST API v3: categories, products with their variations, prices, stock
and images, customers, discount coupons, wholesale price tiers, orders with their payments, refunds and shipping labels, product
reviews and Instant Site redirects. The operator creates a custom app with read scopes in the Ecwid admin and
enters the store's URL, its store ID (the account) and the app's secret access token."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.ecwid import mapping
from idempotent_importer.sources.ecwid.reader import (
	RATE_LIMIT,
	SOURCE_ID,
	EcwidAuthorizer,
	EcwidReader,
	api_base,
)


class EcwidAdapter:
	id = SOURCE_ID
	label = "Ecwid"
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return (
			Kind.COLLECTION,
			Kind.PRODUCT,
			Kind.CUSTOMER,
			Kind.DISCOUNT,
			Kind.ORDER,
			Kind.REVIEW,
			Kind.URL_REDIRECT,
		)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		client = RestClient(
			"Ecwid", api_base(config), EcwidAuthorizer(config.api_secret), transport, sleeper, RATE_LIMIT
		)
		return EcwidReader(config, client)

	def filter_hint(self) -> str | None:
		return None
