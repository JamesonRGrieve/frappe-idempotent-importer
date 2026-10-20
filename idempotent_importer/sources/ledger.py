# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the accounting sources (QuickBooks Online, Xero, Zoho Books and Inventory) share, so each shape is
translated in one place: an item of the books as an ERPNext Item, a country as its ISO code, a source date
as ``YYYY-MM-DD``, and a document's identity of another kind."""

from __future__ import annotations

from decimal import Decimal

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.paths import slugify

DATE_LENGTH = len("YYYY-MM-DD")
ISO_CODE_LENGTH = 2
# English country names the accounting sources write into addresses (they keep the name the user typed or
# picked, not a code), by ISO 3166-1 alpha-2 code.
COUNTRY_NAMES: dict[str, str] = {
	"canada": "CA",
	"united states": "US",
	"united states of america": "US",
	"usa": "US",
	"u.s.a.": "US",
	"us": "US",
	"united kingdom": "GB",
	"uk": "GB",
	"great britain": "GB",
	"england": "GB",
	"scotland": "GB",
	"wales": "GB",
	"northern ireland": "GB",
	"ireland": "IE",
	"australia": "AU",
	"new zealand": "NZ",
	"india": "IN",
	"south africa": "ZA",
	"singapore": "SG",
	"hong kong": "HK",
	"malaysia": "MY",
	"philippines": "PH",
	"indonesia": "ID",
	"united arab emirates": "AE",
	"uae": "AE",
	"saudi arabia": "SA",
	"japan": "JP",
	"china": "CN",
	"mexico": "MX",
	"brazil": "BR",
	"france": "FR",
	"germany": "DE",
	"spain": "ES",
	"italy": "IT",
	"netherlands": "NL",
	"belgium": "BE",
	"switzerland": "CH",
	"austria": "AT",
	"sweden": "SE",
	"norway": "NO",
	"denmark": "DK",
	"finland": "FI",
	"portugal": "PT",
	"poland": "PL",
}


def country_code(value: str | None) -> str | None:
	"""A country as its ISO code: a code as given, or a name the table knows; None when it is neither."""
	text = (value or "").strip()
	if len(text) == ISO_CODE_LENGTH and text.isalpha():
		return text.upper()
	return COUNTRY_NAMES.get(text.lower())


def day(value: str | None) -> str | None:
	"""A source date or timestamp as ``YYYY-MM-DD`` (its own calendar date, never shifted to UTC)."""
	if value is None or value.strip() == "":
		return None
	text = value.strip()[:DATE_LENGTH]
	if (
		len(text) != DATE_LENGTH
		or text[4] != "-"
		or text[7] != "-"
		or not (text[:4] + text[5:7] + text[8:]).isdigit()
	):
		raise SourceError(f"Not a date: {value}")
	return text


def required_day(value: str | None, what: str) -> str:
	found = day(value)
	if found is None:
		raise SourceError(f"{what} has no date.")
	return found


def midday(date: str) -> str:
	"""A document date as the contract's UTC time for writers that date a record from a time (a draft
	Quotation): midday UTC, so the site's calendar date is the document's own wherever the site is (within
	eleven hours of UTC)."""
	return f"{date}T12:00:00Z"


def book_item(
	name: str,
	sku: str | None,
	description: str | None,
	price: Decimal | None,
	is_stock_item: bool,
	stock_qty: Decimal | None = None,
	collections: list[str] | None = None,
	variants: list[Fields] | None = None,
	attributes: list[Field] | None = None,
) -> Fields:
	"""An item the books sell or buy as the shared ``product`` fields: never published on a storefront (a
	business system's item has no web listing), no images, brand, weight or tags; a variable item when it
	has ``variants``."""
	variable = bool(variants)
	return {
		"type": "variable" if variable else "simple",
		"name": name,
		"slug": slugify(name),
		"description": description,
		"short_description": None,
		"sku": None if variable else sku,
		"barcode": None,
		"brand": None,
		"collections": list[Field](collections or []),
		"images": [],
		"published": False,
		"is_stock_item": is_stock_item,
		"weight": None,
		"price": None if variable else price,
		"sale": None,
		"stock_qty": None if variable else stock_qty,
		"attributes": list[Field](attributes or []),
		"specifications": [],
		"variants": list[Field](variants or []),
		"tags": [],
	}
