<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Dynamics 365 Sales → ERPNext

The single source of truth for what the Dynamics 365 Sales adapter mirrors. It reads one environment through
the Dataverse Web API (`https://<org>.crm<n>.dynamics.com/api/data/v9.2/…`, OData v4;
[reference](https://learn.microsoft.com/power-apps/developer/data-platform/webapi/overview)).

**Not yet run against a live Dynamics 365 environment.** Dataverse publishes no static OpenAPI document (each
environment describes itself at `$metadata`), so `tests/schemas/dynamics_sales.json` is transcribed by
`tools/dataverse_schema.py` from the table reference pages (each schema cites its page and docs commit),
with only the columns the reader selects; the fixtures (`tests/fixtures/dynamics_sales`, written by
`tools/dataverse_fixtures.py`) are proven against it.

Access: an app registered in Microsoft Entra ID, added to the environment as an application user with a
read-only security role over the tables below. It reads as itself through OAuth 2's client credentials grant
at `login.microsoftonline.com/<tenant>/oauth2/v2.0/token` for `https://<org>.crm.dynamics.com/.default`.
The connection holds the environment's URL, the tenant (id or domain) as the account, and the app's client ID
and secret. Each table's columns are selected (`$select`), lookups are annotated with the table they name
(`Prefer: odata.include-annotations`), lists are paged by `@odata.nextLink` (200 at a time), an account
arrives with its contacts and a document with its lines (`$expand`). Every table is incremental by
`modifiedon`. Requests are paced under the documented 6,000 requests per user in a five-minute sliding window,
and a 429 waits out its Retry-After
([service protection limits](https://learn.microsoft.com/power-apps/developer/data-platform/api-limits)).

## Mirrored

| Dynamics 365 Sales | ERPNext home | Notes |
|---|---|---|
| Products | **Item** | Number, description, list price; sales-inventory products are stock items with their quantity on hand; services, charges and flat fees aren't. A retired product stays enabled. Not published. |
| Accounts | **Customer** (company) with **Contact**s and an **Address** | Email, phone, currency; its contacts as its people; inactive: disabled. |
| Contacts | **Customer** (person) with an **Address** | Dataverse's documents can be for a contact. |
| Leads | **Lead** | Name, company, email, phones, job title, website, topic and description; qualified → Converted; disqualified → disabled. |
| Opportunities | **Opportunity** | With its account or contact, else the lead it came from; estimated revenue, probability and close date; won → Converted, lost → Lost. |
| Quotes (draft, active) | Draft **Quotation** | Won or closed: none. |
| Orders | **Sales Order** (active: draft; submitted, fulfilled, invoiced: submitted) | Lines at their net amounts (less manual and volume discounts), the order's discount (its amount and percentage) on the net total, freight on the connection's shipping account, the tax total; total checked exactly. Cancelled: cancelled. |
| Invoices | Submitted **Sales Invoice** | As orders; cancelled: none. |
| Documents in another currency | At the rate their totals imply (base total over total), the base total checked exactly | |

## Gaps

| Dynamics 365 Sales | Why it isn't mirrored |
|---|---|
| An invoice's paid state | Operator decision: Dataverse keeps no payments, so a paid invoice is booked and left outstanding in ERPNext (reported) rather than paid with an invented Payment Entry. |
| Addresses whose country isn't an ISO code | Dataverse keeps the country as free text; the address is left out with a warning (planned, card 386: a per-connection country map). |
| Product families and bundles, price lists and their items, units | Planned (card 386): Item templates, Product Bundles, Price Lists. |
| Taxes per line, write-in product details | The tax total is one amount; a write-in product books to the fallback item. |
| Competitors, activities, notes, cases, campaigns, goals | Planned (card 386) where ERPNext has a home (Competitor, Comment, Issue, Campaign). |
| Dates | Documents are dated by their creation in UTC (Dataverse keeps no invoice date). |
