<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Shopify (Admin GraphQL API 2026-10) → ERPNext

The single source of truth for what the Shopify adapter mirrors. Its GraphQL operations and recorded
fixtures are ported from wp-woo-idempotent-importer, and every operation and fixture is proven against
Shopify's published Admin schema (`python -m tools.prove`).

Access: an app made in Shopify's Dev Dashboard (dev.shopify.com, signed in to the store's organization)
with the read scopes below, released and installed on the store. The connection takes the store's
`<name>.myshopify.com` domain and the app's client ID and secret (**Client Credentials**); the importer
exchanges them for a 24-hour token and renews it whenever Shopify rejects it. A refused grant (a wrong
secret, the app not installed) stops the run and holds scheduled runs, like any rejected credentials. A
legacy custom app made in the store admin before 2026 can use its permanent token instead (**Access
Token**).

| Scope | Read for |
|---|---|
| `read_products` | Products, variants, product media, collections |
| `read_inventory` | Stock quantities, weights |
| `read_customers` | Customers and their addresses |
| `read_discounts` | Code discounts |
| `read_orders` | Orders, refunds, transactions, the order timeline (last 60 days) |
| `read_all_orders` | Orders older than 60 days. Granted by Shopify on request only; the Dev Dashboard doesn't offer it |
| `read_draft_orders` | Draft orders |
| `read_files` | Files |
| `read_content` | Pages, blogs, articles, comments |
| `read_online_store_navigation` | URL redirects |
| `read_shipping` | Delivery profiles |
| `read_legal_policies` | Shop policies. Read on their own: without the scope everything else mirrors, the run warns that the policies weren't read, and the policy pages already mirrored stay as they are |

Rate limits: Shopify meters a query-cost bucket and reports it on every response; before each query the
client waits until the bucket can take one as costly as the last, and a throttled query is retried once
it refills.

Query cost: Shopify refuses before running it any query whose requested cost is over 1,000 points. Every
page size the importer asks for (the page itself and each nested connection it pages through) is a query
variable; a refused query is resent at once with every size scaled down by how far over it was (never
below 1), the sizes that fit are kept for the rest of the run, and they grow back toward the defaults (50
per page, 250 per nested page) while responses report room under the limit. `python -m tools.prove` checks
that every operation fits at page size 1 and that its static cost estimate matches the cost a live store
charged for it (`tests/fixtures/shopify-query-cost.json`, recorded by wp-woo-idempotent-importer's
`tools/calibrate-query-cost.php` against a development store).

Incremental runs read everything that has `updated_at` (files, collections, products, customers,
discounts, orders, draft orders, pages, articles); policies, blogs, comments, redirects and delivery
profiles are read in full.

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
| Code and automatic basic discounts (percentage or amount; every item, products, single variants or collections; minimum subtotal or quantity; dates; usage limit; every customer or some; every code) | **Pricing Rule** (coupon based with one **Coupon Code** per code on the Public Site's Webshop Store, or automatic) | Enforced: the discount and what it applies to, the minimum, dates, usage limit (of each code), one customer (a code) or several (automatic); and by the Webshop fork (card 405, recorded too): a code's once-per-customer limit (over all its codes) and a code for several customers. An amount not per item off every item is off the order. By the fork's card 407 (recorded too): an amount spread across some items (a rule on them, the amount taken once in proportion to their prices). Recorded and held (the rule disabled, its codes not redeemable) until the fork enforces them: one usage limit shared by several codes (card 405); customer segments (card 407: the fork takes a segment's customers and groups, but its members aren't read; the query's cost is recorded from a live store). |
| Free-shipping discounts (code and automatic) | **Pricing Rule** and its **Coupon Codes** | The free shipping (and its maximum shipping price) is recorded and holds the discount: the fork gives free shipping up to a maximum charge (card 405), but the countries it ships free to (`destinationSelection`) aren't read yet, as reading them changes the query's cost, which is recorded from a live store (decision 6). |
| Orders (paid, partially refunded, refunded) | Submitted **Sales Invoice** + **Payment Entry** | Exact totals, no stock movement, as for WooCommerce. A refunded order gets exactly Shopify's refunds: nothing is derived from its status. |
| Orders with tax-inclusive prices | The same, each tax in the prices an "On Net Total" row included in the line rates; shipping at its price less its tax, then that tax at its rate on the shipping row | Lines taxed alike: each row at its rate. Lines taxed differently (an exempt line, a tax only some lines bear): each tax on an account of its own (`<title> - <abbr>`) and each line's own rates as an **Item Tax Template** (`GST at 5, PST at 7 - <abbr>`). Kept only when ERPNext computes every tax row and the total to Shopify's amounts. |
| Duties | A tax charge ("Duties") on the tax account | |
| Orders awaiting payment | Draft **Sales Invoice** | Voided and cancelled orders aren't mirrored. |
| Refunds | Credit note + refund **Payment Entry** | Shipping or other amounts refunded beyond the lines become an adjustment line. |
| Fulfillments that shipped | Draft **Shipment** (see the README's fulfilment decision) | Tracking company, numbers and link; delivered once Shopify records the delivery. |
| Order timeline comments | **Comment** on the invoice | |
| Draft orders (open, invoice sent) | Draft **Quotation** to the customer (or the guest customer), its lines, discount, taxes and shipping, the total checked exactly | Never submitted. A completed draft became an order (mirrored as such): its Quotation is marked "[Removed at source]". |
| Files (images, videos, documents) | Public **File** | A file removed at the source stays. |
| Delivery profile (the general one): flat rates to zones of whole countries | Fixed selling **Shipping Rule** `<method> (<zone>) - <abbr>` per rate, with its countries, on the shipping account | A rate the source drops is disabled. |
| Product and article tags | Frappe document tags on the **Item** and **Blog Post** | A tag the source drops is removed; tags a person added are kept. |
| Images embedded in pages, policies and articles (on Shopify's CDN) | **File**; the HTML points at the copy | Images on other hosts are left as they are. |
| Pages | **Web Page** under the prefix | |
| Shop policies (refund, privacy, terms, shipping, …) | **Web Page** `<prefix>/policies/<type>` | Read by their own query (`read_legal_policies`). |
| Blogs, articles | **Blog Category**, **Blog Post** (+ **Blogger**) | |
| Published comments | **Comment** on the Blog Post | |
| URL redirects, old product/collection/page/blog/article URLs | **Website Route Redirect** under the prefix | |

## Gaps

| Shopify | Why it isn't mirrored |
|---|---|
| Gift cards and their balances | No first-party gift-card ledger. |
| Buy X Get Y and app discounts | Planned (card 386): their fields need new query fields whose cost must be recorded from a live store first (an operator decision). |
| Delivery profiles for some products only; zones limited to provinces or "rest of world"; rates with price or weight conditions; carrier-calculated rates | A Shipping Rule is store-wide and per country; outside a rule's band ERPNext charges nothing instead of not offering the rate; ERPNext has no carrier rates. Reported as warnings. |
| Menus | Website Settings' top bar and footer are global; a Public Site has no menu of its own (the router would need one). |
| Shop settings, metafields, metaobjects | No first-party home for them as such. |
| Product reviews | Shopify has no reviews API (the PHP importer reads a review app's CSV export; later phase). |
| Selling plans, subscriptions, markets, B2B, staff, abandoned checkouts, payouts | No first-party home. |
| Customer passwords, saved payment methods | Not exportable. |
