# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every source the importer knows, built or listed for a later phase. The single list of adapters: the
Importer Connection's source options and coverage/<source>.md follow it."""

from __future__ import annotations

from dataclasses import dataclass

from idempotent_importer.core.desired import Mapper
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.http import HttpTransport, Sleeper
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.source import Adapter, ConnectionConfig, SourceReader
from idempotent_importer.sources.bigcommerce.adapter import BigCommerceAdapter
from idempotent_importer.sources.ecwid.adapter import EcwidAdapter
from idempotent_importer.sources.etsy.adapter import EtsyAdapter
from idempotent_importer.sources.lightspeed.adapter import LightspeedAdapter
from idempotent_importer.sources.magento.adapter import MagentoAdapter
from idempotent_importer.sources.odoo.adapter import OdooAdapter
from idempotent_importer.sources.shopify.adapter import ShopifyAdapter
from idempotent_importer.sources.square_online.adapter import SquareOnlineAdapter
from idempotent_importer.sources.squarespace.adapter import SquarespaceAdapter
from idempotent_importer.sources.wix.adapter import WixAdapter
from idempotent_importer.sources.woocommerce.adapter import WooCommerceAdapter


@dataclass(frozen=True)
class ListedAdapter:
	"""A source listed for a later phase: it has a coverage stub and refuses to run."""

	id: str
	label: str
	built: bool = False

	def kinds(self) -> tuple[Kind, ...]:
		return ()

	def mappers(self, config: ConnectionConfig) -> list[Mapper]:
		return []

	def reader(self, config: ConnectionConfig, transport: HttpTransport, sleeper: Sleeper) -> SourceReader:
		raise SourceError(
			f"The {self.label} adapter is not built yet (see coverage/{self.id}.md); this connection can't run."
		)

	def filter_hint(self) -> str | None:
		return None


LISTED: tuple[ListedAdapter, ...] = (
	ListedAdapter("dynamics_business_central", "Dynamics 365 Business Central"),
	ListedAdapter("dynamics_finance_operations", "Dynamics 365 Finance & Operations"),
	ListedAdapter("dynamics_sales", "Dynamics 365 Sales"),
	ListedAdapter("peoplesoft", "PeopleSoft"),
	ListedAdapter("agendrix", "Agendrix"),
	ListedAdapter("netsuite", "NetSuite"),
	ListedAdapter("sap_business_one", "SAP Business One"),
	ListedAdapter("quickbooks_online", "QuickBooks Online"),
	ListedAdapter("xero", "Xero"),
	ListedAdapter("sage", "Sage"),
	ListedAdapter("zoho_books", "Zoho Books"),
	ListedAdapter("zoho_inventory", "Zoho Inventory"),
	ListedAdapter("acumatica", "Acumatica"),
)


def all_adapters() -> dict[str, Adapter]:
	"""Keyed by adapter id, built ones first, in display order."""
	adapters: list[Adapter] = [
		WooCommerceAdapter(),
		ShopifyAdapter(),
		EtsyAdapter(),
		SquareOnlineAdapter(),
		BigCommerceAdapter(),
		SquarespaceAdapter(),
		WixAdapter(),
		MagentoAdapter(),
		LightspeedAdapter(),
		EcwidAdapter(),
		OdooAdapter(),
		*LISTED,
	]
	return {adapter.id: adapter for adapter in adapters}


def adapter_for(source_id: str) -> Adapter:
	adapter = all_adapters().get(source_id)
	if adapter is None:
		raise SourceError(f"Unknown source platform: {source_id}")
	return adapter


def by_label(label: str) -> Adapter:
	"""The adapter an Importer Connection's ``source`` (its label) names."""
	for adapter in all_adapters().values():
		if adapter.label == label:
			return adapter
	raise SourceError(f"Unknown source platform: {label}")
