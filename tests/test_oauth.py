# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared OAuth 2 layer: the authorization-code client (how each kind of client authenticates at the
token endpoint, rotating and kept refresh tokens, refused grants), the client credentials grant with a
secret or a signed JWT assertion (keys generated here, never committed), and the bearer authorizer."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError
from idempotent_importer.core.http import HttpResponse
from idempotent_importer.core.oauth import (
	ASSERTION_LIFETIME_SECONDS,
	BearerAuthorizer,
	ClientAuth,
	ClientCredentials,
	ClientCredentialsProvider,
	OAuthApp,
	OAuthClient,
	OAuthProvider,
	SigningKey,
)
from tests.support.transport import FakeTransport, scripted


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


def test_a_json_provider_sends_json_and_its_refresh_names_the_redirect_uri() -> None:
	json_provider = replace(provider(ClientAuth.FORM), json_token_requests=True, redirect_on_refresh=True)
	transport = scripted(grant(access_token="a1", refresh_token="r2"))
	saved = Saved()
	flow = OAuthClient(
		json_provider, OAuthApp("id", "secret"), transport, "r1", saved, redirect_uri="https://erp/cb"
	)
	assert flow.refresh() == "a1" and saved == ["r2"]
	request = transport.requests[0]
	assert request.headers["Content-Type"] == "application/json"
	assert json.loads(request.body) == {
		"grant_type": "refresh_token",
		"refresh_token": "r1",
		"redirect_uri": "https://erp/cb",
		"client_id": "id",
		"client_secret": "secret",
	}
	without = OAuthClient(json_provider, OAuthApp("id", "secret"), scripted(), "r1", Saved())
	with pytest.raises(SourceError, match="redirect URI"):
		without.refresh()


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


# -- The client credentials grant ---------------------------------------------------------------------

ASSERTION_URL = "https://1234567.suitetalk.api.netsuite.com/services/rest/auth/oauth2/v1/token"
NOW = 1_800_000_000


def pem_of(key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey) -> str:
	return key.private_bytes(
		serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
	).decode()


def ec_pem(curve: ec.EllipticCurve | None = None) -> str:
	return pem_of(ec.generate_private_key(curve or ec.SECP256R1()))


def credentials_provider(auth: ClientAuth, audience: str | None = None) -> ClientCredentialsProvider:
	return ClientCredentialsProvider(
		"Books", ASSERTION_URL, ("rest_webservices",), "check the certificate", auth, audience=audience
	)


def assertion_grant(transport: FakeTransport, pem: str, audience: str | None = None) -> ClientCredentials:
	return ClientCredentials(
		credentials_provider(ClientAuth.ASSERTION, audience),
		OAuthApp("client-1", ""),
		transport,
		SigningKey("cert-9", pem),
		lambda: NOW,
	)


def test_client_credentials_with_a_secret_send_it_and_the_scopes_in_the_form() -> None:
	transport = scripted(grant(access_token="a1", expires_in=3599))
	tokens = ClientCredentials(credentials_provider(ClientAuth.FORM), OAuthApp("id", "secret"), transport)
	assert tokens.refresh() == "a1"
	assert parse_qs(transport.requests[0].body.decode()) == {
		"grant_type": ["client_credentials"],
		"scope": ["rest_webservices"],
		"client_id": ["id"],
		"client_secret": ["secret"],
	}
	assert "secret" not in repr(tokens)
	with pytest.raises(SourceError, match="client ID and client secret"):
		ClientCredentials(credentials_provider(ClientAuth.FORM), OAuthApp("id", ""), transport)


def test_a_client_assertion_is_signed_by_the_certificate_key_for_the_token_endpoint() -> None:
	key = ec.generate_private_key(ec.SECP256R1())
	transport = scripted(grant(access_token="a1", token_type="Bearer", expires_in=3600))
	tokens = assertion_grant(transport, pem_of(key), audience="https://canonical.example/token")
	assert tokens.refresh() == "a1"
	form = parse_qs(transport.requests[0].body.decode())
	assert form["grant_type"] == ["client_credentials"]
	assert form["client_assertion_type"] == ["urn:ietf:params:oauth:client-assertion-type:jwt-bearer"]
	assert "scope" not in form and "client_secret" not in form
	assertion = form["client_assertion"][0]
	assert jwt.get_unverified_header(assertion) == {"alg": "ES256", "typ": "JWT", "kid": "cert-9"}
	claims = jwt.decode(
		assertion,
		key.public_key(),
		algorithms=["ES256"],
		audience="https://canonical.example/token",
		options={"verify_exp": False, "verify_iat": False},
	)
	assert claims == {
		"iss": "client-1",
		"scope": "rest_webservices",
		"aud": "https://canonical.example/token",
		"iat": NOW,
		"exp": NOW + ASSERTION_LIFETIME_SECONDS,
	}
	assert "a1" not in repr(tokens) and "PRIVATE" not in repr(SigningKey("cert-9", pem_of(key)))


def test_the_assertion_is_addressed_to_the_token_endpoint_unless_told_otherwise() -> None:
	transport = scripted(grant(access_token="a1"))
	assertion_grant(transport, ec_pem()).refresh()
	assertion = parse_qs(transport.requests[0].body.decode())["client_assertion"][0]
	assert jwt.decode(assertion, options={"verify_signature": False})["aud"] == ASSERTION_URL


@pytest.mark.parametrize(
	("key", "algorithm"),
	[
		(lambda: ec_pem(ec.SECP384R1()), "ES384"),
		(lambda: ec_pem(ec.SECP521R1()), "ES512"),
		(lambda: pem_of(rsa.generate_private_key(65537, 2048)), "PS256"),
	],
)
def test_each_key_type_signs_with_its_algorithm(key: Callable[[], str], algorithm: str) -> None:
	assert SigningKey("k", key()).algorithm() == algorithm


def test_a_key_pasted_without_its_line_breaks_still_signs() -> None:
	pem = ec_pem()
	flattened = " ".join(pem.split("\n"))
	assert SigningKey("k", flattened).text() == pem
	assert SigningKey("k", pem).text() == pem


def test_unusable_keys_and_missing_settings_are_refused_before_any_request() -> None:
	with pytest.raises(SourceError, match="not an unencrypted PEM key"):
		SigningKey("k", "not a key").algorithm()
	with pytest.raises(SourceError, match="RSA, or EC"):
		SigningKey("k", ec_pem(ec.SECP256K1())).algorithm()
	transport = scripted()
	for app, key in ((OAuthApp("", ""), SigningKey("k", ec_pem())), (OAuthApp("c", ""), SigningKey("", "x"))):
		with pytest.raises(SourceError, match="certificate ID and its private key"):
			ClientCredentials(credentials_provider(ClientAuth.ASSERTION), app, transport, key)
	with pytest.raises(SourceError, match="certificate ID and its private key"):
		ClientCredentials(credentials_provider(ClientAuth.ASSERTION), OAuthApp("c", ""), transport)
	assert transport.requests == []


def test_a_refused_assertion_stops_the_run_and_rate_limiting_pauses_it() -> None:
	refused = scripted(HttpResponse(401, b'{"error":"invalid_client"}'))
	with pytest.raises(CredentialsRejected, match="check the certificate"):
		assertion_grant(refused, ec_pem()).refresh()
	with pytest.raises(RetryLater):
		assertion_grant(scripted(HttpResponse(429, b"")), ec_pem()).refresh()
	with pytest.raises(SourceError, match="without tokens"):
		assertion_grant(scripted(grant(token_type="Bearer")), ec_pem()).refresh()


def test_the_bearer_authorizer_serves_a_client_credentials_grant() -> None:
	transport = scripted(grant(access_token="a1"))
	authorizer = BearerAuthorizer(assertion_grant(transport, ec_pem()))
	assert authorizer.headers() == {"Authorization": "Bearer a1"}
