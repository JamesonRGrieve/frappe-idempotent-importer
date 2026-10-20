# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes tests/schemas/dynamics_sales.json: the Dataverse Web API answers the Dynamics 365 Sales adapter
reads, transcribed from Microsoft's table/entity reference. Dataverse publishes no static OpenAPI document
(each environment describes itself at ``/api/data/v9.2/$metadata``), so each table's columns come from its
reference page (each schema cites it): a column's type gives its JSON as the Web API sends it (a lookup as
``_<column>_value``, a GUID, with its ``@Microsoft.Dynamics.CRM.lookuplogicalname`` annotation when asked
for; a choice as its integer value, one of the page's options; money and decimals as numbers), and a list is
``{"@odata.context", "value": [...], "@odata.nextLink"?}``
(https://learn.microsoft.com/power-apps/developer/data-platform/webapi/query-data-web-api).

``python -m tools.dataverse_schema`` fetches the pages and rewrites the schema."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

import requests

from idempotent_importer.sources.dynamics_sales.reader import API_PATH, LINE_COLUMNS, TABLES

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "tests" / "schemas" / "dynamics_sales.json"
CORE = "https://learn.microsoft.com/en-us/power-apps/developer/data-platform/reference/entities/"
SALES = "https://learn.microsoft.com/en-us/dynamics365/developer/reference/entities/"
FETCH_TIMEOUT_SECONDS = 60
API = API_PATH
LOOKUP_ANNOTATION = "@Microsoft.Dynamics.CRM.lookuplogicalname"
# Where each table the adapter reads is documented: Dataverse's own tables, or Dynamics 365's.
CORE_TABLES = frozenset({"account", "contact", "transactioncurrency", "organization"})
# The tables read besides the adapter's kinds (to know the currencies), and their columns.
SUPPORT: dict[str, tuple[str, tuple[str, ...]]] = {
	"transactioncurrency": (
		"transactioncurrencies",
		("transactioncurrencyid", "isocurrencycode", "currencyname"),
	),
	"organization": ("organizations", ("organizationid", "name", "_basecurrencyid_value")),
}
# Each document's lines: their table and key.
LINES = {
	"quote_details": "quotedetail",
	"order_details": "salesorderdetail",
	"invoice_details": "invoicedetail",
}
# An account's contacts, as the adapter expands them.
ACCOUNT_CONTACTS = "contact_customer_accounts"
ACCOUNT_CONTACT_COLUMNS = ("contactid", "fullname", "jobtitle", "emailaddress1", "telephone1", "statecode")
JSON_TYPES: dict[str, Any] = {
	"String": {"type": ["string", "null"]},
	"Memo": {"type": ["string", "null"]},
	"Uniqueidentifier": {"type": ["string", "null"]},
	"DateTime": {"type": ["string", "null"]},
	"Money": {"type": ["number", "null"]},
	"Decimal": {"type": ["number", "null"]},
	"Double": {"type": ["number", "null"]},
	"Integer": {"type": ["integer", "null"]},
	"BigInt": {"type": ["integer", "null"]},
	"Boolean": {"type": ["boolean", "null"]},
}
LOOKUPS = frozenset({"Lookup", "Customer", "Owner"})
CHOICES = frozenset({"Picklist", "State", "Status"})
TABLE = re.compile(r"<table.*?</table>", re.S)
ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
CELL = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
COMMIT = re.compile(r'<meta name="git_commit_id" content="([0-9a-f]+)"')
VALUE_HEADER = ["Value", "Details"]


def cells(table: str) -> list[list[str]]:
	return [
		[" ".join(html.unescape(re.sub(r"<[^>]+>", " ", cell)).split()) for cell in CELL.findall(row)]
		for row in ROW.findall(table)
	]


def columns(page: str) -> dict[str, tuple[str, list[int]]]:
	"""Each column the page documents: its type and, for a choice, its option values."""
	found: dict[str, tuple[str, list[int]]] = {}
	tables = TABLE.findall(page)
	for index, table in enumerate(tables):
		rows = {row[0]: row[1] for row in cells(table) if len(row) >= len(VALUE_HEADER)}
		if "LogicalName" not in rows or "Type" not in rows:
			continue
		kind = rows["Type"]
		options: list[int] = []
		if kind in CHOICES and index + 1 < len(tables):
			listed = cells(tables[index + 1])
			if listed and listed[0] == VALUE_HEADER:
				options = [int(row[0]) for row in listed[1:] if row and row[0].lstrip("-").isdigit()]
		found[rows["LogicalName"]] = (kind, options)
	return found


def fetch(url: str) -> tuple[str, str | None]:
	response = requests.get(url, timeout=FETCH_TIMEOUT_SECONDS)
	response.raise_for_status()
	commit = COMMIT.search(response.text)
	return response.text, commit.group(1) if commit else None


def table_schema(name: str, base: str, wanted: tuple[str, ...]) -> dict[str, Any]:
	url = base + name
	page, commit = fetch(url)
	documented = columns(page)
	properties: dict[str, Any] = {"@odata.etag": {"type": "string"}}
	for column in wanted:
		if column not in documented:
			raise LookupError(f"{name}.{column} isn't on {url}")
		kind, options = documented[column]
		if kind in LOOKUPS:
			properties[f"_{column}_value"] = {"type": ["string", "null"]}
			properties[f"_{column}_value{LOOKUP_ANNOTATION}"] = {"type": "string"}
		elif kind in CHOICES:
			properties[column] = {"enum": [*options, None]} if options else {"type": ["integer", "null"]}
		elif kind in JSON_TYPES:
			properties[column] = JSON_TYPES[kind]
		else:
			raise ValueError(f"{name}.{column} is a {kind}, which the adapter doesn't read")
	return {
		"type": "object",
		"description": f"Transcribed from {url}" + (f" (docs commit {commit})" if commit else ""),
		"properties": properties,
	}


def list_of(ref: str) -> dict[str, Any]:
	return {
		"type": "object",
		"properties": {
			"@odata.context": {"type": "string"},
			"@odata.nextLink": {"type": "string"},
			"value": {"type": "array", "items": {"$ref": ref}},
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


def logical(column: str) -> str:
	"""A selected column's logical name (a lookup is selected as ``_<name>_value``)."""
	return (
		column[1:].removesuffix("_value") if column.startswith("_") and column.endswith("_value") else column
	)


def page_of(table: str) -> str:
	return CORE if table in CORE_TABLES else SALES


def main() -> int:
	schemas: dict[str, Any] = {}
	paths: dict[str, Any] = {}
	tables: dict[str, tuple[str, tuple[str, ...], str | None]] = {
		name: (entity_set, columns, None) for name, (entity_set, columns) in SUPPORT.items()
	}
	for listed in TABLES.values():
		for table in listed:
			tables[table.columns[0].removesuffix("id")] = (table.entity_set, table.columns, table.expand)
	for name, (entity_set, columns, expand) in tables.items():
		schemas[name] = table_schema(name, page_of(name), tuple(logical(column) for column in columns))
		navigation = expand.split("(", 1)[0] if expand else None
		if navigation == ACCOUNT_CONTACTS:
			schemas[f"{name}Contact"] = table_schema("contact", CORE, ACCOUNT_CONTACT_COLUMNS)
			item: dict[str, Any] = {"$ref": f"#/components/schemas/{name}Contact"}
		elif navigation in LINES:
			line = LINES[navigation]
			schemas[line] = table_schema(
				line, SALES, (f"{line}id", *(logical(column) for column in LINE_COLUMNS))
			)
			item = {"$ref": f"#/components/schemas/{line}"}
		if navigation:
			schemas[name]["properties"][navigation] = {"type": "array", "items": item}
		schemas[f"{name}List"] = list_of(f"#/components/schemas/{name}")
		paths[f"{API}/{entity_set}"] = operation(f"#/components/schemas/{name}List")
	document: dict[str, Any] = {
		"openapi": "3.1.0",
		"info": {
			"title": "Dataverse Web API (v9.2) answers read by frappe-idempotent-importer's Dynamics 365 Sales adapter",
			"version": "v9.2",
			"description": (
				"Dataverse publishes no static OpenAPI document. Transcribed by tools/dataverse_schema.py from the "
				"table/entity reference pages (each schema cites its page): only the tables and columns the "
				"Dynamics 365 Sales adapter reads."
			),
		},
		"paths": paths,
		"components": {"schemas": schemas},
	}
	SCHEMA.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
