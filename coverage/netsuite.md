<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: NetSuite (SuiteTalk REST web services) → ERPNext

The single source of truth for what the NetSuite adapter mirrors. **Not yet run against a live account:**
there are no live credentials; every fixture (`tests/fixtures/netsuite`, written by
`python -m tools.netsuite_fixtures`) is shaped as Oracle publishes the records and proven against it: the
REST API Browser for 2026.1 (the latest published; NetSuite publishes the REST Record API's OpenAPI
description only as this Spectacle page, read back into Swagger 2 by `tools/spectacle.py`, its 28 parts
pinned by SHA-256 in `tools/specs.py`), and SuiteQL's response, transcribed from
https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_157909186990.html into
`tests/schemas/netsuite-suiteql.json`.

Access: OAuth 2.0's client credentials (machine-to-machine) flow
(https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162686838198.html). The account's
administrator creates an integration record with the REST web services scope and the client credentials
grant, and maps a certificate (RSA 3072+ or EC P-256/384/521) to the integration, an entity and a role that
can read these records. On the connection: the account ID (Setup → Company → Company Information, e.g.
`1234567` or `1234567_SB1`) as the account, the integration's client ID as the user, the certificate ID as
**Key or Certificate ID**, and the certificate's private key (PEM) as the secret. Each access token (an hour)
is fetched with a new JWT assertion signed by that key (PS256 for RSA, ES256/384/512 for EC), issued by the
client ID, for the account's token endpoint, with the `rest_webservices` scope
(https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_162755359851.html). A refused
assertion stops the run and holds scheduled runs.

Reading: SuiteQL (`POST /services/rest/query/v1/suiteql`, `Prefer: transient`) lists which records exist,
keyset by internal id in pages of 100 (so no listing reaches SuiteQL's 100,000-result cap); each record is
then read whole from the REST Record API (`GET /services/rest/record/v1/<type>/<id>?expandSubResources=true`,
its sublists included). Incremental runs list records whose `lastmodifieddate` is on or after the day before
the last clean run (SuiteQL dates are in the user's time zone; an unchanged record is skipped by its hash);
deletions come only from a completed, unfiltered full pass. Limits
(https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_1559222360.html): NetSuite governs
web services per account by concurrent requests and by requests per 60 seconds and per 24 hours, answering
429; the reader sends one request at a time, at most four a second, and a 429 pauses the run for the
60-second window.

## Mirrored

| NetSuite | ERPNext home | Notes |
|---|---|---|
| Accounts | **Account** in the company's chart | Number, name, root type and account type from the account type (bank, receivable, payable, fixed asset, cost of goods sold, equity, income, expense; a sales tax account as Tax), currency. Under its parent when that is a summary account (a group); an inactive one disabled. |
| Departments | **Cost Center** and HR **Department** | Name; an inactive one disabled. As an HR Department under its parent, a group when it has children; as a Cost Center top-level and never a group, since NetSuite books to a department with sub-departments and ERPNext only to a cost center without children. |
| Locations | **Warehouse** | Name, parent, main address; an inactive one disabled. |
| Items: inventory and assembly (lot-numbered and serialized too), non-inventory, service, other charge (for sale, purchase or resale), kit, gift certificate, download | **Item** (unpublished **Website Item** unless the item is on the web store), **Item Price**, stock as a **Stock Reconciliation** | Name, item ID as SKU, sales description, UPC, manufacturer, weight, the base price for one unit, the quantity on hand across locations for inventory and assembly items. An inactive item is disabled. |
| Customers (the customer stage) | **Customer**, **Contact**, **Address** | Company or person, email, phone, address book (default billing, default shipping, others), VAT or tax registration, currency, each contact of its contact list (name, email, phone, job title); an inactive one disabled. |
| Leads and prospects (customers at those stages) | **Lead** | Organisation or person, email, phones, job title, web address, comments; a prospect's status is Opportunity; an inactive one disabled. When it becomes a customer it is mirrored as one. |
| Vendors | **Supplier**, **Contact**, **Address** | As customers. |
| Job titles | **Designation** | Employees' distinct titles (NetSuite has no job-title record). |
| Employees | **Employee** | Names, gender, birth, hire and release dates, entity ID as employee number, department, title, supervisor, email, phone; Left when inactive with a release date, Inactive otherwise. |
| Projects (jobs) | **Project** for its customer | Status (Closed → Completed, Not Awarded → Cancelled, else Open), start and end dates, comments. |
| Opportunities | **Opportunity** with its lead or customer | Title, status, date, projected total, probability, expected close. |
| Estimates | Draft **Quotation** | Item lines, discounts, taxes, shipping and handling; dated the estimate's day. A voided one is gone. |
| Sales orders | **Sales Order** | A draft while pending approval; a cancelled one is gone. |
| Purchase orders | **Purchase Order** | A draft while planned or pending supervisor approval; a rejected one is gone. |
| Invoices, cash sales | **Sales Invoice** | Item lines with their department (cost center), project and account, discount lines and the document discount off the net total, taxes per tax code (SuiteTax) or the legacy tax totals, shipping as freight and handling as a fee, the receivable account; the total exactly. A draft while pending approval; a voided one is cancelled. |
| Credit memos, cash refunds | **Sales Invoice** returns (credit notes) | Against the one invoice a credit memo is applied to; a cash refund keeps its own balance (its refund is its own payment). |
| Vendor bills, vendor credits | **Purchase Invoice** (vendor credits as debit notes) | Item lines on their items, expense lines on their accounts, the bill's number as the supplier's reference, the payable account. |
| Customer payments, vendor payments | **Payment Entry** (Receive, Pay) | Into or out of its account, on its receivable or payable account, allocated to the invoices or bills it applies to. |
| Customer deposits | **Payment Entry** (Receive) | An advance (allocated to nothing). |
| Customer refunds | **Payment Entry** (Pay) | Allocated to the credit memos it refunds. |
| A cash sale's and a cash refund's money | **Payment Entry** against its own invoice or credit note | |
| Journal entries | **Journal Entry** | Debits and credits, each line's customer, vendor or employee, its department. A voided one is cancelled. |
| Checks | **Journal Entry** | The bank account credited, each expense line's account debited. |

## Planned and gaps

| NetSuite | Status |
|---|---|
| OneWorld subsidiaries | One connection reads every subsidiary the role can see into one company (decision 1 below). Planned (card 386): a subsidiary filter per connection, for one connection per subsidiary. |
| Classes, and locations as an accounting segment | Planned (card 386): **Accounting Dimensions**. A line books to its department as cost center today. |
| An account whose parent takes postings | Filed under the root of its type, with a warning: ERPNext posts only to leaf accounts (decision 3). |
| Statistical and non-posting accounts | No ERPNext home: they hold no money. Reported as Unsupported (decision 6). |
| Stock per location; inventory adjustments, transfers, fulfilments and receipts | Planned (card 386): the item's total on hand goes to the connection's warehouse; per-location levels need the product contract to carry quantities per warehouse. Stock movements are not mirrored as Stock Entries while absolute levels are authoritative, so nothing is counted twice (decision 4). |
| Kit and item group components | Planned (card 386): **Product Bundle**. The kit is mirrored as a non-stock item. |
| Price levels other than base, quantity pricing, prices in other currencies | Planned (card 386): **Item Price** per **Price List**. The base price for one unit is mirrored (the first currency's, with a warning when there are several). |
| Item accounts (income, expense, asset) | Planned (card 386): **Item Default**. |
| Payment terms | Planned (card 386): **Payment Terms Template**. |
| Time bills | Planned (card 386): **Timesheet**. |
| Expense reports | Planned (card 386): **Expense Claim**. |
| Bank transfers, deposits (bank deposit records), credit card charges and refunds | Planned (card 386): **Journal Entry**. The REST Record API has no transfer record; deposits and card charges have their own records still to read. |
| A payment that applies credits | Planned (card 386): a Payment Entry allocates positive amounts only. Not mirrored (Unsupported) rather than allocated wrongly. |
| A credit memo applied to several invoices | Mirrored as a credit note against none (its balance stays the customer's), so nothing is settled twice (card 386). |
| Payments and journals in another currency than the company's | Planned (card 386): the writers book the company's currency today (the USD deposit and cash sale in the fixtures wait for it). |
| Fulfilments, receipts, return authorizations, work orders, transfer orders, inventory counts | Planned (card 386): **Delivery Note**, **Purchase Receipt**, **Stock Entry**, **Work Order**; see stock above. |
| Support cases, tasks, events, phone calls, campaigns | Planned (card 386): **Issue**, **ToDo**, **Event**, **Communication**, **Campaign**. |
| Custom records and custom fields | Planned (card 386): per-connection field maps, as for Finance & Operations. |
| File cabinet attachments | Planned (card 386): **File**. |
| Payroll | Not in SuiteTalk REST. |
| An invoice changed at the source after a return was booked against it | Planned (card 386): the writer amends a changed booked invoice by cancelling it, which ERPNext refuses while a return names it; that invoice is reported Failed (and retried) until the writer can update it in place. |

## Decisions for the operator (card 386)

1. **OneWorld subsidiaries:** today every subsidiary goes into one company (no data lost). Choose a
   subsidiary filter per connection (one connection per subsidiary) or keep one company.
2. **Classes:** ERPNext's native home is an Accounting Dimension, which the foundation has no writer for yet.
3. **Accounts under posting parents:** filed under the root of their type (warning) rather than under a
   parent that ERPNext would refuse to post to.
4. **Stock authority:** absolute levels (quantity on hand) stay authoritative; mirroring stock movements as
   well would count them twice.
5. **Records with no home yet:** the planned rows above.
6. **Statistical and non-posting accounts:** not mirrored (no money).
