# SPDX-License-Identifier: AGPL-3.0-or-later
"""The outbound HTTP boundary and lockout safety shared by every source client: pacing under a source's
documented limits (RateLimit + RequestPacer) and retrying rate limiting and server errors (RetryPolicy).
A wait too long to sit out inside a request is a RetryLater, so the run pauses instead."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from idempotent_importer.core.errors import RetryLater

HTTP_OK = 200
HTTP_NO_CONTENT = 204
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500
MILLISECONDS_PER_SECOND = 1000.0


@dataclass(frozen=True)
class HttpResponse:
	"""One response. Header names are lower-cased; a repeated header's values are joined by commas."""

	status: int
	body: bytes
	headers: dict[str, str] = field(default_factory=dict)

	def header(self, name: str) -> str | None:
		return self.headers.get(name.lower()) or None


class HttpTransport(Protocol):
	"""The one outbound HTTP boundary. Implementations bound every request with ``timeout_seconds`` and
	raise RetryLater when the source can't be reached at all."""

	def get(self, url: str, headers: dict[str, str], timeout_seconds: float) -> HttpResponse: ...

	def post(
		self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float
	) -> HttpResponse: ...

	def download(self, url: str, timeout_seconds: float, max_bytes: int) -> bytes:
		"""A file's content (media), at most ``max_bytes``."""
		...


class Sleeper(Protocol):
	"""Waiting, and the clock pacing is measured on; injected so tests run without real delays."""

	def sleep(self, seconds: float) -> None: ...

	def now(self) -> float:
		"""Seconds since the Unix epoch, with fractions."""
		...


class SystemSleeper:
	def sleep(self, seconds: float) -> None:
		if seconds > 0:
			time.sleep(seconds)

	def now(self) -> float:
		return time.time()


class ResetFormat(Enum):
	"""How a source's rate-limit reset header says when the window resets."""

	SECONDS_FROM_NOW = "seconds"
	MILLISECONDS_FROM_NOW = "milliseconds"
	EPOCH_SECONDS = "epoch"

	def seconds_until(self, value: str, now: float) -> float | None:
		try:
			number = float(value)
		except ValueError:
			return None
		match self:
			case ResetFormat.SECONDS_FROM_NOW:
				seconds = number
			case ResetFormat.MILLISECONDS_FROM_NOW:
				seconds = number / MILLISECONDS_PER_SECOND
			case ResetFormat.EPOCH_SECONDS:
				seconds = number - now
		return max(0.0, seconds)


@dataclass(frozen=True)
class RateLimit:
	"""A source's documented request limits (cited where the client declares them): a sustained rate, the
	cool-down after a 429 that carries no timing hint, and the headers, if any, that say how many requests
	remain in the window and when it resets (or, for a source that reports only what remains, the
	window's fixed length). The default is no pacing."""

	requests_per_second: float | None = None
	cooldown_seconds: float | None = None
	remaining_header: str | None = None
	reset_header: str | None = None
	reset_format: ResetFormat = ResetFormat.SECONDS_FROM_NOW
	# Requests kept in hand: once the window has this few left, the next request waits for the reset.
	reserve: int = 1
	window_seconds: float | None = None

	@staticmethod
	def per_period(requests: int, seconds: float) -> float:
		"""A rate of ``requests`` per ``seconds``, as sources document it (e.g. 300 per 60)."""
		return requests / seconds

	def min_interval(self) -> float:
		return 1.0 / self.requests_per_second if self.requests_per_second else 0.0


class RetryPolicy:
	"""When a response is worth retrying: rate limiting (429) and server errors (5xx), after the server's
	Retry-After when it sends one, else (for 429) the source's documented cool-down, else an exponential
	backoff capped at MAX_BACKOFF_SECONDS. A client sits out a wait of up to MAX_INLINE_WAIT_SECONDS
	inside the request; a longer one, or running out of attempts, pauses the run (RetryLater), so one step
	never outlives its job's time limit and a source that keeps pushing back isn't hammered."""

	MAX_ATTEMPTS = 6
	BACKOFF_BASE_SECONDS = 1.0
	MAX_BACKOFF_SECONDS = 16.0
	MAX_INLINE_WAIT_SECONDS = 30.0
	RETRY_AFTER_HEADER = "retry-after"

	@classmethod
	def wait(
		cls, response: HttpResponse, attempt: int, cooldown_seconds: float | None = None
	) -> float | None:
		"""Seconds to wait before attempt ``attempt + 1``, or None when the response is final."""
		if response.status != HTTP_TOO_MANY_REQUESTS and response.status < HTTP_SERVER_ERROR:
			return None
		retry_after = cls._seconds(response.header(cls.RETRY_AFTER_HEADER))
		if retry_after is not None:
			return retry_after
		if response.status == HTTP_TOO_MANY_REQUESTS and cooldown_seconds is not None:
			return cooldown_seconds
		return cls.backoff(attempt)

	@classmethod
	def backoff(cls, attempt: int) -> float:
		"""The exponential backoff before attempt ``attempt + 1``: 1, 2, 4, 8, 16, 16, … seconds."""
		return min(cls.MAX_BACKOFF_SECONDS, cls.BACKOFF_BASE_SECONDS * (1 << max(0, attempt - 1)))

	@classmethod
	def waits_inline(cls, wait: float, attempt: int) -> bool:
		"""Whether a client sits out ``wait`` itself before attempt ``attempt + 1``, rather than pause the run."""
		return attempt < cls.MAX_ATTEMPTS and wait <= cls.MAX_INLINE_WAIT_SECONDS

	@staticmethod
	def _seconds(value: str | None) -> float | None:
		if value is None:
			return None
		try:
			seconds = float(value)
		except ValueError:
			return None
		return seconds if seconds >= 0 else None


class RequestPacer:
	"""Keeps one client's requests under its source's RateLimit: no closer together than the documented
	rate, and, when a response says the window is down to the reserve, nothing more until it resets."""

	def __init__(self, limit: RateLimit, sleeper: Sleeper) -> None:
		self._limit = limit
		self._sleeper = sleeper
		self._last_request_at: float | None = None
		self._quiet_until: float | None = None

	def before_request(self, label: str) -> None:
		now = self._sleeper.now()
		earliest = (
			now if self._last_request_at is None else self._last_request_at + self._limit.min_interval()
		)
		wait = max(earliest, self._quiet_until or now) - now
		if wait > RetryPolicy.MAX_INLINE_WAIT_SECONDS:
			raise RetryLater(wait, f"{label}'s request allowance is used up until its window resets.")
		if wait > 0:
			self._sleeper.sleep(wait)
		self._last_request_at = self._sleeper.now()

	def after_response(self, response: HttpResponse) -> None:
		if self._limit.remaining_header is None:
			return
		remaining = response.header(self._limit.remaining_header)
		try:
			left = int(float(remaining)) if remaining is not None else None
		except ValueError:
			left = None
		if left is None or left > self._limit.reserve:
			return
		now = self._sleeper.now()
		reset = response.header(self._limit.reset_header) if self._limit.reset_header else None
		seconds = (
			self._limit.window_seconds
			if reset is None
			else self._limit.reset_format.seconds_until(reset, now)
		)
		if seconds is not None:
			self._quiet_until = now + seconds
