# SPDX-License-Identifier: AGPL-3.0-or-later
"""A Swagger 2 document recovered from its Spectacle rendering. NetSuite publishes its REST Record API's
OpenAPI description only as the REST API Browser, an HTML page Spectacle generates from it
(https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_157373386674.html): every
definition is a panel listing its properties (name, type or ``$ref``, format, enum, and an inner schema
for an array's items or an inline object), and every operation a panel with its method, path and
``200 OK`` schema. Reading those panels back gives the definitions and responses the fixtures are proven
against, with nothing transcribed by hand."""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

DEFINITION_PREFIX = "definition-"
OPERATION_PREFIX = "operation-"
REF_PREFIX = "#/definitions/"
OK_TITLE = "200 OK"
VOID_ELEMENTS = frozenset({"br", "img", "hr", "input", "meta", "link", "wbr", "col", "area", "source"})

type Schema = dict[str, Any]


@dataclass
class Element:
	tag: str
	attributes: dict[str, str]
	children: list[Element | str] = field(default_factory=list)

	def classes(self) -> set[str]:
		return set(self.attributes.get("class", "").split())

	def has_class(self, name: str) -> bool:
		return name in self.classes()

	def elements(self) -> list[Element]:
		return [child for child in self.children if isinstance(child, Element)]

	def text(self) -> str:
		return "".join(child if isinstance(child, str) else child.text() for child in self.children).strip()

	def find(self, class_name: str) -> Element | None:
		"""The first descendant (depth first) with ``class_name``."""
		for child in self.elements():
			if child.has_class(class_name):
				return child
			found = child.find(class_name)
			if found is not None:
				return found
		return None

	def find_all(self, class_name: str) -> list[Element]:
		found: list[Element] = []
		for child in self.elements():
			if child.has_class(class_name):
				found.append(child)
			found += child.find_all(class_name)
		return found

	def child(self, class_name: str) -> Element | None:
		return next((child for child in self.elements() if child.has_class(class_name)), None)


class TreeBuilder(HTMLParser):
	"""The page as a tree of elements. Spectacle's markup is well formed apart from void elements."""

	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.root = Element("root", {})
		self._open: list[Element] = [self.root]

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		element = Element(tag, {name: value or "" for name, value in attrs})
		self._open[-1].children.append(element)
		if tag not in VOID_ELEMENTS:
			self._open.append(element)

	def handle_endtag(self, tag: str) -> None:
		for depth in range(len(self._open) - 1, 0, -1):
			if self._open[depth].tag == tag:
				del self._open[depth:]
				return

	def handle_data(self, data: str) -> None:
		self._open[-1].children.append(data)


def type_schema(holder: Element) -> Schema:
	"""The schema a ``json-property-type`` span (and its siblings) names: a ``$ref``, or a type with its
	enum and format."""
	kind = holder.find("json-property-type") if not holder.has_class("json-property-type") else holder
	schema: Schema = {}
	if kind is not None:
		ref = kind.find("json-schema-ref")
		if ref is not None:
			return {"$ref": ref.attributes["href"]}
		name = kind.text()
		if name:
			schema["type"] = name
	enum = holder.find("json-property-enum")
	if enum is not None:
		schema["enum"] = [item.text() for item in enum.find_all("json-property-enum-item")]
	format_ = holder.find("json-property-format")
	if format_ is not None:
		schema["format"] = format_.text().strip("()")
	return schema


def properties_schema(section: Element) -> Schema:
	"""An object from a ``json-schema-properties`` section: each ``dt`` a property, a following inner
	schema giving an array's items or an inline object's properties."""
	listing = next((child for child in section.elements() if child.tag == "dl"), None)
	properties: dict[str, Schema] = {}
	current: str | None = None
	for item in listing.elements() if listing else []:
		if item.tag != "dt":
			continue
		if item.has_class("json-inner-schema"):
			if current is not None:
				properties[current] = inner_schema(item, properties[current])
			continue
		name = item.attributes.get("data-property-name")
		if name is None:
			continue
		properties[name] = type_schema(item)
		current = name
	return {"type": "object", "properties": properties}


def inner_schema(holder: Element, outer: Schema) -> Schema:
	"""What an inner schema makes of the property before it: an array of its items, or an object."""
	array_items = holder.find("json-schema-array-items")
	if array_items is not None:
		inner = array_items.find("json-inner-schema")
		nested = inner.find("json-schema-properties") if inner is not None else None
		items = properties_schema(nested) if nested is not None else type_schema(array_items)
		return {"type": "array", "items": items}
	nested = holder.find("json-schema-properties")
	if nested is not None:
		return {**{k: v for k, v in outer.items() if k != "$ref"}, **properties_schema(nested)}
	return outer


def definition(panel: Element) -> Schema:
	section = panel.find("json-schema-properties")
	return properties_schema(section) if section is not None else {"type": "object"}


def operation(panel: Element) -> tuple[str, str, Schema | None] | None:
	method = panel.find("operation-method")
	path = panel.find("operation-path")
	if method is None or path is None:
		return None
	responses = panel.find("swagger-responses")
	ok: Schema | None = None
	for row in responses.find_all("prop-row") if responses else []:
		title = row.find("prop-title")
		if title is not None and title.text() == OK_TITLE:
			ref = row.find("json-schema-ref")
			ok = {"$ref": ref.attributes["href"]} if ref is not None else {}
	return method.text().lower(), path.text(), ok


def swagger_of(page: str) -> Schema:
	"""The Swagger 2 document (definitions and the 200 responses of the paths) a Spectacle page renders."""
	builder = TreeBuilder()
	builder.feed(page)
	builder.close()
	definitions: dict[str, Schema] = {}
	paths: dict[str, dict[str, Schema]] = {}
	for panel in builder.root.find_all("panel"):
		panel_id = panel.attributes.get("id", "")
		if panel.has_class("definition") and panel_id.startswith(DEFINITION_PREFIX):
			definitions[panel_id.removeprefix(DEFINITION_PREFIX)] = definition(panel)
		elif panel.has_class("operation") and panel_id.startswith(OPERATION_PREFIX):
			found = operation(panel)
			if found is None:
				continue
			method, path, ok = found
			responses: Schema = {"200": {"schema": ok}} if ok is not None else {}
			paths.setdefault(path, {})[method] = {"responses": responses}
	return {"swagger": "2.0", "definitions": definitions, "paths": paths}
