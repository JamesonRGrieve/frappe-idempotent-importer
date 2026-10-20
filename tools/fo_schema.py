# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes tests/schemas/dynamics_finance_operations.json: the finance and operations data entities the
adapter reads by default, transcribed from Microsoft's entity reference pages (each schema cites its page).
Microsoft publishes no static OpenAPI document (each environment describes itself at ``/data/$metadata``),
and the reference documents only some fields, by their data management names and without types: so each
entity declares its documented fields (any JSON scalar) and allows the others an environment has (an
entity's fields are the customer's to extend). A list is ``{"@odata.context", "value": [...],
"@odata.nextLink"?}``; every record carries its ``dataAreaId`` (the legal entity) and ``@odata.etag``
(learn.microsoft.com/dynamics365/fin-ops-core/dev-itpro/data-entities/odata).

``python -m tools.fo_schema`` fetches the pages and rewrites the schema."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

import requests

from idempotent_importer.sources.dynamics_finance_operations.fieldmap import DOCS
from idempotent_importer.sources.dynamics_finance_operations.reader import DATA_PATH

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "tests" / "schemas" / "dynamics_finance_operations.json"
FETCH_TIMEOUT_SECONDS = 60
# Each documented entity the adapter reads by default: its public collection, and its reference page.
ENTITIES = {
	"CustomersV3": "entity-customers-v3-customerv3",
	"ReleasedProductsV2": "entity-released-products-v2-releasedproductv2",
	"SalesOrderHeadersV2": "entity-sales-order-headers-v2-salesorderheaderv2",
	"SalesOrderLines": "entity-sales-order-lines-v2-salesorderline",
}
FIELD_HEADERS = (["Field", "Description"], ["Field name", "Comments"])
SCALAR: dict[str, Any] = {"type": ["string", "number", "boolean", "null"]}
TABLE = re.compile(r"<table.*?</table>", re.S)
ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
COMMIT = re.compile(r'<meta name="git_commit_id" content="([0-9a-f]+)"')


def cells(row: str) -> list[str]:
	return [" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in CELL.findall(row)]


def fields_of(page: str) -> list[str]:
	"""The documented field names (a row may list several: "PERSONFIRSTNAME PERSONMIDDLENAME …")."""
	for table in TABLE.findall(page):
		rows = [cells(row) for row in ROW.findall(table)]
		header = next((index for index, row in enumerate(rows) if row in FIELD_HEADERS), None)
		if header is not None:
			return [name for row in rows[header + 1 :] if row for name in row[0].split()]
	raise LookupError("the page documents no fields")


def entity_schema(page_name: str) -> dict[str, Any]:
	url = DOCS + page_name
	response = requests.get(url, timeout=FETCH_TIMEOUT_SECONDS)
	response.raise_for_status()
	commit = COMMIT.search(response.text)
	properties: dict[str, Any] = {"@odata.etag": {"type": "string"}, "dataAreaId": {"type": "string"}}
	properties |= {name: SCALAR for name in fields_of(response.text)}
	return {
		"type": "object",
		"description": f"Transcribed from {url}" + (f" (docs commit {commit.group(1)})" if commit else ""),
		"properties": properties,
		"required": ["dataAreaId"],
		"additionalProperties": SCALAR,
	}


def main() -> int:
	schemas: dict[str, Any] = {}
	paths: dict[str, Any] = {}
	for entity, page_name in ENTITIES.items():
		schemas[entity] = entity_schema(page_name)
		schemas[f"{entity}List"] = {
			"type": "object",
			"properties": {
				"@odata.context": {"type": "string"},
				"@odata.nextLink": {"type": "string"},
				"value": {"type": "array", "items": {"$ref": f"#/components/schemas/{entity}"}},
			},
			"required": ["value"],
		}
		ref = f"#/components/schemas/{entity}List"
		paths[f"{DATA_PATH}/{entity}"] = {
			"get": {
				"responses": {
					"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": ref}}}}
				}
			}
		}
	document: dict[str, Any] = {
		"openapi": "3.1.0",
		"info": {
			"title": "Finance and operations data entities read by frappe-idempotent-importer",
			"version": "reference",
			"description": (
				"Microsoft publishes no static OpenAPI document for finance and operations apps. Transcribed by "
				"tools/fo_schema.py from the data entity reference pages (each schema cites its page): the "
				"documented fields of the entities the adapter reads by default."
			),
		},
		"paths": paths,
		"components": {"schemas": schemas},
	}
	SCHEMA.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
