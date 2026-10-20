<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Xero (Accounting and Projects APIs) → ERPNext

The single source of truth for what the Xero adapter mirrors. **Not yet run against a live organisation:**
there are no live credentials; every fixture (`tests/fixtures/xero`) is shaped as Xero's published OpenAPI
documents define it and proven against them strictly: `xero_accounting.yaml` (19.1.0), `xero-projects.yaml`
and `xero-identity.yaml` from `XeroAPI/Xero-OpenAPI`, pinned to commit `5a115f37` in `tools/specs.py`.

Access: a Web app at developer.xero.com with this site's `/api/method/idempotent_importer.api.oauth_callback`
as its redirect URI; its client ID and secret on the connection, then **Connect to Xero**, which keeps the
approved organisation (tenant, from `/Connections`) as the account; when an approval connects several, the
operator enters the one to mirror. OAuth 2 (developer.xero.com/documentation/guides/oauth2/auth-flow): HTTP
Basic client authentication, a 30-minute access token, a new refresh token on every refresh, and the granular
read-only scopes apps made since 2 March 2026 must use (`offline_access accounting.settings.read
accounting.contacts.read accounting.invoices.read accounting.payments.read accounting.banktransactions.read
accounting.manualjournals.read projects.read`; developer.xero.com/documentation/guides/oauth2/scopes).

Reading: every request names the tenant (`xero-tenant-id`). Paged collections 1000 at a time (`page`,
`pageSize`, until the response's page count; projects 500); the rest whole. Incremental runs send
`If-Modified-Since`; tracking categories and projects are read whole (no modified filter) and skipped by
hash; deletions come only from a completed, unfiltered full pass, and a deleted or voided record retires its
copy. Archived contacts are read too. Limits (developer.xero.com/documentation/guides/oauth2/limits): per
tenant 60 calls a minute, 5000 a day, 5 at once; requests are paced a second apart, the remaining minute's
allowance (X-MinLimit-Remaining) is watched, and a 429's Retry-After (a spent day's is long) pauses the run.

What the reader looks up for the mappers: a line item's account id when it names its account by code only
(from /Accounts, read once); the organisation's GST or VAT system account for tax on a bank transaction or
manual journal; the bank account of a prepayment's or overpayment's bank transaction.

## Mirrored

| Xero | ERPNext home | Notes |
|---|---|---|
| Accounts (archived too) | **Account** | Code, name, root type from the class, account type from the type and system account (bank, receivable, payable, tax, stock, fixed asset, cost of goods sold, income, expense, depreciation, equity), currency. |
| Tracking categories and options | **Cost Center** groups and cost centers | A line books to its first tracking option. |
| Items | **Item** (unpublished **Website Item**), **Item Price**, stock | Code as SKU, sales price, quantity on hand for tracked items. |
| Contacts | **Customer** and/or **Supplier**, **Contact**, **Address** | A contact that is a customer, or neither, is a customer; a supplier a supplier; one that is both is both. Person or company, email, default/mobile phone, postal (billing) and street (shipping) addresses, tax number, default currency. |
| Projects | **Project** for its contact | Deadline as the expected end. |
| Quotes | Draft **Quotation** | A deleted or declined quote has none. |
| Purchase orders | **Purchase Order** | Delivery date; a draft stays a draft. |
| Invoices (ACCREC), credit notes (ACCRECCREDIT) | **Sales Invoice** and credit notes | Lines net of tax whatever the line amount type, item, account and tracking per line, tax per tax type, the total exactly; drafts and submitted-for-approval stay drafts. |
| Bills (ACCPAY), credit notes (ACCPAYCREDIT) | **Purchase Invoice** and debit notes | As invoices; the reference as the supplier's. |
| Payments of invoices and refunds of credit notes | **Payment Entry** | Its account, allocated to the document it pays. |
| Prepayments, overpayments | **Payment Entry** (an advance) | Through its bank transaction's account, allocated to the invoices it was applied to. |
| Manual journals | **Journal Entry** | A positive line a debit, a negative one a credit, tax to the tax account; a draft stays a draft. |
| Spend and receive money | **Journal Entry** | The bank account against each line's account (with the contact) and the tax account. |
| Bank transfers | **Journal Entry** | |
| Invoices, credit notes, orders and quotes in other currencies | The document's currency at ERPNext's rate: 1 / Xero's CurrencyRate (document currency per one of the organisation's), to nine places. | Payments and journals in other currencies are planned (card 386). |

## Planned and gaps

| Xero | Status |
|---|---|
| A second tracking category on a line | Planned (card 386): an **Accounting Dimension** (a warning). |
| Refunds of prepayments and overpayments | Planned (card 386). |
| A spend or receive money's contact | Not kept on its lines: ERPNext books a party only on receivable and payable lines. |
| Transfers between accounts in different currencies | Planned (card 386): a multi-currency Journal Entry. |
| Contact groups, repeating invoices, budgets, tax rates, currencies, branding themes, attachments, history notes | Planned (card 386): **Customer Group**, **Auto Repeat**, **Budget**, **Item Tax Template**, **Currency Exchange**, **File**, **Comment**. |
| Project tasks and time entries | Planned (card 386): **Task** and **Timesheet**. |
| Payroll (AU, NZ, UK APIs: employees, leave, timesheets, payslips) | Planned: HRMS **Employee**, **Leave Application**, **Timesheet**, **Salary Slip** (operator decision: which regional payroll API). |
| Expense claims, receipts | Deprecated by Xero; planned with payroll. |
| Fixed assets (Assets API) | Planned (card 386): **Asset**. |
| Xero's own journals (/Journals) | Not needed: every posting comes from its source document; the endpoint also needs partner enrolment. |
