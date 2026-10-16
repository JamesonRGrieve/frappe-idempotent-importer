# SPDX-License-Identifier: AGPL-3.0-or-later
"""Money and quantities as sources send them, kept exact: Decimal, never float arithmetic."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

NUMBER = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")
CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")
# ISO 4217 minor units of every currency that doesn't have two (including withdrawn codes sources still send).
MINOR_UNITS: dict[str, int] = {
	**dict.fromkeys(
		(
			"BIF",
			"BYR",
			"CLP",
			"DJF",
			"GNF",
			"ISK",
			"JPY",
			"KMF",
			"KRW",
			"PYG",
			"RWF",
			"UGX",
			"UYI",
			"VND",
			"VUV",
			"XAF",
			"XOF",
			"XPF",
		),
		0,
	),
	**dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
	**dict.fromkeys(("CLF", "UYW"), 4),
}
DEFAULT_MINOR_UNITS = 2
# Codes ISO 4217 gives no minor unit (precious metals, funds, testing), and non-ISO codes.
NO_MINOR_UNIT = frozenset(
	("XAG", "XAU", "XBA", "XBB", "XBC", "XBD", "XDR", "XPD", "XPT", "XSU", "XTS", "XUA", "XXX", "XUS", "BTC")
)


def decimal_of(value: int | float | str) -> Decimal:
	"""A JSON number or numeric string as an exact Decimal. A float is read as the shortest decimal that
	converts back to it (``repr``), which is the number the JSON text spelled: 19.99 → Decimal("19.99")."""
	if isinstance(value, bool):
		raise ValueError("A boolean is not a decimal.")
	if isinstance(value, int):
		return Decimal(value)
	text = repr(value) if isinstance(value, float) else value.strip()
	if not NUMBER.match(text):
		raise ValueError(f'"{value}" is not a decimal number.')
	try:
		return Decimal(text)
	except InvalidOperation as e:
		raise ValueError(f'"{value}" is not a decimal number.') from e


def decimal_text(value: Decimal) -> str:
	"""A Decimal in plain notation without trailing zeros: 10.50 → "10.5", 1E+2 → "100", -0 → "0"."""
	if value == 0:
		return "0"
	text = format(value, "f")
	if "." in text:
		text = text.rstrip("0").rstrip(".")
	return text


def minor_units(code: str) -> int:
	if not CURRENCY_CODE.match(code) or code in NO_MINOR_UNIT:
		raise ValueError(f"{code} is not a currency with a minor unit.")
	return MINOR_UNITS.get(code, DEFAULT_MINOR_UNITS)


def from_minor_units(amount: int, code: str) -> Decimal:
	"""An amount in the currency's smallest unit as an exact Decimal (1999 CAD → 19.99, 500 JPY → 500)."""
	return Decimal(amount).scaleb(-minor_units(code))


def is_exact_at(value: Decimal, places: int) -> bool:
	"""Whether ``value`` has no digits beyond ``places`` decimal places (so ERPNext can hold it exactly)."""
	return value == value.quantize(Decimal(1).scaleb(-places))


def exact_quotient(dividend: Decimal, divisor: Decimal, places: int) -> Decimal | None:
	"""``dividend / divisor`` when it is exact at ``places`` decimal places, else None (never rounded)."""
	if divisor == 0:
		return None
	quotient = dividend / divisor
	if not is_exact_at(quotient, places) or quotient * divisor != dividend:
		return None
	return quotient.quantize(Decimal(1).scaleb(-places))
