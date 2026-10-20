# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Service Layer's session (SAP Business One has no OAuth): ``POST /Login`` with the company database,
user name and password answers a ``SessionId``, sent back on every request as the ``B1SESSION`` cookie
(with the ``ROUTEID`` cookie a load-balanced Service Layer sets, to keep the session on its node). A session
lapses after 30 minutes idle (the server's ``SessionTimeout``); the Service Layer then answers 401, and the client logs in
again once (``renew``); a refused login stops the run and holds scheduled runs (the Service Layer API
Reference's Login, https://help.sap.com/doc/056f69366b5345a386bb8149f1700c19/10.0/en-US/Service%20Layer%20API%20Reference.html).
The password never appears in errors or reprs."""

from __future__ import annotations

import json
import re

from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError
from idempotent_importer.core.http import (
	HTTP_OK,
	HTTP_SERVER_ERROR,
	HTTP_TOO_MANY_REQUESTS,
	HttpTransport,
	RetryPolicy,
)

LOGIN_PATH = "/Login"
LOGIN_TIMEOUT_SECONDS = 30.0
FIRST_ATTEMPT = 1
SESSION_COOKIE = "B1SESSION"
ROUTE_COOKIE = re.compile(r"(?:^|[,;]\s*)ROUTEID=([^;,\s]+)")


class ServiceLayerSession:
	"""``core.rest.RequestAuthorizer`` for the Service Layer: logs in on first use and again when a session
	has lapsed."""

	def __init__(
		self, base_url: str, company: str, user: str, password: str, transport: HttpTransport
	) -> None:
		if not company or not user or not password:
			raise SourceError(
				"SAP Business One is not configured: enter the company database as the account, and the "
				"user name and password."
			)
		self._url = base_url.rstrip("/") + LOGIN_PATH
		self._company = company
		self._user = user
		self._password = password
		self._transport = transport
		self._cookie: str | None = None

	def headers(self) -> dict[str, str]:
		if self._cookie is None:
			self._cookie = self._login()
		return {"Cookie": self._cookie}

	def renew(self) -> bool:
		self._cookie = None
		return True

	def _login(self) -> str:
		body = json.dumps(
			{"CompanyDB": self._company, "UserName": self._user, "Password": self._password}
		).encode()
		response = self._transport.post(
			self._url,
			{"Content-Type": "application/json", "Accept": "application/json"},
			body,
			LOGIN_TIMEOUT_SECONDS,
		)
		if response.status == HTTP_TOO_MANY_REQUESTS or response.status >= HTTP_SERVER_ERROR:
			raise RetryLater(
				RetryPolicy.wait(response, FIRST_ATTEMPT) or RetryPolicy.backoff(FIRST_ATTEMPT),
				f"SAP Business One could not open a session (HTTP {response.status}).",
			)
		if response.status != HTTP_OK:
			raise CredentialsRejected(
				f"SAP Business One refused the login (HTTP {response.status}); check the company database, "
				"user name and password."
			)
		try:
			session = json.loads(response.body).get("SessionId")
		except ValueError, AttributeError:
			session = None
		if not isinstance(session, str) or not session:
			raise SourceError("SAP Business One's login answered no session.")
		cookie = f"{SESSION_COOKIE}={session}"
		route = ROUTE_COOKIE.search(response.headers.get("set-cookie", ""))
		return f"{cookie}; ROUTEID={route.group(1)}" if route else cookie

	def __repr__(self) -> str:
		return "ServiceLayerSession([redacted])"
