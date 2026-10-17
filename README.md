<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# frappe-idempotent-importer

A Frappe v16 app that keeps ERPNext an exact, one-way copy of other stores and business systems until you
cut over. It is the ERPNext port of
[wp-woo-idempotent-importer](https://github.com/JamesonRGrieve/wp-woo-idempotent-importer): the same
engine and contracts, with ERPNext's own modules as the targets. Run it as often as you like: what's new
at the source is created, what changed is updated, what was deleted is retired, and what didn't change
isn't touched. A re-run against an unchanged source writes nothing at all.

Several sources import side by side. Each **Importer Connection** is one store or system, bound to one
**Public Site** ([frappe-public-site-router](https://github.com/JamesonRGrieve/frappe-public-site-router)):
its company keeps the books, its route prefix holds the pages, and its Webshop Store
([frappe-webshop](https://github.com/JamesonRGrieve/frappe-webshop) fork) sells the items. Two WooCommerce
stores can import into two businesses at once and never touch each other's records.

| Source | Status | What it mirrors |
|---|---|---|
| WooCommerce | Built | Categories, simple and variable products with prices, sales, stock, images, brands and tags, customers, coupons, orders with payments, refunds and notes, pages and posts (their images copied, posts' tags), post categories, reviews, and the Redirection plugin's redirects — [coverage/woocommerce.md](coverage/woocommerce.md) |
| Shopify | Built | Files, collections, products with variants, prices, stock, images and tags, customers, basic code discounts, orders (tax-inclusive ones too) with payments, refunds and timeline comments, draft orders, pages, shop policies, blogs, articles, comments, URL redirects and flat shipping rates — [coverage/shopify.md](coverage/shopify.md) |
| Etsy | Built | Shop policies, sections, listings in every state with variants, prices, stock, images and tags, buyers, receipts with payments, refunds and shipment tracking, and reviews — [coverage/etsy.md](coverage/etsy.md) |
| Square Online | Built | Categories, items with variations, prices, main-location stock and images, customers, orders at every location with payments, refunds and fulfillments, and the return policy — [coverage/square_online.md](coverage/square_online.md) |
| BigCommerce | Built | Categories, products with variants, prices, sales, stock, images and brands, customers, coupons and coupon promotions, orders with payments, refunds and shipments, pages, the blog and its posts, reviews and storefront redirects — [coverage/bigcommerce.md](coverage/bigcommerce.md) |
| Squarespace | Built | Store pages, products with variants, prices, sales, stock, images and tags, customer profiles, promo codes, and orders with payments, refunds and fulfillments — [coverage/squarespace.md](coverage/squarespace.md) |
| Wix, Magento / Adobe Commerce, Lightspeed eCom, Ecwid, Odoo, Dynamics 365 (Business Central, Finance & Operations, Sales), PeopleSoft, Agendrix, NetSuite, SAP Business One, QuickBooks Online, Xero, Sage, Zoho, Acumatica | Listed | Connections can be created but refuse to run until a later phase builds the adapter; each has a coverage page in [coverage/](coverage/). |

Every coverage page lists what lands where, and what has no first-party home (never approximated).

## How it stays idempotent

Every mirrored object is identified by `gid://<source>/<connection>/<Type>/<id>` and linked to its
ERPNext record in an **Importer Link**, with the hash of exactly what was written and the mapper's
version. A run maps each source object to the fields it owns, hashes them, and writes only when the hash
changed or the ERPNext record was deleted by hand. Only mapped fields are written, so anything a person
edits in ERPNext is kept. Retiring never deletes: items and customers are disabled, pages, posts and
Website Items unpublished, pricing rules disabled, invoices and payments cancelled.

- **Incremental runs** (hourly) read only what changed since the last clean run, where the source can say.
- **Full runs** (daily) read everything and retire the copies of objects the source no longer has. A
  connection's runs only ever retire that connection's links.
- **Sync Now** on the connection (or `bench idempotent-import run`) starts a run by hand.

Runs are stepped through the background job queue (one step job per connection at a time) and survive
across jobs; a five-minute tick carries on any run whose pause is over. The first import of a large store
can take hours, so it is careful not to get the key locked out:

- Requests are paced under each source's documented limits (Shopify's query-cost bucket; WooCommerce
  documents none for `wc/v3`).
- Rate limiting and server errors are retried with exponential backoff (1, 2, 4, 8, 16 seconds), after the
  source's own `Retry-After` when it sends one; an unreachable source counts as one that is pushing back.
- When the source keeps pushing back, the run pauses exactly where it is and carries on later: the pauses
  grow from a minute to an hour and hold on for about a day; only then is that kind given up and retried
  next run.
- If the source rejects the credentials (401/403), the run stops at once, and scheduled runs make no
  further calls until the connection is saved or a run is started by hand.

Mirroring history has no side effects: no email is sent or queued, no notification raised, no stock moved
by a sale. Old storefront URLs answer with a 301 to the new route.

## How history lands in the books

- A **paid** order is a submitted **Sales Invoice** (its lines at their prices before discounts, the
  order's discount on the net total, its taxes, shipping and fees as "Actual" charges) and a submitted
  **Payment Entry** into the connection's deposit account, on the order's and the payment's dates.
- An **unpaid** order is a **draft** Sales Invoice, submitted with its payment once paid. A cancelled or
  voided order has none (an earlier copy is cancelled).
- A **refund** is a submitted credit note (a Sales Invoice return against the same customer) and a refund
  Payment Entry out of the deposit account.
- Each invoice's grand total must equal the source's total exactly, or the order isn't kept (it is
  reported); amounts are exact decimals from the source to ERPNext.
- **Invoices never update stock.** Stock is mirrored as the source's absolute count, by a zero-valued Stock
  Reconciliation whenever the warehouse's quantity differs, so a sale is never counted twice and no
  invented cost reaches the books.
- An invoice that changes at the source after it was submitted is cancelled and amended, with its payment.

This is the operator's decision to confirm (see below).

## Setup

1. Install the app on a bench with ERPNext, HRMS, the blog app, the Webshop fork and the Public Site
   router: `bench get-app …`, then `bench --site <site> install-app idempotent_importer`.
2. Create a fiscal year covering the history you import, and per business: its Public Site (company,
   route prefix, Webshop Store), a selling Price List in the company's currency, a guest Customer, a
   non-stock fallback Item (for lines whose product no longer exists), and the deposit, tax, shipping and
   fee accounts.
3. Create an **Importer Connection**: a permanent Connection ID, the source, its URL and credentials
   (secrets are Password fields, never shown or logged), the Public Site, an abbreviation, and the targets
   above. Choose what to mirror.
   - **WooCommerce:** the site's `https://` address, and either a WordPress Application Password (Users →
     Profile; reads drafts and the Redirection plugin's redirects) or a REST API consumer key and secret
     with Read access.
   - **Shopify:** in Shopify's Dev Dashboard (dev.shopify.com, signed in to the store's organization),
     create an app with the read scopes in [coverage/shopify.md](coverage/shopify.md), release a version
     and install it on the store. Enter the store's `<name>.myshopify.com` domain, choose **Client
     Credentials** and enter the app's client ID and client secret: the importer exchanges them for a
     24-hour token and renews it itself. A legacy custom app (made in the store admin before 2026) can use
     **Access Token** with its Admin API access token instead.
   - **Etsy:** register an app at etsy.com/developers with this site's
     `/api/method/idempotent_importer.api.etsy_callback` as its callback URL; enter the shop's URL, the app's
     keystring as the user and its shared secret as the secret, save, then click **Connect to Etsy** and
     approve read access as the shop owner. The refresh token Etsy issues (and replaces on every refresh)
     is kept on the connection.
   - **Square Online:** in the Square Developer Console create an application and copy its production
     access token (or issue an OAuth token with the read scopes in
     [coverage/square_online.md](coverage/square_online.md)); enter the Square Online site's URL and the
     token as the secret.
   - **BigCommerce:** create a store-level API account with the read-only scopes in
     [coverage/bigcommerce.md](coverage/bigcommerce.md); enter the storefront URL, the store hash (the code
     after `/stores/` in the account's API path) as the account, and the access token as the secret.
   - **Squarespace:** create a Developer API key with the read-only permissions in
     [coverage/squarespace.md](coverage/squarespace.md); enter the site's URL and the key as the secret.
4. Run a **Dry Run**, then a **Full Run**, from the connection.

## Commands

```sh
bench --site <site> idempotent-import run [--connection <id>]... [--full] [--dry-run] [--kinds product,order]
bench --site <site> idempotent-import status [--connection <id>]...
bench --site <site> idempotent-import sync --connection <id> <kind> <gid://…>
bench --site <site> idempotent-import reset-cursors --connection <id>
```

## Development

```sh
uv sync --group dev                    # the pure-layer toolchain (Frappe comes from the bench)
git config core.hooksPath .githooks    # the pre-commit gate
scripts/check.sh                       # lock, SPDX, ruff, mypy --strict, pytest + coverage ratchet, schema proofs
python -m tools.specs                  # fetch the REST sources' pinned API documents (the proofs fetch them too)
tests/e2e/run.sh                       # a real bench in podman, against a fake store of every source
```

The pure layers (the engine, every adapter, the field contract, naming) are tested without a bench over
recorded fixtures. Every Shopify operation is validated against Shopify's published Admin schema, priced
by a static cost estimate that must match what a live store charged for it, and every fixture is executed
against the schema; every WooCommerce fixture validates against
[tests/schemas/woocommerce.json](tests/schemas/woocommerce.json), transcribed from the official
WooCommerce, WordPress and Redirection documentation; every other REST source's fixtures validate,
strictly (no undeclared field), against the source's published API document pinned in `tools/specs.py`
(by version, commit or SHA-256), or for Wix, Lightspeed and Ecwid the schema transcribed from their
references. The end-to-end suite builds the official ERPNext v16 image with HRMS, the blog app, the
Webshop fork, the router and this app, sets up a business per connection, and imports two WooCommerce
stores and one store of every other built source at once: the first import, unchanged re-runs that leave
every content table byte-identical, changes and deletions, connections that never touch each other's
records, a rate-limited run that pauses in place, rejected credentials that hold scheduled runs, Sync Now
on the background queue, and no mail. It then runs the writers' unit tests on the same bench (`bench
run-tests --module idempotent_importer.tests.test_writers`: each writer's create, skip, update and retire
paths, and that an update writes only mapped fields, every test rolled back) and holds their line coverage
to the ratchet in `.writer-coverage-baseline`.

## Decisions to confirm

Each of these is the operator's call.

1. **History in the books:** paid orders as submitted Sales Invoices + Payment Entries, unpaid ones as
   draft invoices, refunds as credit notes (Sales Invoice returns, not against the original, so an
   amount-only refund fits) + refund Payment Entries, an amount refunded beyond the returned lines as an
   adjustment line on the connection's fallback item. The alternative, Sales Orders, would leave the
   source's revenue and payments out of the books.
2. **Stock:** invoices never update stock; the source's count is mirrored absolutely by zero-valued Stock
   Reconciliations against the company's Temporary (opening) account. Valuation stays zero until purchases
   are booked in ERPNext.
3. **No Delivery Notes** for fulfilments in phase 1: they would move stock a second time. Tracking numbers
   are not mirrored yet.
4. **Routes under the Public Site's prefix** for Web Pages, Blog Posts, Item Groups, Website Items and
   redirect sources, as the spec asks, so two sites never collide. The router today serves only Web
   Pages (and Builder Pages, Web Forms) by prefix; Website Items, Item Groups and Blog Posts it serves at
   their own route, and Frappe resolves Website Route Redirects globally before the router. Until the
   router maps `/<path>` to `<prefix>/<path>` for those too, mirrored products, categories, posts and
   redirects aren't reachable at clean URLs on a site's host. (Blog Categories keep the blog app's own
   `blog/<name>` route, which can't be prefixed.)
5. **Globally named records** carry the connection's abbreviation: item codes `<ABBR>-<SKU>`, Item Groups
   and Blog Categories `<name> - <ABBR>`, coupon names `<code> - <ABBR>`, Bloggers `<abbr>-<author>`; Web
   Pages and Blog Posts are named after their prefixed route. Brands and Item Attributes are shared by
   name. Coupon Codes are scoped to the connection's Webshop Store (the Webshop fork makes a code unique
   per store), so two stores can hold the same code at once; a code someone already holds on the same
   store, or store-less, is not mirrored (reported), and a connection whose Public Site has no store
   mirrors no codes (a store-less code would apply on every store).
6. **Coupons ERPNext can't enforce exactly** (email or customer limits, exclusions, per-customer limits,
   free shipping, Buy X Get Y, …) are not mirrored rather than mirrored more permissively.
7. **Shopify's compare-at price** becomes the Item Price, and Shopify's price an open-ended sale (a
   Pricing Rule), which is how the webshop shows a sale.
8. **Retiring without deleting:** a Website Route Redirect row has no disabled flag, so a retired redirect's
   row is removed (site configuration, not a record); an Item Review removed at the source stays, marked
   "[Removed at source]"; a code dropped from a discount expires; a draft invoice of a cancelled order is
   marked in its remarks.
9. **Barcodes** stay on the item that has them first (ERPNext keeps a barcode on one item).
10. **Guests and order addresses:** guest orders go to the connection's guest customer; the guest's details
    and the order's addresses are kept in the invoice's remarks.
11. **A product deleted at the source** leaves the invoices that sold it as booked; lines that never named
    a product use the fallback item.
12. **Currency:** an order in another currency than the company's is mirrored only when the source
    recorded its exchange rate and its total in the company's currency (Magento): the invoice is in the
    order's currency at that rate, on the company's receivable account in that currency (create one per
    currency; ERPNext books a customer in one currency), and its total in the company's currency must equal
    the source's exactly. Without both (WooCommerce, Shopify's other currencies, BigCommerce, which records
    the rate but no total in the store's currency) it is a gap: a derived or day's rate would make the
    books inexact.
13. **Required apps:** the app requires ERPNext, Webshop, the blog app and the Public Site router (every
    connection belongs to a Public Site). HRMS is needed only by the HR sources of later phases.
14. **Schedule:** incremental hourly, full daily, a five-minute tick for paused runs. Times are UTC in the
    engine and the site's system time zone on posting dates.
15. **Tax-inclusive prices** (Shopify's `taxesIncluded`): the invoice's taxes are "On Net Total" rows
    included in the line rates, shipping's tax a row on the shipping charge; an order is kept only when
    ERPNext computes every tax row and the total to the source's amounts, and one whose lines are taxed
    at different rates is a gap (Item Tax Templates would be inventing the source's tax setup).
16. **Draft orders** are draft Quotations, never submitted; a completed draft is left to its order and its
    Quotation's title marked "[Removed at source]" (a draft Quotation has no closed state).
17. **Shipping rates** are mirrored only where a Shipping Rule is exact: flat rates to whole countries.
    ERPNext charges nothing outside a price or weight band rather than withholding the rate, so banded and
    minimum-order rates, province-level zones and carrier rates are gaps.
18. **Files and embedded images** stay when removed at the source (a File has no disabled state, and pages
    may still show them); only images the source hosts are copied, so the HTML no longer depends on the
    old store.
19. **Tags** are Frappe document tags; a tag the source drops is removed, tags a person added are kept.
20. **A kind the credentials may not read** (Shopify's policies without `read_legal_policies`) is skipped
    with a warning: its existing copies stay, and the run still completes.

## Next steps

Still to do:

1. **Port the storefront adapters already built in wp-woo-idempotent-importer:** Etsy (OAuth with a
   rotating refresh token kept in a Password field and committed on every refresh), Square Online,
   BigCommerce, Squarespace, Wix, Magento / Adobe Commerce, Lightspeed eCom and Ecwid: readers, cited
   rate limits, fixtures (copied with a provenance note), spec validation (published specs pinned as the
   PHP repo pins them; the Wix, Lightspeed and Ecwid schemas copied), mappers onto the ERPNext targets, a
   coverage page each, and an e2e connection each with zero-write re-runs and isolation. The e2e's
   listed-source check then moves to a source that is still listed (e.g. Odoo).
2. **Menus** need a per-site menu in the router: Website Settings' top bar and footer are global.
3. **Router clean URLs** (frappe-public-site-router): serve Website Items, Item Groups and Blog Posts at
   `/<path>` on a site's hosts from `<prefix>/<path>`, and resolve Website Route Redirects whose source
   is under the site's prefix before Frappe's global redirect lookup.
4. **ERP and HR sources** (Odoo, Dynamics 365, PeopleSoft, Agendrix, NetSuite, …): a later phase.

## License

[AGPL-3.0-or-later](LICENSE).
