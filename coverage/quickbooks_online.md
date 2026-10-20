<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: QuickBooks Online (Accounting API) → ERPNext

The single source of truth for what the QuickBooks Online adapter mirrors. **Not yet run against a live
company:** there are no live credentials; every fixture (`tests/fixtures/quickbooks_online`) is shaped as
Intuit's published data model defines it and proven against it: the XML Schemas in Intuit's official Java
SDK (`intuit/QuickBooks-V3-Java-SDK`, `ipp-v3-java-data/src/main/xsd`, pinned to commit `c4d5dfad` in
`tools/specs.py`; the JSON API returns the same types), read as JSON by `tools/xsd.py`, strictly (no field
the schema doesn't declare).

Access: an app on developer.intuit.com with this site's `/api/method/idempotent_importer.api.oauth_callback`
as a redirect URI; its client ID and secret on the connection, the Store URL `https://quickbooks.api.intuit.com`
(or `https://sandbox-quickbooks.api.intuit.com`), then **Connect to QuickBooks Online**, which keeps the
approved company's realm id as the account. OAuth 2 (developer.intuit.com/app/developer/qbo/docs/develop/
authentication-and-authorization/oauth-2.0): the `com.intuit.quickbooks.accounting` scope (Intuit has no
read-only one; the importer only reads), HTTP Basic client authentication, an hour's access token and a
refresh token Intuit may replace on any refresh (the newest is always kept).

Reading: the query endpoint (`/v3/company/<realm>/query`, minor version 75), `SELECT * FROM <Entity>` in pages
of 1000 (MAXRESULTS' maximum) in creation order; name lists with `Active IN (true, false)` (a query returns only
active ones otherwise). Incremental runs ask for `MetaData.LastUpdatedTime >=` the last clean run; deletions
come only from a completed, unfiltered full pass (QuickBooks deletes transactions outright and makes names
inactive). Limits (developer.intuit.com/app/developer/qbo/docs/learn/limits-and-throttles): 500 requests a
minute and 10 a second per company and app; requests are paced under both, and a 429 (after which Intuit asks
for 60 seconds) pauses the run. A 401 is retried once with a new token; rejected credentials stop the run and
hold scheduled runs.

## Mirrored

| QuickBooks Online | ERPNext home | Notes |
|---|---|---|
| Accounts | **Account** in the company's chart | Number, name, root type from the classification, account type from the type and sub-type (bank, receivable, payable, tax, stock, cash, income, expense, cost of goods sold, fixed asset, equity), currency. Top-level (see decisions). |
| Classes, departments (locations) | **Cost Center** | A line books to its class, else its transaction's location. |
| Item categories | **Item Group** `<name> - <abbr>` | |
| Items: inventory, non-inventory, service | **Item** (unpublished **Website Item**), **Item Price**, stock as a **Stock Reconciliation** | SKU, description, sales price, quantity on hand for inventory items, category. An inactive item is disabled. |
| Customers (sub-customers too) | **Customer**, **Contact**, **Address** | Display name, company or person, email, phone, billing and shipping addresses, business number as tax id, currency. |
| Projects (customers with `IsProject`) | **Project** for its customer | Open while active. |
| Vendors | **Supplier**, **Contact**, **Address** | As customers; business number or tax identifier as tax id. |
| Employees | **Employee** | Names, gender, birth, hire and release dates, number, email, phone; Left when inactive. |
| Estimates | Draft **Quotation** | Item lines, discount, taxes; dated the estimate's day. |
| Purchase orders | **Purchase Order** | Item and expense lines, taxes. |
| Invoices, sales receipts | **Sales Invoice** | Item lines (a bundle's lines as its items), expense lines to their accounts, discounts as lines to the discount account, taxes per tax rate, class/location and project per line; the total exactly. |
| Credit memos, refund receipts | **Sales Invoice** returns (credit notes) | Negative quantities. |
| Bills, vendor credits | **Purchase Invoice** (vendor credits as debit notes) | Item lines on their items, expense lines on their accounts, the vendor's number as the supplier's reference. |
| Payments | **Payment Entry** (Receive) | Into its deposit account, allocated to the invoices it pays. |
| Bill payments | **Payment Entry** (Pay) | From the cheque's bank or the card's account, allocated to the bills it pays. |
| A sales receipt's payment, a refund receipt's refund | **Payment Entry** against its own invoice or credit note | |
| Journal entries | **Journal Entry** | Debits and credits, the customer, vendor or employee of a line, its class or location. |
| Deposits | **Journal Entry** | The bank debited the total (cash back's account its amount); each line's account credited, or Undeposited Funds for payments it deposits. |
| Transfers | **Journal Entry** | To account debited, from account credited. |
| Expenses, cheques, card charges and credits (Purchase) | **Journal Entry** | The paying account credited (debited for a card credit), each expense line's account debited. |

## Planned and gaps

| QuickBooks Online | Status |
|---|---|
| Bundles (Group items) | Planned (card 386): **Product Bundle**. |
| A payment that applies a credit memo, a bill payment that applies a vendor credit | Planned (card 386): the payment entry allocates positive amounts only; the credit's application against an invoice is a reconciliation the writer doesn't make yet. |
| Expenses with tax, expenses of items | Planned (card 386): the tax posts to the tax agency's account and an item's expense to its own account, which the expense doesn't name. |
| A class and a location on the same line | The class is the cost center; the location is planned as an **Accounting Dimension** (card 386, a warning). |
| Sub-accounts, sub-classes, sub-departments | Mirrored top-level: QuickBooks posts to a parent, ERPNext only to a leaf (operator decision). |
| The customer, vendor or employee of a journal, deposit or expense line on an account that isn't receivable or payable | Not kept on the line: ERPNext books a party only on receivable and payable lines. |
| Payments and journals in another currency than the company's | Planned (card 386). |
| Time activities | Planned (card 386): **Timesheet**. |
| Attachments, terms, payment methods, tax codes and rates, budgets, exchange rates, inventory adjustments, recurring transactions, custom fields | Planned (card 386): **File**, **Payment Terms Template**, **Mode of Payment**, **Item Tax Template** / **Sales Taxes and Charges Template**, **Budget**, **Currency Exchange**, **Stock Reconciliation**, **Auto Repeat**, custom fields. |
| Payroll (QuickBooks Payroll) | Not in the Accounting API. |
