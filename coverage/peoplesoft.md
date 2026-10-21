<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: PeopleSoft → ERPNext

The single source of truth for what the PeopleSoft adapter mirrors (PeopleSoft HCM into HRMS, PeopleSoft
Financials into the books). **Not yet run against a live installation:** there are no live credentials;
every fixture (`tests/fixtures/peoplesoft`, written by `python -m tools.peoplesoft_fixtures` for the test
installation's field map, `tests/fixtures/peoplesoft/field-map.json`) is the Query Access Service's answer
as PeopleBooks documents it, and is validated by `tools/prove.py` against the schema transcribed from it
(`tests/schemas/peoplesoft-qas.json`).

How it reads: through the Integration Broker's delivered REST Query Access Service, the service operation
`QAS_EXECUTEQRY_REST_GET` (PeopleTools PeopleBooks, Reporting Web Services, "Query Access Service
Operations" and "Executing the Query":
https://docs.oracle.com/cd/E92519_02/pt856pbr3/eng/pt/trws/task_ExecutingtheQuery-5e7fb0.html). Each kind
is a PeopleSoft Query the installation runs, at
`<Store URL>/PSIGW/RESTListeningConnector/<node>/ExecuteQuery.v1/<owner>/<query>/JSON/NONFILE?isconnectedquery=N&maxrows=<n>&json_resp=true`,
with its prompts as `prompt_uniquepromptname` / `prompt_fieldvalue` pairs; the answer is
`{"status", "data": {"query": {"numrows", "rows"}}}`. Every PeopleSoft installation's records are its own
(customised, effective-dated, set-controlled), so the adapter assumes no record or column: the connection's
**Field Map** names, per kind, the query, its key columns, the columns of each role, the codes translated
into ERPNext's vocabulary (`values`) and constants (`fixed`), and optionally its "key after" prompt (paging,
rows ordered by key) and "changed since" prompt (incremental runs). `idempotent_importer/sources/peoplesoft/
fieldmap.py` documents the shape and every role. A kind the map doesn't name is skipped with a warning.
The installation's queries can join and flatten what each role needs (a department's parent from the
department tree, an employee's current job row, a document's lines with its header).

Access: a PeopleSoft user with the Query Access Service's REST service operations (the QAS_QRY_REST
permission list in delivered security) and query access to the named queries. PeopleBooks ("Understanding
OAuth 2.0", pt859pbr5/eng/pt/tsec/UnderstandingOAuth2_0.html) states that provider REST services
authenticate only by Basic Authentication or a PeopleSoft Token, so the user ID and password are sent as
HTTP Basic credentials (over the web server's https address, the connection's Store URL). A refused login
(401/403) stops the run and holds scheduled runs. Oracle documents no request limit for the Integration
Broker (it runs on the customer's own servers): the importer sends one request at a time, at most two a
second, backs off on 429 and server errors, and pauses the run when the gateway keeps failing. A query with
no "key after" prompt is read in one request, and one that fills `max_rows` is refused rather than
truncated. Deletions come only from a completed, unfiltered full pass; an incremental run passes the
"changed since" prompt the day of the last clean run (kinds without one are read whole, the change hash
skipping what didn't change).

## Mirrored

| PeopleSoft (the field map's query) | ERPNext home | Roles |
|---|---|---|
| Departments | HR **Department** | name, parent (another department's key), disabled |
| Job codes | **Designation** | name |
| Employees (their current job) | **Employee** | first, middle and last names, gender, birth, hire and termination dates, status (translated: Active, Inactive, Suspended or Left), employee number, department, job code, supervisor, work and personal email, phone |
| Customers | **Customer**, **Contact**, **Address** | name, type, first and last names, email, phone, address (its country an ISO code, translated if needed), tax id, currency, disabled |
| Suppliers (vendors) | **Supplier**, **Contact**, **Address** | as customers |
| ChartField accounts | **Account** | number, name, root type (translated from account types), account type, parent, group, currency |
| AR invoices | **Sales Invoice** (a negative total as a return) | number, date, due date, customer, currency, total, header tax, posted; per row: line name, quantity, extended amount, account. Lines and tax must make the total exactly. |
| AP vouchers | **Purchase Invoice** | as AR invoices, with the supplier's invoice number as reference |
| AP payments, AR payments (one query each, or more) | **Payment Entry** | date, party, amount, currency, direction and party type (fixed per query), reference, posted; per row: the invoice or voucher it pays and the amount |
| Journals | **Journal Entry** | number, date, description, posted; per row: account and signed amount (PeopleSoft's monetary amount: a debit when positive) or debit and credit |

## Planned and gaps

| PeopleSoft | Status |
|---|---|
| Absences (Absence Management) | Planned (card 386): **Leave Application** and **Leave Type**, through a field-mapped query like the others. |
| Reported time (Time and Labor) | Planned (card 386): **Attendance** and **Employee Checkin**. |
| Positions (Position Management), locations | Planned (card 386): HRMS has no position record; **Branch** for locations. |
| Payroll (Global Payroll, Payroll for North America) | Operator decision: **Salary Slip** history. |
| Business units, setIDs and other ChartFields (fund, program, project) | Operator decision: **Accounting Dimensions** or one connection per business unit. |
| Items, purchase orders, receipts, inventory, billing invoices before AR | Planned (card 386): the same field-mapped reading, once a query is named. |
| Component Interfaces and other Integration Broker services | Not used: the Query Access Service reads every kind through one delivered operation; a customer's own REST services can be read the same way once their answers are described. |
| Documents in other currencies than the company's (payments) | As every source: Unsupported until card 386 books them. |
