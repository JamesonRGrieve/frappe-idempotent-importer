<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Square Online (Square API) → ERPNext

The single source of truth for what the Square Online adapter mirrors. Square Online keeps its catalog,
stock, customers and orders in the seller's Square account, so the adapter reads the Square API (version
2026-09-16, pinned in `tools/specs.py`; every fixture is proven against it). Its reader, mappers and
fixtures are ported from wp-woo-idempotent-importer.

Access: a Square personal access token (or an OAuth access token the seller issued) with the read scopes
`MERCHANT_PROFILE_READ ITEMS_READ INVENTORY_READ CUSTOMERS_READ ORDERS_READ ONLINE_STORE_SITE_READ`,
entered as the connection's secret; requests carry it as a bearer token with `Square-Version: 2026-09-16`.
Every kind is incremental: categories and items by catalog `begin_time` (deleted objects included, so their
copies are retired), stock by the counts changed since the last run (their items are re-read), customers
and orders by `updated_at`. Square publishes no fixed request rate
(developer.squareup.com/docs/build-basics/handling-errors#rate-limiting), so requests aren't paced; a 429 or
server error is retried with backoff and then pauses the run.

"The main location" is the seller's `main_location_id` (else its first active location): the location whose
prices, stock and presence the items mirror.

## Mirrored

| Square | ERPNext home | Notes |
|---|---|---|
| Main location return policy | **Web Page** `<prefix>/policies/return-policy` | |
| Catalog categories | **Item Group** `<name> - <abbr>` | Square Online permalink as the route, the first image, the parent category. |
| Catalog items (regular, food and beverage, digital) | **Item** (+ **Website Item**) | Archived items, and items not present at the main location, are unpublished; a deleted item is disabled. Description HTML as Square stores it. Digital items aren't stock items. |
| Item variations | One **Item** (one variation, no options) or a template with variant **Items** and **Item Attributes** | A variant is identified by its Square variation id, the same id an order line names. Told apart by item option values, or by name ("Variation") when the item has none. SKU, UPC as barcode. |
| Variation price | **Item Price** | The main location's override wins. |
| In-stock count at the main location | **Stock Reconciliation** | Sold out at the main location counts as zero; untracked variations aren't counted. |
| Item and variation images | **File**; the item's and variant's image | |
| Old Square Online product URLs (`ecom_uri`) | **Website Route Redirect** (301) | |
| Customers | **Customer**, **Contact**, **Address** | The one address, with the company and phone. |
| Orders (open, completed) at every location | **Sales Invoice** (submitted when paid or completed, else draft) + **Payment Entry** | Square's totals, exactly; number `#<order id>`. Canceled orders aren't mirrored. |
| Order lines | Invoice lines at their pre-tax prices before discounts, on the variation's item | |
| Discounts (catalog or ad hoc) | The invoice's discount | Square discounts aren't codes. |
| Taxes (inclusive or additive) | One charge per tax, what Square applied | |
| Shipping and delivery service charges | Shipping charges | A charge named for shipping or delivery (or the shipment's shipping type) on an order that ships. |
| Other service charges, tips, cash rounding | Fee charges | |
| Approved refunds | Credit note (one adjustment line) + refund **Payment Entry** | Pending refunds wait until Square approves them. |
| Fulfillments (shipment, pickup, delivery, in-store) | **Comment** per state reached; a completed one is also a draft **Shipment** | Carrier (or courier), shipping type, tracking number and link, and the items fulfilled; the recipient and their note in the remarks. |
| Payment | **Payment Entry** on the first tender's date | Tender types as the method, the payment id as the reference. |

## Gaps

| Square | Why it isn't mirrored |
|---|---|
| Gift card, appointment, event, donation, legacy service and membership items | No ERPNext item equivalent (reported). |
| Items sold by a measurement unit; fractional stock or quantities | Mirrored items count whole units (reported). |
| Variable-priced variations | No price until the sale (a warning; the variant has no Item Price). |
| Stock at other locations | The connection mirrors one warehouse: the main location's. |
| Item weight and dimensions | Not exposed by the Catalog API. |
| Modifier lists, item location presence details, custom attributes, taxes per item | No first-party Item field. |
| Catalog discounts, pricing rules, product sets | Automatic POS rules, not codes. |
| Return and exchange orders | A return order has no invoice of its own; the sale order's refunds are its credit notes (reported). |
| Service charges apportioned into line items | Already inside the line totals; no exact invoice row (reported). |
| Draft orders | Unsubmitted carts and API drafts, not store history. |
| Seller and location settings, Square Online sites | No home for a storefront's settings. |
| Site pages, blog, navigation, redirects, reviews | Square Online has no public API for them. |
| Loyalty, gift card balances, subscriptions, invoices, bookings, payouts, disputes | Square programs and accounting, not catalog or order data. |
