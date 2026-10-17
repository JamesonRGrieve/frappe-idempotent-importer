<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: BigCommerce (REST Management API) → ERPNext

The single source of truth for what the BigCommerce adapter mirrors. The adapter reads BigCommerce's REST
Management API, v2 and v3 (the OpenAPI documents pinned by hash in `tools/specs.py`; every fixture is
proven against them). Its reader, mappers and fixtures are ported from wp-woo-idempotent-importer.

Access: a store-level API account (Settings → Store-level API accounts) with read-only scopes for Products,
Customers, Orders, Marketing, Content, Sites & Routes and Store Information. The connection holds the
storefront URL, the store hash (the code after `/stores/` in the account's API path) as the account, and
the account's access token as the secret; requests carry it in `X-Auth-Token`. Products, customers and
orders are incremental (by modification time); the other kinds are read in full. BigCommerce's quota is
per plan and reported on every response: requests left in the window (`X-Rate-Limit-Requests-Left`) and
milliseconds until it resets (`X-Rate-Limit-Time-Reset-Ms`)
(docs.bigcommerce.com/docs/start/best-practices/api-rate-limits); the client waits for the reset when the
window runs out, and a 429 or server error is retried with backoff and then pauses the run.

## Mirrored

| BigCommerce | ERPNext home | Notes |
|---|---|---|
| Categories (the category tree) | **Item Group** `<name> - <abbr>` | Storefront path as the route, the image, the parent category. |
| Products (physical, digital) | **Item** (+ **Website Item**) | Hidden products are unpublished; a deleted product is disabled. Description HTML, brand as **Brand**, custom fields as Website Item specifications, images (thumbnail first, largest size). Digital products aren't stock items. |
| Variants | One **Item** (a product whose only variant has no option values) or a template with variant **Items** and **Item Attributes** | A variant's own price, sale price, weight and GTIN/UPC win; else the product's. SKU, GTIN (else UPC) as barcode. |
| Price, sale price | **Item Price**; sale as a **Pricing Rule** | A zero sale price is none. |
| Stock (`inventory_tracking` product or variant) | **Stock Reconciliation** | Untracked products aren't counted. |
| Weight in the store's unit | Item weight and UOM | lbs, oz, kg, g. |
| Old storefront product and category URLs | **Website Route Redirect** (301) | |
| Customers | **Customer**, **Contact**, **Address** | Every saved address, the first as billing. |
| Legacy coupons: percentage (all, some products, some categories), amount per item (some products or categories), amount off the order | **Pricing Rule** + **Coupon Code** | Minimum spend, expiry and use limit. A disabled coupon disables its rule. |
| Coupon promotions with one rule taking a percentage or amount off the order | **Pricing Rule** + one **Coupon Code** per code | At most a minimum-spend condition; dates and use limit. |
| Orders (every status but incomplete, cancelled, declined) | **Sales Invoice** (submitted when the payment is captured, paid or refunded, else draft) + **Payment Entry** | BigCommerce's totals, exactly; number `#<order id>`. |
| Order lines | Invoice lines at their pre-tax prices before discounts, on the variant's item | |
| Coupon and manual discounts | The invoice's discount | |
| Taxes (rows by tax name) | One charge per tax | The order's total tax when BigCommerce gives no rows. |
| Each shipping address's cost and handling | Shipping and fee charges | The first shipping address is the invoice's. |
| Gift wrapping, order fees | Fee charges | |
| Refunds | Credit note (returned product lines, plus an adjustment line for the rest) + refund **Payment Entry** | |
| Shipments, staff notes | **Comment** each | Carrier, tracking number and link. The customer's message as the remarks. |
| Content pages (page, raw) | **Web Page** | Hidden and customers-only pages are unpublished. |
| The blog | **Blog Category** named after its blog page | "Blog" when the store has no blog page. |
| Blog posts | **Blog Post** by its author (**Blogger**) | Tags; unpublished posts are unpublished. |
| Approved product reviews | **Item Review** | Rating out of 5. |
| Storefront 301 redirects | **Website Route Redirect** | A redirect to a product, category, brand, page or post targets that object's storefront path, which redirects on to the mirrored record. |

## Gaps

| BigCommerce | Why it isn't mirrored |
|---|---|
| Free-shipping and shipping-discount coupons | ERPNext coupons can't discount shipping (reported). |
| Coupons limited to countries or shipping methods; per-customer use limits | No ERPNext coupon equivalent (reported). |
| Automatic promotions; promotions with several rules, item, shipping or gift actions, capped discounts, customer, address or schedule conditions; codes with their own limits | No exact ERPNext coupon equivalent (reported). |
| Product-level stock on a product with option variants | Variants are mirrored untracked (a warning). |
| Contact-form and link pages | A form and a menu link, not content (reported). |
| Pending and disapproved reviews | Not published at the source. |
| Blog post thumbnails | Blog Post has no image of its own. |
| Orders in another currency than the store's default | BigCommerce records the exchange rate but no total in the store's currency, so the invoice's company-currency total can't be checked exactly (reported). |
| Abandoned checkouts (incomplete orders), cancelled and declined orders | Not sales. |
| Customer groups, store credit, customer attributes and form fields | No first-party home for a storefront's segments and custom fields. |
| Product options and modifiers that aren't variants, bulk pricing rules, related products, videos | No first-party Item field. |
| Brands' own pages, wishlists, carts, channels, sites, themes, widgets, scripts, menus | Storefront presentation, not catalog or order data. |
| Store settings, tax and shipping configuration | ERPNext's own settings; mirrored orders carry what BigCommerce charged. |
