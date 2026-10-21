<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Magento / Adobe Commerce (REST API) → ERPNext

The single source of truth for what the Magento adapter mirrors. The adapter reads one store view of a
Magento 2.4 / Adobe Commerce store through its REST API (`<store URL>/rest/<store code>/V1/…`; the OpenAPI
document pinned in `tools/specs.py`, every fixture proven against it). Its reader, mappers and fixtures are
ported from wp-woo-idempotent-importer.

Access: an integration (System → Integrations) with read access to the catalog, customers and customer groups,
cart price rules,
sales and content, its access token entered as the connection's secret; since 2.4.4 the store must allow
integration tokens as standalone bearer tokens (`oauth/consumer/enable_integration_as_bearer`). The connection
holds the store's URL and the store view code as the account (blank is `default`). Customers and orders are
incremental (by `updated_at`); products are read in full (Magento doesn't move a product's `updated_at` when
its stock or a configurable product's child changes), as are categories, rules and CMS pages. Magento Open
Source and Adobe Commerce document no request-rate limit for these REST endpoints (2.4.7's optional rate
limiting, off by default, covers only cart payment and order placement:
developer.adobe.com/commerce/webapi/get-started/rate-limiting), so requests aren't paced; a 429 from a store
behind a CDN is waited out for its Retry-After, else backed off, and a long wait pauses the run. Searches ask
for 100 items, under the default "Maximum Page Size" of 300; a list ends when the items read reach its
`total_count` (Magento answers a page past the end with the last page again).

## Mirrored

| Magento | ERPNext home | Notes |
|---|---|---|
| Categories below the store roots | **Item Group** `<name> - <abbr>` | URL key, description, image, parent (none directly under a root). The old URL (url path + `.html`) redirects. |
| Simple, virtual and downloadable products | **Item** (+ **Website Item**) | Virtual and downloadable products aren't stock items. A disabled product is unpublished; a deleted one is disabled. The old URL (url key + `.html`) redirects. |
| Configurable products | Template **Item** with a variant **Item** per enabled child product on the store's website | **Item Attributes** from the configurable attributes' option labels; a variant is keyed by the child product's id, the one an order's child line names. |
| Price, special price | **Item Price**; sale as a **Pricing Rule** | A special price below the price; its dates aren't kept (a warning). |
| Stock items | **Stock Reconciliation** | Products whose stock isn't managed aren't counted. |
| Weight in the store's unit (kgs, lbs) | Item weight and UOM | |
| Enabled gallery images | **File**; the Item's image | The base image first. |
| Customer groups | **Customer Group** `<code> - <abbr>` | Customers are in their group. NOT LOGGED IN is mirrored too; the connection's guest customer isn't in it, so a rule for guests alone applies to nobody (never more widely than at the source). |
| Customers | **Customer**, **Contact**, **Address** | Every saved address, the default billing first; region codes. |
| Cart price rules, with coupons or without (automatic) | **Pricing Rule** (+ a **Coupon Code** per coupon) | Enforced: a percentage or amount off each item (every item or some categories), an amount off the cart, buy X get Y free (the same item, for every X bought); minimum subtotal and quantity; from and to dates; customer groups (one, or every group, with coupons; several, automatic); each coupon's use limit (else the rule's uses per coupon) and expiry. Enforced by the Webshop fork (card 405, recorded too): per-customer limits of rules with coupons (a coupon's own: that coupon alone), a percentage discount applied to shipping too. Recorded and held (rule disabled, codes not redeemable) until the fork enforces them: per-customer limits of rules without coupons, an amount discount applied to shipping (Magento spreads one amount over items and shipping together), free shipping (for the shipment with matching items, or the whole cart) (card 405); the maximum quantity discounted of a discount off the cart or of buy X get Y, discount steps, other item conditions (SKUs, attributes, combinations), other cart conditions (card 407). Enforced by the Webshop fork (card 407, recorded too): the maximum quantity discounted of a percentage or amount off some items (so many units of each cart item, as Magento counts it), an amount off the cart spread over some categories, several customer groups on coupons. An inactive rule, or one not on the store's website, disables its rule. |
| Tier prices (simple, virtual and downloadable products, and configurable products' children) | Automatic **Promotional Scheme** on the item, per customer group (every group for ALL GROUPS), a slab per quantity | A fixed unit price or a percentage off, from each tier's quantity up to the next; tier prices of another website aren't this store's. |
| Orders of the store's website | **Sales Invoice** (submitted when paid, else draft) + **Payment Entry** | Magento's totals in the order's currency, exactly; number `#<increment id>`. The payment method title and last transaction id. |
| Order lines (a configurable product's parent and child lines as one) | Invoice lines at their row totals, on the child product bought | |
| Discount, applied taxes, shipping | The invoice's discount; one charge per applied tax; shipping | The order's tax total when Magento itemises none. |
| Orders in another currency than the store's base | The invoice in the order's currency at the rate Magento's base total implies (else Magento's `base_to_order_rate`), its total in the company's currency checked against Magento's base total | Only when the base currency is the company's (see the README's currency decision). |
| Credit memos | Credit note (the refunded lines at their row totals, an adjustment for the rest) + refund **Payment Entry** | The first comment as the reason. |
| Shipments | Draft **Shipment** | Carrier and tracking numbers from the tracks, and the items shipped. |
| Order comments | **Comment** each | |
| CMS pages | **Web Page** | The identifier's last segment as the slug; inactive pages unpublished; the old URL redirects (not the home page). Content directives are kept as written. |

## Gaps

| Magento | Why it isn't mirrored |
|---|---|
| Bundle products | Planned: fixed bundles as Product Bundles (card 386), dynamic ones (card 409). |
| Grouped and gift card products; gift card order lines | Planned: gift cards (card 403); grouped products (card 409). |
| Catalog price rules | Operator decision: Magento Open Source and Adobe Commerce expose no REST API for them (the pinned 2.4.9 schema has none), so they can't be read without a store-side extension or database access. |
| Cancelled orders | Not sales. |
| Custom options, downloadable files | Planned: custom options (card 408); downloadable files have no first-party Item field (warnings). |
| Products not on the store's website, disabled child products | Not sold in this store view. |
| Other store views' translations | One connection mirrors one store view. |
| CMS blocks, widgets, URL rewrites, reviews (Magento Open Source keeps no review REST API) | Not in the read API, or storefront presentation. |
| Store configuration, tax rules, shipping methods | ERPNext's own settings; orders carry what Magento charged. |
