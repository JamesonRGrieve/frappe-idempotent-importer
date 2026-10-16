# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source timestamps as UTC. Every mapper hands writers ``YYYY-MM-DDTHH:MM:SSZ`` (fractions of a second
dropped); a timestamp without an offset (WooCommerce's ``*_gmt`` fields) is UTC. Writers convert to the
site's system time zone at the ERPNext boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from idempotent_importer.core.errors import SourceError

ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def parse_utc(value: str) -> datetime:
	"""An ISO 8601 / RFC 3339 or RFC 2822 timestamp as an aware UTC datetime."""
	text = value.strip()
	try:
		parsed = datetime.fromisoformat(text)
	except ValueError:
		try:
			parsed = parsedate_to_datetime(text)
		except (TypeError, ValueError) as e:
			raise SourceError(f"Not a timestamp: {value}") from e
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=UTC)
	return parsed.astimezone(UTC).replace(microsecond=0)


def iso_utc(value: str | None) -> str | None:
	"""``value`` as ``YYYY-MM-DDTHH:MM:SSZ``, or None when it is absent or empty."""
	if value is None or value.strip() == "":
		return None
	return parse_utc(value).strftime(ISO_FORMAT)


def format_utc(moment: datetime) -> str:
	return moment.astimezone(UTC).strftime(ISO_FORMAT)
