# SPDX-License-Identifier: AGPL-3.0-or-later
"""Zoho Books, through the Books API v3: the chart of accounts, locations, items, customers and vendors,
projects, estimates, sales orders, invoices, credit notes and sales receipts, purchase orders, bills and
vendor credits, customer and vendor payments, manual journals and expenses. The operator registers a
server-based client in Zoho's API console (api-console.zoho.com, Multi-DC enabled) with this site's
``/api/method/idempotent_importer.api.oauth_callback`` as its redirect URI, enters the organisation's Books
address in its data centre (``https://books.zoho.eu``) as the Store URL and the client's ID and secret, and
connects with Connect to Zoho Books."""

from __future__ import annotations

from idempotent_importer.sources.zoho.adapter import ZohoAdapter
from idempotent_importer.sources.zoho.mapping import books_mappers
from idempotent_importer.sources.zoho_books.reader import MODULES, PRODUCT, SOURCE_ID, ZohoBooksReader


def adapter() -> ZohoAdapter:
	return ZohoAdapter(SOURCE_ID, PRODUCT, MODULES, books_mappers, ZohoBooksReader)
