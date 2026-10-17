<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Ecwid (REST API v3) → ERPNext

The single source of truth for what the Ecwid adapter mirrors. The adapter reads Ecwid's REST API v3
(`<host>/api/v3/<store id>/<resource>`; the schema committed in `tests/schemas/ecwid.json` and pinned in
`tools/specs.py`, every fixture proven against it). Its reader, mappers and fixtures are ported from
wp-woo-idempotent-importer.

Access: a custom app (Ecwid admin → Apps → custom app) with read scopes for the catalog, customers, discount
coupons, orders, reviews and the store profile; the connection holds the store's URL, its store ID as the
account and the app's secret access token as the secret (a bearer token that doesn't expire). Products,
customers, discount coupons, orders and reviews are incremental (`updatedFrom`, a UNIX timestamp); categories
and redirects are read in full. Ecwid accepts up to 600 requests a minute per token and refuses the rest with a
429 and a Retry-After (docs.ecwid.com/api-reference/rest-api/rest-api-overview): requests are paced under it
(one every 0.1 seconds) and a 429 waits out its Retry-After. Products are read with clean URLs, so their URLs
are storefront paths.

## Mirrored

| Ecwid | ERPNext home | Notes |
|---|---|---|
| Categories (hidden ones too) | **Item Group** `<name> - <abbr>` | Custom (else generated) slug, description, image, parent; the old URL redirects. |
| Products | **Item** (+ **Website Item**) | One Item without variations, else a template with a variant **Item** per variation and **Item Attributes** from the options it selects. A disabled product is unpublished; a deleted one is disabled. A product that needs no shipping isn't a stock item. The UPC attribute as barcode, the brand attribute as **Brand**. |
| Variations | Variant **Items** keyed by their Ecwid id (what an order item names) | SKU, UPC, weight in the store's unit, the variation's image; a variation falls back to the product for price, stock policy and weight. |
| Price, compare-to price | **Item Price**; sale as a **Pricing Rule** | A compare-to price above the price is the price and the price the sale. |
| Stock (`unlimited` false) | **Stock Reconciliation** | |
| Media images | **File**; the Item's image | The main image first, at full size. |
| Customers | **Customer**, **Contact**, **Address** | The billing person and the first saved shipping address; the default phone contact. |
| Active discount coupons: a percentage off (optionally of some products or categories), an amount off the order | **Pricing Rule** + **Coupon Code** | Minimum subtotal, a single use, launch and expiry dates. A paused, expired or used-up coupon disables its rule. |
| Orders | **Sales Invoice** (submitted when paid, partly or fully refunded, else draft) + **Payment Entry** | Ecwid's totals in the store's currency, exactly; the order id (with the store's prefix and suffix) as the number. |
| Order items | Invoice lines at their prices before discounts, on the variation's item | The selected options in the line's name. |
| Discount, taxes, shipping option, handling fee, surcharges | The invoice's discount; one charge per tax name (items, shipping, surcharges); shipping and fee charges | The order's tax total when Ecwid itemises none. |
| Gross-price orders (`pricesIncludeTax`) | Items at their gross prices, the discount as Ecwid gives it (with tax, off the grand total); each item tax an "On Net Total" row included in the prices at the item's own rates (different rates per item through an **Item Tax Template** and a tax account per tax); shipping and surcharges at their prices before tax, then each of their taxes at its rate | Kept only when ERPNext computes every row and the total to Ecwid's amounts. |
| Refunds | Credit note (one adjustment line: Ecwid doesn't say which items) + refund **Payment Entry** | Refunds have no ids, so each is keyed by its place in the order's list (Ecwid only adds them). |
| Tracking number, shipping labels, private notes | **Comment** each | |
| Published reviews | **Item Review** | |
| Instant Site redirects | **Website Route Redirect** | A target on the store's site redirects on to its mirrored object. |

## Gaps

| Ecwid | Why it isn't mirrored |
|---|---|
| Gift card products | No ERPNext home for a stored-value liability (reported). |
| Shipping and free-shipping coupons, an amount off some products, once-per-customer coupons, coupons for new or repeat customers, some shipping methods or the front page, codes for both products and categories | No exact ERPNext coupon equivalent (reported). |
| Cancelled and incomplete (unfinished online payment) orders | Not sales. |
| Reviews awaiting moderation | Not published. |
| Product options without variations, wholesale price tiers, files, subscriptions, dimensions | No first-party Item field (warnings). |
| Weights in carats | No ERPNext UOM (a warning). |
| Instant Site pages, store profile settings, taxes and shipping configuration | No read API for page content; the rest is ERPNext's own settings. |
