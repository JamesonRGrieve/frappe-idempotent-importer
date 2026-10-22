# SPDX-License-Identifier: AGPL-3.0-or-later
"""The strict OpenAPI / Swagger response validator the REST fixtures are proven with (tools/openapi.py), and
the pinned spec catalog (tools/specs.py)."""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

import pytest

from tests.support.fixtures import FIXTURES, rest_fixtures, states
from tools.openapi import SWAGGER_REF_PREFIX, ApiSpec, OpenApiValidator
from tools.proofs import invalid_source_fixtures
from tools.specs import SpecDocument, XsdDocument, catalog, corrected, for_source, sources

SCHEMAS: dict[str, dict[str, Any]] = {
	"Product": {
		"type": "object",
		"required": ["id"],
		"properties": {
			"id": {"type": "integer"},
			"name": {"type": "string", "nullable": True},
			"kind": {"type": "string", "enum": ["a", "b"]},
			"price": {"type": "number", "minimum": 0, "maximum": 100},
			"tags": {"type": "array", "items": {"type": "string"}},
			"meta": {"type": "object", "additionalProperties": {"type": "string"}},
			"either": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
		},
	},
	"Wrapped": {
		"allOf": [{"$ref": "#/components/schemas/Product"}, {"properties": {"extra": {"type": "boolean"}}}]
	},
}


def validator(complete: bool = False) -> OpenApiValidator:
	return OpenApiValidator(SCHEMAS, "#/components/schemas/", complete)


def product(**fields: object) -> dict[str, object]:
	return {"id": 1, **fields}


@pytest.mark.parametrize(
	("value", "error"),
	[
		(product(), None),
		(product(name=None, kind="a", price=5, tags=["x"], meta={"k": "v"}, either="s"), None),
		({"name": "x"}, "$.id is missing"),
		(product(color="red"), "$.color is not in the spec"),
		(product(kind="c"), "$.kind is not one of"),
		(product(price=101), "$.price is above 100"),
		(product(price=-1), "$.price is below 0"),
		(product(tags=[1]), "$.tags[0] should be string"),
		(product(meta={"k": 1}), "$.meta.k should be string"),
		(product(either=1.5), "$.either matches no oneOf/anyOf member"),
		(product(id=True), "$.id should be integer"),
		(product(id=None), "$.id is null but not nullable"),
	],
)
def test_a_response_matches_its_schema_strictly(value: object, error: str | None) -> None:
	errors = validator().validate(value, {"$ref": "#/components/schemas/Product"})
	if error is None:
		assert errors == []
	else:
		assert any(e.startswith(error) for e in errors), errors


def test_optional_properties_may_be_null_unless_objects_are_complete() -> None:
	schema = {"$ref": "#/components/schemas/Product"}
	assert validator().validate(product(kind=None), schema) == []
	complete = validator(complete=True).validate(product(kind=None), schema)
	assert "$.kind is null but not nullable" in complete
	assert "$.price is missing" in complete


def test_all_of_merges_its_members() -> None:
	schema = {"$ref": "#/components/schemas/Wrapped"}
	assert validator().validate(product(extra=True), schema) == []
	assert validator().validate({"extra": True}, schema) == ["$.id is missing"]


def test_an_empty_list_is_an_empty_object() -> None:
	assert validator().validate([], {"type": "object", "properties": {}}) == []


def test_operations_are_found_by_path_after_the_server_prefix() -> None:
	document = {
		"swagger": "2.0",
		"definitions": {"Thing": {"type": "object"}},
		"paths": {
			"/V1/things/{id}": {"get": {"responses": {"200": {"schema": {"$ref": "#/definitions/Thing"}}}}}
		},
	}
	spec = ApiSpec.of(document, "/rest/[^/]+")
	assert spec.ref_prefix == SWAGGER_REF_PREFIX
	assert spec.response_schema("GET", "/rest/default/V1/things/7") == {"$ref": "#/definitions/Thing"}
	assert spec.response_schema("POST", "/rest/default/V1/things/7") is None
	assert spec.response_schema("GET", "/V1/things/7") is None


def test_a_correction_replaces_what_it_names_and_only_that() -> None:
	document = {"components": {"schemas": {"Txn": {"properties": {"status": {"type": "object"}}}}}}
	corrected(document, "/components/schemas/Txn/properties/status", {"type": "string"})
	assert document["components"]["schemas"]["Txn"]["properties"]["status"] == {"type": "string"}
	with pytest.raises(KeyError, match="no longer applies"):
		corrected(document, "/components/schemas/Txn/properties/state", {"type": "string"})


def test_a_shared_response_is_followed() -> None:
	document = {
		"openapi": "3.0.0",
		"components": {
			"schemas": {"Thing": {"type": "object"}},
			"responses": {
				"ThingMany": {
					"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Thing"}}}
				}
			},
		},
		"paths": {"/things": {"get": {"responses": {"200": {"$ref": "#/components/responses/ThingMany"}}}}},
	}
	spec = ApiSpec.of(document, "")
	assert spec.response_schema("GET", "/things") == {"$ref": "#/components/schemas/Thing"}


def test_the_catalog_pins_every_fetched_document_and_names_its_sources() -> None:
	assert set(sources()) == {
		"etsy",
		"square_online",
		"square_stored_value",
		"bigcommerce",
		"squarespace",
		"wix",
		"magento",
		"lightspeed",
		"ecwid",
		"odoo",
		"dynamics_business_central",
		"dynamics_sales",
		"dynamics_finance_operations",
		"quickbooks_online",
		"xero",
		"zoho_books",
		"zoho_inventory",
		"netsuite",
		"sap_business_one",
		"acumatica",
		"agendrix",
		"highlevel",
		"peoplesoft",
		"hubspot",
		"salesforce",
		"pipedrive",
		"zoho_crm",
		"webflow",
		"freshbooks",
	}
	commit = re.compile(r"/[0-9a-f]{40}/")
	for document in catalog():
		if isinstance(document, XsdDocument):
			assert commit.search(document.base_url), document.name
		else:
			# A committed transcription, a pinned hash, or a URL naming a commit or version.
			assert document.committed or document.sha256 or "/" in document.url, document.name
	assert [document.name for document in for_source("wix")] == ["rest-reference"]


def test_a_document_inside_an_archive_is_its_member() -> None:
	archive = io.BytesIO()
	with zipfile.ZipFile(archive, "w") as zipped:
		zipped.writestr("items.yml", "openapi: 3.0.0\n")
	document = next(d for d in catalog() if isinstance(d, SpecDocument) and d.member == "items.yml")
	assert document.document(archive.getvalue()) == b"openapi: 3.0.0\n"
	assert document.path().name == "openapi-all.zip"


def test_a_document_changed_upstream_fails_loudly() -> None:
	pinned = next(
		document for document in catalog() if isinstance(document, SpecDocument) and document.sha256
	)
	with pytest.raises(RuntimeError, match="has changed"):
		pinned.verify(b"{}")


@pytest.mark.parametrize("source", ["wix", "lightspeed", "ecwid"])
def test_the_committed_schemas_prove_their_fixtures(source: str) -> None:
	assert list(invalid_source_fixtures(source)) == []
	assert sum(len(rest_fixtures(source, state)) for state in states(source)) > 0
	assert (FIXTURES / source).is_dir()
