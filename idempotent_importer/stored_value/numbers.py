# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Square gift card account number (GAN) a source card gets. Square takes a custom GAN of 8 to 20
alphanumeric characters, unique per seller, that doesn't start with a card BIN or Square's own gift card
BINs (https://developer.squareup.com/docs/gift-cards/using-gift-cards-api, "Custom GAN gift cards"). A
source code that fits (once its separators are dropped) is kept, so the customer's card works as it is. A
source that hides its codes, or a code Square can't take, gets a new number: 16 digits from a keyed hash of
the card's identity (stable for this site, so re-runs find it again, and not guessable without the site's
key), starting with 9, which no major card network or Square uses. The same rules as
wp-woo-idempotent-importer's StoredValue/GiftCardNumber."""

from __future__ import annotations

import hashlib
import hmac
import re

GENERATED_LENGTH = 16
MIN_LENGTH, MAX_LENGTH = 8, 20
GENERATED_PREFIX = "9"
SQUARE_BINS = ("778273", "778332")  # Square's physical and digital gift card BINs
CARD_BIN_FIRST_DIGITS = frozenset("23456")  # Mastercard 2-series, Amex/Diners/JCB, Visa, Mastercard, Discover
BIN_LENGTH = 6
SEPARATORS = re.compile(r"[^A-Za-z0-9]")


def from_source_code(code: str) -> str | None:
	"""The source code as a GAN, or None when Square can't take it."""
	gan = SEPARATORS.sub("", code).upper()
	if not MIN_LENGTH <= len(gan) <= MAX_LENGTH or starts_like_a_card(gan):
		return None
	return gan


def generated(gid: str, site_key: str) -> str:
	"""A new number for card ``gid``, keyed by ``site_key``."""
	digest = hmac.new(site_key.encode(), gid.encode(), hashlib.sha256).hexdigest()
	digits = "".join(str(int(digest[i : i + 2], 16) % 10) for i in range(0, len(digest), 2))
	return GENERATED_PREFIX + digits[: GENERATED_LENGTH - 1]


def starts_like_a_card(gan: str) -> bool:
	if gan[0] in CARD_BIN_FIRST_DIGITS and gan[:BIN_LENGTH].isdigit():
		return True
	return gan.startswith(SQUARE_BINS)
