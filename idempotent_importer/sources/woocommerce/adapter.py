# SPDX-License-Identifier: AGPL-3.0-or-later
"""WooCommerce, through its REST API (``wc/v3``) and its WordPress site's REST API: product categories,
simple and variable products with their variations, images and stock, customers, coupons, orders with
their refunds and notes, pages, post categories and posts, product reviews, and the Redirection plugin's
redirects. The operator enters the store URL and either a WordPress Application Password (full access,
including drafts and redirects) or a WooCommerce consumer key and secret (read-only key)."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.woocommerce import mapping
from idempotent_importer.sources.woocommerce.reader import SOURCE_ID, WooCommerceReader


class WooCommerceAdapter:
	id = SOURCE_ID
	label = "WooCommerce"
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
			Kind.REVIEW,
			Kind.URL_REDIRECT,
		)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		return WooCommerceReader.connect(config, transport, sleeper)

	def filter_hint(self) -> str | None:
		return None
