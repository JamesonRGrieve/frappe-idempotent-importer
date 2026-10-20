# SPDX-License-Identifier: AGPL-3.0-or-later
"""Wix, through its REST APIs: the Stores catalog (V3 categories or V1 collections, products with variants,
prices, sales, stock and images), contacts, Stores coupons, eCommerce discount rules and orders with their payments, refunds and
fulfillments, the blog's categories and posts, product reviews and site redirects. The operator creates an
account API key with read permissions for those APIs and enters the site's URL, its site ID (the account)
and the key."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.wix import mapping
from idempotent_importer.sources.wix.reader import API_BASE, RATE_LIMIT, SOURCE_ID, WixAuthorizer, WixReader


class WixAdapter:
	id = SOURCE_ID
	label = "Wix"
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return (
			Kind.COLLECTION,
			Kind.PRODUCT,
			Kind.CUSTOMER,
			Kind.DISCOUNT,
			Kind.ORDER,
			Kind.BLOG,
			Kind.ARTICLE,
			Kind.REVIEW,
			Kind.URL_REDIRECT,
		)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		client = RestClient(
			"Wix",
			(config.endpoint_override or API_BASE).rstrip("/"),
			WixAuthorizer(config.api_secret, config.account.strip()),
			transport,
			sleeper,
			RATE_LIMIT,
		)
		return WixReader(config, client)

	def filter_hint(self) -> str | None:
		return None
