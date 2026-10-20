<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Acumatica (contract-based REST API) → ERPNext

The single source of truth for what the Acumatica adapter mirrors. **Not yet run against a live tenant:**
there are no live credentials; every fixture (`tests/fixtures/acumatica`, written by
`python -m tools.acumatica_fixtures`) is shaped as the Default endpoint 24.200.001 defines its entities and
proven against it strictly. Acumatica publishes the endpoint's OpenAPI description only from each instance
(`/entity/Default/24.200.001/swagger.json`); its official REST client
(https://github.com/Acumatica/AcumaticaRESTAPIClientForCSharp, commit `bc657f6f`) carries the endpoint's
entities generated from that description (`Endpoints/Acumatica.Default_24.200.001/Model`), which
`tools/acumatica_schema.py` transcribes into `tests/schemas/acumatica.json`.

Access: OAuth 2's authorization code flow through the instance's identity server
(`/identity/connect/authorize`, `/identity/connect/token`;
https://help.myob.com.au/advanced/Docs/Published/IntegrationDevelopmentGuide/OAuthOIDC_AuthorizationCode_AuthorizationCode.html,
the Integration Development Guide as MYOB publishes it for MYOB Acumatica). Register a connected application
(Connected Applications, SM303010) with the authorization code flow, a secret, this site's
`/api/method/idempotent_importer.api.oauth_callback` as a redirect URI, and refresh tokens allowed; enter the
instance's address as the Store URL and the application's client ID (`<id>@<tenant>`) and secret; then
**Connect to Acumatica**, approving as a user who can read the records below. The scopes are `api` and
`offline_access`; the refresh token chain lasts 30 days by default (every refresh's newest token is kept), so
the connection must run within that.

Reading: each entity at `/entity/Default/24.200.001/<Entity>`, with the linked and detail entities the
mappers read (`$expand`), in pages of 100 (`$top`/`$skip`). Incremental runs ask for
`LastModifiedDateTime gt` the day before the last clean run (an unchanged record is skipped by its hash);
subaccounts, which keep no modification time, are read whole every run. Deletions come only from a
completed, unfiltered full pass. Limits: Acumatica throttles web service requests by license (requests per
minute and concurrent requests, shown on the License Monitoring Console, SM604000) and answers 429 past
them; the importer sends one request at a time, at most one a second, and a 429 pauses the run.

## Mirrored

| Acumatica | ERPNext home | Notes |
|---|---|---|
| GL accounts | **Account** | Code, description, type (asset, liability, income, expense: Acumatica keeps equity under liability), cash accounts as Bank, currency; an inactive one disabled. Acumatica's chart is flat. |
| Subaccounts | **Cost Center** | A line's subaccount is its cost center; an inactive one disabled. |
| Warehouses | **Warehouse** | An inactive one disabled. |
| Item classes | **Item Group** | |
| Stock items, non-stock items | **Item**, **Item Price**, stock as a **Stock Reconciliation** | Description, default price, class, a stock item's quantity on hand across warehouses. An inactive item, or one marked for deletion, is disabled. |
| Customers, vendors | **Customer** / **Supplier**, **Contact**, **Address** | Name, individual or organisation, the main contact's email and phone, billing and shipping (else main) addresses, tax registration, currency, and each active contact (name, email, phone, job title); an inactive one disabled. |
| Employees' departments and job titles | **Department**, **Designation** | The Default endpoint has no department or position entity: the employees' distinct ones. |
| Employees | **Employee** | Names, gender, birth date, email, phone, department, job title, reports to; Inactive when inactive. |
| Projects | **Project** for its customer | Status (completed, canceled → Cancelled, suspended or on hold → On hold, else Open). |
| Leads | **Lead** | Person and company, email, phones, job title, web site, description; converted and disqualified statuses; an inactive one disabled. |
| Opportunities | **Opportunity** with its business account (as a customer) or its lead | Subject, status (won → Converted, lost), estimated close (also its date: the endpoint names no other), total, currency. |
| Sales orders (quotations, order type QT, as draft **Quotation**s) | **Sales Order** | Details, document discounts, taxes per tax, freight as shipping; on hold, credit hold or pending approval a draft; a cancelled one gone. |
| Purchase orders | **Purchase Order** | |
| A/R invoices, debit memos, credit memos | **Sales Invoice** (credit memos as returns, against the one invoice they are applied to) | Details with their account, subaccount and project, document discounts, taxes per tax; released (open, closed) booked, on hold or balanced a draft, voided or canceled gone. |
| Cash sales, cash returns | **Sales Invoice** and its own **Payment Entry** | |
| A/P bills, credit adjustments, debit adjustments | **Purchase Invoice** (debit adjustments as debit notes) | The vendor's reference. |
| Customer payments, prepayments, refunds; vendor checks (payments, prepayments, quick checks, refunds) | **Payment Entry** | Allocated to the invoices or bills they were applied to. |
| General ledger batches (module GL) | **Journal Entry** | Account, subaccount, project, debit and credit; the batches of other modules are their documents' postings and are not booked again. |

## Planned and gaps

| Acumatica | Status |
|---|---|
| The invoice's and cash sale's currency | The Default endpoint's A/R invoice names no currency: it is booked in its customer's currency; a customer allowed to bill in other currencies makes its invoices Unsupported (planned, card 386: a custom endpoint or the currency field added to the endpoint). |
| Exchange rates of A/R invoices in another currency | The Default endpoint records none: such an invoice waits (Unsupported) until a rate can be read (card 386). |
| Cash accounts | The Default endpoint has no cash account entity: payments book to the connection's deposit account (card 386: map each cash account to its GL account). |
| The customer or vendor on a journal line, branches, the AR and AP accounts of documents | Planned (card 386). Party accounts are the party's defaults in ERPNext. |
| Payments applied to credit memos or debit adjustments, voided payments, A/P prepayments | Planned (card 386): a Payment Entry allocates positive amounts only. Not mirrored (Unsupported) rather than booked wrongly. |
| Batches in another currency than their ledger's | Planned (card 386). |
| Stock per warehouse, inventory receipts, issues, adjustments, transfers, shipments, purchase receipts | Planned (card 386); quantities on hand stay authoritative so nothing is counted twice (operator decision). |
| Kits | Planned (card 386): **Product Bundle**. |
| Sales prices and price worksheets, discounts and promotions | Planned (card 386): **Item Price**, **Pricing Rule**. |
| Activities, tasks, events, cases, campaigns | Planned (card 386): **ToDo**, **Event**, **Issue**, **Campaign**. |
| Time entries, expense claims and receipts, payroll | Planned (card 386): **Timesheet**, **Expense Claim**; payroll has no ERPNext home without the payroll module (operator decision). |
| Project tasks, budgets, change orders, pro forma invoices | Planned (card 386): **Task**, **Budget**. |
| Attributes, custom fields, files | Planned (card 386): per-connection field maps, **File**. |
| A document changed at the source after an item it names was retired (marked for deletion) | Planned (card 386): the document waits (deferred) for the retired item; the writers resolve only live items. |
| An invoice changed at the source after a return was booked against it | Planned (card 386): the writer amends a changed booked invoice by cancelling it, which ERPNext refuses while a return names it; that invoice is reported Failed (and retried) until the writer can update it in place. |
