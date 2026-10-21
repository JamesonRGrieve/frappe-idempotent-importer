# SPDX-License-Identifier: AGPL-3.0-or-later
"""A phone number in E.164 (``+`` and 8 to 15 digits), as Square Loyalty enrols by
(https://developer.squareup.com/docs/loyalty-api/loyalty-accounts). A number written with its country code is
kept; a 10-digit number (or 11 starting with 1) for a company in the North American Numbering Plan gets +1.
Anything else is not guessed at: None."""

from __future__ import annotations

import re

MIN_DIGITS, MAX_DIGITS = 8, 15
NANP_COUNTRIES = frozenset({"US", "CA"})
NANP_LENGTH = 10
NON_DIGITS = re.compile(r"\D")


def e164(phone: str, country: str) -> str | None:
	digits = NON_DIGITS.sub("", phone)
	if phone.strip().startswith("+"):
		return f"+{digits}" if MIN_DIGITS <= len(digits) <= MAX_DIGITS else None
	if country.upper() not in NANP_COUNTRIES:
		return None
	if len(digits) == NANP_LENGTH + 1 and digits.startswith("1"):
		digits = digits[1:]
	return f"+1{digits}" if len(digits) == NANP_LENGTH else None
