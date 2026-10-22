# SPDX-License-Identifier: AGPL-3.0-or-later
"""The single list of sources and what follows it (the connection's source options, the coverage pages),
the doctype definitions, and the fixture proofs that need no network."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.registry import LISTED, ListedAdapter, adapter_for, all_adapters, by_label
from tests.support.transport import RecordingSleeper, scripted
from tools.proofs import first_difference, invalid_rest_fixtures, response_schema_pointer

ROOT = Path(__file__).resolve().parents[1]
DOCTYPES = ROOT / "idempotent_importer" / "idempotent_importer" / "doctype"


def doctype(name: str) -> dict[str, object]:
	folder = name.lower().replace(" ", "_")
	document = json.loads((DOCTYPES / folder / f"{folder}.json").read_text())
	assert isinstance(document, dict)
	return document


def field(document: dict[str, object], fieldname: str) -> dict[str, object]:
	fields = document["fields"]
	assert isinstance(fields, list)
	return next(f for f in fields if f["fieldname"] == fieldname)


def test_the_connection_offers_every_source_in_the_registry() -> None:
	options = str(field(doctype("Importer Connection"), "source")["options"]).split("\n")
	assert options == [adapter.label for adapter in all_adapters().values()]


@pytest.mark.parametrize("adapter_id", list(all_adapters()))
def test_every_source_has_a_coverage_page(adapter_id: str) -> None:
	page = ROOT / "coverage" / f"{adapter_id}.md"
	assert page.is_file(), f"coverage/{adapter_id}.md"
	assert adapter_for(adapter_id).label in page.read_text()


def test_listed_sources_refuse_to_run() -> None:
	"""A source listed before its adapter is built (QuickBooks Desktop) refuses to run; every other is built."""
	assert all(adapter.built for adapter in all_adapters().values() if adapter not in LISTED)
	assert [adapter.label for adapter in LISTED] == ["QuickBooks Desktop"]
	assert not by_label("QuickBooks Desktop").built
	listed = ListedAdapter("example", "Example")
	assert not listed.built
	assert listed.kinds() == () and listed.filter_hint() is None
	config = ConnectionConfig(listed.id, "erp", "https://erp.example")
	with pytest.raises(SourceError, match="not built yet"):
		listed.reader(config, scripted(), RecordingSleeper())
	assert listed.mappers(config) == []


def test_lookups() -> None:
	assert by_label("WooCommerce").id == "woocommerce"
	with pytest.raises(SourceError):
		by_label("Nope")
	with pytest.raises(SourceError):
		adapter_for("nope")


@pytest.mark.parametrize("name", ["Importer Connection", "Importer Link", "Importer Run", "Importer Seen"])
def test_doctype_definitions_are_consistent(name: str) -> None:
	document = doctype(name)
	fields = document["fields"]
	assert isinstance(fields, list)
	assert document["field_order"] == [f["fieldname"] for f in fields]
	assert document["module"] == "Idempotent Importer"


def test_every_rest_fixture_matches_its_openapi_document() -> None:
	assert list(invalid_rest_fixtures("woocommerce")) == []


def test_operations_are_found_by_path() -> None:
	document: dict[str, Any] = {
		"paths": {"/things": {"get": {}}, "/things/{id}": {"get": {}}, "/things/special": {"get": {}}}
	}
	assert response_schema_pointer(document, "GET", "/things/special") == (
		"/paths/~1things~1special/get/responses/200/content/application~1json/schema"
	)
	assert response_schema_pointer(document, "GET", "/things/7") == (
		"/paths/~1things~1{id}/get/responses/200/content/application~1json/schema"
	)
	assert response_schema_pointer(document, "GET", "/other") is None


def test_first_difference() -> None:
	assert first_difference({"a": [1, {"b": 2.0}]}, {"a": [1, {"b": 2}]}) is None
	assert (
		first_difference({"a": 1}, {"a": 1, "b": 2})
		== "$.b is selected by the operation but missing from the fixture"
	)
	assert (
		first_difference({"a": 1, "c": 3}, {"a": 1})
		== "$.c is in the fixture but not selected by the operation"
	)
	assert first_difference([1], [1, 2]) == "$: 1 items in the fixture, 2 serialised"
	assert (
		first_difference({"e": "ACTIVE"}, {"e": "DRAFT"})
		== "$.e: the fixture has 'ACTIVE', the schema serialises 'DRAFT'"
	)
