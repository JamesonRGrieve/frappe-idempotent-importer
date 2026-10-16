# SPDX-License-Identifier: AGPL-3.0-or-later
"""The failures the engine tells apart. Messages never contain credentials."""

from __future__ import annotations


class SourceError(RuntimeError):
	"""A source platform's transport, protocol or data-shape failure."""


class RetryLater(SourceError):
	"""The source can't be read right now: it keeps rate limiting, asks for a wait too long to sit out
	inside a request, keeps failing or can't be reached. The run pauses where it is and resumes after
	``seconds``, rather than failing the kind and carrying on against a source that is pushing back."""

	def __init__(self, seconds: float, message: str) -> None:
		super().__init__(message)
		self.seconds = seconds


class CredentialsRejected(SourceError):
	"""The source refused the credentials (401, or 403 for a missing scope or a block). Retrying with the
	same credentials can get the key or account locked, so the run stops and scheduled runs hold off until
	the operator saves the connection or starts a run by hand (see ``CredentialHold``)."""


class Unsupported(Exception):
	"""A source object ERPNext has no first-party home for (a gap in coverage/<source>.md), or one that
	can't be represented exactly. Reported and counted, never approximated, never fatal."""


class UnresolvedDependency(Exception):
	"""An object refers to another one that isn't mirrored yet (an order's product, a variant's
	template). The object is deferred and retried on the next run, never skipped."""
