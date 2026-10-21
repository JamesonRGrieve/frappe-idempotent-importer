<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: HubSpot → ERPNext

The single source of truth for what the HubSpot adapter mirrors. **Not yet run against a live account:**
there are no live credentials; every fixture (`tests/fixtures/hubspot`, written by
`python -m tools.hubspot_fixtures`) is shaped as HubSpot's CRM API v3 OpenAPI documents define each response,
and is validated against them by `tools/prove.py`. The documents are the ones HubSpot publishes for its API
reference (https://github.com/HubSpot/HubSpot-public-api-spec-collection, `PublicApiSpecs/CRM/*/v3`, pinned to
commit `dd4246cd` and each document's SHA-256 in `tools/specs.py`). Property meanings follow HubSpot's CRM
guides (developers.hubspot.com/docs/api-reference/crm-*-v3/guide); OAuth and limits follow
developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/oauth/working-with-oauth and
developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines.

Access: one connection per HubSpot account. Create an app in a HubSpot developer account with this site's
`/api/method/idempotent_importer.api.oauth_callback` as its redirect URL and the scopes `oauth`,
`crm.objects.owners.read`, `crm.objects.contacts.read`, `crm.objects.companies.read`,
`crm.objects.deals.read`, `crm.objects.calls.read`, `crm.objects.emails.read`, `crm.objects.meetings.read`,
`crm.objects.notes.read`, `crm.objects.tasks.read`, `crm.objects.products.read`,
`crm.objects.line_items.read`, `crm.objects.quotes.read`, `crm.objects.orders.read`,
`crm.objects.invoices.read` and `crm.objects.commercepayments.read`. Enter its client ID and secret on the
connection (Store URL `https://app.hubspot.com`), save, and click **Connect to HubSpot**, choosing the account.
OAuth 2: the authorization code grant, the client's secret in the token form, a half-hour access token and a
refresh token that stays the same. A refused token stops the run and holds scheduled runs.

Reading: every object list pages after the cursor its answer gives, 100 at a time, with the properties the
mappers read plus every property the account's users defined (`hubspotDefined` false), and the associations
each object needs; notes, line items, discounts, fees, taxes and a company's people are read in batches of
100. Incremental runs ask the search API for the objects changed since the last clean run
(`hs_lastmodifieddate`, a contact's `lastmodifieddate`), in id order after the last id read, so no query reaches
search's 10,000-result cap, and read each changed object whole by id; the customers, owners and pipelines are
read whole every run. Deletions come only from a completed, unfiltered full pass. Limits: 110 requests every
10 seconds per account for an OAuth app, and five a second for search; the importer paces 100 every 10
seconds (quieting when `X-HubSpot-RateLimit-Remaining` runs out) and search four a second, backs off on 429
and server errors, and pauses the run when HubSpot keeps refusing. Amounts are exact decimals as HubSpot
states them.

## Mirrored

| HubSpot | ERPNext home | Notes |
|---|---|---|
| Owners | **Employee** | Names and email; an archived owner is inactive. A queue is no person and isn't kept. HubSpot keeps no gender or dates: they stay empty, never invented. |
| Products | **Item** with **Item Price** | Name, SKU, description, unit price; an inventory product is a stock item. |
| Contacts | **Lead** with its **CRM Notes** | Names, company, email, phone, mobile, job title, website and the notes on the contact; the lead status as ERPNext's (new Lead, open and attempted Open, in progress and connected Replied, open deal Opportunity, bad timing Interested); a customer is Converted; an unqualified lead is closed (disabled). |
| Companies | **Prospect** with its **Contact**, **Address**, leads and **CRM Notes** | Named with the connection's abbreviation; its contacts are its leads; its domain is its website when it has none. |
| Contacts and companies at the customer lifecycle stage, and those invoices, orders and payments are for | **Customer**, **Contact**, **Address** | A contact as a person; a company with its people as its **Contacts** (names, email, phone, job title). |
| Deal pipelines | **Opportunity Type** | Named with the connection's abbreviation. |
| Deals | **Opportunity** with its company (a prospect) or contact (a lead), at its **Sales Stage** | Name, amount and currency, stage probability, close date, description and notes; a closed stage with full probability is Converted, any other closed stage Lost. |
| Calls | **Communication** (phone) | Title, body, duration and status, direction, the number dialled or calling; recorded on the contact, else the deal, else the company. |
| Emails | **Communication** (email) | Subject, text, direction and address; recorded, never sent. |
| Meetings | **Event** (private) with its contacts, deals and companies | Title, start and end (shown on the site's clock), body, internal notes and location; outcome as status (scheduled and rescheduled Open, completed Completed, no-show Closed, cancelled Cancelled). |
| Tasks | **ToDo** about its contact, deal or company | Subject and body, due date, completed (Closed) or open. |
| Notes | **CRM Notes** on the lead, prospect or opportunity they are on | Body and time. |
| Quotes | Draft **Quotation** to the company (a prospect) or contact (a lead) | Line items, discounts, fees and taxes, which must make the quote's amount exactly; a rejected quote isn't kept. |
| Orders | Submitted **Sales Order** to the customer | Line items, discounts, fees (charges) and taxes. |
| Invoices | **Sales Invoice** to the customer | Line items, discounts, fees and taxes; drafts stay drafts, open and paid invoices are submitted; voided ones aren't kept. The purchase order number and comments are its remarks. |
| Commerce payments | **Payment Entry** | A collected payment from its customer, allocated to the one invoice it pays (else an advance); what was refunded (or lost in a dispute) is a payment out of its own. Processing and failed payments move no money. |
| Properties the account's users defined (on every object above) | **Source Fields** on the record | By label; a choice property's value is its option's label. |

## Planned and gaps

| HubSpot | Status |
|---|---|
| Line items taxed by a tax rate group | Planned (card 386): HubSpot doesn't document the amount a tax rate group adds to a line, so such a document is Unsupported until it does. |
| Recurring products and line items, subscriptions | Planned (card 386): **Subscription Plan** and **Subscription**. |
| HubSpot lead objects (the Leads pipeline) | Operator decision: a contact is already its **Lead**; a separate lead record would duplicate it. |
| Deal type, priority and other built-in properties without an ERPNext field | Operator decision: which built-in properties beyond those above deserve fields. |
| Tickets, custom objects, carts, subscriptions, goals, feedback, marketing emails, forms, lists, workflows | Operator decision: outside the sales pipeline and the books, or with no first-party home. |
| Order fulfilment status and tracking | Planned (card 386): **Shipment** for a CRM order. |
| Owners' teams and the owner of each record | Operator decision: HubSpot's users aren't this site's users. |
