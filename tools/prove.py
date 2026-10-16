# SPDX-License-Identifier: AGPL-3.0-or-later
"""``python -m tools.prove``: the adapters' Shopify operations and every recorded fixture, proven against
the sources' published API descriptions (see tools/proofs.py). Fetches Shopify's schema into .cache/ the
first time. Exits non-zero, listing every failure, when anything doesn't hold."""

from __future__ import annotations

import sys

from idempotent_importer.sources.shopify.queries import QueryLibrary
from tools.proofs import invalid_operations, invalid_rest_fixtures, mismatched_shopify_fixtures
from tools.schemas import shopify_schema

REST_SOURCES = ("woocommerce",)


def main() -> int:
	library = QueryLibrary.bundled()
	schema = shopify_schema()
	checks = {
		"Shopify operations that don't validate against the Admin schema": invalid_operations(
			schema, library
		),
		"Shopify fixtures that don't match Shopify's response shape": mismatched_shopify_fixtures(
			schema, library
		),
		"REST fixtures that don't match their source's OpenAPI document": [
			failure for source in REST_SOURCES for failure in invalid_rest_fixtures(source)
		],
	}
	failed = False
	for title, failures in checks.items():
		if failures:
			failed = True
			sys.stderr.write(f"{title}:\n  " + "\n  ".join(failures) + "\n")
	if failed:
		return 1
	sys.stdout.write(
		f"{len(library.operations())} Shopify operations valid; every fixture matches its source's API.\n"
	)
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
