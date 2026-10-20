# SPDX-License-Identifier: AGPL-3.0-or-later
"""The adapter both Zoho products are: OAuth 2 through Zoho's accounts server in the organisation's data
centre (``sources.zoho.client``), the organisation an approval opens kept as the account, and the product's
modules read through its reader."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import SYNC_ORDER, Kind
from idempotent_importer.core.oauth import BearerAuthorizer, OAuthClient, TokenGrant
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.zoho import client
from idempotent_importer.sources.zoho.client import RATE_LIMIT, SCHEME, Module, Product, ZohoReader


class ZohoAdapter:
	built = True

	def __init__(
		self,
		id_: str,
		product: Product,
		modules: Mapping[Kind, tuple[Module, ...]],
		mappers: Callable[[ConnectionConfig], list[Mapper]],
		reader: Callable[[ConnectionConfig, RestClient], ZohoReader],
	) -> None:
		self.id = id_
		self.label = product.label
		self._product = product
		self._modules = modules
		self._mappers = mappers
		self._reader = reader

	def kinds(self) -> tuple[Kind, ...]:
		return tuple(kind for kind in SYNC_ORDER if kind in self._modules)

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return self._mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		authorizer = BearerAuthorizer(self.oauth(config, transport), SCHEME)
		rest = RestClient(self.label, client.api_url(config), authorizer, transport, sleeper, RATE_LIMIT)
		return self._reader(config, rest)

	def filter_hint(self) -> str | None:
		return None

	def oauth(self, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
		return client.oauth(self._product, config, transport)

	def connected(
		self, config: ConnectionConfig, transport: HttpTransport, grant: TokenGrant, params: Mapping[str, str]
	) -> dict[str, str]:
		return client.connected(self._product, config, transport, grant, params)
