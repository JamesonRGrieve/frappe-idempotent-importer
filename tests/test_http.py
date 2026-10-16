# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lockout safety at the HTTP boundary: retrying, pacing under documented limits, and the REST client."""

from __future__ import annotations

import pytest

from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError
from idempotent_importer.core.http import HttpResponse, RateLimit, RequestPacer, ResetFormat, RetryPolicy
from idempotent_importer.core.rest import (
	LIST_KEY,
	BasicAuthorizer,
	RequestAuthorizer,
	RestClient,
	query_string,
)
from tests.support.transport import RecordingSleeper, scripted

JSON_OK = HttpResponse(200, b'{"ok": true}')


def response(status: int, body: bytes = b"{}", **headers: str) -> HttpResponse:
	return HttpResponse(status, body, {k.replace("_", "-").lower(): v for k, v in headers.items()})


class TestRetryPolicy:
	def test_final_responses_are_not_retried(self) -> None:
		assert RetryPolicy.wait(response(200), 1) is None
		assert RetryPolicy.wait(response(404), 1) is None
		assert RetryPolicy.wait(response(401), 1) is None

	def test_retry_after_wins(self) -> None:
		assert RetryPolicy.wait(response(429, retry_after="7"), 1, cooldown_seconds=60) == 7
		assert RetryPolicy.wait(response(503, retry_after="2"), 3) == 2

	def test_cooldown_for_a_bare_429(self) -> None:
		assert RetryPolicy.wait(response(429), 1, cooldown_seconds=60) == 60

	def test_backoff_doubles_to_sixteen_seconds(self) -> None:
		assert [RetryPolicy.backoff(attempt) for attempt in range(1, 8)] == [1, 2, 4, 8, 16, 16, 16]
		assert RetryPolicy.wait(response(500), 3) == 4

	def test_inline_waits_are_short_and_few(self) -> None:
		assert RetryPolicy.waits_inline(30, 1)
		assert not RetryPolicy.waits_inline(31, 1)
		assert not RetryPolicy.waits_inline(1, RetryPolicy.MAX_ATTEMPTS)


class TestResetFormat:
	def test_formats(self) -> None:
		assert ResetFormat.SECONDS_FROM_NOW.seconds_until("5", 100) == 5
		assert ResetFormat.MILLISECONDS_FROM_NOW.seconds_until("1500", 100) == 1.5
		assert ResetFormat.EPOCH_SECONDS.seconds_until("130", 100) == 30
		assert ResetFormat.EPOCH_SECONDS.seconds_until("90", 100) == 0
		assert ResetFormat.SECONDS_FROM_NOW.seconds_until("soon", 100) is None


class TestRequestPacer:
	def test_keeps_requests_apart_at_the_documented_rate(self) -> None:
		sleeper = RecordingSleeper()
		pacer = RequestPacer(RateLimit(requests_per_second=RateLimit.per_period(2, 1)), sleeper)
		pacer.before_request("Source")
		pacer.before_request("Source")
		assert sleeper.waits == [0.5]

	def test_waits_for_the_window_when_down_to_the_reserve(self) -> None:
		sleeper = RecordingSleeper()
		limit = RateLimit(
			remaining_header="X-Left", reset_header="X-Reset", reset_format=ResetFormat.MILLISECONDS_FROM_NOW
		)
		pacer = RequestPacer(limit, sleeper)
		pacer.before_request("Source")
		pacer.after_response(response(200, x_left="1", x_reset="4000"))
		pacer.before_request("Source")
		assert sleeper.waits == [4.0]

	def test_a_long_wait_pauses_the_run(self) -> None:
		sleeper = RecordingSleeper()
		pacer = RequestPacer(RateLimit(remaining_header="X-Left", window_seconds=3600), sleeper)
		pacer.before_request("Source")
		pacer.after_response(response(200, x_left="0"))
		with pytest.raises(RetryLater) as paused:
			pacer.before_request("Source")
		assert paused.value.seconds == 3600

	def test_plenty_left_means_no_wait(self) -> None:
		sleeper = RecordingSleeper()
		pacer = RequestPacer(RateLimit(remaining_header="X-Left", window_seconds=60), sleeper)
		pacer.after_response(response(200, x_left="50"))
		pacer.before_request("Source")
		assert sleeper.waits == []


class RenewingAuthorizer:
	def __init__(self) -> None:
		self.renewals = 0

	def headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer token-{self.renewals}"}

	def renew(self) -> bool:
		self.renewals += 1
		return True


def client(
	*responses: HttpResponse, authorizer: RequestAuthorizer | None = None
) -> tuple[RestClient, RecordingSleeper]:
	sleeper = RecordingSleeper()
	transport = scripted(*responses)
	auth = authorizer or BasicAuthorizer("user", "secret", "not configured")
	return RestClient("Source", "https://api.example", auth, transport, sleeper), sleeper


class TestRestClient:
	def test_objects_lists_and_empty_answers(self) -> None:
		rest, _ = client(JSON_OK)
		result = rest.get("/thing")
		assert result is not None
		assert result.node.flag("ok")
		listed, _ = client(HttpResponse(200, b"[1, 2]", {"x-wp-totalpages": "3"}))
		page = listed.get("/things")
		assert page is not None
		assert page.node.get(LIST_KEY) == [1, 2]
		assert page.header("X-WP-TotalPages") == "3"
		empty, _ = client(response(204, b""))
		nothing = empty.get("/things")
		assert nothing is not None
		assert nothing.node.get(LIST_KEY) == []
		missing, _ = client(response(404))
		assert missing.get("/gone") is None

	def test_rejected_credentials_stop(self) -> None:
		rest, _ = client(response(401))
		with pytest.raises(CredentialsRejected, match="HTTP 401"):
			rest.get("/thing")
		forbidden, _ = client(response(403))
		with pytest.raises(CredentialsRejected, match="HTTP 403"):
			forbidden.get("/thing")

	def test_a_token_is_renewed_once(self) -> None:
		authorizer = RenewingAuthorizer()
		rest, _ = client(response(401), JSON_OK, authorizer=authorizer)
		assert rest.get("/thing") is not None
		assert authorizer.renewals == 1

	def test_rate_limits_are_retried_then_pause_the_run(self) -> None:
		rest, sleeper = client(response(429), response(500), JSON_OK)
		assert rest.get("/thing") is not None
		assert sleeper.waits == [1, 2]
		stubborn, sleeper = client(response(429, retry_after="120"))
		with pytest.raises(RetryLater) as paused:
			stubborn.get("/thing")
		assert paused.value.seconds == 120
		assert sleeper.waits == []

	def test_errors_and_garbage(self) -> None:
		rest, _ = client(response(400))
		with pytest.raises(SourceError, match="HTTP 400"):
			rest.get("/thing")
		garbage, _ = client(HttpResponse(200, b"<html>"))
		with pytest.raises(SourceError, match="isn't JSON"):
			garbage.get("/thing")

	def test_credentials_never_reach_errors_or_reprs(self) -> None:
		authorizer = BasicAuthorizer("user", "s3cret", "not configured")
		assert "s3cret" not in repr(authorizer)
		rest, _ = client(response(401), authorizer=authorizer)
		with pytest.raises(CredentialsRejected) as rejected:
			rest.get("/thing")
		assert "s3cret" not in str(rejected.value)

	def test_unconfigured_credentials(self) -> None:
		with pytest.raises(SourceError, match="not configured"):
			BasicAuthorizer("", "secret", "not configured")

	def test_query_strings(self) -> None:
		assert query_string({"a": "x y", "b": True, "c": 2, "d": ["1", "2"]}) == "?a=x%20y&b=true&c=2&d=1&d=2"
		assert query_string({}) == ""
