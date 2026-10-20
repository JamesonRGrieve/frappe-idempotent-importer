# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes tests/schemas/odoo.json: the JSON-2 API's answers the Odoo adapter reads, transcribed from Odoo
19's own model definitions. Odoo publishes no OpenAPI document (each database describes its models on its
``/doc`` page), so each field's JSON type comes from its declaration in the odoo/odoo repository at the
pinned commit of the 19.0 branch (``fields.Char`` → a string or ``false``, ``fields.Many2one`` → ``[id,
"name"]`` or ``false``, a selection → one of its values or ``false`` …), and each model's schema cites the
files it came from. The JSON-2 API answers ``search_read`` and ``read`` with a list of records holding
``id`` and the requested fields, and ``fields_get`` with an object of field descriptions
(https://www.odoo.com/documentation/19.0/developer/reference/external_api.html).

``python -m tools.odoo_schema`` fetches the sources and rewrites the schema."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests

from tools.odoo_fixtures import MODELS

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "tests" / "schemas" / "odoo.json"
COMMIT = "0faf35b5ff680a172a73fc22230957c2771d32c9"
RAW = f"https://raw.githubusercontent.com/odoo/odoo/{COMMIT}/"
BLOB = f"https://github.com/odoo/odoo/blob/{COMMIT}/"
DOCS = "https://www.odoo.com/documentation/19.0/developer/reference/external_api.html"
FETCH_TIMEOUT_SECONDS = 60
# Where each model's fields are declared (its own module, and the modules that extend it).
SOURCES: dict[str, tuple[str, ...]] = {
	"res.company": ("odoo/addons/base/models/res_company.py",),
	"res.users": ("odoo/addons/base/models/res_users.py",),
	"res.partner": ("odoo/addons/base/models/res_partner.py", "addons/account/models/partner.py"),
	"account.account": ("addons/account/models/account_account.py",),
	"account.tax.repartition.line": ("addons/account/models/account_tax.py",),
	"account.payment.method.line": ("addons/account/models/account_payment_method.py",),
	"account.analytic.account": ("addons/analytic/models/analytic_account.py",),
	"stock.warehouse": ("addons/stock/models/stock_warehouse.py",),
	"product.category": ("addons/product/models/product_category.py",),
	"product.template": (
		"addons/product/models/product_template.py",
		"addons/stock/models/product.py",
		"addons/website/models/mixins.py",
	),
	"product.product": ("addons/product/models/product_product.py", "addons/stock/models/product.py"),
	"product.template.attribute.value": ("addons/product/models/product_template_attribute_value.py",),
	"hr.department": ("addons/hr/models/hr_department.py",),
	"hr.job": ("addons/hr/models/hr_job.py",),
	"hr.employee": ("addons/hr/models/hr_employee.py", "addons/hr/models/hr_version.py"),
	"project.project": (
		"addons/project/models/project_project.py",
		"addons/analytic/models/analytic_mixin.py",
	),
	"crm.lead": ("addons/crm/models/crm_lead.py",),
	"sale.order": ("addons/sale/models/sale_order.py",),
	"sale.order.line": ("addons/sale/models/sale_order_line.py",),
	"purchase.order": ("addons/purchase/models/purchase_order.py",),
	"purchase.order.line": ("addons/purchase/models/purchase_order_line.py",),
	"account.move": ("addons/account/models/account_move.py",),
	"account.move.line": ("addons/account/models/account_move_line.py",),
	"account.partial.reconcile": ("addons/account/models/account_partial_reconcile.py",),
	"account.payment": ("addons/account/models/account_payment.py",),
}
# Models whose fields a model gets through _inherits or a mixin (as Odoo exposes them on it).
DELEGATES: dict[str, tuple[str, ...]] = {
	"product.product": ("product.template",),
	"hr.employee": ("hr.version",),
	"product.template": ("website.published.mixin",),
	"project.project": ("analytic.plan.fields.mixin",),
}
MAGIC = {"id": {"type": "integer"}, "write_date": "Datetime", "create_date": "Datetime"}
CLASS = re.compile(r"^class \w+\(.*?\):\n(.*?)(?=^class |\Z)", re.M | re.S)
NAME = re.compile(r"^\s{4}_(name|inherit)\s*=\s*(\[[^\]]*\]|['\"][\w.]+['\"])", re.M)
DECLARATION = re.compile(r"^\s{4}(\w+)(?:\s*:\s*[^=\n]+)?\s*=\s*fields\.(\w+)\(", re.M)
SELECTION_VALUE = re.compile(r"\(\s*['\"]([\w ~-]+)['\"]\s*,")
FALSE: dict[str, Any] = {"enum": [False]}


def fetch(path: str) -> str:
	response = requests.get(RAW + path, timeout=FETCH_TIMEOUT_SECONDS)
	response.raise_for_status()
	return response.text


def classes_for(text: str, model: str) -> list[str]:
	"""The bodies of the classes in a file that define or extend ``model``."""
	bodies = []
	for match in CLASS.finditer(text):
		body = match.group(1)
		names = {name for _, value in NAME.findall(body) for name in re.findall(r"['\"]([\w.]+)['\"]", value)}
		if model in names:
			bodies.append(body)
	return bodies


def declarations(body: str) -> dict[str, tuple[str, list[str]]]:
	"""Each field a class declares: its type and, for a selection, its values."""
	found: dict[str, tuple[str, list[str]]] = {}
	for match in DECLARATION.finditer(body):
		name, kind = match.groups()
		values: list[str] = []
		if kind == "Selection":
			snippet = body[match.end() : match.end() + 1500]
			listed = re.match(r"\s*(?:selection\s*=\s*)?\[(.*?)\]", snippet, re.S)
			values = SELECTION_VALUE.findall(listed.group(1)) if listed else []
		found[name] = (kind, values)
	return found


def json_type(kind: str, values: list[str]) -> dict[str, Any]:
	"""The JSON Odoo's JSON-2 API sends for a field of ``kind`` (``false`` for an unset one)."""
	match kind:
		case "Char" | "Text" | "Html" | "Date" | "Datetime":
			return {"anyOf": [{"type": "string"}, FALSE]}
		case "Selection":
			return {"anyOf": [{"enum": values}, FALSE]} if values else {"anyOf": [{"type": "string"}, FALSE]}
		case "Many2one":
			return {"anyOf": [{"type": "array", "items": {"type": ["integer", "string"]}}, FALSE]}
		case "One2many" | "Many2many":
			return {"type": "array", "items": {"type": "integer"}}
		case "Integer":
			return {"type": "integer"}
		case "Float" | "Monetary":
			return {"type": "number"}
		case "Boolean":
			return {"type": "boolean"}
		case "Json":
			return {"anyOf": [{"type": "object"}, FALSE]}
	raise ValueError(f"no JSON type for fields.{kind}")


def model_schema(model: str, texts: dict[str, str]) -> dict[str, Any]:
	declared: dict[str, tuple[str, list[str]]] = {}
	for owner in (*DELEGATES.get(model, ()), model):
		for path in SOURCES.get(owner, SOURCES[model]):
			for body in classes_for(texts[path], owner):
				for name, (kind, values) in declarations(body).items():
					# A later extension's selection values add to the base's.
					old = declared.get(name, (kind, []))[1]
					declared[name] = (kind, sorted({*old, *values}) if values or old else [])
	properties: dict[str, Any] = {"id": MAGIC["id"]}
	for name in MODELS[model]:
		if name in ("write_date", "create_date"):
			properties[name] = json_type("Datetime", [])
			continue
		if name not in declared:
			raise LookupError(f"{model}.{name} is declared in none of {SOURCES[model]}")
		properties[name] = json_type(*declared[name])
	return {
		"type": "object",
		"description": f"{model}, transcribed from " + ", ".join(BLOB + path for path in SOURCES[model]),
		"properties": properties,
		"required": ["id"],
	}


def operation(model: str, method: str, ref: str) -> dict[str, Any]:
	return {
		"post": {
			"externalDocs": {"url": DOCS},
			"responses": {
				"200": {"description": "OK", "content": {"application/json": {"schema": {"$ref": ref}}}}
			},
		}
	}


def main() -> int:
	paths = {path for files in SOURCES.values() for path in files}
	texts = {path: fetch(path) for path in sorted(paths)}
	schemas: dict[str, Any] = {
		"FieldsGet": {
			"type": "object",
			"description": "fields_get's answer: each field's description (the adapter reads the names).",
			"additionalProperties": {
				"type": "object",
				"properties": {"type": {"type": "string"}},
				"required": ["type"],
			},
		}
	}
	paths_doc: dict[str, Any] = {}
	for model in MODELS:
		schema_name = "".join(part.title() for part in model.split("."))
		schemas[schema_name] = model_schema(model, texts)
		schemas[f"{schema_name}List"] = {
			"type": "array",
			"items": {"$ref": f"#/components/schemas/{schema_name}"},
		}
		for method in ("search_read", "read"):
			paths_doc[f"/json/2/{model}/{method}"] = operation(
				model, method, f"#/components/schemas/{schema_name}List"
			)
		paths_doc[f"/json/2/{model}/fields_get"] = operation(
			model, "fields_get", "#/components/schemas/FieldsGet"
		)
	document = {
		"openapi": "3.1.0",
		"info": {
			"title": "Odoo 19 JSON-2 API answers read by frappe-idempotent-importer",
			"version": f"19.0@{COMMIT[:12]}",
			"description": (
				"Odoo publishes no OpenAPI document. Transcribed by tools/odoo_schema.py from the field "
				f"declarations of odoo/odoo at {COMMIT} (19.0), per {DOCS}: only the models and fields the "
				"Odoo adapter reads."
			),
		},
		"paths": paths_doc,
		"components": {"schemas": schemas},
	}
	SCHEMA.write_text(json.dumps(document, indent=1, ensure_ascii=False) + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
