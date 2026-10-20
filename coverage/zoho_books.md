<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Zoho Books (API v3) → ERPNext

The single source of truth for what the Zoho Books adapter mirrors. **Not yet run against a live
organisation:** there are no live credentials; every fixture (`tests/fixtures/zoho_books`) is shaped as
Zoho's published OpenAPI modules define it (`https://www.zoho.com/books/api/v3/openapi-all.zip`, pinned by
SHA-256 in `tools/specs.py`) and proven against them strictly. Where those documents describe one object as
a list of one (several modules' `page_context`, a sales order read by id) or a list as one object (a vendor
credit's `line_items`), as the reference's own examples also do, the reader takes either.

Access: a server-based client in Zoho's API console (api-console.zoho.com, Multi-DC enabled) with this
site's `/api/method/idempotent_importer.api.oauth_callback` as its redirect URI; the Store URL is the
organisation's Books address in its data centre (`https://books.zoho.com`, `.eu`, `.in`, `.com.au`, `.jp`,
`.ca`, `.com.cn`, `.sa`, `.ae`), the client's ID and secret, then **Connect to Zoho Books**, which keeps the
organisation the approval opens as the account (the operator enters it when there are several). OAuth 2
(www.zoho.com/books/api/v3/oauth): offline access with the read-only scopes of every module read, the secret
in the form, an hour's access token, and a refresh token that never changes. Requests carry
`Zoho-oauthtoken` and `organization_id`.

Reading: each module 200 a page from page 1 while its page context says there are more (or a page is full);
a document's list entry is a summary, so each is read whole by its id. Incremental runs ask for
`last_modified_time` where the module takes it (accounts, estimates, sales orders, invoices, credit notes,
purchase orders, bills, vendor credits, vendor payments, journals); the others are read whole and skipped
by hash. Deletions come only from a completed, unfiltered full pass; a void document retires its copy.
Limits (www.zoho.com/books/api/v3/introduction/#api-call-limit): 100 requests a minute per organisation
(paced), a daily allowance by plan and 5 or 10 at once; a 429 pauses the run. An expense names its
paid-through account by name only; its id is looked up in the chart of accounts.

## Mirrored

| Zoho Books | ERPNext home | Notes |
|---|---|---|
| Chart of accounts | **Account** | Code, name, root and account type from Zoho's account type, currency. Top-level (see decisions). |
| Locations | **Warehouse** | Address. |
| Items | **Item** (unpublished **Website Item**), **Item Price** | SKU, rate, description; an inactive item is disabled. |
| Contacts | **Customer** or **Supplier**, **Contact**, **Address** | Primary contact person, billing and shipping addresses, GST number as tax id, currency. |
| Projects | **Project** | For its customer. |
| Estimates | Draft **Quotation** | A declined or expired estimate has none. |
| Sales orders | **Sales Order** | Shipment date as delivery date. |
| Invoices, sales receipts | **Sales Invoice** | Lines (rate times quantity), shipping charge and adjustment as lines, the entity-level discount as a line, taxes, the total exactly; a draft stays a draft. |
| Credit notes | **Sales Invoice** returns | |
| Purchase orders, bills | **Purchase Order**, **Purchase Invoice** | Account per line. |
| Vendor credits | **Purchase Invoice** returns (debit notes) | |
| Customer payments | **Payment Entry** (Receive) | Allocated to the invoices it was applied to. |
| Vendor payments | **Payment Entry** (Pay) | From its paid-through account, allocated to its bills. |
| A sales receipt's payment | **Payment Entry** against the receipt | |
| Manual journals | **Journal Entry** | Base-currency amounts; customer per line. |
| Expenses | **Journal Entry** | The expense account debited, the paid-through account credited. |

## Planned and gaps

| Zoho Books | Status |
|---|---|
| Tax-inclusive documents, line discounts | Planned (card 386): the net per line and its tax aren't in the line. |
| Expenses with tax | Planned (card 386): the tax posts to an account Zoho names per tax. |
| Credit-note lines | Zoho's published module documents them without a rate: mirrored when the API sends one, otherwise reported (unproven against the spec). |
| Sub-accounts | Mirrored top-level: Zoho posts to a parent, ERPNext only to a leaf (operator decision). |
| Reporting tags | Planned (card 386): **Accounting Dimension**. |
| Bank transactions | Not mirrored: their categorised entries are the invoices, payments and expenses already mirrored (operator decision for uncategorised ones). |
| Time entries, tasks, employees (expense), retainer invoices, recurring transactions, price lists, taxes, currencies, fixed assets, opening balances, attachments, comments | Planned (card 386): **Timesheet**, **Task**, **Employee**, advance **Payment Entry**, **Auto Repeat**, **Price List**, **Item Tax Template**, **Currency Exchange**, **Asset**, opening **Journal Entry**, **File**, **Comment**. |
| Zoho CRM leads and deals | A separate Zoho product: its own adapter (operator decision). |
