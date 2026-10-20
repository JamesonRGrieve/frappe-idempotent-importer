# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lightspeed eCom (C-Series, formerly SEOshop / WebshopApp), through its REST API: categories, products with
their variants, prices, stock, images and brand, customers, discount codes and rules, orders with their payments,
credit notes and shipments, text pages, blogs and their articles, reviews and redirects. The operator
creates an API key with read permissions in the eCom back office and enters the shop's URL, the cluster and
shop language as the account (``us1/en``), the key as the user and its secret as the secret. Lightspeed
Retail (R-Series) and X-Series are other products with other APIs."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import BasicAuthorizer, RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.lightspeed import mapping
from idempotent_importer.sources.lightspeed.reader import RATE_LIMIT, SOURCE_ID, LightspeedReader, api_base

MISSING_KEY = "Lightspeed eCom is not configured: enter the API key as the user and its secret."


class LightspeedAdapter:
	id = SOURCE_ID
	label = "Lightspeed eCom (C-Series)"
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
		authorizer = BasicAuthorizer(config.api_user, config.api_secret, MISSING_KEY)
		client = RestClient("Lightspeed eCom", api_base(config), authorizer, transport, sleeper, RATE_LIMIT)
		return LightspeedReader(config, client)

	def filter_hint(self) -> str | None:
		return None
