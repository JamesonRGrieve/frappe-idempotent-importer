# SPDX-License-Identifier: AGPL-3.0-or-later
"""The published API descriptions the fixtures are proven against: Shopify's Admin GraphQL schema (cached
in .cache/, it is Shopify's and not committed), Wave's public GraphQL schema as pruned in tests/schemas, and the
OpenAPI documents transcribed there."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import requests
from graphql import (
	GraphQLSchema,
	build_client_schema,
	build_schema,
	get_introspection_query,
	print_schema,
)

from idempotent_importer.sources.shopify.queries import API_VERSION

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache"
SHOPIFY_PROXY = "https://shopify.dev/admin-graphql-direct-proxy/"
FETCH_TIMEOUT_SECONDS = 120
OPENAPI_SCHEMAS = ROOT / "tests" / "schemas"


def shopify_schema_path(version: str = API_VERSION) -> Path:
	return CACHE / f"shopify-admin-{version}.graphql"


def fetch_shopify_schema(version: str = API_VERSION) -> Path:
	"""Downloads Shopify's public Admin GraphQL schema for ``version`` (as SDL) into .cache/."""
	response = requests.post(
		SHOPIFY_PROXY + version,
		json={"query": get_introspection_query()},
		timeout=FETCH_TIMEOUT_SECONDS,
	)
	response.raise_for_status()
	payload = response.json()
	if (
		not isinstance(payload, dict)
		or not isinstance(payload.get("data"), dict)
		or "__schema" not in payload["data"]
	):
		raise RuntimeError(f"The schema proxy returned no introspection for {version}.")
	path = shopify_schema_path(version)
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(print_schema(build_client_schema(payload["data"])))
	return path


def shopify_schema(version: str = API_VERSION) -> GraphQLSchema:
	path = shopify_schema_path(version)
	if not path.is_file():
		path = fetch_shopify_schema(version)
	return build_schema(path.read_text())


def wave_schema() -> GraphQLSchema:
	"""Wave's public schema as tools/wave_schema.py pruned it (committed: it is introspected without a token)."""
	document = json.loads((OPENAPI_SCHEMAS / "wave.json").read_text())
	return build_schema("\n".join(document["sdl"]))


def openapi(source: str) -> dict[str, Any]:
	document = json.loads((OPENAPI_SCHEMAS / f"{source}.json").read_text())
	if not isinstance(document, dict):
		raise TypeError(f"{source}.json is not an OpenAPI document")
	return document
