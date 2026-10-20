# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared OAuth 2 authorization-code client: how each kind of client authenticates at the token
endpoint, rotating and kept refresh tokens, refused grants, and the bearer authorizer."""

from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.oauth import (
	BearerAuthorizer,
	ClientAuth,
	OAuthApp,
	OAuthClient,
	OAuthProvider,
)
from tests.support.transport import scripted


def provider(auth: ClientAuth, rotates: bool = True) -> OAuthProvider:
	return OAuthProvider(
		label="Books",
		authorize_url="https://accounts.example/authorize",
		token_url="https://accounts.example/token",
		scopes=("a.read", "b.read"),
		client_auth=auth,
		reconnect="connect the books again",
		rotates_refresh_token=rotates,
		scope_separator=",",
		authorize_params=(("access_type", "offline"),),
	)


def grant(**fields: object) -> HttpResponse:
	return HttpResponse(200, json.dumps(fields).encode())


class Saved(list[str]):
	def __call__(self, token: str) -> None:
		self.append(token)


def test_the_approval_url_names_the_scopes_and_the_providers_parameters() -> None:
	flow = OAuthClient(provider(ClientAuth.BASIC), OAuthApp("id", "secret"), scripted(), "r", Saved())
	query = parse_qs(urlsplit(flow.authorization_url("https://erp/cb", "s1", "v")).query)
	assert query["scope"] == ["a.read,b.read"]
	assert (query["access_type"], query["state"], query["client_id"]) == (["offline"], ["s1"], ["id"])
	assert "code_challenge" not in query


def test_a_confidential_client_authenticates_with_http_basic_and_saves_each_rotated_token() -> None:
	saved = Saved()
	transport = scripted(grant(access_token="a1", refresh_token="r2"))
	flow = OAuthClient(provider(ClientAuth.BASIC), OAuthApp("id", "secret"), transport, "r1", saved)
	assert flow.refresh() == "a1"
	sent = transport.requests[0]
	assert sent.headers["Authorization"] == "Basic " + base64.b64encode(b"id:secret").decode()
	assert parse_qs(sent.body.decode()) == {"grant_type": ["refresh_token"], "refresh_token": ["r1"]}
	assert saved == ["r2"]
	flow.refresh()
	assert parse_qs(transport.requests[1].body.decode())["refresh_token"] == ["r2"]


def test_a_form_client_sends_its_secret_and_a_kept_refresh_token_stays() -> None:
	saved = Saved()
	transport = scripted(grant(access_token="a1", api_domain="https://www.zohoapis.eu", expires_in=3600))
	flow = OAuthClient(
		provider(ClientAuth.FORM, rotates=False), OAuthApp("id", "secret"), transport, "r1", saved
	)
	issued = flow.refresh_grant()
	assert issued.access_token == "a1" and issued.extra["api_domain"] == "https://www.zohoapis.eu"
	form = parse_qs(transport.requests[0].body.decode())
	assert (form["client_id"], form["client_secret"]) == (["id"], ["secret"])
	assert "Authorization" not in transport.requests[0].headers
	assert saved == []
	assert "a1" not in repr(issued) and "secret" not in repr(flow)


def test_the_code_exchange_must_grant_a_refresh_token() -> None:
	flow = OAuthClient(
		provider(ClientAuth.FORM, rotates=False),
		OAuthApp("id", "s"),
		scripted(grant(access_token="a")),
		"",
		Saved(),
	)
	with pytest.raises(SourceError, match="without tokens"):
		flow.exchange("code", "v", "https://erp/cb")


def test_refusals_stop_the_run_and_trouble_pauses_it() -> None:
	app = OAuthApp("id", "s")
	with pytest.raises(CredentialsRejected, match="not connected"):
		OAuthClient(provider(ClientAuth.BASIC), app, scripted(), "", Saved()).refresh()
	refused = scripted(HttpResponse(400, b'{"error":"invalid_grant"}'))
	with pytest.raises(CredentialsRejected, match="connect the books again"):
		OAuthClient(provider(ClientAuth.BASIC), app, refused, "r", Saved()).refresh()
	# Zoho refuses with 200 and an error code.
	with pytest.raises(CredentialsRejected, match="invalid_code"):
		OAuthClient(
			provider(ClientAuth.FORM), app, scripted(grant(error="invalid_code")), "r", Saved()
		).refresh()
	with pytest.raises(RetryLater):
		OAuthClient(provider(ClientAuth.BASIC), app, scripted(HttpResponse(429, b"")), "r", Saved()).refresh()
	with pytest.raises(SourceError, match="unreadable"):
		OAuthClient(
			provider(ClientAuth.BASIC), app, scripted(HttpResponse(200, b"<")), "r", Saved()
		).refresh()


def test_the_bearer_authorizer_fetches_once_and_again_after_a_rejection() -> None:
	transport = scripted(grant(access_token="a1", refresh_token="r2"))
	flow = OAuthClient(provider(ClientAuth.BASIC), OAuthApp("id", "s"), transport, "r1", Saved())
	authorizer = BearerAuthorizer(flow, "Zoho-oauthtoken", {"X-Org": "1"})
	assert authorizer.headers() == {"X-Org": "1", "Authorization": "Zoho-oauthtoken a1"}
	authorizer.headers()
	assert len(transport.requests) == 1
	assert authorizer.renew()
	authorizer.headers()
	assert len(transport.requests) == 2
	assert "a1" not in repr(authorizer)
