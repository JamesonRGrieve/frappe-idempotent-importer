# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Odoo connection the fixtures were recorded for (the unit tests', the fixture writer's and the end-to-end
fake store's): the database "harbour" with the e2e API key."""

from __future__ import annotations

from idempotent_importer.core.source import ConnectionConfig

ODOO_KEY = "e2e-odoo-key"
ODOO_DATABASE = "harbour"
ODOO_CONFIG = ConnectionConfig(
	"odoo", "odoo-o", "http://odoo-fake:8080", api_secret=ODOO_KEY, account=ODOO_DATABASE
)
