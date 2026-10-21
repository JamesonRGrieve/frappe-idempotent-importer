<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: WooCommerce (REST API `wc/v3`, WordPress REST API, Redirection) → ERPNext

The single source of truth for what the WooCommerce adapter mirrors. **Mirrored** means it lands in a
first-party home (ERPNext, Webshop, the blog app, Frappe's website); **Gap** means there is none, or the
home can't hold it exactly, so it is not approximated. Gaps are decisions for the operator.

Access: a WordPress **Application Password** (reads everything: drafts, private content, the
Redirection plugin's redirects) or a WooCommerce **consumer key and secret** with Read permission (reads
the store; WordPress content is then read anonymously, so only published pages and posts, and no
redirects). Rate limits: WooCommerce documents none for `wc/v3` (its rate limiting covers only the Store
API), so requests aren't paced; a host's own 429s and 5xx are retried with backoff and pause the run.

Incremental runs read products, coupons, orders, pages and posts changed since the last clean run
(`modified_after`, WooCommerce 5.8+ / WordPress 5.7+); categories, customers, reviews and redirects are
read in full every run (they have no such filter).

## Mirrored

| WooCommerce | ERPNext home | Notes |
|---|---|---|
| Product categories (name, slug, parent, description, image) | **Item Group** `<name> - <abbr>` under the connection's Item Group, routed `<prefix>/<slug>` | A renamed category renames its Item Group. |
| Simple products | **Item** `<abbr>-<SKU>` (or `<abbr>-Product-<id>` without a SKU): name, description, group (first category), brand, image, weight, stock tracking | Status, catalog visibility and the store's other categories go to the Website Item. |
| Variable products and their variations | Template **Item** with its variant attributes, a variant **Item** per variation (`<abbr>-<variation SKU>`), **Item Attribute** values added as needed | A variation removed at the source is disabled. |
| Regular price | **Item Price** on the connection's Price List | |
| Sale price (and its dates) | **Pricing Rule** setting the item's rate, valid over the sale's dates | A sale that ends disables its rule. |
| Stock quantity (managed stock) | **Stock Reconciliation** into the connection's warehouse, when the count differs | Absolute counts at zero valuation (see README, "Decisions to confirm"). |
| GTIN/UPC/EAN (`global_unique_id`) | **Item Barcode** | ERPNext keeps a barcode on one item only: a barcode another item holds stays there. |
| Images | **File** (public), the first as the item's and Website Item's image | One File per image URL per connection. |
| Brands (WooCommerce 9.6 Brands) | **Brand** (shared by name) | |
| Published + visible products | **Website Item** on the Public Site's Webshop Store: published flag, route `<prefix>/<slug>`, short and long description, other categories as website item groups, non-variation attributes as specifications | Hidden or draft products are unpublished. |
| Customers | **Customer** (individual, or company when there is only a company name), **Contact** (name, email, phone), **Address** per billing/shipping address | Linked to each other. |
| Coupons: percent, fixed cart, fixed product (every product too); product or category limits; minimum and maximum spend; expiry; usage limit | **Pricing Rule** (coupon based, on the company) + **Coupon Code** on the Public Site's Webshop Store | Enforced: the discount, products or categories, minimum and maximum spend, expiry, usage limit. Recorded and held (the rule disabled, the code not redeemable) until the Webshop fork enforces them: email limits, excluded products and categories, sale-item exclusion, per-user limits, free shipping and categories beside products (card 405); a limit on the items discounted and a fixed cart amount off only some products (card 407). Subscription coupon types are planned (card 410). |
| Orders: processing, completed, refunded | Submitted **Sales Invoice** (lines at their prices before discounts, the coupon discount on the net total, tax, shipping and fee lines as Actual charges) + **Payment Entry** into the deposit account | The invoice must total exactly what WooCommerce charged, or it isn't kept. Never moves stock. |
| Orders: pending, on hold | Draft **Sales Invoice**, submitted (with its payment) once paid | |
| Shipment tracking (the WooCommerce Shipment Tracking extension's items in the order's meta) | Draft **Shipment** | Provider, tracking number, link and ship date. WooCommerce core keeps no shipments. |
| Orders in another currency (WooPayments Multi-Currency, Aelia Currency Switcher) | A multi-currency **Sales Invoice** at the rate the extension recorded in the order's meta | Aelia's base-currency total is checked exactly. |
| Orders: cancelled, failed, checkout drafts | Not mirrored (an earlier copy is cancelled) | |
| Refunds | Submitted credit note (**Sales Invoice** return) + refund **Payment Entry** | Amount-only refunds become one adjustment line on the connection's fallback item. |
| Order notes (private and to the customer) | **Comment** on the invoice | |
| Customer note, guest details, order addresses | The invoice's remarks | A guest is the connection's guest customer. |
| Pages | **Web Page** routed `<prefix>/<parent>/<slug>`, published when published | |
| Post categories | **Blog Category** `<name> - <abbr>` | |
| Posts | **Blog Post** routed `<prefix>/<slug>` in its first category, by a **Blogger** per author | |
| Product tags, post tags | Frappe document tags on the **Item** and **Blog Post** (post tags read by name from `wp/v2/tags`) | A tag the source drops is removed; tags a person added are kept. |
| Images embedded in pages and posts (the store's own host or its `wp-content/uploads`) | **File**; the HTML points at the copy | Images on other hosts are left as they are. |
| Product reviews (approved) | **Item Review** on the product's Website Item, rating as a fraction of 5 | |
| Redirection plugin redirects (plain URL redirects) | **Website Route Redirect** (source and target under the Public Site's prefix) | |
| Old product, category, page, post and category URLs | **Website Route Redirect** (301) to the new route | |
| Gift cards (the WooCommerce Gift Cards extension: code, remaining balance, activities, recipient) | **Square Gift Cards**, kept as an **Importer Gift Card** | Written to Square (switch on "Gift Cards and Store Credit" and give the connection its Square account): the card's number is its own code where Square takes it (8 to 20 letters and digits, separators dropped), else a new 16-digit number when "New Numbers for Unusable Codes" is on. Its balance is loaded once; afterwards only what the source changes is applied (a rise is held unless "Load Balance Increases" is on, since Square charges a load fee on it; a drop beyond what Square holds is reported, never taken below zero). Linked to the recipient's Square customer (by email, created when missing). Each card is an **Importer Gift Card** with its ledger: the source history and every write made in Square. A card switched off, or past its expiry date, carries no balance. It belongs to the customer who redeemed it into their account. Its activities (issued, used, refunded: the extension's reference lists no full set, so other types keep the sign it records) are the ledger. The cards are in the store's currency (its currency setting). |

## Gaps

| WooCommerce | Why it isn't mirrored |
|---|---|
| Grouped and external/affiliate products | ERPNext has no item that is a set of separately sold items or a link to another shop (a Product Bundle sells its parts together). |
| Orders in another currency than the company's that record no exchange rate (no multi-currency extension's meta) | The source never had a rate for them; a rate of the day would make the books inexact (reported; an operator decision). |
| Order lines whose amount has no exact unit price (e.g. 10.00 for 3) | ERPNext holds quantity × rate; the line can't be represented exactly. |
| Taxes per rate (several tax accounts) | Taxes land in the connection's one tax account, labelled per rate. |
| Downloadable products' files and download permissions | No first-party home. |
| Sticky posts, comments on posts | Not read yet (Blog Post comments have a home; WooCommerce's adapter doesn't read `wp/v2/comments`). |
| Regular-expression redirects | Can't be scoped to one site's prefix. |
| Reviews removed at the source | Item Review has no unpublished state; the review stays, marked "[Removed at source]". |
| Customer passwords, saved payment methods | Not exportable. |
| Shipping zones and methods, tax rates, store settings | Configure ERPNext (Shipping Rule, Sales Taxes and Charges Template) by hand. |
