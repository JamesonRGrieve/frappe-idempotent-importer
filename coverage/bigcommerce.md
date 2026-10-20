<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: BigCommerce (REST Management API) → ERPNext

The single source of truth for what the BigCommerce adapter mirrors. The adapter reads BigCommerce's REST
Management API, v2 and v3 (the OpenAPI documents pinned by hash in `tools/specs.py`; every fixture is
proven against them). Its reader, mappers and fixtures are ported from wp-woo-idempotent-importer.

Access: a store-level API account (Settings → Store-level API accounts) with read-only scopes for Products,
Customers (customer groups too), Orders, Marketing, Content, Sites & Routes and Store Information. The connection holds the
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
| Customer groups | **Customer Group** `<name> - <abbr>` | Customers are in their group. |
| Customers | **Customer**, **Contact**, **Address** | Every saved address, the first as billing. |
| Legacy coupons: percentage (all, some products, some categories), amount per item (every product, some products or categories), amount off the order, free shipping, an amount off shipping | **Pricing Rule** + **Coupon Code** | Enforced: the discount and what it applies to, minimum spend, expiry, use limit. Recorded and held (rule disabled, code not redeemable) until the Webshop fork enforces them: per-customer limits, countries and shipping methods, free shipping and shipping discounts (card 405); an order amount off only some products (card 407). A disabled coupon disables its rule. |
| Promotions, automatic or with codes (one or many) | **Pricing Rule** (automatic, or coupon based with a **Coupon Code** per code) | Enforced: the first rule's percentage or amount off the order or off products, categories or variants (per item), a gift item (once, or for every so many bought), a discount on other items than those the condition asks for (one product or category); minimum spend and quantity; dates; the use limit of a single code and each code's own use limit and dates; customer groups; the promotion's currency. Recorded and held: per-customer limits (of a code: that code alone), a use limit shared by several codes or of an automatic promotion, countries, schedules, combination settings, free shipping and shipping discounts (card 405); caps, amounts once across items, item limits and choices, sale-item exclusions, brand, option or combined item criteria, conditions beyond spend, quantity and items, sets at a fixed price, excluded groups, order counts and segments (card 407); other channels and further rules (an operator decision). |
| Customer group discount rules (everything, a category, a product: a percentage, an amount off, a fixed price) | Automatic **Pricing Rule** for the **Customer Group** | One per rule. A rule through a price list is recorded and held (an operator decision). The guest group's rules apply to group members only: the guest customer isn't in a group. |
| Bulk pricing rules | Automatic **Promotional Scheme** on the product, a slab per quantity band | A percentage or amount off each unit, or a fixed unit price. |
| Orders (every status but incomplete, cancelled, declined) | **Sales Invoice** (submitted when the payment is captured, paid or refunded, else draft) + **Payment Entry** | BigCommerce's totals, exactly; number `#<order id>`. |
| Orders paid in another currency than the store's default | A multi-currency **Sales Invoice** in the transactional currency at BigCommerce's `store_default_to_transactional_exchange_rate` (inverted: store currency per order currency), paid at that rate | BigCommerce books no total in the store's currency, so only the order-currency total is checked (see the README's currency decision). |
| Order lines | Invoice lines at their pre-tax prices before discounts, on the variant's item | |
| Coupon and manual discounts | The invoice's discount | |
| Taxes (rows by tax name) | One charge per tax | The order's total tax when BigCommerce gives no rows. |
| Each shipping address's cost and handling | Shipping and fee charges | The first shipping address is the invoice's. |
| Gift wrapping, order fees | Fee charges | |
| Refunds | Credit note (returned product lines, plus an adjustment line for the rest) + refund **Payment Entry** | |
| Shipments | Draft **Shipment** | Carrier, method, tracking number and link, and the products shipped. |
| Staff notes | **Comment** | The customer's message as the remarks. |
| Content pages (page, raw) | **Web Page** | Hidden and customers-only pages are unpublished. |
| The blog | **Blog Category** named after its blog page | "Blog" when the store has no blog page. |
| Blog posts | **Blog Post** by its author (**Blogger**) | Tags; unpublished posts are unpublished. |
| Approved product reviews | **Item Review** | Rating out of 5. |
| Storefront 301 redirects | **Website Route Redirect** | A redirect to a product, category, brand, page or post targets that object's storefront path, which redirects on to the mirrored record. |

## Gaps

| BigCommerce | Why it isn't mirrored |
|---|---|
| Product-level stock on a product with option variants | Variants are mirrored untracked (a warning). |
| Contact-form and link pages | A form and a menu link, not content (reported). |
| Pending and disapproved reviews | Not published at the source. |
| Blog post thumbnails | Blog Post has no image of its own. |
| Abandoned checkouts (incomplete orders), cancelled and declined orders | Not sales. |
| Store credit, customer attributes and form fields | No first-party home for a storefront's custom fields. |
| Product options and modifiers that aren't variants, related products, videos | Planned: product options (card 408); related products and videos have no first-party Item field. |
| Wishlists | Planned (card 386): the Webshop's Wishlist. |
| Brands' own pages, carts, channels, sites, themes, widgets, scripts, menus | Storefront presentation (menus: card 406; abandoned carts: card 415). |
| Store settings, tax and shipping configuration | ERPNext's own settings; mirrored orders carry what BigCommerce charged. |
