<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Odoo → ERPNext

The single source of truth for what the Odoo adapter mirrors. It reads one company of an Odoo 17, 18 or 19
database (Community or Enterprise; Odoo Online, Odoo.sh or self-hosted) through Odoo's external API:

- the **JSON-2 API** (Odoo 19 and later): `POST /json/2/<model>/<method>` with an API key as a bearer token
  and the database in `X-Odoo-Database`
  ([reference](https://www.odoo.com/documentation/19.0/developer/reference/external_api.html));
- the **JSON-RPC API** (Odoo 17 and 18, and 19 until Odoo 22 removes it): `POST /jsonrpc`, the object
  service's `execute_kw` after `authenticate` with the user's login and API key
  ([reference](https://www.odoo.com/documentation/18.0/developer/reference/external_api.html)). XML-RPC is
  the same object service in another encoding, so it isn't read separately.

**Not yet run against a live Odoo database.** The fixtures (`tests/fixtures/odoo`) are written by
`tools/odoo_fixtures.py`, which runs the real reader against a small Odoo 19 database held in memory and
records each request with the answer Odoo's ORM gives it; every answer is proven against
`tests/schemas/odoo.json`, transcribed by `tools/odoo_schema.py` from Odoo 19's own field declarations
(odoo/odoo at commit `0faf35b5ff680a172a73fc22230957c2771d32c9`). Odoo publishes no OpenAPI document; each
database describes its models on its `/doc` page.

Access: an API key (Preferences → Account Security → New API Key) of a user with read access to the apps
mirrored (Accounting, Inventory, Sales, Purchase, CRM, Project, Employees: an app the user may not read is
skipped with a warning, an app the database hasn't installed too). The connection holds the database's URL,
the database name as the account (`<database>:<company id>` for one company of several), the key as the
secret, and, for JSON-RPC, the user's login. Only fields the database has are asked for (`fields_get`), so
one reader serves every supported version and the apps installed. Lists are `search_read` ordered by id and
paged by offset (200 a page), archived records included. Every kind is incremental by `write_date`; a
partner's child contacts and addresses don't move their parent's `write_date`, so a full run catches those.
Odoo documents no request-rate limit for either API: requests aren't paced, a 429 or 5xx (a proxy, Odoo
Online's front end) is retried with backoff after its Retry-After, and a long wait pauses the run; rejected
credentials (an invalid key, `AccessDenied`) stop the run and hold scheduled runs.

## Mirrored

| Odoo | ERPNext home | Notes |
|---|---|---|
| Chart of accounts (`account.account`) | **Account** `<name> - <abbr>`, its code as the account number | Flat under the company's root of its root type. Account types: receivable → Receivable, payable → Payable, bank and cash → Bank, fixed assets → Fixed Asset, income → Income Account, expenses → Expense Account, cost of revenue → Cost of Goods Sold, depreciation → Depreciation; a tax's account → Tax; an outstanding receipts or payments account → Bank (ERPNext pays only into and out of Bank or Cash accounts). Its currency. A deprecated account stays enabled (its history still books). |
| Analytic accounts | **Cost Center** `<code> <name> - <abbr>` | A line or project all on one analytic account books to its cost center. |
| Warehouses | **Warehouse** `<name> - <abbr>` with its partner's address | An archived warehouse is disabled. |
| Product categories | **Item Group** `<name> - <abbr>` | Parent kept. |
| Products (templates) and variants | **Item** (+ **Website Item**); a template with a variant **Item** per variant | Internal reference, barcode, sales description, category, weight (kg), price (the variant's with its extra), attribute values. Storable products are stock items, their quantity on hand mirrored by **Stock Reconciliation**; services and consumables aren't. Published only when the website publishes it. |
| Customers and suppliers (commercial partners) | **Customer** / **Supplier** with **Contact** and **Address** | A company or a person; its tax id; its own address and its children's invoice, delivery and other addresses; each contact child a **Contact** of its own. Customers are the partners that are customers or not suppliers; suppliers those that are suppliers (a partner both is mirrored as both). The company's own partner and its users' partners are neither. Archived: disabled. |
| Departments | **Department** `<name> - <abbr>` | Parent kept; archived: disabled. |
| Job positions | **Designation** `<name> - <abbr>` | |
| Employees | **Employee** | Full name (ERPNext's first name holds it whole), gender (19's `sex`, 17 and 18's `gender`), birthday, contract start (19) as joining date, department, job, manager, work and private email, mobile or work phone. Archived with a departure date: Left (relieving date), else Inactive. What Odoo doesn't keep is left empty (see the README's decision on employees). |
| Projects | **Project** | Customer, analytic account (cost center), start and end dates, description; done → Completed, on hold → On hold, archived → Cancelled. |
| CRM leads and opportunities | **Lead**; **Opportunity** | Every lead and opportunity is a Lead (contact name, company, email, phone, job, website, description). An opportunity is with its customer when its partner is one, else with its own Lead; its expected revenue, probability and deadline. Won (probability 100) → Converted; lost (archived) → Lost; a lost lead is disabled. |
| Quotations (draft, sent) | Draft **Quotation** | Never booked; confirmed, it becomes the Sales Order. |
| Sale orders (confirmed) | Submitted **Sales Order** | Lines at their net amounts, the order's tax as one amount, commitment date as delivery date, the customer's reference. Cancelled: cancelled. |
| Purchase orders | **Purchase Order** (RFQs as drafts, confirmed submitted) | Lines, tax, planned date, vendor reference. Cancelled: cancelled. |
| Customer invoices, credit notes, sales receipts | **Sales Invoice** (credit notes as returns against the invoice they reverse) | Product lines at their exact net prices on their income accounts and cost centers; tax, cash rounding and early-payment discount lines as amounts on their accounts; the invoice's receivable account; total checked exactly. Posted: submitted; draft: a draft; cancelled: cancelled. |
| Vendor bills, refunds, purchase receipts | **Purchase Invoice** (refunds as returns) | As invoices, on expense and payable accounts; the vendor's reference as the bill number. |
| Payments to and from partners | Submitted **Payment Entry** | Into or out of its outstanding account, on the party's receivable or payable account, allocated to the invoices and bills it was reconciled with (`account.partial.reconcile`). |
| Journal entries (miscellaneous, bank statement lines, opening balances, partner-less transfers) | **Journal Entry** | Each line on its account with its partner and cost center; a receivable or payable line reconciled with invoices is split to settle each. A partner's payment's own entry is that Payment Entry, not a journal. |
| Documents in another currency | At the rate Odoo booked (its total in the company's currency over its total), that total checked exactly | Payments and journal entries in another currency are planned (card 386). |

## Gaps

| Odoo | Why it isn't mirrored |
|---|---|
| Off-balance accounts | No ERPNext home (an operator decision). |
| Anglo-saxon cost of goods sold lines on invoices | Stock is mirrored at zero value (README decision 2), so the cost postings have nothing to match. |
| A line split across several analytic accounts | Planned (card 386): ERPNext books one cost center a line; the line is mirrored without one (a warning). |
| Payments and journal entries in another currency | Planned (card 386). |
| Payments reconciled with journal entries | Planned (card 386): allocated as an advance today (a warning). |
| Taxes per order line (sale and purchase orders) | Planned (card 386): an order carries its tax total as one amount on the connection's tax account. |
| Taxes, fiscal positions, payment terms | Planned (card 386): Sales/Purchase Taxes and Charges Templates, Tax Rules, Payment Terms Templates. |
| Pricelists | Planned (card 386): Price Lists and Pricing Rules through the generalised Pricing Rule writer. |
| Stock by location, stock moves and pickings | Planned (card 386): one Warehouse per location; moves can't be Stock Entries while stock is mirrored absolutely. |
| Product images | Planned (card 386): Odoo serves them as base64 fields, not URLs. |
| Partner tags, CRM stages, lost reasons, chatter messages and activities | Planned (card 386): Tags, Sales Stages, Lost Reasons, Comments. |
| Project tasks and timesheets | Planned (card 386): Tasks and Timesheets. |
| Attendance, time off and allocations, work schedules, payslips, expenses | Planned (card 386): Attendance and Employee Checkins, Leave Applications and Allocations, Shift Types, Salary Slips, Expense Claims. |
| Assets and deferred revenue | Planned (card 386): Assets. |
| Website pages, blogs and the eCommerce module | Planned (card 386): Web Pages (Odoo stores them as QWeb views), Blog Posts. |
| Employee numbers and national ids | Odoo's standard employee has none (the badge id identifies attendance); identification numbers are personal data, not mirrored. |
