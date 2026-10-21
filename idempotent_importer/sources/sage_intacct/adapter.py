# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sage Intacct, through its XML Web Services gateway (schema 2.1): GL accounts, departments, warehouses,
items, customers and vendors with their contact lists, employees and their titles, projects, order entry
quotes and sales orders, purchase orders, AR invoices, AP bills, AR and AP payments, and the general
journal's transactions.

The operator needs a Web Services sender (the sender ID and its password, from a Web Services developer
license, authorized in the company's Company → Security) and a company user with read access to these
objects (its Web Services permissions). On the connection: the company ID as the account, the user ID and
password as the user and secret, the sender ID as **Key or Certificate ID** and the sender password as
**Sender Password**. The Field Map names the company's own quote, order and purchase order transaction
definitions and journals, and any equity accounts (see mapping and reader)."""

from __future__ import annotations

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.source import ConnectionConfig, SourceReader
from idempotent_importer.sources.sage_intacct import mapping
from idempotent_importer.sources.sage_intacct.gateway import GATEWAY, Credentials, Gateway
from idempotent_importer.sources.sage_intacct.reader import KINDS, SOURCE_ID, IntacctReader

LABEL = "Sage Intacct"


class SageIntacctAdapter:
	id = SOURCE_ID
	label = LABEL
	built = True

	def kinds(self) -> tuple[Kind, ...]:
		return KINDS

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return mapping.mappers(config)

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		credentials = Credentials(
			sender_id=config.key_id.strip(),
			sender_password=config.sender_secret,
			company_id=config.account.strip(),
			user_id=config.api_user.strip(),
			user_password=config.api_secret,
		)
		return IntacctReader(
			config, Gateway(credentials, transport, sleeper, config.endpoint_override or GATEWAY)
		)

	def filter_hint(self) -> str | None:
		return None
