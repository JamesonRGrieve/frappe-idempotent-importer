# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the CRM sources (HubSpot, Salesforce, Pipedrive, Zoho CRM) share, so each shape is translated in one
place: the values of the fields a CRM's users defined, which each reader gathers on a node by label, and the
text of the HTML a CRM keeps notes in."""

from __future__ import annotations

from html.parser import HTMLParser

from idempotent_importer.core.node import Node

CUSTOM = "custom"
BLOCKS = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})


def custom_of(node: Node) -> dict[str, str]:
	"""The values of the fields the source's users defined, by label (``contract.with_custom``)."""
	custom = node.child(CUSTOM)
	return {label: value for label, value in custom.items() if isinstance(value, str)} if custom else {}


class TextOf(HTMLParser):
	"""The text of an HTML fragment, a block element starting a new line."""

	def __init__(self) -> None:
		super().__init__(convert_charrefs=True)
		self.parts: list[str] = []

	def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
		if tag in BLOCKS:
			self.parts.append("\n")

	def handle_data(self, data: str) -> None:
		self.parts.append(data)


def plain_text(fragment: str | None) -> str | None:
	"""An HTML note's text, its lines trimmed and blank ones dropped; None when it has none."""
	if not fragment:
		return None
	parser = TextOf()
	parser.feed(fragment)
	parser.close()
	lines = [line.strip() for line in "".join(parser.parts).splitlines()]
	return "\n".join(line for line in lines if line) or None
