# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stand-in for every source the end-to-end suite imports, reachable as each of its host names:
woo-a-fake and woo-b-fake (two WooCommerce stores), shopify-fake (Shopify's Admin GraphQL API) and woo-fake
(the WooCommerce stores' media host). Each store answers from the fixture state run.sh wrote to
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
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support.fixtures import Answer, RestResponder, ShopifyResponder

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
		if host == "shopify-fake":
			length = int(self.headers.get("Content-Length") or 0)
			body = self.rfile.read(length)
			if method == "POST" and self.path == SHOPIFY_TOKEN_PATH:
				self.send(shopify_grant(body))
				return
			if self.headers.get("X-Shopify-Access-Token") != SHOPIFY_TOKEN:
				self.send(Answer(HTTP_UNAUTHORIZED, b'{"errors":"[API] Invalid API key or access token"}'))
				return
			self.send(ShopifyResponder(state).answer(body))
			return
		if self.headers.get("Authorization") != WOO_AUTHORIZATION:
			self.send(Answer(HTTP_UNAUTHORIZED, b'{"code":"rest_not_logged_in"}'))
			return
		self.send(RestResponder("woocommerce", state).answer(method, self.path))

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


def shopify_grant(body: bytes) -> Answer:
	"""Shopify's client credentials grant: the e2e app's client ID and secret get the token the Admin API
	answers to; anything else is refused as Shopify refuses it."""
	if parse_qs(body.decode()) != SHOPIFY_CLIENT_GRANT:
		return Answer(HTTP_BAD_REQUEST, b'{"error":"invalid_client"}')
	grant = {"access_token": SHOPIFY_TOKEN, "scope": "read_products", "expires_in": 86399}
	return Answer(200, json.dumps(grant).encode())


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
