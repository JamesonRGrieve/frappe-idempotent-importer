# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SAP Business One test company's login, shared by the fixture writer, the unit tests and the e2e
fake store: what the Service Layer's Login accepts, and the session it answers."""

from __future__ import annotations

B1_COMPANY_DB = "SBO_NORTHWIND"
B1_USER = "e2e-b1-user"
B1_PASSWORD = "e2e-b1-password"
B1_SESSION = "e2e-b1-session"
B1_ROUTE = ".node1"
B1_COOKIE = f"B1SESSION={B1_SESSION}; ROUTEID={B1_ROUTE}"
