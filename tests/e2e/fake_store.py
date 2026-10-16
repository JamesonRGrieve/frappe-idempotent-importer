# SPDX-License-Identifier: AGPL-3.0-or-later
"""A stand-in for every source the end-to-end suite imports, reachable as each of its host names:
woo-a-fake and woo-b-fake (two WooCommerce stores), shopify-fake (Shopify's Admin GraphQL API) and woo-fake
(the WooCommerce stores' media host). Each store answers from the fixture state run.sh wrote to
<state dir>/<host>-state, through the responders the unit tests use, and only to the test credentials.
While <state dir>/<host>-trouble holds "throttle" every API request is rate limited, and while it holds
"reject" every API request is refused (the lockout-safety checks). Image paths return a 1x1 PNG.

Run as ``python3 -I tests/e2e/fake_store.py <state dir>`` from the repository root."""

from __future__ import annotations

import base64
import hashlib
import struct
import sys
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests.support.fixtures import Answer, RestResponder, ShopifyResponder

PORT = 8080
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# Bytes no text encoding takes, as a real image has: Frappe sniffs file content for text.
BINARY_MARKER = bytes(range(0x80, 0xA0))
MEDIA_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")
WOO_AUTHORIZATION = "Basic " + base64.b64encode(b"e2e:e2e-application-password").decode()
SHOPIFY_TOKEN = "e2e-shopify-token"
THROTTLE_RETRY_AFTER_SECONDS = "120"
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
		if self.path.split("?", 1)[0].lower().endswith(MEDIA_SUFFIXES):
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
			if self.headers.get("X-Shopify-Access-Token") != SHOPIFY_TOKEN:
				self.send(Answer(HTTP_UNAUTHORIZED, b'{"errors":"[API] Invalid API key or access token"}'))
				return
			length = int(self.headers.get("Content-Length") or 0)
			self.send(ShopifyResponder(state).answer(self.rfile.read(length)))
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
