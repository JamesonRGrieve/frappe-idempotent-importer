# SPDX-License-Identifier: AGPL-3.0-or-later
"""An XML Schema as the JSON a REST API built on it returns, for sources whose only machine-readable spec is
an XSD (Intuit's QuickBooks Online data schema, in its official SDKs). Each complex type becomes an object
schema (tools/openapi.py's dialect): its elements and attributes are its properties, an element that may
repeat is an array, simple content is the ``value`` property beside the attributes (Intuit's
ReferenceType: ``{"value": "1", "name": "Amy"}``), an abstract element is any element of its substitution
group, and ``xs:any`` allows extra keys. Simple types are JSON strings, numbers or booleans, with their
enumerations. Nothing is required (the JSON omits empty elements) and no undeclared key is allowed."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from typing import Any

XS = "{http://www.w3.org/2001/XMLSchema}"
REF_PREFIX = "#/components/schemas/"
VALUE = "value"
UNBOUNDED = "unbounded"
INTEGERS = frozenset(
	{
		"int",
		"integer",
		"long",
		"short",
		"byte",
		"positiveInteger",
		"nonNegativeInteger",
		"negativeInteger",
		"nonPositiveInteger",
		"unsignedInt",
		"unsignedLong",
		"unsignedShort",
		"unsignedByte",
	}
)
NUMBERS = frozenset({"decimal", "double", "float"})
BOOLEANS = frozenset({"boolean"})

type Schema = dict[str, Any]


def local(name: str) -> str:
	"""A QName without its prefix ("xs:string" → "string", "Customer" → "Customer")."""
	return name.rsplit(":", 1)[-1]


class XsdSchemas:
	"""Every complex and simple type of a set of XSD files that include each other (one target namespace)."""

	def __init__(self, files: Iterable[Path]) -> None:
		self._complex: dict[str, ET.Element] = {}
		self._simple_types: dict[str, ET.Element] = {}
		self._groups: dict[str, ET.Element] = {}
		self._attribute_groups: dict[str, ET.Element] = {}
		self._elements: dict[str, ET.Element] = {}
		self._substitutes: dict[str, list[str]] = {}
		for path in files:
			# A schema pinned to a vendor's commit, parsed by expat, which expands no external entities and
			# (since 2.4.1, which Python 3.14 bundles) refuses entity amplification.
			for node in ET.parse(path).getroot():  # noqa: S314 - see above
				name = node.get("name")
				if name is None:
					continue
				match node.tag:
					case tag if tag == XS + "complexType":
						self._complex[name] = node
					case tag if tag == XS + "simpleType":
						self._simple_types[name] = node
					case tag if tag == XS + "group":
						self._groups[name] = node
					case tag if tag == XS + "attributeGroup":
						self._attribute_groups[name] = node
					case tag if tag == XS + "element":
						self._elements[name] = node
						group = node.get("substitutionGroup")
						if group is not None:
							self._substitutes.setdefault(local(group), []).append(name)

	def schemas(self) -> dict[str, Schema]:
		"""Every complex type as a named object schema, and every global element's type under its own name."""
		converted = {name: self._complex_type(node) for name, node in self._complex.items()}
		for name, node in self._elements.items():
			kind = node.get("type")
			if name not in converted and kind is not None and local(kind) in self._complex:
				converted[name] = {"$ref": REF_PREFIX + local(kind)}
		return converted

	def _complex_type(self, node: ET.Element) -> Schema:
		properties: dict[str, Schema] = {}
		extra = False
		for child in node:
			match child.tag:
				case tag if tag in (XS + "sequence", XS + "choice", XS + "all", XS + "group"):
					extra = self._particles(child, properties, False) or extra
				case tag if tag in (XS + "attribute", XS + "attributeGroup"):
					self._attributes(child, properties)
				case tag if tag == XS + "complexContent":
					extra = self._derived(child, properties) or extra
				case tag if tag == XS + "simpleContent":
					self._simple_content(child, properties)
				case tag if tag == XS + "anyAttribute":
					extra = True
		return {"type": "object", "properties": properties, "additionalProperties": extra}

	def _derived(self, content: ET.Element, properties: dict[str, Schema]) -> bool:
		"""A complex type derived from another: the base's properties, then its own."""
		extra = False
		for derivation in content:
			base = derivation.get("base")
			if base is not None and local(base) in self._complex:
				inherited = self._complex_type(self._complex[local(base)])
				properties.update(inherited["properties"])
				extra = bool(inherited["additionalProperties"])
			for child in derivation:
				if child.tag in (XS + "sequence", XS + "choice", XS + "all", XS + "group"):
					extra = self._particles(child, properties, False) or extra
				elif child.tag in (XS + "attribute", XS + "attributeGroup"):
					self._attributes(child, properties)
		return extra

	def _simple_content(self, content: ET.Element, properties: dict[str, Schema]) -> None:
		for derivation in content:
			base = derivation.get("base")
			properties[VALUE] = self._simple(local(base)) if base else {"type": "string"}
			for child in derivation:
				if child.tag in (XS + "attribute", XS + "attributeGroup"):
					self._attributes(child, properties)

	def _particles(self, node: ET.Element, properties: dict[str, Schema], repeated: bool) -> bool:
		"""The elements of a sequence, choice, all or group (``repeated`` when an enclosing one may repeat);
		True when it allows any element (xs:any)."""
		if node.tag == XS + "group" and node.get("ref") is not None:
			return self._particles(self._groups[local(node.get("ref", ""))], properties, repeated)
		repeated = repeated or node.get("maxOccurs") == UNBOUNDED
		extra = False
		for child in node:
			match child.tag:
				case tag if tag == XS + "element":
					self._element(child, properties, repeated)
				case tag if tag in (XS + "sequence", XS + "choice", XS + "all", XS + "group"):
					extra = self._particles(child, properties, repeated) or extra
				case tag if tag == XS + "any":
					extra = True
		return extra

	def _element(self, node: ET.Element, properties: dict[str, Schema], repeated: bool) -> None:
		many = repeated or node.get("maxOccurs") not in (None, "0", "1")
		ref = node.get("ref")
		if ref is not None:
			name = local(ref)
			for member in self._substitutes.get(name, []) or [name]:
				schema = self._of_element(self._elements[member])
				properties[member] = {"type": "array", "items": schema} if many else schema
			return
		name = node.get("name", "")
		schema = self._of_element(node)
		properties[name] = {"type": "array", "items": schema} if many else schema

	def _of_element(self, node: ET.Element) -> Schema:
		kind = node.get("type")
		if kind is not None:
			name = local(kind)
			return {"$ref": REF_PREFIX + name} if name in self._complex else self._simple(name)
		inline = node.find(XS + "complexType")
		if inline is not None:
			return self._complex_type(inline)
		simple = node.find(XS + "simpleType")
		return self._simple_type(simple) if simple is not None else {}

	def _attributes(self, node: ET.Element, properties: dict[str, Schema]) -> None:
		if node.tag == XS + "attributeGroup":
			group = self._attribute_groups[local(node.get("ref", ""))]
			for child in group:
				self._attributes(child, properties)
			return
		name = node.get("name") or local(node.get("ref", ""))
		kind = node.get("type")
		inline = node.find(XS + "simpleType")
		properties[name] = (
			self._simple(local(kind))
			if kind
			else self._simple_type(inline)
			if inline is not None
			else {"type": "string"}
		)

	def _simple(self, name: str) -> Schema:
		if name in INTEGERS:
			return {"type": "integer"}
		if name in NUMBERS:
			return {"type": "number"}
		if name in BOOLEANS:
			return {"type": "boolean"}
		if name in self._simple_types:
			return self._simple_type(self._simple_types[name])
		return {"type": "string"}

	def _simple_type(self, node: ET.Element) -> Schema:
		restriction = node.find(XS + "restriction")
		if restriction is None:
			# A list or union of simple values: their text.
			return {"type": "string"}
		schema = self._simple(local(restriction.get("base", "string")))
		values = [item.get("value") for item in restriction.findall(XS + "enumeration")]
		if values and schema.get("type") == "string":
			return {"type": "string", "enum": [value for value in values if value is not None]}
		return schema
