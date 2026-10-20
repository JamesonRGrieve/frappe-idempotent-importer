# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zoho Inventory, through the Inventory API v1: locations (warehouses), categories, item groups with their
variants and the items in no group, customers and vendors, sales orders, invoices and credit notes,
purchase orders, bills and vendor credits, and customer payments. Set up as Zoho Books is (a server-based
client with this site's ``/api/method/idempotent_importer.api.oauth_callback`` as its redirect URI), with
the organisation's Inventory address in its data centre (``https://inventory.zoho.com``) as the Store URL,
and connect with Connect to Zoho Inventory. An organisation on both products shares its contacts,
invoices and bills between them: mirror those from one connection only (the other's switches off)."""

from __future__ import annotations

from idempotent_importer.sources.zoho.adapter import ZohoAdapter
from idempotent_importer.sources.zoho.mapping import inventory_mappers
from idempotent_importer.sources.zoho_inventory.reader import MODULES, PRODUCT, SOURCE_ID, ZohoInventoryReader


def adapter() -> ZohoAdapter:
	return ZohoAdapter(SOURCE_ID, PRODUCT, MODULES, inventory_mappers, ZohoInventoryReader)
