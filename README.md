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
| Shopify | Built | Files, collections, products with variants, prices, stock, images and tags, customers, code and automatic discounts, orders (tax-inclusive ones too, at each line's own rates) with payments, refunds and timeline comments, draft orders, pages, shop policies, blogs, articles, comments, URL redirects and flat shipping rates — [coverage/shopify.md](coverage/shopify.md) |
| Etsy | Built | Shop policies, sections, listings in every state with variants, prices, stock, images and tags, buyers, receipts with payments, refunds and shipment tracking, and reviews — [coverage/etsy.md](coverage/etsy.md) |
| Square Online | Built | Categories, items with variations, prices, main-location stock and images, customers, catalog discounts and pricing rules, orders at every location with payments, refunds and fulfillments, and the return policy — [coverage/square_online.md](coverage/square_online.md) |
| BigCommerce | Built | Categories, products with variants, prices, sales, stock, images and brands, customer groups, customers, coupons, promotions, customer group discounts and bulk pricing, orders with payments, refunds and shipments, pages, the blog and its posts, reviews and storefront redirects — [coverage/bigcommerce.md](coverage/bigcommerce.md) |
| Squarespace | Built | Store pages, products with variants, prices, sales, stock, images and tags, customer profiles, discounts (promo codes and automatic), and orders with payments, refunds and fulfillments — [coverage/squarespace.md](coverage/squarespace.md) |
| Wix | Built | Stores categories or collections, V3 and V1 catalog products with variants, prices, sales, stock and images, contacts, Stores coupons and discount rules, orders with payments, refunds and fulfillments, the blog's categories and posts (rich content and its images), reviews and redirects — [coverage/wix.md](coverage/wix.md) |
| Magento / Adobe Commerce | Built | Categories, simple, virtual, downloadable and configurable products with prices, special prices, stock and images, customer groups, customers, cart price rules (with coupons and automatic), tier prices, orders (other currencies too) with payments, credit memos, shipments and comments, and CMS pages — [coverage/magento.md](coverage/magento.md) |
| Lightspeed eCom (C-Series) | Built | Categories, products with variants, prices, sales, stock, images and brands, customers, discount codes and rules, orders with payments, credit notes and shipments, text pages, blogs and articles, reviews and redirects — [coverage/lightspeed.md](coverage/lightspeed.md) |
| Ecwid | Built | Categories, products with variations, prices, sales, stock and images, customers, discount coupons, wholesale price tiers, orders with payments, refunds and shipping labels, reviews and Instant Site redirects — [coverage/ecwid.md](coverage/ecwid.md) |
| Odoo | Built | The chart of accounts, analytic accounts, warehouses, product categories and products with variants and stock, customers and suppliers with contacts and addresses, departments, job positions and employees, projects, leads and opportunities, quotations, sale and purchase orders, invoices and bills with credit notes, payments allocated to what they settle, and journal entries — [coverage/odoo.md](coverage/odoo.md) |
| Dynamics 365 Business Central | Built | Posting accounts, dimensions, locations, item categories and items with variants and inventory, customers, vendors, employees, projects, contacts and opportunities, sales quotes, sales and purchase orders, and sales and purchase invoices and credit memos — [coverage/dynamics_business_central.md](coverage/dynamics_business_central.md) |
| Dynamics 365 Sales | Built | Products, accounts with their contacts, contacts, leads, opportunities, quotes, orders and invoices (other currencies too) — [coverage/dynamics_sales.md](coverage/dynamics_sales.md) |
| Dynamics 365 Finance & Operations | Built | One legal entity's customers, released products and sales orders through the documented data entities, and vendors and purchase orders through the entities its field map names — [coverage/dynamics_finance_operations.md](coverage/dynamics_finance_operations.md) |
| QuickBooks Online | Built | The chart of accounts, classes and locations, items and categories, customers, projects, vendors, employees, estimates, purchase orders, invoices, sales receipts, credit memos, refund receipts, bills, vendor credits, payments, bill payments, journal entries, deposits, transfers and expenses — [coverage/quickbooks_online.md](coverage/quickbooks_online.md) |
| Xero | Built | Accounts, tracking categories, items, contacts as customers and suppliers, projects, quotes, purchase orders, sales and purchase invoices and credit notes, payments, prepayments and overpayments, manual journals, spend and receive money and bank transfers — [coverage/xero.md](coverage/xero.md) |
| Zoho Books | Built | The chart of accounts, locations, items, customers and vendors, projects, estimates, sales orders, invoices, credit notes, sales receipts, purchase orders, bills, vendor credits, customer and vendor payments, journals and expenses — [coverage/zoho_books.md](coverage/zoho_books.md) |
| Zoho Inventory | Built | Locations, categories, item groups with their variants and items, customers and vendors, sales orders, invoices, credit notes, purchase orders, bills, vendor credits and customer payments — [coverage/zoho_inventory.md](coverage/zoho_inventory.md) |
| NetSuite | Built | The chart of accounts, departments, locations, items, customers with their contacts, leads and prospects, vendors, job titles, employees, projects, opportunities, estimates, sales and purchase orders, invoices, cash sales, credit memos, cash refunds, vendor bills and credits, customer and vendor payments, deposits and refunds, journal entries and checks — [coverage/netsuite.md](coverage/netsuite.md) |
| SAP Business One | Built | The chart of accounts, profit centers, warehouses, item groups and items, customers, suppliers and leads with their contact persons and addresses, departments, positions, employees, projects, sales opportunities, quotations, sales and purchase orders, A/R and A/P invoices and credit notes, incoming and outgoing payments, and manual journal entries — [coverage/sap_business_one.md](coverage/sap_business_one.md) |
| Acumatica | Built | GL accounts, subaccounts, warehouses, item classes, stock and non-stock items, customers and vendors with their contacts, departments and job titles, employees, projects, leads, opportunities, quotations, sales and purchase orders, A/R invoices, memos and cash sales, A/P bills and adjustments, payments and checks, and general ledger batches — [coverage/acumatica.md](coverage/acumatica.md) |
| Sage Intacct | Built | GL accounts, departments, warehouses, items, customers and vendors with their contact lists, employees and titles, projects, order entry quotes and sales orders, purchase orders, AR invoices, AP bills, AR and AP payments, and general journal transactions; the other Sage products are planned — [coverage/sage_intacct.md](coverage/sage_intacct.md) |
| Agendrix | Built | Sites, positions and members into HRMS, time off types and requests, shifts on the Shift Types of their hours, time clock punches and each day's attendance from its time entries — [coverage/agendrix.md](coverage/agendrix.md) |
| HighLevel | Built | One sub-account's users, products with variant prices, contacts as leads with their notes (billed ones as customers), businesses as prospects, pipelines and opportunities, conversation messages, appointments, tasks, invoices and payments with their refunds — [coverage/highlevel.md](coverage/highlevel.md) |
| PeopleSoft | Built | Through the Integration Broker's Query Access Service and the queries the connection's field map names: departments, job codes and employees into HRMS; customers, suppliers, ChartField accounts, AR invoices, AP vouchers, payments and journals into the books — [coverage/peoplesoft.md](coverage/peoplesoft.md) |
| HubSpot | Built | Owners, products, contacts as leads with their notes, companies as prospects with their people, customers, deal pipelines and deals at their stage, calls, emails, meetings and tasks, quotes, orders, invoices and commerce payments with their refunds, and every property the account defined — [coverage/hubspot.md](coverage/hubspot.md) |
| Salesforce | Built | Users, products at their standard price, accounts as prospects with their contacts and notes (customer accounts as customers), leads with their notes, opportunity types and opportunities at their stage, logged calls and emails, email messages, tasks, events, quotes and orders, and every custom field — [coverage/salesforce.md](coverage/salesforce.md) |
| Pipedrive | Built | Users, products with their variations, persons as leads, organizations as prospects with their persons, the customers of won deals, pipelines, deals and leads at their stage, activities, notes and call logs, and every custom field — [coverage/pipedrive.md](coverage/pipedrive.md) |
| Zoho CRM | Built | Users, products, vendors, leads, accounts as prospects with their contacts (customer accounts as customers), pipelines and deals at their stage, calls, notes, events and tasks, quotes, sales orders, invoices and purchase orders, and every custom field — [coverage/zoho_crm.md](coverage/zoho_crm.md) |
| Webflow | Built | Assets, e-commerce categories, products with their SKUs, prices and inventory, the customers orders name, orders with payments, refunds and shipments, pages with their content, CMS collections and their items as blog posts (their own fields kept), and redirects — [coverage/webflow.md](coverage/webflow.md) |

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
   fee accounts. A store that sold in several currencies needs those currencies enabled and Accounts
   Settings' "Allow multi-currency invoices against single party account" on.
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
   - **Wix:** create an account API key with the read permissions in [coverage/wix.md](coverage/wix.md);
     enter the site's URL, its site ID as the account and the key as the secret.
   - **Magento / Adobe Commerce:** create an integration with the read access in
     [coverage/magento.md](coverage/magento.md) and allow integration tokens as bearer tokens; enter the
     store's URL, the store view code as the account (blank for `default`) and the integration's access
     token as the secret.
   - **Lightspeed eCom (C-Series):** create an API key with read permissions; enter the shop's URL, the
     cluster and shop language as the account (e.g. `us1/en`), the key as the user and its secret as the
     secret.
   - **Odoo:** create an API key (Preferences → Account Security → New API Key) for a user who can read the
     apps in [coverage/odoo.md](coverage/odoo.md); enter the database's URL, the database name as the account
     (`<database>:<company id>` for one company of several) and the key as the secret, and choose **JSON-2
     API Key** (Odoo 19 and later) or **JSON-RPC API Key** (Odoo 17 and 18, with the user's login).
   - **Dynamics 365 Business Central and Sales:** register an app in Microsoft Entra ID (Business Central:
     its `API.ReadWrite.All` application permission, the app added on Business Central's "Microsoft Entra
     Applications" page with read permission sets; Sales: the app added to the environment as an application
     user with a read-only role). Enter the app's client ID as the user and its secret as the secret; for
     Business Central the environment's API URL (`https://api.businesscentral.dynamics.com/v2.0/<tenant>/<environment>`)
     and the company's id as the account; for Sales the environment's URL (`https://<org>.crm.dynamics.com`)
     and the tenant as the account. The importer reads as the app through the client credentials grant.
   - **Dynamics 365 Finance & Operations:** register an app in Microsoft Entra ID and add it on the
     environment's "Microsoft Entra applications" page mapped to a user with read access; enter the
     environment's URL, `<tenant>/<legal entity>` as the account, the app's client ID and secret, and the
     **Field Map** naming what your environment's entities call the fields the entity reference doesn't
     document ([coverage/dynamics_finance_operations.md](coverage/dynamics_finance_operations.md)).
   - **Ecwid:** create a custom app with the read scopes in [coverage/ecwid.md](coverage/ecwid.md); enter the
     store's URL, its store ID as the account and the app's secret access token as the secret.
   - **QuickBooks Online:** create an app at developer.intuit.com with this site's
     `/api/method/idempotent_importer.api.oauth_callback` as a redirect URI; enter
     `https://quickbooks.api.intuit.com` (or the sandbox host) as the Store URL and the app's client ID and
     secret, save, then click **Connect to QuickBooks Online**: the approved company becomes the account.
   - **Xero:** create a Web app at developer.xero.com with the same redirect URI; enter `https://go.xero.com`
     as the Store URL and its client ID and secret, save, then click **Connect to Xero**: the approved
     organisation becomes the account (enter its tenant id when an approval connects several).
   - **Zoho Books / Zoho Inventory:** create a server-based client at api-console.zoho.com (Multi-DC on) with
     the same redirect URI; enter the product's address in the organisation's data centre
     (`https://books.zoho.eu`, `https://inventory.zoho.com`, …) as the Store URL and the client's ID and
     secret, save, then click **Connect to Zoho Books** (or Inventory): the organisation becomes the account.
     An organisation on both products shares its contacts, invoices, bills and payments: mirror those from
     one of the two connections only.
   - **NetSuite:** in the account, create an integration record with the REST web services scope and the
     client credentials (machine-to-machine) grant, and on the OAuth 2.0 Client Credentials Setup page map a
     certificate to it with an entity and a role that can read the records in
     [coverage/netsuite.md](coverage/netsuite.md). Enter the account ID (`1234567`, or `1234567_SB1` for a
     sandbox) as the account, the integration's client ID as the user, the certificate ID as **Key or
     Certificate ID**, and the certificate's private key (PEM) as the secret.
   - **SAP Business One:** enter the Service Layer's address (`https://<server>:50000`) as the Store URL,
     the company database as the account, and a user's name and password with read access; in the **Field
     Map**, the root type of each "other" drawer of the chart
     (`{"root_types": {"1": "Asset", "2": "Liability", "3": "Equity"}}`, see
     [coverage/sap_business_one.md](coverage/sap_business_one.md)).
   - **Acumatica:** register a connected application (SM303010) with the authorization code flow, a secret,
     refresh tokens allowed and the same redirect URI; enter the instance's address as the Store URL and the
     application's client ID (`<id>@<tenant>`) and secret, save, then click **Connect to Acumatica**
     ([coverage/acumatica.md](coverage/acumatica.md)).
   - **Sage Intacct:** a Web Services sender authorized by the company and a user with read access; enter
     the company ID as the account, the user ID and password, the sender ID as **Key or Certificate ID** and
     its password as **Sender Password**, and in the **Field Map** any equity accounts and the company's own
     transaction definitions and journals ([coverage/sage_intacct.md](coverage/sage_intacct.md)).
   - **Agendrix:** on an organisation on Agendrix's Plus plan, create an application in its developer
     portal (developers.agendrix.com) with the same redirect URI; enter `https://app.agendrix.com` as the
     Store URL and the application's client ID and secret, save, then click **Connect to Agendrix** signed in
     as an administrator of the organisation ([coverage/agendrix.md](coverage/agendrix.md)).
   - **HighLevel:** one connection per sub-account. Create a sub-account app in HighLevel's Marketplace with
     the same redirect URI and the read scopes in [coverage/highlevel.md](coverage/highlevel.md); enter
     `https://app.gohighlevel.com` as the Store URL and the app's client ID and secret, save, then click
     **Connect to HighLevel** and choose the sub-account.
   - **PeopleSoft:** give a PeopleSoft user the Query Access Service's REST service operations and access
     to a query per kind; enter the web server's address as the Store URL, the user ID and password, and
     in the **Field Map** the Integration Broker node and each kind's query and columns
     ([coverage/peoplesoft.md](coverage/peoplesoft.md)).
   - **HubSpot:** create an app in a HubSpot developer account with the same redirect URI and the read
     scopes in [coverage/hubspot.md](coverage/hubspot.md); enter `https://app.hubspot.com` as the Store URL
     and the app's client ID and secret, save, then click **Connect to HubSpot** and choose the account.
   - **Salesforce:** create an External Client App (or a connected app) with OAuth, the `api` and
     `refresh_token` scopes and the same callback URL; enter the org's My Domain URL
     (`https://<domain>.my.salesforce.com`) as the Store URL and the app's consumer key and secret, save, then
     click **Connect to Salesforce** ([coverage/salesforce.md](coverage/salesforce.md)).
   - **Pipedrive:** create an app in Pipedrive's Developer Hub with the same callback URL and the scopes in
     [coverage/pipedrive.md](coverage/pipedrive.md); enter the company's `https://<company>.pipedrive.com` as
     the Store URL and the app's client ID and secret, save, then click **Connect to Pipedrive**.
   - **Zoho CRM:** register a server-based client in Zoho's API console as for Zoho Books; enter the
     organisation's CRM address in its data centre (`https://crm.zoho.eu`) as the Store URL and the client's
     ID and secret, save, then click **Connect to Zoho CRM** ([coverage/zoho_crm.md](coverage/zoho_crm.md)).
   - **Webflow:** register a Data Client app in a Webflow workspace with the same redirect URI and the read
     scopes in [coverage/webflow.md](coverage/webflow.md); enter the site's address as the Store URL and the
     app's client ID and secret, save, then click **Connect to Webflow** and authorise the site.
4. Run a **Dry Run**, then a **Full Run**, from the connection.

## Gift cards, store credit and loyalty

Gift cards and store credit live in **Square Gift Cards**, loyalty in **Square Loyalty**: one balance, spent
online and at the Square counter. A connection with "Gift Cards and Store Credit" or "Loyalty Accounts" on
writes each source card or account to its Square account (environment, location and access token on the
connection) and keeps it in ERPNext as an **Importer Gift Card** with its ledger, since Square can't backdate
activity. Balances are loaded once; afterwards only what the source changes is applied, so what is spent in
Square stays spent, and a write in Square is recorded at once so a retried run never repeats it. Sources
today: WooCommerce Gift Cards, BigCommerce gift certificates and store credit, and Wix Loyalty (each coverage page has the
details). **None of this has run against the Square sandbox yet**: every Square request and response the
tests use is recorded, and validated against Square's published API specification.

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
3. **Fulfilments are Shipments, never Delivery Notes** (a Delivery Note would move stock a second time):
   each fulfilment is a Shipment from the company's address (give each company one) to the order's
   shipping address (an Address of the customer's, kept with the order), with the source's carrier,
   service, tracking number (AWB) and link, delivery state and contents. Shipments stay drafts: ERPNext
   submits one only with its parcels' weights, which no source gives per fulfilment. A fulfilment the
   source drops is discarded.
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
6. **Discounts through ERPNext's own pricing:** a discount is a Pricing Rule (a Promotional Scheme when it
   has tiers or applies to several customers or customer groups), coupon based with a Coupon Code per code,
   or automatic without one. A percentage, an amount or a fixed rate; a free or discounted item (the same
   item, recursively); on the transaction, products, single variants, collections or every item; minimum
   and maximum quantity and spend, dates, use limit, a customer or customer group (source groups mirror to
   Customer Groups `<name> - <abbr>`), discounts on other items than those bought. What a source's
   discount holds that ERPNext's pricing doesn't enforce fails closed: each condition is recorded on the
   Pricing Rule, Promotional Scheme and Coupon Code (`importer_conditions`: its kind, the companion card in
   the frappe-webshop fork that enforces it, and the source's terms), and the discount is **held**: written
   disabled, marked "held: awaiting enforcement (card N)" (`importer_hold`), its codes not redeemable (a
   Coupon Code has no disabled flag, so a held code is valid until yesterday; the source's dates stay on
   its rule), with one warning per run that writes it. A code with conditions of its own is held alone.
   **The fork enforces** (card 405, `contract.ENFORCED`), so these are written enabled, onto its fields:
   per-customer and once-per-customer limits of a code's discount (`maximum_use_per_customer` on the
   Pricing Rule, over all its codes) or of one code (on its Coupon Code); the customers and email addresses
   a code is for (`eligible_customers`, `eligible_emails`); free shipping and an amount or percentage off
   shipping on one Pricing Rule (`shipping_discount`, with its cap, maximum shipping charge, countries and
   products). Held still: per-customer limits and eligibility of an automatic discount (nothing counts its
   uses), shipping discounts of a Promotional Scheme or limited to a source's own shipping methods or zones
   (not mirrored as Shipping Rules), a shipping amount the source shares with the items' discount (Magento,
   Lightspeed), and terms not read from the source (`unread`: Shopify's free-shipping countries,
   WooCommerce's free-shipping zones); and the rest of card 405 (shared and shipping-method limits,
   exclusions, schedules, combination settings). **Card 407** (discount caps and segments), also enforced
   on one Pricing Rule, coupon based or automatic: who a discount is for (a segment's members as
   `eligible_customers` and `eligible_customer_groups`, several customer groups, `excluded_customer_groups`,
   new customers only or a number of earlier orders) and how much it gives (`maximum_discount_amount` off
   the order; an amount once, spread across some items, `spread_discount_amount`, which makes the rule one on
   those items; at most so many units, the cheapest or dearest first or of each line; so many units for one
   price, `fixed_price_set_*`). Held still: those limits on a Promotional Scheme; an item limit or a
   fixed-price set whose order the source doesn't document (WooCommerce, Square, Squarespace, Wix, and
   BigCommerce without a strategy); segments whose members aren't read (Shopify, BigCommerce, Ecwid); terms
   the source leaves undocumented (`undocumented`: whether an amount off some products is per item or in
   total in Wix, Ecwid, Lightspeed and Squarespace; Square's cap; Lightspeed's repeat customers) or groups
   not mirrored (Square, Lightspeed); and a source's own item criteria, rule conditions and item choices
   (`source`). What no card covers yet (other sales channels, further rules, a source's own price lists,
   staff-applied discounts) awaits an operator decision. A site whose Webshop fork lacks a card's fields
   defers a discount with a condition of that card the fork enforces until it is updated. When the fork
   enforces another condition, it joins `contract.ENFORCED` and the next run enables the discounts it held.
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
12. **Currency:** an order in another currency than the company's is a multi-currency Sales Invoice in
    the order's currency at the exchange rate the source recorded (BigCommerce's store-to-transactional
    rate, Magento's base total or rate, WooPayments Multi-Currency's or Aelia's order meta), paid at that
    rate; when the source booked its total in the company's currency too (Magento, Aelia) that total must
    match exactly. Enable Accounts Settings' "Allow multi-currency invoices against single party account"
    so a customer (and the guest customer) can be invoiced in several currencies on its own receivable;
    without it, an order needs a receivable account in its currency and a customer that never bought in
    another. An order in another currency whose source recorded no rate at all (WooCommerce without a
    multi-currency extension's meta) isn't booked at a rate the source never had (reported).
13. **Required apps:** the app requires ERPNext, Webshop, the blog app and the Public Site router (every
    connection belongs to a Public Site). HRMS is needed only by the HR sources of later phases.
14. **Schedule:** incremental hourly, full daily, a five-minute tick for paused runs. Times are UTC in the
    engine and the site's system time zone on posting dates.
15. **Tax-inclusive prices** (Shopify, Squarespace, Wix, Ecwid): the invoice's taxes are "On Net Total"
    rows included in the line rates, shipping's (and a fee's) tax a row on its charge; an order is kept
    only when ERPNext computes every tax row and the total to the source's amounts. Lines taxed at
    different rates carry their own: each included tax books to an account of its own (`<title> - <abbr>`,
    beside the connection's tax account) and each line's rates are an Item Tax Template (`GST at 5, PST
    at 7 - <abbr>`), created as orders need them.
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
21. **ERP and HR sources' books** (their own invoices, orders, payments and journals): customer invoices
    and vendor bills are Sales and Purchase Invoices (credit and debit notes their returns, against the
    invoice they return when the source names it), each line on its mirrored income or expense account,
    taxes as "Actual" amounts on their mirrored accounts; payments are Payment Entries allocated to the
    invoices they settle (an invoice must be posted before a payment is allocated to it); journal entries
    are Journal Entries on the mirrored accounts. Sales and Purchase Orders are submitted once confirmed
    and never move stock. The source's chart of accounts, cost centers, warehouses and departments go
    under the company's roots, named with the connection's abbreviation. Payments and journals in another
    currency than the company's are planned (card 386).
22. **Employees without a gender, date of birth or date of joining** (ERPNext requires all three) are
    saved without them, never with an invented value; a person fills them in before editing the record.
23. **Leads sharing an email:** ERPNext refuses a second Lead with the same email unless CRM Settings'
    "Allow Lead Duplication based on Emails" is on; turn it on when several connections (or a source)
    hold leads with one address, or the later ones aren't mirrored (reported).
24. **Archived records stay live:** an account, cost center or product a source archived stays enabled
    (and a customer, supplier, department or lead disabled but linked), so the history that names it still
    books.
25. **A stock item bought without a receipt** books to Stock Received But Not Billed, as ERPNext's perpetual
    inventory requires, not to the expense account the source's bill line names.
26. **Payments a source doesn't expose with their party aren't invented:** Business Central's API v2.0 gives
    posted payments only as G/L entries naming no customer or vendor, and Dataverse keeps none, so their
    invoices stay outstanding in ERPNext. Mirroring them needs Business Central's customer and vendor ledger
    entry pages published as OData web services (or a custom API page): the operator's call.
27. **Finance & Operations reads through a per-connection field map:** the defaults name only what
    Microsoft's entity reference documents; whatever else a kind needs (an order date, a product name,
    vendors' and purchase orders' entities) the operator maps for their environment.
28. **A source's own fields are kept by label, not as ERPNext fields:** the values of the fields a CRM's
    users defined (HubSpot properties, Salesforce and Zoho custom fields, Pipedrive's; a Webflow CMS item's
    fields beyond its body) land in a read-only **Source Fields** JSON field the app adds to Item, Lead,
    Prospect, Opportunity, Customer, Supplier, Communication, Event, ToDo and Blog Post, so every value is
    mirrored and change-tracked without one connection's
    fields reshaping the site's doctypes. Promoting a field to a Custom Field of its own is the operator's
    call.

## Next steps

Still to do:

1. **Menus** need a per-site menu in the router: Website Settings' top bar and footer are global.
2. **Router clean URLs** (frappe-public-site-router): serve Website Items, Item Groups and Blog Posts at
   `/<path>` on a site's hosts from `<prefix>/<path>`, and resolve Website Route Redirects whose source
   is under the site's prefix before Frappe's global redirect lookup.
3. **HR and CRM sources' planned homes** (card 386): Agendrix's part-day leave, open shifts and time banks,
   HighLevel's pipeline stages and orders (undocumented shapes), PeopleSoft's absences and reported time;
   each coverage page lists them.
4. **Native mirroring still to build (card 386):** multi-location stock as one Warehouse per source
   location; fixed bundles as Product Bundles; wishlists in the Webshop's Wishlist; units of measure
   (Square's measurement units, Ecwid's carats); itemised refunds where the source gives the refunded items
   (Square's return orders). Until built, each source's coverage page lists them as planned.
5. **Discount conditions held for enforcement:** every source's discounts are mirrored; what ERPNext's
   pricing can't enforce is recorded and holds the discount until the Webshop fork enforces it (cards 405 and
   407, decision 6; card 405's coupon limits, eligibility and shipping discounts, and card 407's eligibility
   and discount limits, are enforced where the source's terms are known). Conditions no card covers yet
   (other sales channels, further rules, a source's price
   lists, staff-applied discounts) and Magento's catalog price rules (no REST API) are operator decisions.
6. **Operator decision:** Shopify's Buy X Get Y and app discounts, per-location stock and bundles need new
   Admin query fields whose cost must be recorded from a live store (wp-woo-idempotent-importer's
   `tools/calibrate-query-cost.php`); every operation's cost is proven against such a recording.

## License

[AGPL-3.0-or-later](LICENSE).
