# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zoho CRM, through the CRM API v8: one organisation per connection. The operator registers a server-based
client in Zoho's API console (api-console.zoho.com, Multi-DC enabled) with this site's
``/api/method/idempotent_importer.api.oauth_callback`` as its redirect URI, enters the organisation's CRM
address in its data centre (``https://crm.zoho.com``, ``https://crm.zoho.eu``, …) as the Store URL and the
client's ID and secret, and connects with Connect to Zoho CRM. OAuth 2 as for Zoho's other products
(``sources.zoho.client``): the accounts server of the data centre, the secret in the form, a refresh token that
never changes; the CRM's read scopes (www.zoho.com/crm/developer/docs/api/v8/scopes.html)."""

from __future__ import annotations

from collections.abc import Mapping

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import SYNC_ORDER, Kind
from idempotent_importer.core.oauth import BearerAuthorizer, OAuthClient, TokenGrant
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.zoho import client
from idempotent_importer.sources.zoho_crm import mapping
from idempotent_importer.sources.zoho_crm.reader import KINDS, PRODUCT, RATE_LIMIT, SOURCE_ID, ZohoCrmReader


class ZohoCrmAdapter:
	id = SOURCE_ID
	label = PRODUCT.label
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return tuple(kind for kind in SYNC_ORDER if kind in KINDS)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		authorizer = BearerAuthorizer(self.oauth(config, transport), client.SCHEME)
		rest = RestClient(self.label, client.api_url(config), authorizer, transport, sleeper, RATE_LIMIT)
		return ZohoCrmReader(config, rest)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return client.oauth(PRODUCT, config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		"""A CRM token opens its one organisation; it must be in the Store URL's data centre."""
		client.same_data_centre(config, params)
		return {}
