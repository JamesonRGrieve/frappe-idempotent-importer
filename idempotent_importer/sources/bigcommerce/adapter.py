# SPDX-License-Identifier: AGPL-3.0-or-later
"""BigCommerce, through its REST Management API (v2 and v3): categories, products with their variants,
prices, sales, stock, images and brands, customer groups, customers, legacy coupons, promotions, customer
group discounts and bulk pricing, orders with their
payments, refunds and shipments, content pages, the blog and its posts, product reviews and storefront
redirects. The operator creates a store-level API account with read-only scopes and enters the store's
storefront URL, its store hash (the account) and the account's access token."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.bigcommerce import mapping
from idempotent_importer.sources.bigcommerce.reader import (
	RATE_LIMIT,
	SOURCE_ID,
	BigCommerceAuthorizer,
	BigCommerceReader,
)


class BigCommerceAdapter:
	id = SOURCE_ID
	label = "BigCommerce"
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
			Kind.BLOG,
			Kind.ARTICLE,
			Kind.REVIEW,
			Kind.URL_REDIRECT,
		)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		client = RestClient(
			"BigCommerce",
			BigCommerceReader.store_base(config),
			BigCommerceAuthorizer(config.api_secret),
			transport,
			sleeper,
			RATE_LIMIT,
		)
		return BigCommerceReader(config, client)

	def filter_hint(self) -> str | None:
		return None
