<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: SAP Business One (Service Layer) → ERPNext

The single source of truth for what the SAP Business One adapter mirrors. **Not yet run against a live
company:** there are no live credentials; every fixture (`tests/fixtures/sap_business_one`, written by
`python -m tools.sap_business_one_fixtures`) is shaped as the Service Layer's OData v3 `$metadata`
(namespace `SAPB1`) defines its entities, and proven against it. SAP publishes that model only as each
installation's `$metadata` and as the HTML Service Layer API Reference
(https://help.sap.com/doc/056f69366b5345a386bb8149f1700c19/10.0/en-US/Service%20Layer%20API%20Reference.html,
sha256 `cb7282dd…fa5cd896` when read on 2026-10-20); the machine-readable documents the fixtures are proven
against are the OpenAPI documents WSO2 generated from that `$metadata` for its Ballerina connector
(`ballerina-platform/module-ballerinax-sap.businessone`, `docs/spec/*.json`, pinned to commit `f06691e0` in
`tools/specs.py`), one per module read: administration, banking, business partners, CRM, financials, human
resources, inventory, projects, purchasing and sales. They are strict: no property the model doesn't declare.

Access: the Service Layer's session (SAP Business One has no OAuth). Enter the Service Layer's address
(`https://<server>:50000`) as the Store URL, the company database as the account, and a SAP Business One
user's name and password (the user needs read access to the objects below). `POST /b1s/v1/Login` answers a
session id, sent back as the `B1SESSION` cookie (with the `ROUTEID` cookie of a load-balanced Service Layer);
a session lapses after 30 minutes idle and the Service Layer answers 401, so the importer logs in again
once. A refused login stops the run and holds scheduled runs.

The chart's root types: SAP Business One types an account only as revenue, expense or other; whether an
"other" account is an asset, a liability or equity is its drawer's (the top-level title it sits under),
which the Service Layer doesn't expose. The connection's **Field Map** names each such drawer's root type:
`{"root_types": {"1": "Asset", "2": "Liability", "3": "Equity"}}` (keys are the drawers' account codes). An
account whose drawer it doesn't name is not mirrored, and the run says why; nothing is guessed. The Field
Map may also name the price list items are priced from (`"price_list": 2`; the first, `1`, otherwise).

Reading: each entity set in key order (`$orderby`), up to 100 entities a page (`Prefer:
odata.maxpagesize=100`), through `odata.nextLink`. Incremental runs ask the sets that keep `UpdateDate`
(items, business partners, employees, opportunities, every marketing document) for those updated on or after
the day before the last clean run (the server's local date: a day's overlap; an unchanged entity is skipped
by its hash); the others (the chart, profit centers, warehouses, item groups, departments, positions,
projects, payments, journal entries) are read whole every run. Deletions come only from a completed,
unfiltered full pass. The Service Layer documents no request limit (it is the company's own server); the
importer sends one request at a time, at most five a second, and backs off on 429 and 5xx.

## Mirrored

| SAP Business One | ERPNext home | Notes |
|---|---|---|
| Chart of accounts | **Account** | Code (format code) and name, root type (above), revenue and expense accounts as income and expense accounts, cash accounts as Cash, account currency. Title accounts are groups, each account under its father; a frozen or invalid account disabled. |
| Profit centers of dimension 1 | **Cost Center** | An inactive one disabled. A line's distribution rule (dimension 1) is its cost center. |
| Warehouses | **Warehouse** | Name, address; an inactive one disabled. |
| Item groups | **Item Group** | |
| Items | **Item**, **Item Price**, stock as a **Stock Reconciliation** | Name, code, remarks, bar code, the price list's price, the quantity in stock across warehouses for an inventory item, its group. An invalid or frozen item is disabled. |
| Business partners: customers, suppliers | **Customer** / **Supplier**, **Contact**, **Address** | Name, company or private, email, phone, bill-to and ship-to addresses, federal tax id, currency, active contact persons (name, email, phone, position); an invalid or frozen one disabled. |
| Business partners: leads | **Lead** | Organisation or person, email, phones, web site, remarks; frozen ones disabled. A lead converted to a customer is mirrored as one. |
| Departments, positions | **Department**, **Designation** | |
| Employees | **Employee** | Names, gender, birth, start and termination dates, external number, department, position, manager, email, phone; Left when inactive with a termination date. |
| Projects | **Project** | Open while active; valid from and to. |
| Sales opportunities | **Opportunity** with its lead or customer | Name, status (open, won → Converted, lost), start and predicted closing dates, maximum local total, closing percentage, remarks. |
| Sales quotations | Draft **Quotation** | |
| Sales orders, purchase orders | **Sales Order**, **Purchase Order** | |
| A/R invoices, A/R credit notes | **Sales Invoice** (credit notes as returns, against the invoice their lines are based on) | Item and service lines with their account, distribution rule and project, the document discount, tax per tax code, freight as shipping, rounding as a fee; in another currency, the document's own amounts with the base total checked exactly. A cancelled document, and the cancellation that reverses it, are not booked. |
| A/P invoices, A/P credit notes | **Purchase Invoice** (credit notes as debit notes) | As above; the vendor's reference number. |
| Incoming payments, outgoing payments | **Payment Entry** | From or to a customer or supplier, by cash, transfer, cheques or card, into or out of that account, allocated to the invoices it settles. |
| Journal entries (manual) | **Journal Entry** | Each line's account, debit and credit, business partner (customer or supplier), distribution rule and project. The postings of documents, which the Service Layer lists as journal entries too, are not booked again. |

## Planned and gaps

| SAP Business One | Status |
|---|---|
| Profit centers of dimensions 2 to 5 | Planned (card 386): **Accounting Dimensions**. |
| Control accounts (receivable and payable) as the documents' and payments' party accounts | The party's default account in ERPNext is used: SAP Business One doesn't mark which accounts are control accounts through the Service Layer (card 386). |
| Payments on account (to no business partner), payments by several means at once, payments applying credit notes, payments in another currency | Planned (card 386): a Payment Entry books one account, positive allocations and the company's currency. Not mirrored (Unsupported) rather than booked wrongly. |
| Documents drawing a down payment; down payment invoices | Planned (card 386). |
| Rounding on purchase documents | Planned (card 386): ERPNext's purchase charges have no fee role. |
| Price lists other than the one mirrored, special prices, period discounts | Planned (card 386): **Price List** / **Item Price**, **Pricing Rule**. |
| Bills of materials (sales and assembly trees) | Planned (card 386): **Product Bundle**, **BOM**. |
| Stock per warehouse; deliveries, returns, goods receipts and issues, inventory transfers | Planned (card 386); stock levels stay authoritative so nothing is counted twice (operator decision, as for NetSuite). |
| Activities, service calls, campaigns | Planned (card 386): **Event** / **ToDo**, **Issue**, **Campaign**. |
| Payment terms, sales employees, territories, business partner groups | Planned (card 386): **Payment Terms Template**, **Sales Person**, **Territory**, **Customer Group** / **Supplier Group**. |
| Deposits, cheques for payment, bank statements | Planned (card 386): **Journal Entry**, **Bank Transaction**. |
| Fixed assets, production orders, time sheets | Planned (card 386): **Asset**, **Work Order**, **Timesheet**. |
| User-defined fields and tables | Planned (card 386): per-connection field maps. |
| Attachments | Planned (card 386): **File**. |
| Payroll | Not in SAP Business One. |
| An invoice changed at the source after a return was booked against it | Planned (card 386): the writer amends a changed booked invoice by cancelling it, which ERPNext refuses while a return names it; that invoice is reported Failed (and retried) until the writer can update it in place. |
