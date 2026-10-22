# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stand-in for every source the end-to-end suite imports, reachable as each of its host names:
woo-a-fake and woo-b-fake (two WooCommerce stores), shopify-fake (Shopify's Admin GraphQL API) and woo-fake
(the WooCommerce stores' media host), and one host per other source. Each store answers from the fixture state run.sh wrote to
<state dir>/<host>-state, through the responders the unit tests use, and only to the test credentials.
While <state dir>/<host>-trouble holds "throttle" every API request is rate limited, and while it holds
"reject" every API request is refused (the lockout-safety checks). Media paths (images, PDFs) return a
1x1 PNG. Shopify grants its Admin API token to the e2e app's client credentials only.

Run as ``python3 -I tests/e2e/fake_store.py <state dir>`` from the repository root."""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import sys
import xml.etree.ElementTree as ET
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support.dynamics import ENTRA_CLIENT_SECRET, ENTRA_TOKEN
from tests.support.fixtures import Answer, RestResponder, ShopifyResponder, intacct_request
from tests.support.peoplesoft import PS_PASSWORD, PS_USER
from tests.support.sage_intacct import (
	INTACCT_COMPANY,
	INTACCT_PASSWORD,
	INTACCT_SENDER,
	INTACCT_SENDER_PASSWORD,
	INTACCT_USER,
)
from tests.support.sap_business_one import B1_COOKIE

PORT = 8080
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Bytes no text encoding takes, as a real image has: Frappe sniffs file content for text.
BINARY_MARKER = bytes(range(0x80, 0xA0))
MEDIA_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
PDF_SUFFIX = ".pdf"
WOO_AUTHORIZATION = "Basic " + base64.b64encode(b"e2e:e2e-application-password").decode()
# The token the fake Shopify issues for the e2e app's client credentials (a Dev Dashboard app's grant).
SHOPIFY_TOKEN = "e2e-shopify-token"
SHOPIFY_TOKEN_PATH = "/admin/oauth/access_token"
SHOPIFY_CLIENT_GRANT = {
	"grant_type": ["client_credentials"],
	"client_id": ["e2e-client-id"],
	"client_secret": ["e2e-client-secret"],
}
ETSY_TOKEN_PATH = "/v3/public/oauth/token"
# Microsoft Entra ID's token endpoint, under the tenant (Dynamics 365's app grant).
ENTRA_GRANT_PATH = "/oauth2/v2.0/token"
# Etsy's grant for the connected shop owner (user 7001), with a new refresh token every time.
ETSY_GRANT = {
	"access_token": "7001.fixture-access-token",
	"token_type": "Bearer",
	"expires_in": 3600,
	"refresh_token": "fixture-refresh-token",
}
# The REST sources besides WooCommerce, by host: their fixtures, and the headers every request must carry.
REST_SOURCES: dict[str, tuple[str, dict[str, str]]] = {
	"etsy-fake": (
		"etsy",
		{"x-api-key": "e2e-keystring:e2e-shared-secret", "Authorization": "Bearer 7001.fixture-access-token"},
	),
	"square_online-fake": (
		"square_online",
		{"Authorization": "Bearer e2e-square-token", "Square-Version": "2026-09-16"},
	),
	"bigcommerce-fake": ("bigcommerce", {"X-Auth-Token": "e2e-bigcommerce-token"}),
	# The Square account gift cards and loyalty are written to (whatever the source).
	"square_stored_value-fake": (
		"square_stored_value",
		{"Authorization": "Bearer e2e-square-token", "Square-Version": "2026-09-16"},
	),
	"squarespace-fake": (
		"squarespace",
		{"Authorization": "Bearer e2e-squarespace-key", "User-Agent": "frappe-idempotent-importer"},
	),
	"wix-fake": ("wix", {"Authorization": "e2e-wix-key", "wix-site-id": "e2e-wix-site"}),
	"magento-fake": ("magento", {"Authorization": "Bearer e2e-magento-token"}),
	"lightspeed-fake": (
		"lightspeed",
		{"Authorization": "Basic " + base64.b64encode(b"e2e-lightspeed-key:e2e-lightspeed-secret").decode()},
	),
	"ecwid-fake": ("ecwid", {"Authorization": "Bearer e2e-ecwid-token"}),
	"odoo-fake": ("odoo", {"Authorization": "bearer e2e-odoo-key", "X-Odoo-Database": "harbour"}),
	"dynamics_business_central-fake": (
		"dynamics_business_central",
		{"Authorization": f"Bearer {ENTRA_TOKEN}"},
	),
	"dynamics_sales-fake": ("dynamics_sales", {"Authorization": f"Bearer {ENTRA_TOKEN}"}),
	"dynamics_finance_operations-fake": (
		"dynamics_finance_operations",
		{"Authorization": f"Bearer {ENTRA_TOKEN}"},
	),
	"netsuite-fake": ("netsuite", {"Authorization": "Bearer e2e-netsuite-token"}),
	"sap_business_one-fake": ("sap_business_one", {"Cookie": B1_COOKIE}),
	"peoplesoft-fake": (
		"peoplesoft",
		{"Authorization": "Basic " + base64.b64encode(f"{PS_USER}:{PS_PASSWORD}".encode()).decode()},
	),
}
# Sage Intacct's gateway: the e2e Web Services sender and company user, checked in every request's body.
INTACCT_HOST = "sage_intacct-fake"
INTACCT_PATH = "/ia/xml/xmlgw.phtml"
INTACCT_CREDENTIALS = {
	"sender": INTACCT_SENDER,
	"sender_password": INTACCT_SENDER_PASSWORD,
	"company": INTACCT_COMPANY,
	"user": INTACCT_USER,
	"password": INTACCT_PASSWORD,
}
INTACCT_REFUSED = (
	b"<response><control><status>failure</status><senderid></senderid><controlid></controlid></control>"
	b"<errormessage><error><errorno>XL03000006</errorno><description2>Incorrect Intacct XML Partner ID or "
	b"password.</description2></error></errormessage></response>"
)
# The Service Layer's login, answered from the fixtures (which hold the test company's credentials) before
# any session exists.
B1_LOGIN_PATH = "/b1s/v1/Login"
# The sources approved through OAuth 2, by host: their token endpoint, whether it rotates the refresh token,
# and the headers every API request must carry with the access token it grants.
OAUTH_ACCESS_TOKEN = "e2e-oauth-access-token"
OAUTH_ROTATED_REFRESH_TOKEN = "e2e-oauth-rotated-refresh-token"
OAUTH_SOURCES: dict[str, tuple[str, str, bool, dict[str, str]]] = {
	"quickbooks_online-fake": (
		"quickbooks_online",
		"/oauth2/v1/tokens/bearer",
		True,
		{"Authorization": f"Bearer {OAUTH_ACCESS_TOKEN}"},
	),
	"xero-fake": (
		"xero",
		"/connect/token",
		True,
		{
			"Authorization": f"Bearer {OAUTH_ACCESS_TOKEN}",
			"xero-tenant-id": "70784a63-d24b-46a9-a4db-0e70a274b056",
		},
	),
	"zoho_books-fake": (
		"zoho_books",
		"/oauth/v2/token",
		False,
		{"Authorization": f"Zoho-oauthtoken {OAUTH_ACCESS_TOKEN}"},
	),
	"zoho_inventory-fake": (
		"zoho_inventory",
		"/oauth/v2/token",
		False,
		{"Authorization": f"Zoho-oauthtoken {OAUTH_ACCESS_TOKEN}"},
	),
	"acumatica-fake": (
		"acumatica",
		"/identity/connect/token",
		True,
		{"Authorization": f"Bearer {OAUTH_ACCESS_TOKEN}"},
	),
	# Agendrix takes its token requests as JSON, and every API request is JSON.
	"agendrix-fake": (
		"agendrix",
		"/oauth/token",
		True,
		{"Authorization": f"Bearer {OAUTH_ACCESS_TOKEN}", "Content-Type": "application/json"},
	),
	"highlevel-fake": ("highlevel", "/oauth/token", True, {"Authorization": f"Bearer {OAUTH_ACCESS_TOKEN}"}),
	"hubspot-fake": ("hubspot", "/oauth/v1/token", False, {"Authorization": f"Bearer {OAUTH_ACCESS_TOKEN}"}),
	"salesforce-fake": (
		"salesforce",
		"/services/oauth2/token",
		False,
		{"Authorization": f"Bearer {OAUTH_ACCESS_TOKEN}"},
	),
	"pipedrive-fake": ("pipedrive", "/oauth/token", False, {"Authorization": f"Bearer {OAUTH_ACCESS_TOKEN}"}),
	"zoho_crm-fake": (
		"zoho_crm",
		"/oauth/v2/token",
		False,
		{"Authorization": f"Zoho-oauthtoken {OAUTH_ACCESS_TOKEN}"},
	),
	"webflow-fake": (
		"webflow",
		"/oauth/access_token",
		False,
		{"Authorization": f"Bearer {OAUTH_ACCESS_TOKEN}"},
	),
	# FreshBooks takes its token requests as JSON, and wants the redirect URI on a refresh too.
	"freshbooks-fake": (
		"freshbooks",
		"/auth/oauth/token",
		True,
		{"Authorization": f"Bearer {OAUTH_ACCESS_TOKEN}"},
	),
}
# What a source's token response names beside the tokens: HighLevel's, the sub-account it was issued for.
GRANT_EXTRAS: dict[str, dict[str, str]] = {
	"highlevel-fake": {"locationId": "loc0harbour0studio01", "userType": "Location"},
	# Pipedrive's, the company domain its API answers on.
	"pipedrive-fake": {"api_domain": "https://harbour.pipedrive.com"},
}
# The OAuth sources whose token endpoint takes JSON and wants the redirect URI on a refresh too.
JSON_GRANT_HOSTS = frozenset({"agendrix-fake", "freshbooks-fake"})
XERO_CONNECTIONS = "/Connections"
# NetSuite's machine-to-machine grant: a client assertion signed by the e2e certificate's key (generated on
# the bench, so its signature isn't checked here), for the account's canonical token endpoint.
NETSUITE_HOST = "netsuite-fake"
NETSUITE_GRANT_PATH = "/services/rest/auth/oauth2/v1/token"
NETSUITE_TOKEN = "e2e-netsuite-token"
NETSUITE_ASSERTION = {
	"iss": "e2e-netsuite-client",
	"scope": "rest_webservices",
	"aud": "https://1234567.suitetalk.api.netsuite.com" + NETSUITE_GRANT_PATH,
}
NETSUITE_CERTIFICATE = "e2e-certificate"
THROTTLE_RETRY_AFTER_SECONDS = "120"
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_TOO_MANY_REQUESTS = 429


class FakeStore(BaseHTTPRequestHandler):
	state_dir: Path

	def do_GET(self) -> None:
		self.answer("GET")

	def do_POST(self) -> None:
		self.answer("POST")

	def answer(self, method: str) -> None:
		host = (self.headers.get("Host") or "").split(":", 1)[0]
		media_path = self.path.split("?", 1)[0].lower()
		if media_path.endswith(PDF_SUFFIX):
			self.send(Answer(200, pdf(), {"Content-Type": "application/pdf"}))
			return
		if media_path.endswith(MEDIA_SUFFIXES):
			self.send(Answer(200, png(self.path), {"Content-Type": "image/png"}))
			return
		trouble = self.read_state(f"{host}-trouble")
		if trouble == "throttle":
			self.send(
				Answer(
					HTTP_TOO_MANY_REQUESTS,
					b'{"errors":"throttled"}',
					{"Retry-After": THROTTLE_RETRY_AFTER_SECONDS},
				)
			)
			return
		if trouble == "reject":
			self.send(Answer(HTTP_UNAUTHORIZED, b'{"code":"rest_forbidden"}'))
			return
		state = self.read_state(f"{host}-state")
		body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
		if host == "shopify-fake":
			if method == "POST" and self.path == SHOPIFY_TOKEN_PATH:
				self.send(shopify_grant(body))
				return
			if self.headers.get("X-Shopify-Access-Token") != SHOPIFY_TOKEN:
				self.send(Answer(HTTP_UNAUTHORIZED, b'{"errors":"[API] Invalid API key or access token"}'))
				return
			self.send(ShopifyResponder(state).answer(body))
			return
		if method == "POST" and self.path.endswith(ENTRA_GRANT_PATH):
			self.send(entra_grant(body))
			return
		if host == INTACCT_HOST:
			self.send(intacct_answer(state, body))
			return
		if method == "POST" and self.path == B1_LOGIN_PATH:
			self.send(RestResponder("sap_business_one", state).answer(method, self.path, body))
			return
		if host == NETSUITE_HOST and method == "POST" and self.path == NETSUITE_GRANT_PATH:
			self.send(netsuite_grant(body))
			return
		if host == "etsy-fake" and method == "POST" and self.path == ETSY_TOKEN_PATH:
			self.send(Answer(200, json.dumps(ETSY_GRANT).encode()))
			return
		if host in OAUTH_SOURCES:
			source, grant_path, rotates, headers = OAUTH_SOURCES[host]
			if method == "POST" and self.path.split("?", 1)[0] == grant_path:
				if host in JSON_GRANT_HOSTS:
					self.send(json_grant(body))
					return
				self.send(
					oauth_grant(
						rotates,
						parse_qs(body.decode()).get("grant_type") == ["authorization_code"],
						GRANT_EXTRAS.get(host, {}),
					)
				)
				return
			if self.path.startswith(XERO_CONNECTIONS):
				# Xero's identity API lists the tenants before one is chosen.
				headers = {name: value for name, value in headers.items() if name != "xero-tenant-id"}
		else:
			source, headers = REST_SOURCES.get(host, ("woocommerce", {"Authorization": WOO_AUTHORIZATION}))
		if any(self.headers.get(name) != value for name, value in headers.items()):
			self.send(Answer(HTTP_UNAUTHORIZED, b'{"code":"rest_not_logged_in"}'))
			return
		self.send(RestResponder(source, state).answer(method, self.path, body))

	def read_state(self, name: str) -> str:
		path = self.state_dir / name
		return path.read_text().strip() if path.exists() else ""

	def send(self, answer: Answer) -> None:
		self.send_response(answer.status)
		headers = {"Content-Type": "application/json", **answer.headers}
		for name, value in headers.items():
			self.send_header(name, value)
		self.send_header("Content-Length", str(len(answer.body)))
		self.end_headers()
		self.wfile.write(answer.body)

	def log_message(self, format: str, *args: object) -> None:
		sys.stderr.write(f"{self.headers.get('Host')} {format % args}\n")


def entra_grant(body: bytes) -> Answer:
	"""Microsoft Entra ID's client credentials grant (Dynamics 365): the e2e app's secret gets the token the
	Dynamics fakes answer to; anything else is refused as Entra refuses it."""
	form = parse_qs(body.decode())
	if form.get("grant_type") != ["client_credentials"] or form.get("client_secret") != [ENTRA_CLIENT_SECRET]:
		return Answer(HTTP_UNAUTHORIZED, b'{"error":"invalid_client"}')
	grant = {"token_type": "Bearer", "expires_in": 3599, "access_token": ENTRA_TOKEN}
	return Answer(200, json.dumps(grant).encode())


def intacct_answer(state: str, body: bytes) -> Answer:
	"""Sage Intacct's gateway: the e2e sender and user get the fixtures' answer; anything else is refused as
	the gateway refuses a sender (a failed control block)."""
	try:
		sent = intacct_request(body)
	except ET.ParseError, IndexError, StopIteration:
		return Answer(HTTP_BAD_REQUEST, b"<response/>")
	if any(sent.get(name) != value for name, value in INTACCT_CREDENTIALS.items()):
		return Answer(200, INTACCT_REFUSED, {"Content-Type": "application/xml"})
	return RestResponder("sage_intacct", state).answer("POST", INTACCT_PATH, body)


def jwt_part(token: str, index: int) -> dict[str, object]:
	"""One of a JWT's JSON parts (0: the header, 1: the claims), unverified."""
	part = token.split(".")[index]
	decoded = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
	return decoded if isinstance(decoded, dict) else {}


def netsuite_grant(body: bytes) -> Answer:
	"""NetSuite's client credentials grant: an assertion from the e2e integration, for the e2e certificate
	and the account's token endpoint, gets the token the fake NetSuite answers to; anything else is refused
	as NetSuite refuses it."""
	form = parse_qs(body.decode())
	assertion = (form.get("client_assertion") or [""])[0]
	try:
		header, claims = jwt_part(assertion, 0), jwt_part(assertion, 1)
	except ValueError, IndexError:
		return Answer(HTTP_BAD_REQUEST, b'{"error":"invalid_request"}')
	valid = (
		form.get("grant_type") == ["client_credentials"]
		and form.get("client_assertion_type") == ["urn:ietf:params:oauth:client-assertion-type:jwt-bearer"]
		and header.get("kid") == NETSUITE_CERTIFICATE
		and all(claims.get(name) == value for name, value in NETSUITE_ASSERTION.items())
	)
	if not valid:
		return Answer(HTTP_BAD_REQUEST, b'{"error":"invalid_client"}')
	grant = {"access_token": NETSUITE_TOKEN, "token_type": "Bearer", "expires_in": 3600}
	return Answer(200, json.dumps(grant).encode())


def shopify_grant(body: bytes) -> Answer:
	"""Shopify's client credentials grant: the e2e app's client ID and secret get the token the Admin API
	answers to; anything else is refused as Shopify refuses it."""
	if parse_qs(body.decode()) != SHOPIFY_CLIENT_GRANT:
		return Answer(HTTP_BAD_REQUEST, b'{"error":"invalid_client"}')
	grant = {"access_token": SHOPIFY_TOKEN, "scope": "read_products", "expires_in": 86399}
	return Answer(200, json.dumps(grant).encode())


def oauth_grant(rotates: bool, exchange: bool, extras: dict[str, str] | None = None) -> Answer:
	"""An OAuth 2 token response: the access token the API answers to, and a new refresh token for an
	authorization code's exchange (and every refresh of a source that rotates it), with what the source
	names beside them."""
	grant: dict[str, object] = {
		"access_token": OAUTH_ACCESS_TOKEN,
		"token_type": "Bearer",
		"expires_in": 3600,
		**(extras or {}),
	}
	if rotates or exchange:
		grant["refresh_token"] = OAUTH_ROTATED_REFRESH_TOKEN
	return Answer(200, json.dumps(grant).encode())


def json_grant(body: bytes) -> Answer:
	"""A token request sent as JSON, naming the redirect URI on a refresh as well as an exchange (Agendrix's);
	anything else is refused as an OAuth 2 server refuses it."""
	try:
		request = json.loads(body)
	except ValueError:
		return Answer(HTTP_BAD_REQUEST, b'{"error":"invalid_request"}')
	grant_type = request.get("grant_type") if isinstance(request, dict) else None
	if grant_type not in ("authorization_code", "refresh_token") or not request.get("redirect_uri"):
		return Answer(HTTP_BAD_REQUEST, b'{"error":"invalid_request"}')
	return oauth_grant(True, grant_type == "authorization_code")


def pdf() -> bytes:
	"""A one-page PDF with a correct cross-reference table (Frappe reads uploaded PDFs to refuse scripts)."""
	objects = [
		b"<< /Type /Catalog /Pages 2 0 R >>",
		b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
		b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] >>",
	]
	body = b"%PDF-1.4\n"
	offsets = []
	for number, content in enumerate(objects, start=1):
		offsets.append(len(body))
		body += f"{number} 0 obj\n".encode() + content + b"\nendobj\n"
	xref = len(body)
	body += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
	body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
	body += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
	return body


def png(path: str) -> bytes:
	"""A 1x1 PNG whose colour comes from ``path`` (so different images differ), with a binary text chunk."""
	red, green, blue = hashlib.sha256(path.encode()).digest()[:3]

	def chunk(kind: bytes, data: bytes) -> bytes:
		return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

	header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
	pixels = zlib.compress(bytes([0, red, green, blue]))
	return (
		PNG_SIGNATURE
		+ chunk(b"IHDR", header)
		+ chunk(b"tEXt", b"fake\0" + BINARY_MARKER)
		+ chunk(b"IDAT", pixels)
		+ chunk(b"IEND", b"")
	)


def main() -> None:
	FakeStore.state_dir = Path(sys.argv[1])
	ThreadingHTTPServer(("0.0.0.0", PORT), FakeStore).serve_forever()  # noqa: S104 - a test container's own network


if __name__ == "__main__":
	main()
