# SPDX-License-Identifier: AGPL-3.0-or-later
"""What several source adapters share, so each shape is translated in one place: text sources send
HTML-escaped or as plain lines, Unix timestamps, amounts in a power-of-ten minor unit, and the store's
policies and content pages as Web Pages."""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.fields import Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.paths import slugify
from idempotent_importer.core.source import Page
from idempotent_importer.core.times import ISO_FORMAT

PARAGRAPH_BREAK = re.compile(r"\n{2,}")
DECIMAL_BASE = 10


def unescaped(value: str | None) -> str | None:
	"""Text a source sends with HTML entities encoded (``&quot;``, ``&#39;``), as plain text."""
	return html.unescape(value) if value is not None else None


def plain_html(value: str | None) -> str:
	"""Plain multi-line text as HTML: blank lines separate paragraphs, single newlines become breaks."""
	text = (unescaped(value) or "").replace("\r\n", "\n").strip()
	if not text:
		return ""
	return "\n".join(
		"<p>" + html.escape(paragraph.strip()).replace("\n", "<br>") + "</p>"
		for paragraph in PARAGRAPH_BREAK.split(text)
	)


def from_unix(seconds: int) -> str:
	"""A Unix timestamp as ``YYYY-MM-DDTHH:MM:SSZ``."""
	return datetime.fromtimestamp(seconds, UTC).strftime(ISO_FORMAT)


def unix_time(node: Node, field: str) -> str | None:
	seconds = node.optional_integer(field)
	return None if seconds is None else from_unix(seconds)


def required_unix_time(node: Node, field: str) -> str:
	value = unix_time(node, field)
	if value is None:
		raise SourceError(f"{node.path}.{field} is missing.")
	return value


def shifted(amount: int, divisor: int, what: str) -> Decimal:
	"""``amount / divisor`` exactly, for a divisor that is a power of ten (an amount in minor units)."""
	places = len(str(divisor)) - 1
	if divisor < 1 or DECIMAL_BASE**places != divisor:
		raise SourceError(f"{what} has a divisor ({divisor}) that isn't a power of ten.")
	return Decimal(amount).scaleb(-places)


FEED_SEPARATOR = ":"


def chained(feeds: int, cursor: str | None, read: Callable[[int, str | None], Page]) -> Page:
	"""One page of a kind a source keeps as several collections read one after another (QuickBooks' invoices,
	then its sales receipts, then its credit memos): ``read(feed, position)`` reads one page of feed
	``feed`` from ``position`` (None first). The cursor is ``<feed>:<position>``."""
	if cursor is None:
		feed, position = 0, None
	else:
		index, _, rest = cursor.partition(FEED_SEPARATOR)
		feed, position = int(index), rest or None
	page = read(feed, position)
	if page.has_next_page:
		return Page(page.nodes, True, f"{feed}{FEED_SEPARATOR}{page.end_cursor or ''}")
	following = feed + 1
	return Page(page.nodes, following < feeds, f"{following}{FEED_SEPARATOR}" if following < feeds else None)


class ContentPageMapper:
	"""A store's page or policy that a reader hands over already shaped (``title``, ``slug``, ``html``,
	``published``, ``legacy_path``) as a Web Page. Sources whose content isn't otherwise structured share
	it."""

	def __init__(self, kind: Kind) -> None:
		self.kind = kind
		self.version = 1

	def map(self, node: Node) -> Desired | None:
		title = node.string("title")
		fields: Fields = {
			"title": title,
			"slug": node.optional_string("slug") or slugify(title),
			"parent": None,
			"published": node.optional_flag("published") is not False,
			"html": node.text("html"),
			"images": [],
		}
		legacy = node.optional_string("legacy_path")
		return Desired(self.kind, node.id(), fields, (legacy,) if legacy else ())
