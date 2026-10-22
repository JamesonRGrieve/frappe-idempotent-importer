<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: FreshBooks → ERPNext

The single source of truth for what the FreshBooks adapter mirrors. **Not yet run against a live business:**
there are no live credentials; every fixture (`tests/fixtures/freshbooks`, written by
`python -m tools.freshbooks_fixtures`) is shaped as the API answers, and is validated by `tools/prove.py` against
`tests/schemas/freshbooks.json`. FreshBooks publishes no OpenAPI document, so that schema is transcribed by
`tools/freshbooks_schema.py` from its API reference (https://www.freshbooks.com/api/, each path citing its page:
clients, invoices, estimates, taxes, items, services, project, time_entries, vendors, bills, bill_payments,
payments, credits, chart-of-accounts, general-ledger-report, journal-entries, identity_model).

Access: one connection per business. Create an app in FreshBooks' developer portal with this site's
`/api/method/idempotent_importer.api.oauth_callback` as its redirect URI and the scopes `user:profile:read`,
`user:clients:read`, `user:invoices:read`, `user:estimates:read`, `user:payments:read`, `user:expenses:read`,
`user:bills:read`, `user:bill_vendors:read`, `user:bill_payments:read`, `user:billable_items:read`,
`user:projects:read`, `user:time_entries:read`, `user:taxes:read`, `user:journal_entries:read`,
`user:other_income:read` and `user:reports:read`. Enter its client ID and secret on the connection (Store URL
`https://my.freshbooks.com`), save, and click **Connect to FreshBooks**: the business `users/me` names (with
several, the one whose account id is already the account) becomes the account, its account id, business id and
business uuid together (`<account id>|<business id>|<business uuid>`). OAuth 2: JSON token requests naming the
redirect URI, short-lived access tokens, and one-time refresh tokens, each refresh's new one kept before the
access token is used. A refused token stops the run and holds scheduled runs.

Reading: accounting lists 100 a page (`per_page`, FreshBooks' cap) with `include[]=lines` for invoices and
estimates; projects, time entries and services with their `meta`. Expenses and other income are read as their
postings in the general ledger (from 2000-01-01 to today), grouped by the expense or income they belong to.
Incremental runs ask for what changed since the last clean run: clients, invoices, estimates and payments by
`search[updated_min]` (a date: the whole day is read again), projects and time entries by `updated_since`; the
rest is read whole and skipped by its hash. Deletions come only from a completed, unfiltered full pass.
FreshBooks documents no request limit beyond rate limiting bursts (www.freshbooks.com/api/limits); the importer
paces two requests a second, backs off on 429 and server errors, and pauses the run when FreshBooks keeps
refusing. Amounts are exact decimals in their currency.

## Mirrored

| FreshBooks | ERPNext home | Notes |
|---|---|---|
| Chart of accounts | **Account** | Accounts and their sub-accounts (an account with sub-accounts is their group), numbers and names, under the company's root of their type. |
| Taxes | **Sales Taxes and Charges Template** | One row on the net total at the tax's rate, booked to the connection's tax account, its registration number in its description. |
| Items | **Item** with **Item Price** | Name, SKU, description, unit cost; a stock item at its inventory when FreshBooks counts it. |
| Services | **Item** with **Item Price** | Name and rate. |
| Clients | **Customer**, **Contact**, **Address** | An organisation (else the person), email, phone, billing address, currency and VAT number; archived is disabled, deleted isn't kept. |
| Bill vendors | **Supplier**, **Contact**, **Address** | Name, primary contact, phone, address and currency; archived is disabled. |
| Projects | **Project** for the client | Title, description, due date; completed, active (Open) or on hold. |
| Time entries | **Timesheet** (submitted; a running timer a draft) | One time log from the start for the duration (hours to the microhour), on the project for the client, billable or not, with the note. |
| Estimates | Draft **Quotation** to the client | Lines, discount and taxes, which must make the amount exactly. |
| Invoices | **Sales Invoice** | Lines at quantity x unit cost, the discount, each tax on the discounted lines it is on (rounded to the cent), which must make the amount exactly; a draft or created invoice is a draft, any other submitted. |
| Credit notes | **Sales Invoice** return | Their amount as one line: FreshBooks documents no credit note lines. |
| Bills | **Purchase Invoice** from the vendor | Lines, their taxes as FreshBooks computed them, which must make the total; the bill number is the supplier's reference. |
| Payments | **Payment Entry** (receive) | From the client, allocated to the invoice it pays. |
| Bill payments | **Payment Entry** (pay) | To the bill's vendor, allocated to the bill. |
| Manual journal entries | **Journal Entry** | Line by line on the mirrored accounts. |
| Expenses and other income | **Journal Entry** | Their postings in the general ledger, exactly as FreshBooks booked them. |

## Planned and gaps

| FreshBooks | Status |
|---|---|
| Compound taxes | Planned (card 386): a template row on the previous row's total. |
| Payments made from a client's credit | Planned (card 386): the credit's application to the invoice (they move no money). |
| Credit note lines and their taxes | Planned (card 386): FreshBooks documents no credit note lines. |
| Team members and the time they log | Planned (card 386): **Employee** for each team member, and their time logs on their timesheets. |
| Retainers, recurring invoice profiles, invoice and expense attachments, other income's own record | Planned (card 386): **Auto Repeat**, **File**. |
| Gateways, online payment settings, webhooks, reports | Operator decision: settings with no mirrored history. |
