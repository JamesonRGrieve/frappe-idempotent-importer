<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Lightspeed eCom (C-Series) → ERPNext

The single source of truth for what the Lightspeed eCom (C-Series) adapter mirrors (formerly SEOshop /
WebshopApp; Lightspeed Retail R-Series and X-Series are other products with other APIs). The adapter reads
Lightspeed eCom's REST API (`<cluster>/<language>/<resource>.json`; the OpenAPI schema committed in
`tests/schemas/lightspeed.json` and pinned in `tools/specs.py`, every fixture proven against it). Its reader,
mappers and fixtures are ported from wp-woo-idempotent-importer.

Access: an API key with read permissions (eCom back office → Settings → Developers → API keys); the connection
holds the shop's URL, the cluster and shop language as the account (`us1/en`, `eu1/nl`: clusters `eu1` and
`us1`, developers.lightspeedhq.com/ecom/introduction/clusters/), the key as the user and its secret as the
secret (HTTP Basic). Products, customers and orders are incremental (`updated_at_min`, asked from a day
earlier since Lightspeed doesn't say its zone); a product also counts as changed when one of its variants
did, and an order when one of its shipments or invoices did, so those runs re-read the parents of changed
children. Lightspeed limits a key to 300 requests per 5 minutes, 3,000 per hour and 12,000 per day
(developers.lightspeedhq.com/ecom/introduction/rate-limiting/): requests are paced under the hourly bucket (one
every 1.2 seconds), and a shop needing more than the day's quota pauses on the 429 until it frees up.

## Mirrored

| Lightspeed eCom | ERPNext home | Notes |
|---|---|---|
| Categories | **Item Group** `<name> - <abbr>` | Content (else the short description), image, parent; the old URL redirects. |
| Products | **Item** (+ **Website Item**) | One Item for a single variant without matrix options, else a template with variant **Items**: matrix options as **Item Attributes**, several plain variants told apart by a "Variant" attribute of their titles. Hidden products are unpublished; a deleted product is disabled. Brand as **Brand**. The old URL (`/<url>.html`) redirects. |
| Variants | Variant **Items** keyed by their Lightspeed id (what an order line links to) | SKU, EAN as barcode, weight in g, kg, oz or lb, the variant's image. |
| Prices, old prices | **Item Price** as the shop shows it (with tax in a retail shop, without in a B2B one); sale as a **Pricing Rule** | An old price above the price is the price and the price the sale. |
| Stock (tracked variants) | **Stock Reconciliation** | Untracked variants aren't counted. |
| Product images | **File**; the Item's image | By sort order. |
| Customers | **Customer**, **Contact**, **Address** | Billing and shipping addresses (one when they're the same), the company. |
| Discount codes: a percentage off the order, some products or some categories; an amount off the order | **Pricing Rule** + **Coupon Code** | Minimum order amount (checked before the discount), use limit, start and end dates. An inactive code disables its rule. |
| Orders | **Sales Invoice** (submitted when paid, else draft) + **Payment Entry** | Lightspeed's totals in the shop's currency, exactly, booked from its tax-exclusive amounts and its taxes (so retail and B2B shops both balance); its order number. |
| Order lines | Invoice lines at their tax-exclusive base prices, on the variant's item | The variant title in the line's name. |
| Line discounts, order taxes, shipping, payment costs | The invoice's discount; one charge per tax; shipping and fee charges | The order's tax total when Lightspeed itemises none. |
| Credit notes | Credit note (the credited product lines, an adjustment for their tax and the rest) + refund **Payment Entry** | |
| Shipments (not cancelled) | Draft **Shipment** | The order's shipping method and the shipment's tracking code. |
| The staff memo | **Comment** | |
| Text pages | **Web Page** | Hidden pages unpublished; the old URL (`/service/<url>`) redirects. |
| Blogs | **Blog Category** | The old URL (`/blogs/<url>`) redirects. |
| Blog articles | **Blog Post** in its blog's category, by its author | Unpublished articles unpublished; the old URL redirects. |
| Visible reviews | **Item Review** | Score out of 5. |
| Redirects | **Website Route Redirect** | A target on the shop's domain redirects on to its mirrored object. |

## Gaps

| Lightspeed eCom | Why it isn't mirrored |
|---|---|
| Discounts on shipping and free shipping, an amount off some products, codes for both products and categories, minimums checked after the discount, discounts after tax, automatic discount rules | Planned (card 386): the generalised Pricing Rule / Promotional Scheme writer is built (Shopify, WooCommerce); this source's mapper isn't moved to it yet. Its limits will then be planned on cards 405 (coupon limits, shipping) and 407 (caps, segments). |
| Cancelled orders | Not sales. |
| Hidden reviews | Not published. |
| Article tags, comments | Separate resources, not read. |
| Product long titles, template data, suppliers, delivery dates, unit prices, stock limits, dimensions, tax settings | No first-party Item field. |
| Other shop languages | One connection mirrors one language. |
| Shop and website settings, tax and shipping configuration | ERPNext's own settings; orders carry what Lightspeed charged. |
