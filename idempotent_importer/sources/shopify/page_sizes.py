# SPDX-License-Identifier: AGPL-3.0-or-later
"""The page sizes the client asks for, per operation, learned over one run. Shopify refuses before running
it any query whose requested cost (fixed by the selection and its page sizes, not by the store's data) is
over its single-query limit (shopify.dev/docs/apps/build/apis/graphql-admin/rate-limits: 1,000 points).
Each operation starts at QueryLibrary's defaults; a refusal scales all of its sizes down together, in
proportion to how far over the limit it was (never below 1), and the next request uses the smaller sizes.
Every successful response reports its requested cost, so sizes grow back toward the defaults while there
is room, but never back to sizes Shopify refused. Nested connections are paged to the end, so smaller
pages only mean more requests, never less data. Deterministic: the same store gives the same sizes.

Sizes are scaled from requested cost, not actual cost: the limit is enforced on requested cost, and a
store's small objects only lower the actual cost after Shopify has accepted the query. Ported from
wp-woo-idempotent-importer's PageSizes."""

from __future__ import annotations

import math

from idempotent_importer.core.errors import SourceError
from idempotent_importer.sources.shopify.queries import QueryLibrary

# Shopify's documented single-query cost limit, used until a refusal reports the store's own.
MAX_QUERY_COST = 1000
# The share of the limit a resized query aims for, so it fits with a margin.
TARGET_SHARE = 0.9
FULL_SCALE = 1.0
# The step by which growth backs off from a refused share until the sizes differ from the refused ones.
BELOW_REFUSED = 0.99
SMALLEST_SIZE = 1


def target(limit: int) -> float:
	return limit * TARGET_SHARE


def sizes_at(defaults: dict[str, int], scale: float) -> dict[str, int]:
	return {name: max(SMALLEST_SIZE, math.floor(size * scale)) for name, size in defaults.items()}


class PageSizes:
	def __init__(self, library: QueryLibrary) -> None:
		self._library = library
		# operation -> share of the default sizes it currently asks for
		self._scales: dict[str, float] = {}
		# operation -> the smallest share Shopify refused
		self._refused: dict[str, float] = {}
		# operation -> the cost limit its last refusal reported
		self._limits: dict[str, int] = {}

	def sizes(self, operation: str) -> dict[str, int]:
		"""The size variables to send with the operation's next request."""
		return sizes_at(self._library.page_sizes(operation), self._scales.get(operation, FULL_SCALE))

	def refused(self, operation: str, cost: int, limit: int) -> None:
		"""Shopify refused the operation at ``sizes(operation)`` because its requested cost was over the
		limit: every size shrinks, by at least one step. When every size is already 1 it can't fit at all."""
		defaults = self._library.page_sizes(operation)
		scale = self._scales.get(operation, FULL_SCALE)
		current = sizes_at(defaults, scale)
		if all(size <= SMALLEST_SIZE for size in current.values()):
			raise SourceError(
				f"{operation}: one object costs {cost} points even one at a time, over Shopify's "
				f"single-query limit of {limit}."
			)
		self._refused[operation] = min(scale, self._refused.get(operation, FULL_SCALE))
		self._limits[operation] = limit
		factor = min(TARGET_SHARE, target(limit) / max(cost, 1))
		following = scale * factor
		while sizes_at(defaults, following) == current:
			following *= factor
		self._scales[operation] = following

	def succeeded(self, operation: str, requested_cost: int) -> None:
		"""Shopify ran the operation at ``sizes(operation)``, reporting this requested cost: grow back
		where there is room."""
		scale = self._scales.get(operation, FULL_SCALE)
		aim = target(self._limits.get(operation, MAX_QUERY_COST))
		if scale >= FULL_SCALE or requested_cost <= 0 or requested_cost >= aim:
			return
		defaults = self._library.page_sizes(operation)
		following = min(FULL_SCALE, scale * aim / requested_cost)
		refused = self._refused.get(operation)
		if refused is not None and following >= refused:
			refused_sizes = sizes_at(defaults, refused)
			following = refused
			while sizes_at(defaults, following) == refused_sizes:
				following *= BELOW_REFUSED
		if following > scale:
			self._scales[operation] = following
