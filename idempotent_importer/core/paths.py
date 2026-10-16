# SPDX-License-Identifier: AGPL-3.0-or-later
"""Storefront URL paths and slugs, for the redirects from an object's old URL to its new home."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

ROOT = "/"
WWW = "www."
NON_SLUG = re.compile(r"[^a-z0-9]+")


def path_of(url: str | None) -> str | None:
	"""A storefront URL or path ("/trail-mug/", "https://shop.example/shop/p/trail-mug") as its normalised
	path; None when there is none, or it is the home page (nothing to redirect)."""
	if url is None or url.strip() == "":
		return None
	path = normalise(url)
	return None if path == ROOT else path


def normalise(url: str) -> str:
	"""Lower case, no scheme, host, query or trailing slash: "/Shop/Hoodie/?x=1" → "/shop/hoodie"."""
	path = urlsplit(url.strip()).path.lower()
	return "/" + path.strip("/")


def redirect_target(target: str, store_host: str | None) -> str | None:
	"""A redirect's target: a path, or a URL on the store's own host (``www.`` or not), is an old storefront
	URL, so its path; any other URL as it is. None when it targets the home page."""
	host = urlsplit(target.strip()).hostname
	if host is None:
		return path_of(target)
	if store_host is not None and bare_host(host) == bare_host(store_host):
		return path_of(target)
	return target.strip()


def bare_host(host: str) -> str:
	host = host.lower()
	return host.removeprefix(WWW)


def slugify(text: str) -> str:
	""" "Wool Toque (Navy)" → "wool-toque-navy"."""
	return NON_SLUG.sub("-", text.lower()).strip("-")
