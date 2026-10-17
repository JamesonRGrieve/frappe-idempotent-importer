# SPDX-License-Identifier: AGPL-3.0-or-later
"""Etsy, through Open API v3: the shop's policies, sections, listings in every state with their variants,
stock and images, buyers, receipts with their refunds and shipments, and reviews. Etsy has no webhooks, so
it is kept current by scheduled runs. The operator registers an Etsy app (keystring and shared secret),
saves them on the connection, and connects the shop with Connect to Etsy (OAuth with PKCE); the refresh
token Etsy replaces on every refresh is kept on the connection."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.etsy import mapping
from idempotent_importer.sources.etsy.auth import API_BASE, EtsyApp, EtsyAuthorizer, EtsyOAuth
from idempotent_importer.sources.etsy.reader import RATE_LIMIT, SOURCE_ID, EtsyReader


def app_of(config: ConnectionConfig) -> EtsyApp:
	return EtsyApp(config.api_user, config.api_secret, config.endpoint_override or API_BASE)


def oauth(config: ConnectionConfig, transport: HttpTransport) -> EtsyOAuth:
	"""The connection's OAuth client: the connect flow, and every token refresh."""
	if config.save_refresh_token is None:
		raise SourceError("Etsy needs somewhere to keep the refresh token it replaces on every refresh.")
	return EtsyOAuth(app_of(config), transport, config.refresh_token, config.save_refresh_token)


class EtsyAdapter:
	id = SOURCE_ID
	label = "Etsy"
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return (Kind.COLLECTION, Kind.PRODUCT, Kind.CUSTOMER, Kind.ORDER, Kind.POLICY, Kind.REVIEW)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		app = app_of(config)
		owner = EtsyAuthorizer(app, oauth(config, transport))
		client = RestClient("Etsy", app.api_base, owner, transport, sleeper, RATE_LIMIT)
		return EtsyReader(config, client, owner)

	def filter_hint(self) -> str | None:
		return None
