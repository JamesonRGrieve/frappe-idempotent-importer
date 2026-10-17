<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Recorded source responses

One directory per source, each with a store in two states (`v1`, then `v2` with changes and deletions;
Wix also has `catalog-v1`, the same store on Wix's older catalog API). Shopify's and WooCommerce's
directories say where theirs came from.

The Etsy, Square Online, BigCommerce, Squarespace, Wix, Magento, Lightspeed eCom and Ecwid fixtures are
copied unchanged from [wp-woo-idempotent-importer](https://github.com/JamesonRGrieve/wp-woo-idempotent-importer)
(`tests/fixtures/<source>`, commit 9c462e9, 2026-10-16), so the two projects read the same stores. Each is
`{"request": {"method"?, "path", "query"?, "body"?}, "response"}`: what the source answers that request
(`tests/support/fixtures.py`). Every response is proven against the source's published API document,
pinned in `tools/specs.py` (`python -m tools.prove`); Wix, Lightspeed and Ecwid publish none, so theirs are
transcribed from their official references in `tests/schemas/` (copied from the same commit).
