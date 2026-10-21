# SPDX-License-Identifier: AGPL-3.0-or-later
"""XML documents validated against a DTD, for sources whose only machine-readable spec is one (Sage
Intacct's Web Services gateway). Reads the element declarations (``<!ELEMENT name model>``, parameter
entities ``%NAME;`` expanded) and checks every element of a document: that it is declared, and that its
children, in order, match its content model (a sequence, choice, ``?``, ``*``, ``+``; ``EMPTY``; ``ANY``;
text only for ``#PCDATA``; text and the listed children in any order for mixed content). Attributes are not
checked. A child a content model names but the DTD never declares is taken as text."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass

ENTITY = re.compile(r'<!ENTITY\s+%\s+([\w.-]+)\s+"([^"]*)"\s*>', re.S)
ELEMENT = re.compile(r"<!ELEMENT\s+([\w.-]+)\s+(.*?)>", re.S)
COMMENT = re.compile(r"<!--.*?-->", re.S)
REFERENCE = re.compile(r"%([\w.-]+);")
NAME = re.compile(r"[\w.-]+")
PCDATA = "#PCDATA"
MAX_EXPANSIONS = 20


@dataclass(frozen=True)
class Model:
	"""One element's content: EMPTY, ANY, text only, mixed (text and ``names``), or children (``pattern``
	over the children's tags, each written ``<tag>``)."""

	kind: str
	names: frozenset[str] = frozenset()
	pattern: re.Pattern[str] | None = None
	order: tuple[str, ...] = ()


def expand(text: str, entities: dict[str, str]) -> str:
	for _ in range(MAX_EXPANSIONS):
		replaced = REFERENCE.sub(lambda m: entities.get(m.group(1), m.group(0)), text)
		if replaced == text:
			return text
		text = replaced
	return text


def model_of(text: str) -> Model:
	spec = " ".join(text.split())
	if spec == "EMPTY":
		return Model("empty")
	if spec == "ANY":
		return Model("any")
	if PCDATA in spec:
		names = frozenset(n for n in NAME.findall(spec.replace(PCDATA, "")) if n)
		return Model("mixed", names) if names else Model("text")
	order = tuple(dict.fromkeys(NAME.findall(spec)))
	pattern = NAME.sub(lambda m: f"(?:<{m.group(0)}>)", spec.replace(" ", ""))
	pattern = pattern.replace(",", "").replace("(", "(?:").replace("(?:?:", "(?:")
	return Model("children", pattern=re.compile(pattern), order=order)


class Dtd:
	def __init__(self, *texts: str) -> None:
		source = COMMENT.sub("", "\n".join(texts))
		entities: dict[str, str] = {}
		for name, value in ENTITY.findall(source):
			entities.setdefault(name, value)
		self.models: dict[str, Model] = {}
		for name, model in ELEMENT.findall(source):
			self.models.setdefault(name, model_of(expand(model, entities)))
		# A child a content model names but the DTD never declares (Intacct's response DTD names
		# ``retainagereleased`` without declaring it) is read as text.
		for model in list(self.models.values()):
			for child in model.order:
				self.models.setdefault(child, Model("text"))

	def order(self, tag: str) -> list[str]:
		"""The children an element's content model names, in its order."""
		model = self.models.get(tag)
		return list(model.order) if model is not None else []

	def errors(self, document: str) -> Iterator[str]:
		yield from self._check(ET.fromstring(document), "/")  # noqa: S314 - see the import

	def _check(self, element: ET.Element, parent: str) -> Iterator[str]:
		path = f"{parent}{element.tag}"
		model = self.models.get(element.tag)
		if model is None:
			yield f"{path} is not declared"
			return
		children = list(element)
		text = (element.text or "") + "".join(child.tail or "" for child in children)
		match model.kind:
			case "empty" if children or text.strip():
				yield f"{path} must be empty"
			case "text" if children:
				yield f"{path} holds elements, but only text is declared"
			case "mixed":
				for child in children:
					if child.tag not in model.names:
						yield f"{path} may not hold {child.tag}"
			case "children":
				if text.strip():
					yield f"{path} holds text, but only elements are declared"
				sequence = "".join(f"<{child.tag}>" for child in children)
				if model.pattern is not None and model.pattern.fullmatch(sequence) is None:
					yield f"{path}'s children {[c.tag for c in children]} don't match its content model"
		for child in children:
			yield from self._check(child, path + "/")
