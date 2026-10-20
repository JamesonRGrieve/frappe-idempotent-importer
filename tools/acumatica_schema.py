# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes tests/schemas/acumatica.json: the contract-based REST API's Default endpoint (version 24.200.001)
as the adapter reads it. Acumatica publishes the endpoint's OpenAPI description only from each instance
(``/entity/Default/24.200.001/swagger.json``); its official REST client
(github.com/Acumatica/AcumaticaRESTAPIClientForCSharp, the commit pinned below) carries the endpoint's
entities generated from that description (``Endpoints/Acumatica.Default_24.200.001/Model``), so each entity's
fields are read from those classes, every one typed as the API sends it: a field is ``{"value": …}`` (a
string, number, integer, boolean or date-time string), a linked entity an object, a detail list an array;
every entity carries ``id``, ``rowNumber``, ``note``, ``custom``, ``files`` and ``_links``. A list answers a
JSON array of entities, one entity by id the entity.

``python -m tools.acumatica_schema`` fetches the classes and rewrites the schema."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "tests" / "schemas" / "acumatica.json"
COMMIT = "bc657f6f8a703057ed8d623fcab48617ba6180f2"
ENDPOINT = "Default"
VERSION = "24.200.001"
MODELS = (
	f"https://raw.githubusercontent.com/Acumatica/AcumaticaRESTAPIClientForCSharp/{COMMIT}/Endpoints/"
	f"Acumatica.{ENDPOINT}_{VERSION}/Model/"
)
FETCH_TIMEOUT_SECONDS = 60
API = f"/entity/{ENDPOINT}/{VERSION}"
# The top-level entities the adapter reads; their linked and detail entities are followed from them.
ENTITIES = (
	"Account",
	"Subaccount",
	"Warehouse",
	"ItemClass",
	"StockItem",
	"NonStockItem",
	"Customer",
	"Vendor",
	"Lead",
	"Opportunity",
	"Employee",
	"Project",
	"SalesOrder",
	"PurchaseOrder",
	"Invoice",
	"CashSale",
	"Bill",
	"Payment",
	"Check",
	"JournalTransaction",
	"Ledger",
)
PROPERTY = re.compile(r"^\s*public\s+([A-Za-z0-9_<>]+)\??\s+([A-Za-z0-9_]+)\s*\{\s*get;\s*set;\s*\}", re.M)
LIST = re.compile(r"^List<([A-Za-z0-9_]+)>$")
VALUE_TYPES: dict[str, Any] = {
	"StringValue": "string",
	"StringSingleSelectValue": "string",
	"StringMultiSelectValue": "string",
	"GuidValue": "string",
	"DateTimeValue": "string",
	"DateOnlyValue": "string",
	"DecimalValue": "number",
	"DoubleValue": "number",
	"IntValue": "integer",
	"IntSingleSelectValue": "integer",
	"ShortValue": "integer",
	"LongValue": "integer",
	"ByteValue": "integer",
	"BooleanValue": "boolean",
}
REF = "#/components/schemas/"
LINKS: dict[str, Any] = {"type": "object"}
ENTITY_BASE: dict[str, Any] = {
	"id": {"type": "string"},
	"rowNumber": {"type": "integer"},
	"note": {"$ref": f"{REF}StringValue"},
	"custom": {"type": "object"},
	"files": {"type": "array", "items": {"type": "object"}},
	"_links": LINKS,
	"error": {"type": "string"},
}


def value_schema(json_type: str) -> dict[str, Any]:
	return {
		"type": "object",
		"properties": {"value": {"type": [json_type, "null"]}, "error": {"type": "string"}},
		"additionalProperties": False,
	}


def fetch(name: str) -> str:
	response = requests.get(MODELS + name + ".cs", timeout=FETCH_TIMEOUT_SECONDS)
	response.raise_for_status()
	return response.text


def entity_schemas(text_of: Any = fetch) -> dict[str, Any]:
	"""Every reachable entity's schema, following linked and detail entities from ENTITIES."""
	schemas: dict[str, Any] = {name: value_schema(json_type) for name, json_type in VALUE_TYPES.items()}
	pending = list(ENTITIES)
	while pending:
		name = pending.pop(0)
		if name in schemas:
			continue
		properties: dict[str, Any] = dict(ENTITY_BASE)
		for type_, field in PROPERTY.findall(text_of(name)):
			listed = LIST.match(type_)
			target = listed.group(1) if listed else type_
			if target in ("Dictionary", "Links") or field in (
				"Custom",
				"Files",
				"Links",
				"Delete",
				"Error",
				"ID",
				"Note",
			):
				continue
			if target not in VALUE_TYPES and target not in schemas and target not in pending:
				pending.append(target)
			reference = {"$ref": REF + target}
			properties[field] = {"type": "array", "items": reference} if listed else reference
		schemas[name] = {"type": "object", "properties": properties, "additionalProperties": False}
	return schemas


def document(schemas: dict[str, Any]) -> dict[str, Any]:
	paths: dict[str, Any] = {}
	for name in ENTITIES:
		listing = {"type": "array", "items": {"$ref": REF + name}}
		paths[f"{API}/{name}"] = {"get": {"responses": {"200": ok(listing)}}}
		paths[f"{API}/{name}/{{id}}"] = {"get": {"responses": {"200": ok({"$ref": REF + name})}}}
	return {
		"openapi": "3.1.0",
		"info": {
			"title": f"Acumatica contract-based REST API, {ENDPOINT} {VERSION} (the adapter's entities)",
			"version": VERSION,
			"description": f"Transcribed by tools/acumatica_schema.py from {MODELS} (commit {COMMIT}).",
		},
		"paths": paths,
		"components": {"schemas": schemas},
	}


def ok(schema: dict[str, Any]) -> dict[str, Any]:
	return {"description": "OK", "content": {"application/json": {"schema": schema}}}


def main() -> int:
	SCHEMA.write_text(json.dumps(document(entity_schemas()), indent="\t", sort_keys=True) + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
