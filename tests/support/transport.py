# SPDX-License-Identifier: AGPL-3.0-or-later
"""In-process stand-ins for the network and the clock: a transport that answers from the recorded
fixtures (or a script of responses) and a sleeper that records waits instead of sleeping."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from idempotent_importer.core.http import HttpResponse
from tests.support.fixtures import Answer, RestResponder, ShopifyResponder

type Route = Callable[[str, str, bytes, dict[str, str]], HttpResponse]


@dataclass
class Request:
	method: str
	url: str
	headers: dict[str, str]
	body: bytes = b""


@dataclass
class FakeTransport:
	"""Answers every request through ``route``, and records it."""

	route: Route
	requests: list[Request] = field(default_factory=list)

	def get(self, url: str, headers: dict[str, str], timeout_seconds: float) -> HttpResponse:
		assert timeout_seconds > 0, "every request carries a timeout"
		self.requests.append(Request("GET", url, headers))
		return self.route("GET", url, b"", headers)

	def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> HttpResponse:
		assert timeout_seconds > 0, "every request carries a timeout"
		self.requests.append(Request("POST", url, headers, body))
		return self.route("POST", url, body, headers)

	def download(self, url: str, timeout_seconds: float, max_bytes: int) -> bytes:
		self.requests.append(Request("DOWNLOAD", url, {}))
		return b"image"


def response(answer: Answer) -> HttpResponse:
	return HttpResponse(answer.status, answer.body, {k.lower(): v for k, v in answer.headers.items()})


def woocommerce(state: str) -> FakeTransport:
	responder = RestResponder("woocommerce", state)
	return FakeTransport(lambda method, url, _body, _headers: response(responder.answer(method, url)))


def shopify(state: str) -> FakeTransport:
	responder = ShopifyResponder(state)
	return FakeTransport(lambda _method, _url, body, _headers: response(responder.answer(body)))


def scripted(*responses: HttpResponse) -> FakeTransport:
	"""Answers requests with ``responses`` in order (the last one repeats)."""
	queue = list(responses)

	def route(_method: str, _url: str, _body: bytes, _headers: dict[str, str]) -> HttpResponse:
		return queue.pop(0) if len(queue) > 1 else queue[0]

	return FakeTransport(route)


@dataclass
class RecordingSleeper:
	"""A clock that only moves when something sleeps, recording every wait."""

	clock: float = 1_000_000.0
	waits: list[float] = field(default_factory=list)

	def sleep(self, seconds: float) -> None:
		self.waits.append(seconds)
		self.clock += seconds

	def now(self) -> float:
		return self.clock
