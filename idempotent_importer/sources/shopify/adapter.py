# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shopify, through the Admin GraphQL API (pinned to API_VERSION): collections, products with their
variants, images and stock, customers, basic code discounts, orders with their refunds and timeline
comments, pages, the shop's legal policies, blogs, articles, comments and URL redirects. The operator makes
an app in Shopify's Dev Dashboard with the read scopes in coverage/shopify.md and enters the store's
``<name>.myshopify.com`` domain and the app's client ID and secret (exchanged for a 24-hour token the
client renews itself); a legacy custom app can use its permanent Admin API access token instead."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import RequestAuthorizer
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.shopify import mapping
from idempotent_importer.sources.shopify.auth import ClientCredentialsAuthorizer, ShopifyTokenAuthorizer
from idempotent_importer.sources.shopify.client import Connection, GraphQlClient
from idempotent_importer.sources.shopify.queries import QueryLibrary
from idempotent_importer.sources.shopify.reader import SOURCE_ID, ShopifyReader

# The connection's credential choices (its ``auth_method``) for a Dev Dashboard app and a legacy custom app.
DEV_DASHBOARD_APP = "Client Credentials"
LEGACY_APP = "Access Token"


def authorizer(
	config: ConnectionConfig, connection: Connection, transport: HttpTransport
) -> RequestAuthorizer:
	"""A Dev Dashboard app's client credentials (the way to connect since 2026), or a legacy app's token."""
	if config.auth_method == DEV_DASHBOARD_APP:
		return ClientCredentialsAuthorizer(
			connection.token_url(), config.api_user, config.api_secret, transport
		)
	if config.auth_method == LEGACY_APP:
		return ShopifyTokenAuthorizer(config.api_secret)
	raise SourceError(
		f"Shopify is not configured: choose {DEV_DASHBOARD_APP} (a Dev Dashboard app) or "
		f"{LEGACY_APP} (a legacy custom app)."
	)


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
			Kind.POLICY,
			Kind.BLOG,
			Kind.ARTICLE,
			Kind.COMMENT,
			Kind.URL_REDIRECT,
		)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		connection = (
			Connection(config.endpoint_override)
			if config.endpoint_override
			else Connection.for_store(config.store_url)
		)
		client = GraphQlClient(
			connection, QueryLibrary.bundled(), authorizer(config, connection, transport), transport, sleeper
		)
		return ShopifyReader(config, client)

	def filter_hint(self) -> str | None:
		return "Shopify search syntax, e.g. status:active"
