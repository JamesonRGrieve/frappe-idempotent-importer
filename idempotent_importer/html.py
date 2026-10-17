# SPDX-License-Identifier: AGPL-3.0-or-later
"""Images embedded in a source's page and post HTML (pure; the writers localise them). The images the old
store hosts would vanish with it, so mappers list them (``images`` in the field contract) and the writer
copies each into a File and points the HTML at the copy. Images on other hosts are left as they are."""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*(["'])(.*?)\1""", re.IGNORECASE)
PROTOCOL_RELATIVE = "//"


def absolute(url: str) -> str:
	return f"https:{url}" if url.startswith(PROTOCOL_RELATIVE) else url


def embedded_images(fragment: str, owned: Callable[[str], bool]) -> list[str]:
	"""The distinct absolute URLs of the images ``fragment`` embeds that ``owned`` says the source hosts,
	in document order."""
	urls: dict[str, None] = {}
	for match in SRC.finditer(fragment):
		url = absolute(html.unescape(match.group(2)).strip())
		if urlsplit(url).scheme in ("http", "https") and owned(url):
			urls[url] = None
	return list(urls)


def replace_images(fragment: str, replacements: Mapping[str, str]) -> str:
	"""``fragment`` with each embedded image whose absolute URL is in ``replacements`` pointed at its new
	URL; everything else is left byte for byte."""
	if not replacements:
		return fragment

	def replace(match: re.Match[str]) -> str:
		new = replacements.get(absolute(html.unescape(match.group(2)).strip()))
		if new is None:
			return match.group(0)
		start, end = match.span(2)
		offset = match.start(0)
		whole = match.group(0)
		return whole[: start - offset] + html.escape(new, quote=True) + whole[end - offset :]

	return SRC.sub(replace, fragment)


def hosted_on(*hosts: str) -> Callable[[str], bool]:
	"""Whether a URL is on one of ``hosts`` (or a subdomain of one), ignoring ``www.``."""
	wanted = {host.lower().removeprefix("www.") for host in hosts if host}

	def owned(url: str) -> bool:
		host = (urlsplit(url).hostname or "").removeprefix("www.")
		return any(host == h or host.endswith(f".{h}") for h in wanted)

	return owned
