<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# WooCommerce fixtures

One store in two states: `v1`, then `v2` with a price and stock change, a deleted product, a renamed
category, a new customer, a deleted coupon, a refund, a payment, a new order, an edited and a deleted
page, a new post and review, and a deleted redirect. Each file is `{"request", "headers"?, "response"}`
as WooCommerce 10 / WordPress 6.8 (and the Redirection plugin) answer that request. Every response
validates against `tests/schemas/woocommerce.json`, transcribed from the official documentation
(`python -m tools.prove`). The end-to-end suite serves the same store to two connections at once.
