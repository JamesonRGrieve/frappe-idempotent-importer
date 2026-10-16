# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shopify, through the Admin GraphQL API (pinned to API_VERSION): collections, products with their
variants, images and stock, customers, basic code discounts, orders with their refunds and timeline
comments, pages, blogs, articles, comments and URL redirects. The operator creates a custom app with read
scopes (coverage/shopify.md) and enters the store's ``<name>.myshopify.com`` domain and its Admin API
access token."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.shopify import mapping
from idempotent_importer.sources.shopify.client import Connection, GraphQlClient
from idempotent_importer.sources.shopify.queries import QueryLibrary
from idempotent_importer.sources.shopify.reader import SOURCE_ID, ShopifyReader


class ShopifyAdapter:
	id = SOURCE_ID
	label = "Shopify"
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return (
			Kind.COLLECTION,
			Kind.PRODUCT,
			Kind.CUSTOMER,
			Kind.DISCOUNT,
			Kind.ORDER,
			Kind.PAGE,
			Kind.BLOG,
			Kind.ARTICLE,
			Kind.COMMENT,
			Kind.URL_REDIRECT,
		)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		connection = (
			Connection(config.endpoint_override, config.api_secret)
			if config.endpoint_override
			else Connection.for_store(config.store_url, config.api_secret)
		)
		return ShopifyReader(config, GraphQlClient(connection, QueryLibrary.bundled(), transport, sleeper))

	def filter_hint(self) -> str | None:
		return "Shopify search syntax, e.g. status:active"
