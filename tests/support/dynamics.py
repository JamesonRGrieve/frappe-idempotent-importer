# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Dynamics 365 connections the fixtures were recorded for (the unit tests', the fixture writers' and the
end-to-end fake store's), and the token Microsoft Entra ID's fake grants their app."""

from __future__ import annotations

from idempotent_importer.core.source import ConnectionConfig

ENTRA_TENANT = "e2e-tenant"
ENTRA_CLIENT_ID = "e2e-entra-client"
ENTRA_CLIENT_SECRET = "e2e-entra-secret"
ENTRA_TOKEN = "e2e-entra-token"
BC_COMPANY = "c0ffee00-0000-4000-8000-000000000001"
BC_ENVIRONMENT_PATH = f"/v2.0/{ENTRA_TENANT}/production"
BC_CONFIG = ConnectionConfig(
	"dynamics_business_central",
	"business-central-b",
	f"https://api.businesscentral.dynamics.com{BC_ENVIRONMENT_PATH}",
	api_user=ENTRA_CLIENT_ID,
	api_secret=ENTRA_CLIENT_SECRET,
	account=BC_COMPANY,
)
SALES_ORGANISATION = "https://harbourpine.crm3.dynamics.com"
SALES_CONFIG = ConnectionConfig(
	"dynamics_sales",
	"dynamics-sales-d",
	SALES_ORGANISATION,
	api_user=ENTRA_CLIENT_ID,
	api_secret=ENTRA_CLIENT_SECRET,
	account=ENTRA_TENANT,
)
FO_ENVIRONMENT = "https://harbourpine.operations.dynamics.com"
FO_LEGAL_ENTITY = "hpt"
# The test environment's field map: its sales orders' order date is ORDERDATE (the reference documents none).
FO_FIELD_MAP = '{"sales_order": {"fields": {"date": "ORDERDATE"}}}'
FO_CONFIG = ConnectionConfig(
	"dynamics_finance_operations",
	"finance-operations-f",
	FO_ENVIRONMENT,
	api_user=ENTRA_CLIENT_ID,
	api_secret=ENTRA_CLIENT_SECRET,
	account=f"{ENTRA_TENANT}/{FO_LEGAL_ENTITY}",
	field_map=FO_FIELD_MAP,
)
