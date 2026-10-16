<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Shopify fixtures

Recorded Admin GraphQL responses of one store in two states (`v1`, then `v2` with changes and deletions),
ported from [wp-woo-idempotent-importer](https://github.com/JamesonRGrieve/wp-woo-idempotent-importer)
(`tests/fixtures/shopify`, commit 8085d44, 2026-10-16) for the kinds this adapter reads. Each file is
`{"operation", "variables", "data"}`; every one executes against Shopify's published Admin schema
unchanged (`python -m tools.prove`).
