# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes tests/schemas/dynamics_business_central.json: the Business Central API v2.0 answers the adapter
reads, transcribed from Microsoft's API (v2.0) reference. Microsoft publishes no OpenAPI document for v2.0
(only v1.0's: learn.microsoft.com/dynamics365/business-central/dev-itpro/api-reference/v2.0/dynamics-open-api),
so each entity's properties come from the "Properties" table of its resource-type page (each schema cites the
page and the docs commit it was read at), as OData v4 sends them: a list is ``{"@odata.context", "value": [...],
"@odata.nextLink"?}`` and every entity carries its ``@odata.etag``.

``python -m tools.bc_schema`` fetches the pages and rewrites the schema."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "tests" / "schemas" / "dynamics_business_central.json"
PAGES = (
	"https://learn.microsoft.com/en-us/dynamics365/business-central/dev-itpro/api-reference/v2.0/resources/"
)
FETCH_TIMEOUT_SECONDS = 60
API = "/v2.0/{tenant}/{environment}/api/v2.0"
# Each entity the adapter reads: its resource page, and the entity set it is listed from (under a company,
# or for a document's lines under the document).
ENTITIES: dict[str, str] = {
	"company": "companies",
	"companyInformation": "companyInformation",
	"account": "accounts",
	"dimension": "dimensions",
	"dimensionValue": "dimensionValues",
	"location": "locations",
	"itemCategory": "itemCategories",
	"item": "items",
	"itemVariant": "itemVariants",
	"customer": "customers",
	"vendor": "vendors",
	"contact": "contacts",
	"employee": "employees",
	"project": "projects",
	"opportunity": "opportunities",
	"salesQuote": "salesQuotes",
	"salesQuoteLine": "salesQuoteLines",
	"salesOrder": "salesOrders",
	"salesOrderLine": "salesOrderLines",
	"salesInvoice": "salesInvoices",
	"salesInvoiceLine": "salesInvoiceLines",
	"salesCreditMemo": "salesCreditMemos",
	"salesCreditMemoLine": "salesCreditMemoLines",
	"purchaseOrder": "purchaseOrders",
	"purchaseOrderLine": "purchaseOrderLines",
	"purchaseInvoice": "purchaseInvoices",
	"purchaseInvoiceLine": "purchaseInvoiceLines",
	"purchaseCreditMemo": "purchaseCreditMemos",
	"purchaseCreditMemoLine": "purchaseCreditMemoLines",
}
# The documents whose lines arrive expanded with them ($expand), by their lines' navigation property.
LINES = {
	"salesQuote": "salesQuoteLines",
	"salesOrder": "salesOrderLines",
	"salesInvoice": "salesInvoiceLines",
	"salesCreditMemo": "salesCreditMemoLines",
	"purchaseOrder": "purchaseOrderLines",
	"purchaseInvoice": "purchaseInvoiceLines",
	"purchaseCreditMemo": "purchaseCreditMemoLines",
	"item": "itemVariants",
}
TYPES: dict[str, dict[str, Any]] = {
	"guid": {"type": "string"},
	"string": {"type": "string"},
	"decimal": {"type": "number"},
	"numeric": {"type": "number"},
	"integer": {"type": "integer"},
	"int": {"type": "integer"},
	"int32": {"type": "integer"},
	"int64": {"type": "integer"},
	"boolean": {"type": "boolean"},
	"date": {"type": "string"},
	"datetime": {"type": "string"},
	"datetimeoffset": {"type": "string"},
	"stream": {"type": "string"},
}
HEADER = ["Property", "Type"]
TABLE = re.compile(r"<table.*?</table>", re.S)
ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
COMMIT = re.compile(r'<meta name="git_commit_id" content="([0-9a-f]+)"')


def cells(row: str) -> list[str]:
	return [html.unescape(re.sub(r"<[^>]+>", "", cell)).strip() for cell in CELL.findall(row)]


def properties(page: str) -> dict[str, str]:
	"""The resource page's Properties table: each property's documented type."""
	for table in TABLE.findall(page):
		rows = [cells(row) for row in ROW.findall(table)]
		if rows and rows[0][: len(HEADER)] == HEADER:
			return {row[0]: row[1] for row in rows[1:] if len(row) >= len(HEADER) and row[0]}
	raise LookupError("the page has no Properties table")


def json_type(documented: str) -> dict[str, Any]:
	"""A documented property type as JSON; an enum (``NAV.<enum>``) is its value's name. OData sends a
	property with no value as null."""
	name = documented.strip().lower()
	base = TYPES.get(name) or ({"type": "string"} if name.startswith("nav.") else None)
	if base is None:
		raise ValueError(f"no JSON type for {documented}")
	return {"type": [base["type"], "null"]}


def entity_schema(entity: str) -> dict[str, Any]:
	url = PAGES + f"dynamics_{entity.lower()}"
	response = requests.get(url, timeout=FETCH_TIMEOUT_SECONDS)
	response.raise_for_status()
	commit = COMMIT.search(response.text)
	documented = properties(response.text)
	schema: dict[str, Any] = {
		"type": "object",
		"description": f"Transcribed from {url}" + (f" (docs commit {commit.group(1)})" if commit else ""),
		"properties": {
			"@odata.etag": {"type": "string"},
			**{name: json_type(kind) for name, kind in documented.items()},
		},
		"required": ["id"],
	}
	return schema


def list_of(entity: str) -> dict[str, Any]:
	return {
		"type": "object",
		"properties": {
			"@odata.context": {"type": "string"},
			"@odata.nextLink": {"type": "string"},
			"value": {"type": "array", "items": {"$ref": f"#/components/schemas/{entity}"}},
		},
		"required": ["value"],
	}


def operation(ref: str) -> dict[str, Any]:
	return {
		"get": {
			"responses": {
				"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": ref}}}}
			}
		}
	}


def main() -> int:
	schemas: dict[str, Any] = {}
	paths: dict[str, Any] = {}
	for entity in ENTITIES:
		schemas[entity] = entity_schema(entity)
	for header, lines in LINES.items():
		line = next(name for name, entity_set in ENTITIES.items() if entity_set == lines)
		schemas[header]["properties"][lines] = {
			"type": "array",
			"items": {"$ref": f"#/components/schemas/{line}"},
		}
	for entity, entity_set in ENTITIES.items():
		schemas[f"{entity}List"] = list_of(entity)
		under = "" if entity == "company" else "/companies({company})"
		paths[f"{API}{under}/{entity_set}"] = operation(f"#/components/schemas/{entity}List")
	paths[f"{API}/companies({{company}})/dimensions({{dimension}})/dimensionValues"] = operation(
		"#/components/schemas/dimensionValueList"
	)
	document: dict[str, Any] = {
		"openapi": "3.1.0",
		"info": {
			"title": "Business Central API v2.0 answers read by frappe-idempotent-importer",
			"version": "v2.0",
			"description": (
				"Microsoft publishes no OpenAPI document for API v2.0. Transcribed by tools/bc_schema.py from the "
				"Properties tables of the API (v2.0) resource-type pages (each schema cites its page): only the "
				"entities the Business Central adapter reads."
			),
		},
		"paths": paths,
		"components": {"schemas": schemas},
	}
	SCHEMA.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
