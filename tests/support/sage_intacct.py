# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Sage Intacct test company's credentials, shared by the fixture writer, the unit tests, the fixture
responder and the e2e fake store: the Web Services sender and the company user it logs in as."""

from __future__ import annotations

INTACCT_SENDER = "e2e-sender"
INTACCT_SENDER_PASSWORD = "e2e-sender-password"
INTACCT_COMPANY = "LINCPA"
INTACCT_USER = "e2e-reader"
INTACCT_PASSWORD = "e2e-reader-password"
