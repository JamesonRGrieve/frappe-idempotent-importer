<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Wix (REST APIs) → ERPNext

The single source of truth for what the Wix adapter mirrors. The adapter reads Wix's REST APIs: Stores
(catalog V1 or V3, whichever the site runs), Categories, Contacts, eCommerce orders, payments and
fulfillments and discount rules, Stores coupons, Blog, Reviews and SEO redirects (the schemas committed in `tests/schemas/wix.json`
and pinned in `tools/specs.py`; every fixture is proven against them). Its reader, mappers and fixtures are
ported from wp-woo-idempotent-importer.

Access: an account API key (Wix account settings → API Keys) with read permissions for those APIs, entered as
the connection's secret, with the site's URL and its site ID as the account; requests carry the key in
`Authorization` and the site in `wix-site-id`. Contacts and orders are incremental (by `updatedDate`), as are a
V3 catalog's categories and products; an incremental product run then re-reads the products whose inventory
changed (an inventory change doesn't update its product). Wix documents no request rate or rate-limit headers;
a 429 carries no timing hint and its docs say to wait a minute
(dev.wix.com/docs/api-reference/articles/work-with-wix-apis/troubleshooting/troubleshooting.md), so the client
sits out a minute on a 429, which pauses the run.

## Mirrored

| Wix | ERPNext home | Notes |
|---|---|---|
| V3 categories (Stores tree, hidden ones too), V1 collections | **Item Group** `<name> - <abbr>` | Plain-text description, image, a category's parent. |
| Products (V3 or V1) | **Item** (+ **Website Item**) | One Item for a product without options (its one variant), else a template with variant **Items** and **Item Attributes** (option choice names). Hidden products are unpublished; a deleted product is disabled. Downloads aren't stock items. Brand as **Brand**. The old product page (`/product-page/<slug>`) redirects to the item. |
| Variants | Variant **Items** keyed by product and variant id (Wix variant ids repeat across products) | SKU, barcode, weight in kg or lb (V3), the variant's image. |
| Price, compare-at (V3) or discounted (V1) price | **Item Price**; sale as a **Pricing Rule** | A compare-at price above the selling price is the price and the selling price the sale. |
| Stock (V3 inventory items summed over locations; V1 variant stock) | **Stock Reconciliation** | Untracked variants aren't counted. |
| Product media | **File**; the Item's image | Videos aren't images. |
| Contacts | **Customer**, **Contact**, **Address** | Every saved address (the one tagged billing, else the default, else the first, as billing); the company. |
| Stores coupons: a percentage off (the store, a product or a collection), an amount off the order or some products, a fixed price, buy X get Y, free shipping | **Pricing Rule** + **Coupon Code** | Enforced: the discount and what it applies to (buy X get Y as the same item free for every X), minimum subtotal, start and expiry, use limit. Recorded and held (rule disabled, code not redeemable) until the Webshop fork enforces them: free shipping and per-customer limits (card 405); an amount off some products (Wix doesn't say per item or in total), coupons limited to one item, other Wix apps' coupons and subscriptions (card 407). A deactivated coupon disables its rule. |
| eCommerce discount rules (automatic) | Automatic **Pricing Rule** | Enforced: a percentage or amount off, or a fixed price, on Wix Stores products (or every item); a subtotal or item-quantity range (minimum and maximum); the active time. Recorded and held: "3 for $10" prices, other apps' items and custom filters (Wix documents their parameters only by example), triggers on other items than those discounted, "or", custom and member triggers (card 407). An inactive or expired rule disables its rule. |
| eCommerce orders (approved) | **Sales Invoice** (submitted when paid, partly or fully refunded, else draft) + **Payment Entry** | Wix's totals, exactly; number `#<order number>`. The payment's method name and provider transaction id. |
| Order lines | Invoice lines at their prices before discounts, on the variant's item | The chosen options in the line's name. |
| Order discount | The invoice's discount | |
| Taxes | One charge per tax name, summed over the lines, the shipping and the fees | The order's tax total when nothing itemises it. |
| Tax-inclusive orders (`taxIncludedInPrices`) | Lines at their prices with tax; each line tax an "On Net Total" row included in the line prices at the line's own rates (different rates per line through an **Item Tax Template** and a tax account per tax); the shipping option and each fee at their prices before tax, then each of their taxes at its rate | Kept only when ERPNext computes every row and the total to Wix's amounts. |
| Shipping option, additional fees | Shipping and fee charges | |
| Refunds that returned money | Credit note (the refunded lines at the price paid, an adjustment for their tax and the rest) + refund **Payment Entry** | Failed or pending refunds wait. |
| Fulfillments | Draft **Shipment** | Shipping provider, tracking number and link, and the lines fulfilled. |
| The site's blog and its categories | **Blog Category** ("Blog" at /blog for posts in no category) | |
| Published blog posts | **Blog Post** in its first category | Rich content rendered to HTML (paragraphs, headings, lists, quotes, code, dividers, images, links and text marks), its images copied into **Files**; hashtags as **Tags**; excerpt. |
| Published Stores reviews (approved, or without moderation) | **Item Review** | |
| Site redirects | **Website Route Redirect** | A target on the site redirects on to its mirrored object. |

## Gaps

| Wix | Why it isn't mirrored |
|---|---|
| Gift card products and sales, subscriptions | No ERPNext home for a stored-value liability (reported). |
| Cancelled, rejected and declined orders | Not sales. |
| Product modifiers, info sections, ribbons, price per unit, subscriptions | No first-party Item field (modifiers are a warning). |
| V1 product weights | V1 gives no unit (a warning). |
| Hidden variants | ERPNext sells them (a warning). |
| Posts with galleries, videos, embeds, buttons, tables, polls or other blocks | No HTML the importer writes exactly (reported). |
| Group redirects (every path under a source) | Website Route Redirects match exact paths (reported). |
| Site pages and menus | Wix has no API that returns a page's content. |
| Reviews in moderation or rejected | Not published. |
| Site properties (business info, locale) | ERPNext's own settings. |
