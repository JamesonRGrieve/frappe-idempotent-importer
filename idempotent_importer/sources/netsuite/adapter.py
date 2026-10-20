# SPDX-License-Identifier: AGPL-3.0-or-later
"""NetSuite, through SuiteTalk REST web services (SuiteQL and the REST Record API): the chart of accounts,
departments, locations, items, customers, leads and prospects, vendors, employees and their job titles,
projects, opportunities, estimates, sales and purchase orders, invoices, cash sales, credit memos, vendor
bills and credits, customer and vendor payments, deposits and refunds, journal entries and checks.

The operator sets up OAuth 2.0's client credentials (machine-to-machine) flow in the account
(https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162686838198.html): an integration
record with the REST web services scope, and a certificate mapped to an entity and a role that can read
these records; then enters the account ID as the account, the integration's client ID, the certificate
ID, and the certificate's private key as the secret. Access tokens last 60 minutes and are fetched again
with a new signed assertion (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162755359851.html)."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.oauth import (
	BearerAuthorizer,
	ClientAuth,
	ClientCredentials,
	ClientCredentialsProvider,
	OAuthApp,
	SigningKey,
)
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.netsuite import mapping
from idempotent_importer.sources.netsuite.reader import (
	KINDS,
	RATE_LIMIT,
	SOURCE_ID,
	NetSuiteReader,
	account_host,
)

GRANT_PATH = "/services/rest/auth/oauth2/v1/token"
# The scope claim of the REST web services
# (https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162790605110.html).
SCOPE = "rest_webservices"
RECONNECT = "check the client ID, the certificate ID and its mapping, and the private key"


def grant(config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> ClientCredentials:
	"""The machine-to-machine grant. The assertion's audience is always the account's own token endpoint,
	also when the API is reached elsewhere (a test endpoint)."""
	provider = ClientCredentialsProvider(
		"NetSuite",
		NetSuiteReader.base_url(config) + GRANT_PATH,
		(SCOPE,),
		RECONNECT,
		ClientAuth.ASSERTION,
		audience=account_host(config) + GRANT_PATH,
	)
	key = SigningKey(config.key_id.strip(), config.api_secret)
	return ClientCredentials(provider, OAuthApp(config.api_user.strip(), ""), transport, key, sleeper.now)


class NetSuiteAdapter:
	id = SOURCE_ID
	label = "NetSuite"
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return KINDS

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		client = RestClient(
			"NetSuite",
			NetSuiteReader.base_url(config),
			BearerAuthorizer(grant(config, transport, sleeper)),
			transport,
			sleeper,
			RATE_LIMIT,
		)
		return NetSuiteReader(config, client)

	def filter_hint(self) -> str | None:
		return None
