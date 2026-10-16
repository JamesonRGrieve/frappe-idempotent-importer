# SPDX-License-Identifier: AGPL-3.0-or-later
"""How mirrored records are named and routed, so two connections never collide (pure; the writers apply
it). ERPNext names some records globally (item codes, item groups, blog categories, coupon names, web
pages): those carry the connection's abbreviation or its Public Site's route prefix."""

from __future__ import annotations

import html
import re

from idempotent_importer.core.identity import Identity

ITEM_CODE_LENGTH = 140
ABBREVIATION_LENGTH = 10
UNSAFE_NAME = re.compile(r"[<>\"'%/\\]")
TAGS = re.compile(r"<[^>]+>")


def safe_name(text: str) -> str:
	"""Characters Frappe won't take in a document name, replaced."""
	return UNSAFE_NAME.sub("-", text).strip()


def suffixed(name: str, abbreviation: str) -> str:
	"""A globally named record's name, scoped to one connection ("Hoodies - WA")."""
	return f"{safe_name(name)} - {abbreviation}"


def item_code(abbreviation: str, sku: str | None, gid: str) -> str:
	"""The SKU when the source has one, else the source's type and id, prefixed by the connection's
	abbreviation ("WA-TOQUE-1", "WA-ProductVariation-1022")."""
	identity = Identity.parse(gid)
	code = f"{abbreviation}-{safe_name(sku) if sku else f'{identity.type}-{identity.id}'}"
	return code[:ITEM_CODE_LENGTH]


def route(prefix: str, *segments: str) -> str:
	"""A route under a Public Site's prefix ("store-a", "/about-us/" → "store-a/about-us")."""
	return "/".join(part.strip("/") for part in (prefix, *segments) if part.strip("/"))


def named_after(route_: str) -> str:
	"""A routed record's name: its route ("store-a/about-us" → "store-a-about-us"), unique where a
	title-based name would collide across sites."""
	return route_.replace("/", "-")


def unique_abbr(value: str, taken: set[str]) -> str:
	"""An attribute value's abbreviation (part of its variants' codes), unique within its attribute."""
	base = "".join(ch for ch in value.upper() if ch.isalnum())[:ABBREVIATION_LENGTH] or "V"
	abbr = base
	counter = 1
	while abbr in taken:
		counter += 1
		abbr = f"{base[: ABBREVIATION_LENGTH - len(str(counter))]}{counter}"
	taken.add(abbr)
	return abbr


def plain_text(fragment: str | None) -> str:
	"""HTML as plain text (for fields that don't render markup)."""
	return " ".join(html.unescape(TAGS.sub(" ", fragment or "")).split())
