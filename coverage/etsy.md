<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Etsy (Open API v3) → ERPNext

The single source of truth for what the Etsy adapter mirrors. Its reader, mappers and recorded fixtures
are ported from wp-woo-idempotent-importer; every fixture is proven against Etsy's published OpenAPI
document (3.0.0) with complete objects (`python -m tools.prove`).

Access: an Etsy app (keystring and shared secret, entered as the connection's user and secret) and the
shop owner's consent with **Connect to Etsy** (OAuth with PKCE, read-only scopes `listings_r
transactions_r shops_r email_r`; the callback is `/api/method/idempotent_importer.api.etsy_callback` on this
site). Etsy issues a new refresh token on every refresh; it is saved on the connection, and committed, before
the access token is used. A refused refresh stops the run and holds scheduled runs until the shop is
connected again. Etsy has no webhooks; scheduled runs keep the mirror current (receipts, buyers and reviews
incrementally; listings, sections and policies in full, unchanged objects skipped by hash).

Rate limits: per app (shown in Etsy's Developer Portal) and reported on every response; when
`x-remaining-this-second` reaches the last request the next one waits out the second; a 429's Retry-After
is honoured, and a spent daily quota pauses the run until Etsy says to retry
(developers.etsy.com/documentation/essentials/rate-limits).

## Mirrored

| Etsy | ERPNext home | Notes |
|---|---|---|
| Shop policies (welcome, payment, shipping, refunds, additional, seller info, privacy) | **Web Page** each, `<prefix>/policies/<name>` | Plain text as paragraphs. |
| Structured return policies | **Web Page** each | Written out from accepts returns / exchanges and the deadline. |
| Shop sections | **Item Group** `<title> - <abbr>` | |
| Listings (active, inactive, sold out, draft, expired) | **Item** (+ **Website Item**) | Active and sold out are published; the others are unpublished. Entities decoded; the description's paragraphs as HTML. A listing removed at Etsy is disabled. |
| Listing inventory (products, offerings, property values) | One **Item**, or a template with a variant **Item** per inventory product and **Item Attributes** | A variant is identified by its listing and property values (Etsy renumbers inventory product ids on every edit). Price on the connection's **Item Price**, SKU, quantity (a disabled offering is 0). |
| Listing quantity | **Stock Reconciliation** | Download-only listings aren't stock items. |
| Listing images | **File**, by rank (the first is the item's image) | |
| Listing tags | Document tags on the **Item** | |
| Listing weight | Item weight and its UOM | |
| Buyers | **Customer** (+ **Contact** with the email) | Etsy exposes only the buyer's id and email. |
| Receipts (paid, completed, partially or fully refunded) | Submitted **Sales Invoice** + **Payment Entry** | Etsy's totals, exactly; number `#<receipt id>`. |
| Receipts awaiting payment (open, payment processing) | Draft **Sales Invoice** | Cancelled receipts aren't mirrored. |
| Receipt transactions | Invoice lines, on the listing's item or its variant | |
| Shipping, sales tax, VAT, gift wrap | Charges (Etsy doesn't split tax per line) | |
| Discount amount | The invoice's discount | |
| Refunds | Credit note (one adjustment line: Etsy refunds name no lines) + refund **Payment Entry** | Identified by receipt and refund time. |
| Shipments | Draft **Shipment** | Carrier and tracking code. |
| The seller's message | **Comment** on the invoice | |
| Buyer's message, recipient's name and address | The invoice's remarks | |
| Transaction reviews | **Item Review** on the listing's Website Item | Attributed to "Etsy buyer". |

## Gaps

| Etsy | Why it isn't mirrored |
|---|---|
| Shop name, currency, title, announcement, vacation, icon | No ERPNext home for a storefront's settings (the company and Public Site are set up by hand). |
| Materials, styles, who/when made, supply, taxonomy, processing days, dimensions, personalization, translations, shipping and return profile ids | No first-party Item field. |
| Listing videos | No ERPNext home (a warning). |
| Shipping profiles | Assigned per listing; a Shipping Rule is store-wide. |
| Variation images, digital files | Not in the listing response (per-listing endpoints the adapter doesn't read). |
| Coupon codes, sales | Not exposed by Open API v3 (only a receipt's discount amount). |
| Payments, ledger entries | Etsy Payments accounting, not order data. |
| Old Etsy URLs | Listings live on etsy.com, which the new site doesn't answer for. |
