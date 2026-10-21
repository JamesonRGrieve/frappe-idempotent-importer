<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: HighLevel (GoHighLevel) → ERPNext

The single source of truth for what the HighLevel adapter mirrors. **Not yet run against a live
sub-account:** there are no live credentials; every fixture (`tests/fixtures/highlevel`, written by
`python -m tools.highlevel_fixtures`) is shaped as HighLevel's API v2 OpenAPI documents define each
response, and is validated against them by `tools/prove.py`. The documents are the ones HighLevel publishes
for its developer reference (https://github.com/GoHighLevel/highlevel-api-docs, `apps/*.json`, CC0, pinned to
commit `0af86a4c` and each document's SHA-256 in `tools/specs.py`); OAuth, scopes and limits follow the same
repository's `docs/oauth` pages. Where a document types a field against its own response example, the
example's shape is used, and each correction is recorded in `tools/specs.py` (an invoice's items and
contact, a transaction's status).

Access: one connection per sub-account (location). Create an app in HighLevel's Marketplace
(https://marketplace.gohighlevel.com, a developer account) for sub-accounts, with this site's
`/api/method/idempotent_importer.api.oauth_callback` as its redirect URI and the read scopes `users.readonly`,
`products.readonly`, `products/prices.readonly`, `contacts.readonly`, `businesses.readonly`,
`opportunities.readonly`, `conversations.readonly`, `conversations/message.readonly`, `invoices.readonly` and
`payments/transactions.readonly`. Enter its client ID and secret on the connection (Store URL
`https://app.gohighlevel.com`), save, and click **Connect to HighLevel**, choosing the sub-account: the token's
`locationId` becomes the connection's account. OAuth 2: the authorization code grant, the client's secret in
the token form, an access token for a day and a refresh token replaced on each refresh (the newest kept). A
refused token stops the run and holds scheduled runs.

Reading: each endpoint's own paging (contacts after the last one read, opportunities after the `meta`
cursor, the rest by offset until a short page); conversations in windows of the time they were added,
halved until one page holds a window, each with all its messages. HighLevel documents no modified-since
filter for any of these, so every run is a full pass and the change hash skips what didn't change; deletions
come only from a completed, unfiltered full pass. Every request carries the API version its document names.
Limits: 100 requests in 10 seconds and 200,000 a day per app and sub-account; the importer paces ten a second,
quiets when `X-RateLimit-Remaining` runs out, backs off on 429 and server errors, and pauses the run when the
day is spent. Amounts are taken as HighLevel states them, in the record's currency.

## Mirrored

| HighLevel | ERPNext home | Notes |
|---|---|---|
| Users | **Employee** | Names, email, phone; a deleted user is inactive (ERPNext's Left needs a leaving date HighLevel doesn't keep). HighLevel keeps no gender or dates: they stay empty, never invented. |
| Products and their prices | **Item** with **Item Price**; variants as **Item** variants | A one-time price is the item's price; a product with variant options has a variant per price of a set of options (its stock when the price tracks inventory). Physical products are stock items. |
| Contacts | **Lead** with its **CRM Notes** | Names, company, email, phone, website and the contact's notes (title and body, when written); a contact asking not to be disturbed is Do Not Contact. |
| Contacts that are billed | **Customer**, **Contact**, **Address** | The contacts the invoices and transactions name, as people (or their company) with email, phone and address. |
| Businesses | **Prospect** with its **Contact**, **Address** and leads | Named with the connection's abbreviation; its contacts are its leads. |
| Pipelines | **Opportunity Type** | Named with the connection's abbreviation. |
| Opportunities | **Opportunity** with its lead | Name, monetary value, pipeline as its type; open, won (Converted), lost, abandoned (Closed). |
| Conversation messages | **Communication** on the lead's timeline | Email, SMS, call (duration and status), chat or other, received or sent, when, the body and attachment addresses; recorded, never sent. |
| Appointments | **Event** (private) with the lead as participant | Title, start and end as the calendar shows them, status (booked and confirmed Open, showed Completed, no-show Closed, cancelled Cancelled), notes and address. |
| Tasks | **ToDo** about the lead | Title and body, due date, completed (Closed) or open; assigned to no one (HighLevel's users aren't this site's). |
| Invoices | **Sales Invoice** to the customer | Items at their amounts, the total summary's discount and tax, which must make the total exactly; drafts stay drafts, sent and paid ones are submitted; void and test-mode ones aren't kept. |
| Payment transactions | **Payment Entry** | A succeeded live payment from the customer, allocated to the invoice it pays (else an advance); what was refunded of it is a payment out of its own. |

## Planned and gaps

| HighLevel | Status |
|---|---|
| Pipeline stages (an opportunity's stage) | Planned (card 386): HighLevel's documents don't describe a stage (`stages` is an array of arrays), so **Sales Stage** can't be named yet. |
| Orders (`/payments/orders`) | Planned (card 386): an order's items are documented only as strings, so an order can't be booked exactly; its payments are mirrored as advances. |
| Product collections | Planned (card 386): the collection list's items are undocumented (an array of arrays). |
| Recurring prices and subscriptions | Planned (card 386): **Subscription Plan** and **Subscription**. |
| Several one-time prices of a product without variants | Planned (card 386): a **Price List** per price. |
| Estimates, invoice schedules and templates, coupons | Planned (card 386): **Quotation**, **Auto Repeat**, **Coupon Code**. |
| Calendars, calendar groups, blocked slots, availability | Operator decision: ERPNext has no calendar of its own beyond **Event**. |
| Custom fields and custom values, tags, custom objects and associations | Operator decision: custom fields per sub-account. |
| Campaigns, workflows, forms, surveys, funnels, websites, blogs, social posts, media, courses | Operator decision: outside the CRM and the books (the website ones belong to the site router). |
| Contacts merged into others, a contact's attribution | Recorded as HighLevel has them on the surviving contact only. |
