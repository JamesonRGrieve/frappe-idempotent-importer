# SPDX-License-Identifier: AGPL-3.0-or-later
"""A Wix rich content (Ricos) document as HTML, ported from wp-woo-idempotent-importer's RichContent:
paragraphs, headings, lists, block quotes, code blocks, dividers and images, with bold, italic, underline,
strikethrough, superscript, subscript and links. Text colour, size and font are presentation and aren't
carried. Any other block or decoration has no HTML the importer writes exactly, so the document is
Unsupported rather than mirrored with something missing."""

from __future__ import annotations

from html import escape

from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.node import Node

PRESENTATION = frozenset({"COLOR", "FONT_SIZE", "FONT_FAMILY", "FONT_WEIGHT"})
ELEMENTS = {
	"BOLD": "strong",
	"ITALIC": "em",
	"UNDERLINE": "u",
	"STRIKETHROUGH": "s",
	"SUPERSCRIPT": "sup",
	"SUBSCRIPT": "sub",
}
BLOCKS = {
	"PARAGRAPH": "p",
	"BULLETED_LIST": "ul",
	"ORDERED_LIST": "ol",
	"LIST_ITEM": "li",
	"BLOCKQUOTE": "blockquote",
}
MIN_HEADING, MAX_HEADING, DEFAULT_HEADING = 1, 6, 2
# Where Wix serves a media file named only by its Wix Media id.
MEDIA_BASE = "https://static.wixstatic.com/media/"


def rich_html(document: Node | None) -> str:
	return "" if document is None else nodes_html(document.children("nodes"))


def nodes_html(nodes: list[Node]) -> str:
	return "".join(node_html(node) for node in nodes)


def node_html(node: Node) -> str:
	block = node.string("type")
	if block in BLOCKS:
		element = BLOCKS[block]
		return f"<{element}>{nodes_html(node.children('nodes'))}</{element}>"
	match block:
		case "TEXT":
			return text_html(node.required_child("textData"))
		case "HEADING":
			data = node.child("headingData")
			level = min(
				MAX_HEADING,
				max(MIN_HEADING, (data.optional_integer("level") if data else None) or DEFAULT_HEADING),
			)
			return f"<h{level}>{nodes_html(node.children('nodes'))}</h{level}>"
		case "CODE_BLOCK":
			return f"<pre><code>{nodes_html(node.children('nodes'))}</code></pre>"
		case "DIVIDER":
			return "<hr>"
		case "IMAGE":
			return image_html(node.required_child("imageData"))
		case _:
			raise Unsupported(f"a Wix {block} block has no HTML the importer writes")


def text_html(text: Node) -> str:
	html = escape(text.text("text"))
	for decoration in text.children("decorations"):
		kind = decoration.string("type")
		if kind in PRESENTATION:
			continue
		if kind == "LINK":
			url = decoration.required_child("linkData").required_child("link").optional_string("url")
			if url is None:
				raise Unsupported("a link to an anchor within the post has no HTML the importer writes")
			html = f'<a href="{escape(url)}">{html}</a>'
			continue
		element = ELEMENTS.get(kind)
		if element is None:
			raise Unsupported(f"Wix {kind} text has no HTML the importer writes")
		html = f"<{element}>{html}</{element}>"
	return html


def image_html(data: Node) -> str:
	source = data.required_child("image").required_child("src")
	media_id = source.optional_string("id")
	url = source.optional_string("url") or (MEDIA_BASE + media_id if media_id else None)
	if url is None:
		raise Unsupported("an image with no source")
	image = f'<img src="{escape(url)}" alt="{escape(data.text("altText"))}">'
	link = data.child("link")
	target = link.optional_string("url") if link else None
	if target:
		image = f'<a href="{escape(target)}">{image}</a>'
	return f"<figure>{image}</figure>"
