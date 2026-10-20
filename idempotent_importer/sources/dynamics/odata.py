# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the three Dynamics 365 adapters (Business Central, Finance & Operations, Sales) share: an app
registered in Microsoft Entra ID that reads as itself through OAuth 2's client credentials grant
(``core.oauth.ClientCredentials``, against the tenant's v2.0 token endpoint, for the resource's
``.default`` scope), and OData v4 lists read page by page through each response's ``@odata.nextLink``.

Every one of them documents the same service protection limit: 6,000 requests per user (the app) in a
five-minute sliding window, answered past it with 429 and a Retry-After in seconds (Business Central:
learn.microsoft.com/dynamics365/business-central/dev-itpro/administration/operational-limits-online;
Dataverse: learn.microsoft.com/power-apps/developer/data-platform/api-limits; Finance & Operations:
learn.microsoft.com/dynamics365/fin-ops-core/dev-itpro/data-entities/service-protection-api-limits). The
clients pace under it and wait out what the server asks for."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import HttpTransport, RateLimit
from idempotent_importer.core.node import Node
from idempotent_importer.core.oauth import (
	BearerAuthorizer,
	ClientCredentials,
	ClientCredentialsProvider,
	OAuthApp,
)
from idempotent_importer.core.rest import QueryValue, RestClient, query_string
from idempotent_importer.core.source import ConnectionConfig

ENTRA = "https://login.microsoftonline.com"
GRANT_PATH = "/{tenant}/oauth2/v2.0/token"
DEFAULT_SCOPE = "/.default"
SERVICE_PROTECTION_REQUESTS = 6000
SERVICE_PROTECTION_WINDOW_SECONDS = 300
RATE_LIMIT = RateLimit(
	requests_per_second=RateLimit.per_period(SERVICE_PROTECTION_REQUESTS, SERVICE_PROTECTION_WINDOW_SECONDS)
)
NEXT_LINK = "@odata.nextLink"
VALUE = "value"
RECONNECT = (
	"check the app's client ID, client secret and its access in Microsoft Entra ID and the environment"
)


def entra_authorizer(
	label: str,
	tenant: str,
	resource: str,
	config: ConnectionConfig,
	transport: HttpTransport,
	headers: Mapping[str, str] | None = None,
) -> BearerAuthorizer:
	"""The app's bearer token for ``resource`` (its URL's origin), from the tenant's token endpoint (in
	tests, the fake store's)."""
	if not tenant:
		raise SourceError(f"{label} is not configured: the tenant is missing.")
	origin = (config.endpoint_override or ENTRA).rstrip("/")
	provider = ClientCredentialsProvider(
		label, origin + GRANT_PATH.format(tenant=tenant), (resource.rstrip("/") + DEFAULT_SCOPE,), RECONNECT
	)
	tokens = ClientCredentials(provider, OAuthApp(config.api_user.strip(), config.api_secret), transport)
	return BearerAuthorizer(tokens, headers=headers)


@dataclass(frozen=True)
class ODataPage:
	nodes: list[Node]
	next_path: str | None


class OData:
	"""One OData v4 service: a list's first page from its path and query, each later page from the
	previous page's ``@odata.nextLink`` (an absolute URL on the same service, kept as the cursor)."""

	def __init__(self, client: RestClient, base_url: str) -> None:
		self._client = client
		self._base = base_url.rstrip("/")
		self._base_path = urlsplit(self._base).path

	def first(self, path: str, query: Mapping[str, QueryValue]) -> str:
		"""The cursor of a list's first page."""
		return path + query_string(query)

	def page(self, cursor: str) -> ODataPage:
		result = self._client.get(cursor)
		if result is None:
			raise SourceError(f"{self._client.label} has no {cursor.split('?', 1)[0]}.")
		link = result.node.optional_string(NEXT_LINK)
		return ODataPage(result.node.children(VALUE), self.relative(link) if link else None)

	def all(self, path: str, query: Mapping[str, QueryValue]) -> list[Node]:
		nodes: list[Node] = []
		cursor: str | None = self.first(path, query)
		while cursor is not None:
			page = self.page(cursor)
			nodes += page.nodes
			cursor = page.next_path
		return nodes

	def relative(self, link: str) -> str:
		"""A next link as a path under the service (its host is the service's, whatever the link names)."""
		parts = urlsplit(link)
		if not parts.path.startswith(self._base_path):
			raise SourceError(f"{self._client.label} sent a next link outside its service: {parts.path}")
		return parts.path.removeprefix(self._base_path) + (f"?{parts.query}" if parts.query else "")


def quoted(value: str) -> str:
	"""A string literal in an OData filter (single quotes doubled)."""
	return "'" + value.replace("'", "''") + "'"
