<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Wave → ERPNext

The single source of truth for what the Wave adapter mirrors. **Not yet run against a live business:** there are
no live credentials; every fixture (`tests/fixtures/wave`, written by `python -m tools.wave_fixtures`) is an
answer to one of the adapter's GraphQL operations (`idempotent_importer/sources/wave/operations.py`), and
`tools/prove.py` validates every operation against Wave's schema and executes every answer back through its
operation, so each has exactly the shape Wave's schema gives it. The schema is `tests/schemas/wave.json`:
Wave's public schema, introspected from https://gql.waveapps.com/graphql/public (which answers introspection
without a token) and pruned by `tools/wave_schema.py` to what the operations select (its SDL a line each),
with the date and the SHA-256 of the whole schema.

Access: one connection per business. Create an application in Wave's developer portal
(developer.waveapps.com, Manage Applications) with this site's
`/api/method/idempotent_importer.api.oauth_callback` as its redirect URI; the adapter asks for the scopes
`business:read`, `account:read`, `sales_tax:read`, `product:read`, `customer:read`, `vendor:read`,
`invoice:read` and `user:read`. Enter its client ID and secret on the connection (Store URL
`https://next.waveapps.com`), save, and click **Connect to Wave**: the business the approval opens (with
several, the one whose id is already the account) becomes the account. OAuth 2: the client ID, secret and
redirect URI in the form of every token request (refreshes too), short-lived access tokens, and the refresh
token kept (a new one when a refresh returns it). A refused token or an unauthenticated GraphQL answer stops the
run and holds scheduled runs. Wave's API answers only for businesses on a paid (Pro or Advisor) plan: a refresh
for a lapsed one is refused, which holds the connection the same way.

Reading: each list a page at a time (`page`, `pageSize` 45, Wave's default; `pageInfo.totalPages`), oldest
first where Wave orders it (products, customers, estimates, invoices). Incremental runs ask for what changed
since the last clean run (`modifiedAtAfter`) of sales taxes, products, customers, vendors, estimates and
invoices; payments are read with their invoice, so an incremental run reads the payments of the invoices that
changed; the chart of accounts is read whole and skipped by its hash. Deletions come only from a completed,
unfiltered full pass. Wave documents no request quota for its public API; the importer paces two requests a
second, backs off on 429 and server errors, and pauses the run when Wave keeps refusing. Wave's ids are global
ids (base64 of `Business:<uuid>;<Type>:<id>`): an identity carries the record's own id. Amounts are exact
decimals in their currency (Wave's `Decimal` and `Money.value`, strings).

## Mirrored

| Wave | ERPNext home | Notes |
|---|---|---|
| Business | The connection's company | The business is the connection's account; its records go to the connection's company. |
| Chart of accounts | **Account** | Number and name, under the company's root of its type, the ERPNext account type of its subtype (cash and bank, receivable, payable, sales tax, inventory, fixed asset, cost of goods sold, depreciation, income, expense, equity). |
| Sales taxes | **Sales Taxes and Charges Template** | One row on the net total at the tax's current rate (Wave's decimal x 100), booked to the connection's tax account, its tax number in its description; an archived tax isn't kept. |
| Products and services | **Item** with **Item Price** | Name, description and unit price (Wave counts no stock). |
| Customers | **Customer**, **Contact**, **Address** | A business (else the person), contact, email, phone, address and currency; archived is disabled. |
| Vendors | **Supplier**, **Contact**, **Address** | The same; archived is disabled. |
| Estimates | Draft **Quotation** to the customer | Lines (on their products), discount and the taxes Wave computed, which must make the total exactly; a deleted estimate isn't kept. |
| Invoices | **Sales Invoice** | Lines at quantity x unit price on their products, the discount, the taxes Wave computed on each line summed by sales tax, which must make the total exactly; a draft stays a draft, any other status is submitted; the exchange rate Wave recorded for another currency. |
| Invoice payments | **Payment Entry** (receive) | From the customer into the account Wave deposited it to, allocated to the invoice; an inactive (removed) payment isn't kept. |

## Planned and gaps

| Wave | Status |
|---|---|
| Bills, expenses, receipts, bank and journal transactions | Gap: Wave's public API exposes no read of them (only `moneyTransactionCreate`, which writes one). Mirrored when Wave publishes a read. |
| Compound sales taxes | Planned (card 386): a template row on the previous row's total. |
| Refunds of invoice payments, estimate deposit payments | Planned (card 386): a payment out against a credit note, and the deposit applied to the invoice the estimate becomes. |
| Recurring invoices, invoice reminders and attachments | Planned (card 386): **Auto Repeat**, **File**. |
| Payroll, bank connections, receipts | Gap: not in Wave's public API. |
