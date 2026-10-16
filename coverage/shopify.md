<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Shopify (Admin GraphQL API 2026-10) → ERPNext

The single source of truth for what the Shopify adapter mirrors. Its GraphQL operations and recorded
fixtures are ported from wp-woo-idempotent-importer, and every operation and fixture is proven against
Shopify's published Admin schema (`python -m tools.prove`).

Access: a custom app with read scopes for products, inventory, customers, orders (`read_all_orders` for
orders older than 60 days), discounts, content (pages and blogs) and online store navigation; enter the
store's `<name>.myshopify.com` domain and the Admin API access token. Rate limits: Shopify meters a
query-cost bucket and reports it on every response; before each query the client waits until the bucket
can take one as costly as the last, and a throttled query is retried once it refills.

Incremental runs read everything that has `updated_at` (collections, products, customers, discounts,
orders, pages, articles); blogs, comments and redirects are read in full.

## Mirrored

| Shopify | ERPNext home | Notes |
|---|---|---|
| Collections (custom and smart) | **Item Group** `<title> - <abbr>`, routed `<prefix>/<handle>` | Smart-collection rules aren't kept (a warning): an item's groups are set per item. |
| Products with only the default variant | **Item** `<abbr>-<SKU>` with price, sale, stock, barcode and weight | |
| Products with options | Template **Item** + variant **Item** per variant, options as **Item Attributes** | |
| Variant price, compare-at price | **Item Price** at the compare-at price when it is higher, and a **Pricing Rule** at the price (Shopify's sale) | |
| Inventory (tracked) | **Stock Reconciliation** to the total quantity across locations | |
| Product images | **File**; Website Item image | Videos and 3D models: a warning (no home). |
| Vendor, product type | **Brand**; a "Product type" specification on the Website Item | |
| Active, published products | **Website Item** on the Public Site's Webshop Store | |
| Customers and their addresses | **Customer**, **Contact**, **Address** (default address as billing) | |
| Basic code discounts (percentage or per-item amount; all items, products or collections; minimum subtotal; dates; usage limit; every code) | **Pricing Rule** + one **Coupon Code** per code | |
| Orders (paid, partially refunded, refunded) | Submitted **Sales Invoice** + **Payment Entry** | Exact totals, no stock movement, as for WooCommerce. |
| Orders awaiting payment | Draft **Sales Invoice** | Voided and cancelled orders aren't mirrored. |
| Refunds | Credit note + refund **Payment Entry** | Shipping or other amounts refunded beyond the lines become an adjustment line. |
| Order timeline comments | **Comment** on the invoice | |
| Pages | **Web Page** under the prefix | |
| Blogs, articles | **Blog Category**, **Blog Post** (+ **Blogger**) | |
| Published comments | **Comment** on the Blog Post | |
| URL redirects, old product/collection/page/blog/article URLs | **Website Route Redirect** under the prefix | |

## Gaps

| Shopify | Why it isn't mirrored |
|---|---|
| Gift cards and their balances | No first-party gift-card ledger. |
| Discounts: automatic, Buy X Get Y, free shipping, app discounts, once-per-customer, customer- or segment-specific, minimum quantity, single-variant | ERPNext's coupons can't enforce them exactly. |
| Orders with tax-inclusive prices | The taxes can't be split out of the prices exactly into ERPNext's tax rows. |
| Duties | No sales tax and charge type for them. |
| Fulfillments and tracking | Delivery Notes would move stock a second time (see README, "Decisions to confirm"). |
| Draft orders, files, menus, delivery profiles, shop policies and settings, metafields, metaobjects | Later phases (Quotation, File, Website Settings, Shipping Rule have homes; not built yet). |
| Product tags, article tags and images | Later phase. |
| Product reviews | Shopify has no reviews API (the PHP importer reads a review app's CSV export; later phase). |
| Selling plans, subscriptions, markets, B2B, staff, abandoned checkouts, payouts | No first-party home. |
| Customer passwords, saved payment methods | Not exportable. |
