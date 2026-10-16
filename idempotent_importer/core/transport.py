# SPDX-License-Identifier: AGPL-3.0-or-later
"""HttpTransport over ``requests``. An unreachable source (DNS, connection or timeout failure) is a
RetryLater: the run pauses and tries again rather than failing every kind against a source that is down."""

from __future__ import annotations

from urllib.parse import urlsplit

import requests

from idempotent_importer.core.errors import RetryLater, SourceError
from idempotent_importer.core.http import HTTP_BAD_REQUEST, HttpResponse

UNREACHABLE_RETRY_SECONDS = 60.0
DOWNLOAD_CHUNK_BYTES = 64 * 1024


class RequestsTransport:
	def __init__(self, session: requests.Session | None = None) -> None:
		self._session = session or requests.Session()

	def get(self, url: str, headers: dict[str, str], timeout_seconds: float) -> HttpResponse:
		try:
			response = self._session.get(url, headers=headers, timeout=timeout_seconds, allow_redirects=True)
		except requests.RequestException as e:
			raise self._unreachable(url, e) from e
		return self._response(response)

	def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> HttpResponse:
		try:
			response = self._session.post(url, headers=headers, data=body, timeout=timeout_seconds)
		except requests.RequestException as e:
			raise self._unreachable(url, e) from e
		return self._response(response)

	def download(self, url: str, timeout_seconds: float, max_bytes: int) -> bytes:
		try:
			with self._session.get(url, timeout=timeout_seconds, stream=True) as response:
				if response.status_code >= HTTP_BAD_REQUEST:
					raise SourceError(
						f"Downloading {self._host(url)} media failed (HTTP {response.status_code})."
					)
				content = bytearray()
				for chunk in response.iter_content(DOWNLOAD_CHUNK_BYTES):
					content.extend(chunk)
					if len(content) > max_bytes:
						raise SourceError(f"Media from {self._host(url)} is larger than {max_bytes} bytes.")
				return bytes(content)
		except requests.RequestException as e:
			raise self._unreachable(url, e) from e

	@staticmethod
	def _response(response: requests.Response) -> HttpResponse:
		headers = {name.lower(): value for name, value in response.headers.items()}
		return HttpResponse(response.status_code, response.content, headers)

	@classmethod
	def _unreachable(cls, url: str, error: requests.RequestException) -> RetryLater:
		return RetryLater(
			UNREACHABLE_RETRY_SECONDS, f"Could not reach {cls._host(url)}: {type(error).__name__}"
		)

	@staticmethod
	def _host(url: str) -> str:
		return urlsplit(url).hostname or "the source"
