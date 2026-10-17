<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Shopify fixtures

Recorded Admin GraphQL responses of one store in two states (`v1`, then `v2` with changes and deletions),
ported from [wp-woo-idempotent-importer](https://github.com/JamesonRGrieve/wp-woo-idempotent-importer)
(`tests/fixtures/shopify`) for the kinds this adapter reads: first at commit 8085d44 (2026-10-16), then
`orders.json` (with the live development store's fully refunded order, anonymised), `shop-policies.json`,
`draft-orders.json`, `files.json` and `delivery-profiles.json` at commit 9c462e9 (2026-10-16). Each file is `{"operation", "variables", "data"}`;
every one executes against Shopify's published Admin schema unchanged (`python -m tools.prove`).

`../shopify-query-cost.json` is the requested cost a live development store charged for every read
operation at its default page sizes, and the connection multipliers it charged per page size, recorded by
that project's `tools/calibrate-query-cost.php` (same commit 9c462e9); `tools/query_cost.py` must match it.
