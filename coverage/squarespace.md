<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Squarespace (Commerce and Profiles APIs) → ERPNext

The single source of truth for what the Squarespace adapter mirrors. The adapter reads Squarespace's
Commerce APIs (Products v2, Store Pages, Orders, Transactions, Discounts) and Profiles API (the OpenAPI
documents pinned by hash in `tools/specs.py`; every fixture is proven against them). Its reader, mappers and
fixtures are ported from wp-woo-idempotent-importer.

Access: an API key (Settings → Advanced → Developer API Keys) with read-only access to Products, Inventory,
Orders, Transactions and Profiles, entered as the connection's secret with the site's URL; requests carry
it as a bearer token with this app's User-Agent, which Squarespace requires. Products and orders are
incremental: Squarespace filters by a modification window with both ends (the time the run started closes
it). Squarespace allows 300 requests a minute and, after a 429, refuses requests for a minute without
Retry-After (developers.squarespace.com/commerce-apis/rate-limits): the client paces itself to the rate and
sits out the minute when limited anyway (which pauses the run).

## Mirrored

| Squarespace | ERPNext home | Notes |
|---|---|---|
| Store pages | **Item Group** `<name> - <abbr>` | The page's URL slug as the route; its old URL redirects. |
| Physical and service products | **Item** (+ **Website Item**) | One Item (a single variant without attributes) or a template with variant **Items** and **Item Attributes** in the product's attribute order. Hidden products are unpublished; a deleted product is disabled. Services aren't stock items. |
| Downloads | One non-stock **Item** priced at the product | Squarespace's API doesn't give the file (a warning). |
| Variants | Variant **Items** keyed by their Squarespace variant id (what an order line names) | SKU, GTIN as barcode, weight in kg or lb, the variant's image. |
| Base price, sale price | **Item Price**; sale as a **Pricing Rule** | The sale counts while the variant is on sale. |
| Stock | **Stock Reconciliation** | Unlimited stock is untracked. |
| Product images, tags | **File**; the Item's image; **Tag** | Images by their position. |
| Old product URLs | **Website Route Redirect** (301) | |
| Profiles that are customers or have an account | **Customer**, **Contact**, **Address** | The profile's one address with its phone. |
| Discounts, promo codes and automatic: a percentage or amount off the order (any order, or above a minimum cart total), a percentage or amount off one product, free shipping, buy X get Y | **Pricing Rule** (+ a **Coupon Code** for a promo code) | Enforced: the discount and what it applies to, start and expiry dates, a code's site-wide use limit; buy X get Y as the same product free for every X, or a discount on one other product once X are bought. Enforced by the Webshop fork (card 405, recorded too): a promo code's once-per-customer limit. Recorded and held (rule disabled, code not redeemable) until the fork enforces them: free shipping (on one of Squarespace's fulfilment options or shipping options, which aren't mirrored as Shipping Rules), an automatic discount's once-per-customer limit and use limit (card 405); an amount off one product (Squarespace doesn't say per item or in total), how many reward items are discounted and how often per cart, several products to buy or reward, discounts on subscription or payment-plan payments (card 407). An expired discount disables its rule. |
| Orders (every payment state) | **Sales Invoice** (submitted when paid or refunded, else draft) + **Payment Entry** | Squarespace's totals, exactly; number `#<order number>`. The payment's gateway and card as the method, its transaction id as the reference. |
| Order lines | Invoice lines at the unit price paid, on the variant's item | Option choices and product-form answers in the line's name. |
| Promo discounts | The invoice's discount | |
| Taxes | One charge per tax name, from the order's transactions (its sale and shipping items) | The order's tax total when it has no transactions. |
| Tax-inclusive orders (`priceTaxInterpretation` INCLUSIVE) | Each tax an "On Net Total" row included in the line prices, at each line's rates from its transaction sale item (paired by amount; different rates per line through an **Item Tax Template** and a tax account per tax); shipping at its price less its taxes, then each tax at its rate | Kept only when ERPNext computes every row and the total to Squarespace's amounts. An order with no transactions yet includes its tax total at the one rate that comes to it. |
| Shipping lines | Shipping charges | One for the shipping total when the order has none. |
| Refunds of the order's payments | Credit note (one adjustment line) + refund **Payment Entry** | Squarespace doesn't say which lines a refund was for. |
| Fulfillments | Draft **Shipment** | Carrier, service, tracking number and link. |
| Internal notes | **Comment** each | |

## Gaps

| Squarespace | Why it isn't mirrored |
|---|---|
| Gift card products and gift card sales | No ERPNext home for a stored-value liability (reported). |
| Cancelled, failed and test-mode orders | Not sales. |
| Mailing-list subscribers and donors | Not customers. |
| Download files | Not in the API. |
| Variant shipping dimensions and MPN, SEO titles and descriptions, marketing consent | No first-party Item or Customer field. |
| Website settings (time zone, measurement standard, currency) | ERPNext's own settings; the importer doesn't change them. |
| Pages, blog posts, navigation, redirects, reviews | Squarespace has no public API for them. |
| Subscriptions, donations, abandoned carts | Not catalog or order data the importer mirrors. |
