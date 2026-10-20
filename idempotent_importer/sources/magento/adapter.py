# SPDX-License-Identifier: AGPL-3.0-or-later
"""Magento 2.4 / Adobe Commerce, through one store view's REST API: categories, simple, virtual,
downloadable and configurable products with their prices, special prices, stock and images, customer groups,
customers, cart price rules (with coupons or without), tier prices, orders with their payments, credit memos, shipments and comments, and CMS pages.
The operator creates an integration with read access to those resources, allows integration tokens as
bearer tokens, and enters the store's URL, the store view code (the account; blank is "default") and the
integration's access token."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.magento import mapping
from idempotent_importer.sources.magento.reader import SOURCE_ID, MagentoAuthorizer, MagentoReader, api_base


class MagentoAdapter:
	id = SOURCE_ID
	label = "Magento / Adobe Commerce"
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return (
			Kind.COLLECTION,
			Kind.PRODUCT,
			Kind.CUSTOMER_GROUP,
			Kind.CUSTOMER,
			Kind.DISCOUNT,
			Kind.ORDER,
			Kind.PAGE,
		)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		client = RestClient(
			"Magento", api_base(config), MagentoAuthorizer(config.api_secret), transport, sleeper
		)
		return MagentoReader(config, client)

	def filter_hint(self) -> str | None:
		return None
