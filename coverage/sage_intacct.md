<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Sage Intacct (XML Web Services gateway) → ERPNext

The single source of truth for what the Sage Intacct adapter mirrors, and for the other Sage products.
**Not yet run against a live company:** there are no live credentials; every fixture
(`tests/fixtures/sage_intacct`, written by `python -m tools.sage_intacct_fixtures`) is the gateway's XML
answer as its version 2.1 response DTD declares it, and is validated against that DTD by `tools/prove.py`
(`tools/dtd.py`). The DTDs are the ones Sage publishes at https://github.com/intacct/intacct_dtd (Apache-2.0,
pinned to commit `5e7ff4c6` in `tools/specs.py`); the requests follow the same repository's request and
function DTDs and Sage's official SDKs (https://github.com/intacct/intacct-sdk-php: the gateway endpoint
`https://api.intacct.com/ia/xml/xmlgw.phtml`). Sage now recommends its REST API for new integrations; its
reference is published only on developer.sage.com, which refuses automated access, so it could not be
verified here (see the other Sage products below).

Access: a Web Services sender (sender ID and password, from a Web Services developer license) that the
company authorizes (Company → Security → Web Services authorizations), and a company user with read
permissions to the objects below. On the connection: the company ID as the account, the user ID and
password as the user and secret, the sender ID as **Key or Certificate ID** and its password as **Sender
Password**. Every request carries the sender and logs in as the user; a refused sender or login stops the
run and holds scheduled runs.

Reading: `get_list` per object, 100 records at a time from `start` in key order, until the list's total.
Incremental runs filter `whenmodified >=` the day before the last clean run (the company's time zone: a
day's overlap; an unchanged record is skipped by its hash); warehouses and GL transactions, which have no
`whenmodified`, are read whole. Deletions come only from a completed, unfiltered full pass. Intacct
governs the gateway by the company's performance tier; the importer sends one request at a time, at most
two a second, backs off on 429 and 5xx, and pauses the run when the gateway keeps pushing back.

The Field Map (JSON) names the company's own transaction definitions and journals and any equity
accounts: `{"quotes": ["Sales Quote"], "sales_orders": ["Sales Order"], "purchase_orders": ["Purchase
Order"], "journals": ["GJ"], "root_types": {"3000": "Equity"}}` (the values shown are the defaults, but
for `root_types`).

## Mirrored

| Sage Intacct | ERPNext home | Notes |
|---|---|---|
| GL accounts | **Account** | Number and title; root type from the account type (balance sheet, income statement) and normal balance, or the Field Map (equity); inactive ones disabled. |
| Departments | **Cost Center** and HR **Department** | Top-level cost centers that are never groups (Intacct posts to a parent department); the HR tree keeps the parent. Inactive ones disabled. |
| Warehouses | **Warehouse** | Name, ship-to address; inactive ones disabled. |
| Items | **Item** | Name, ID, extended description, UPC, stocked for inventory items; an inactive item disabled. |
| Customers, vendors | **Customer** / **Supplier**, **Contact**, **Address** | Name, the contact's names, email and phone, bill-to (pay-to) and ship-to (return-to) addresses, tax ID, currency, the contact list (name and category); inactive ones disabled. |
| Employees' titles | **Designation** | Intacct has no position record: the employees' distinct titles. |
| Employees | **Employee** | Names, gender, birth, start and end dates, department, title, supervisor, email, phone; Left when inactive with an end date. |
| Projects | **Project** for its customer | Name, begin and end dates, status (completed, cancelled, on hold, else open), description. |
| Order entry quotes and sales orders (the Field Map's definitions) | Draft **Quotation**, **Sales Order** | Items, quantities and amounts, in the transaction's currency at its rate; pending ones are drafts, reversed ones gone. |
| Purchase orders (the Field Map's definitions) | **Purchase Order** | |
| AR invoices, AP bills | **Sales Invoice**, **Purchase Invoice** (a negative total as a return) | Each line item's amount on its GL account and department; in another currency, the transaction amounts at the rate their base total makes exactly. Reversed ones are gone. |
| AR payments, AP payments | **Payment Entry** | Allocated to the invoices and bills they pay; an AR payment into its undeposited funds account, in its customer's currency (the 2.1 AR payment names none). |
| GL transactions of the Field Map's journals (GJ by default) | **Journal Entry** | Each entry's account, debit or credit, department and customer, vendor or employee. The subledger journals and order entry invoices are the AR and AP documents' own postings, so they are never booked twice. |

## Planned and gaps

| Sage Intacct | Status |
|---|---|
| Locations, classes and the other dimensions | Planned (card 386): **Accounting Dimensions**. |
| An entry's or line's project | Planned (card 386): the 2.1 project list keys projects by record, the GL entries by project ID. |
| Item prices (price lists) | Planned (card 386): **Item Price** per **Price List**. |
| Taxes (tax details, schedules), AR and AP adjustments, AR and AP terms | Planned (card 386): taxes are inside the document's line items as posted; **Payment Terms Template**. |
| Stock (item totals per warehouse, inventory transactions) | Planned (card 386); see the stock decision on card 386. |
| Expense reports, timesheets, employee rates, contracts, revenue recognition | Planned (card 386): **Expense Claim**, **Timesheet**. |
| AP payments in another currency than the company's | Planned (card 386). |
| AR payments of a customer with no currency of its own | Planned (card 386): the payment can't name its currency. |
| Attachments (supporting documents), custom fields | Planned (card 386). |
| Sage Intacct's REST API (the 2025 successor of the XML gateway) | Planned (card 386): its OpenAPI reference is only on developer.sage.com. |

## The other Sage products

Each has an official network API, documented only on developer.sage.com, which answers 403 to automated
clients; the Internet Archive's copies of their machine-readable specs are truncated (Sage 200's swagger
stops at 1 MB), stubs (Sage 300's `openapi.json` holds only its title) or HTML (Sage Accounting's
`swagger.*.json`). None can be verified, so none is built: each is planned on card 386, waiting for the
operator to supply the published specs (or a way to fetch them).

| Product | API (per developer.sage.com's archived pages) | Status |
|---|---|---|
| Sage Accounting (Business Cloud Accounting) | REST v3.1, OAuth 2 | Planned (card 386): spec unverifiable. |
| Sage 200 (Standard, Professional) | REST (`/200`, `/200c` swagger) | Planned (card 386): spec unverifiable. |
| Sage 300 | Web API (`/300` openapi.json) | Planned (card 386): spec unverifiable. |
| Sage X3 | GraphQL and SOAP web services | Planned (card 386): schema unverifiable. |
| Sage HR | REST (`/hr` swagger) | Planned (card 386): spec unverifiable. |
| Sage People | Salesforce-based (Postman collection) | Planned (card 386): spec unverifiable. |
| Sage 50, Sage 100, Sage 500 | No network API found on developer.sage.com's archived index | Operator decision (card 386): desktop SDKs only, as far as could be checked. |
