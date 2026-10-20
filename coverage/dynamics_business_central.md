<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Dynamics 365 Business Central → ERPNext

The single source of truth for what the Business Central adapter mirrors. It reads one company of a
Business Central online environment through API v2.0
(`https://api.businesscentral.dynamics.com/v2.0/<tenant>/<environment>/api/v2.0/companies(<id>)/…`, OData
v4; [reference](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/api-reference/v2.0/)).

**Not yet run against a live Business Central environment.** Microsoft publishes no OpenAPI document for
API v2.0 (only v1.0's), so `tests/schemas/dynamics_business_central.json` is transcribed by
`tools/bc_schema.py` from the Properties table of each entity's resource-type page (each schema cites its
page and docs commit); the fixtures (`tests/fixtures/dynamics_business_central`, written by
`tools/bc_fixtures.py`) are proven against it.

Access: an app registered in Microsoft Entra ID with Business Central's `API.ReadWrite.All` application
permission (Business Central's APIs need it to read), added on Business Central's "Microsoft Entra
Applications" page with read permission sets. The app reads as itself through OAuth 2's client credentials
grant (`core.oauth.ClientCredentials`) at `login.microsoftonline.com/<tenant>/oauth2/v2.0/token` for
`https://api.businesscentral.dynamics.com/.default`; a refused grant stops the run and holds scheduled runs.
The connection holds the environment's API URL, the company's id (a GUID) as the account, and the app's
client ID and secret. Lists are paged by `@odata.nextLink` (200 at a time, `Prefer: odata.maxpagesize`);
documents arrive with their lines and items with their variants (`$expand`). Every kind is incremental by
`lastModifiedDateTime` except dimensions. Requests are paced under the documented per-user limit of 6,000
OData requests in a five-minute sliding window, and a 429 waits out its Retry-After
([operational limits](https://learn.microsoft.com/dynamics365/business-central/dev-itpro/administration/operational-limits-online)).

## Mirrored

| Business Central | ERPNext home | Notes |
|---|---|---|
| Posting accounts of the chart | **Account** `<name> - <abbr>`, its number | Flat under the company's root of its category; income → Income Account, cost of goods sold → Cost of Goods Sold, expense → Expense Account, the Cash subcategory → Bank. A blocked account stays enabled (its history still books). |
| Dimensions and their values | Group **Cost Center** per dimension, a **Cost Center** per value | |
| Locations | **Warehouse** with its address | |
| Item categories | **Item Group** `<name> - <abbr>` | Flat (the API keeps no parent). |
| Items and their variants | **Item**; a template with a variant **Item** per variant (attribute "Variant": its code) | Number, GTIN, category, unit price; inventory items are stock items with their inventory. Not published (the API says nothing of a web shop). |
| Customers and vendors | **Customer** / **Supplier** with **Contact** and **Address** | Company or person, address, email, phone, tax registration number, currency; blocked for everything: disabled. |
| Employees | **Employee** | Given, middle and surname, birth, employment and termination dates (Business Central's blank date `0001-01-01` is none), number, work and personal email, phone; active / inactive / terminated → Left. |
| Projects | **Project** | Number and name (all the API keeps). |
| Contacts | **Lead** | A company or a person, its company, email, phones, job title, website; one that is a customer: Converted. |
| Opportunities | **Opportunity** with its contact's Lead | Estimated value, chance of success, estimated closing; won → Converted, lost → Lost. |
| Sales quotes | Draft **Quotation** | |
| Sales orders | **Sales Order** (draft or in review: draft; open: submitted) | Lines at their net amounts, the tax total, the requested delivery date, the external document number. |
| Purchase orders | **Purchase Order** | As sales orders; the requested receipt date. |
| Sales invoices and credit memos | **Sales Invoice** (credit memos as returns against the invoice they correct) | Item lines on their items (or variants), account lines on their accounts; posted (open, paid, corrective) → submitted, draft → draft, cancelled → none. The tax total on the connection's tax account. |
| Purchase invoices and credit memos | **Purchase Invoice** (credit memos as returns) | As sales; the vendor's invoice number as the bill number. |

## Gaps

| Business Central | Why it isn't mirrored |
|---|---|
| Heading, total, begin-total and end-total lines of the chart | They lay the chart out; their ranges aren't in the API, so there is no parent to build (reported). |
| Customer and vendor payments, ledger entries and their applications | Operator decision: API v2.0 exposes posted payments and applications only through G/L entries, which name no customer or vendor. Mirroring them needs the Customer/Vendor Ledger Entries pages published as OData web services (a per-connection setting) or a custom API page. Until then invoices keep their outstanding balance in ERPNext. |
| General journals and G/L entries | As above: the journals in the API are unposted; posted entries repeat the invoices. |
| Documents in another currency | Planned (card 386): the API gives no exchange rate (currency factor) for a document. |
| Resource, fixed asset and charge lines | Booked on the connection's fallback item (a warning): ERPNext has no such line type. |
| Line dimensions, document dimensions | Planned (card 386): the lines' `dimensionSetLines` → each line's cost center. |
| Inventory per variant and per location | Planned (card 386): the API gives an item's total inventory only. |
| Taxes per tax area/group | Planned (card 386): the API gives each document's tax total, not its taxes. |
| Item prices and discounts, units of measure, payment terms, shipment methods, salespeople | Planned (card 386). |
