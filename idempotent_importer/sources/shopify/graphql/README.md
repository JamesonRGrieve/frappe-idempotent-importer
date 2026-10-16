<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Shopify GraphQL fragments

Ported from [wp-woo-idempotent-importer](https://github.com/JamesonRGrieve/wp-woo-idempotent-importer)
(`graphql/fragments`, commit 8085d44, 2026-10-16): the fragments of the kinds this adapter reads, copied
in so the two projects share no code. The list, by-id and nested-page operations around them are generated
by `queries.QueryLibrary`. Every operation is validated against Shopify's Admin schema for the pinned API
version (`python -m tools.prove`).
