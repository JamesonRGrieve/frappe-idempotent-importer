# SPDX-License-Identifier: AGPL-3.0-or-later
"""The requests transport against a real local HTTP server: responses, headers, media size caps, and an
unreachable source pausing the run."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from idempotent_importer.core.errors import RetryLater, SourceError
from idempotent_importer.core.transport import UNREACHABLE_RETRY_SECONDS, RequestsTransport

TIMEOUT = 5.0
IMAGE = b"\x89PNG" + bytes(range(256))


class Handler(BaseHTTPRequestHandler):
	def do_GET(self) -> None:
		if self.path.startswith("/image"):
			self.reply(200, IMAGE, "image/png")
		elif self.path.startswith("/missing"):
			self.reply(404, b"{}", "application/json")
		else:
			self.reply(200, b'{"method": "GET"}', "application/json", {"X-WP-TotalPages": "2"})

	def do_POST(self) -> None:
		length = int(self.headers.get("Content-Length") or 0)
		self.reply(200, self.rfile.read(length), "application/json")

	def reply(
		self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None
	) -> None:
		self.send_response(status)
		self.send_header("Content-Type", content_type)
		for name, value in (headers or {}).items():
			self.send_header(name, value)
		self.send_header("Content-Length", str(len(body)))
		self.end_headers()
		self.wfile.write(body)

	def log_message(self, message_format: str, *args: object) -> None:
		return None


@pytest.fixture
def server() -> Iterator[str]:
	httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
	thread = threading.Thread(target=httpd.serve_forever, daemon=True)
	thread.start()
	yield f"http://127.0.0.1:{httpd.server_address[1]}"
	httpd.shutdown()
	httpd.server_close()


def closed_port() -> int:
	with socket.socket() as probe:
		probe.bind(("127.0.0.1", 0))
		return int(probe.getsockname()[1])


def test_get_and_post(server: str) -> None:
	transport = RequestsTransport()
	response = transport.get(f"{server}/things", {"Accept": "application/json"}, TIMEOUT)
	assert (response.status, response.body) == (200, b'{"method": "GET"}')
	assert response.header("X-WP-TotalPages") == "2"
	posted = transport.post(f"{server}/query", {}, b'{"q": 1}', TIMEOUT)
	assert posted.body == b'{"q": 1}'
	assert transport.get(f"{server}/missing", {}, TIMEOUT).status == 404


def test_downloads_are_capped(server: str) -> None:
	transport = RequestsTransport()
	assert transport.download(f"{server}/image.png", TIMEOUT, 1024) == IMAGE
	with pytest.raises(SourceError, match="larger than"):
		transport.download(f"{server}/image.png", TIMEOUT, 10)
	with pytest.raises(SourceError, match="HTTP 404"):
		transport.download(f"{server}/missing.png", TIMEOUT, 1024)


def test_an_unreachable_source_pauses_the_run() -> None:
	transport = RequestsTransport()
	with pytest.raises(RetryLater) as paused:
		transport.get(f"http://127.0.0.1:{closed_port()}/", {}, TIMEOUT)
	assert paused.value.seconds == UNREACHABLE_RETRY_SECONDS
	assert "127.0.0.1" in str(paused.value)
