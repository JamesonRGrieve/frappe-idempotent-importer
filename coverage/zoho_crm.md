<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Zoho CRM → ERPNext

The single source of truth for what the Zoho CRM adapter mirrors. **Not yet run against a live organisation:**
there are no live credentials; every fixture (`tests/fixtures/zoho_crm`, written by
`python -m tools.zoho_crm_fixtures`) is shaped as Zoho's CRM API v8 OpenAPI documents define each response,
and is validated against them by `tools/prove.py`. The documents are the ones Zoho publishes for the v8 API
(https://github.com/zoho/crm-oas, `v8.0/{fields,modules,org,users,notes,related_records,record}`, pinned to commit
`a6687fc9` and each document's SHA-256 in `tools/specs.py`); limits follow
https://www.zoho.com/crm/developer/docs/api/v8/api-limits.html.

Access: one connection per organisation. Register a server-based client in Zoho's API console
(https://api-console.zoho.com, Multi-DC enabled) with this site's `/api/method/idempotent_importer.api.oauth_callback`
as its redirect URI. Enter the organisation's CRM address in its data centre (`https://crm.zoho.com`,
`https://crm.zoho.eu`, `https://crm.zoho.com.au`, …) as the Store URL and the client's ID and secret, save, and click
**Connect to Zoho CRM**. OAuth 2 as for Zoho Books: the data centre's accounts server, the scopes
`ZohoCRM.modules.READ`, `ZohoCRM.settings.READ`, `ZohoCRM.users.READ` and `ZohoCRM.org.READ`, the secret in the
token form, a refresh token that never changes; an approval from another data centre is refused. A refused
token stops the run and holds scheduled runs.

Reading: the modules the organisation has (`/settings/modules`; an edition without inventory has no quotes or
orders, which are then skipped), each module's fields (`/settings/fields`): the standard fields the mappers read
that the organisation has, and every custom field. Zoho answers at most 50 fields a request, so a module needing
more reads its pages with the first 50 and the rest by the page's ids, merged. Pages of 100 from `page` 1, then
after Zoho's `next_page_token`. An activity or note on a contact carries the contact's account. Incremental runs
ask for what changed since the last clean run (`If-Modified-Since`); users, pipelines and customers are read
whole. Deletions come only from a completed, unfiltered full pass. Limits: API credits over a rolling 24 hours
by edition and licences, and 5 to 25 calls at once; the importer paces five requests a second, backs off on
`TOO_MANY_REQUESTS` and server errors, and once `X-API-CREDITS-REMAINING` falls to 1,000 pauses the run for an
hour, leaving the rest to the organisation's other integrations. Amounts are exact decimals, in the record's
currency (an organisation with one currency keeps its `org.iso_code`).

## Mirrored

| Zoho CRM | ERPNext home | Notes |
|---|---|---|
| Users | **Employee** | Names, email, phone and manager; an active user is active, any other inactive. Zoho keeps no gender or dates: they stay empty, never invented. |
| Products | **Item** with **Item Price** | Name, code, description, unit price; a product with stock in hand is a stock item at that quantity; an inactive product is disabled. |
| Vendors | **Supplier** with its **Contact** and **Address** | Name, email, phone, address. |
| Leads | **Lead** | Names, company, email, phone, mobile, designation, website, description; not contacted and attempted Open, contacted Replied, pre-qualified Opportunity, contact in future Interested; a junk, lost or unqualified lead is closed (disabled). |
| Accounts | **Prospect** with its **Contacts** and **Address** | Named with the connection's abbreviation; its contacts (the related list) as Contacts linked to it. |
| Customer accounts, and the accounts sales orders and invoices are for | **Customer** with its **Contacts** and **Address** | A company with its contacts. |
| Pipelines (the Deals module's Pipeline picklist) | **Opportunity Type** | Named with the connection's abbreviation. |
| Deals | **Opportunity** with its account (a prospect), at its stage's **Sales Stage** | Name, amount and currency, probability, closing date, pipeline, description; won at a "Closed Won" stage (or 100%), lost at another "Closed" stage. |
| Calls | **Communication** (phone) | Subject, description, duration and result, inbound or outbound; on the deal, account or lead it is on, else the contact's account. |
| Notes | **Communication** (other) | Title and text, on what they are attached to. |
| Events | **Event** (private) | Title, start and end (shown on the site's clock), all day on its day, venue and description; with the record it is on and its lead participants. |
| Tasks | **ToDo** | Subject and description, due date; completed is Closed. |
| Quotes | Draft **Quotation** to the account (a prospect) | Lines at list price; the lines' and the quote's discounts, taxes and adjustment, which must make the grand total exactly; a lost quote isn't kept. |
| Sales orders | **Sales Order** to the account (a customer) | As a quote; created stays a draft, approved and on is submitted, cancelled isn't kept. |
| Invoices | **Sales Invoice** to the account (a customer) | As a sales order. |
| Purchase orders | **Purchase Order** to the vendor (a supplier) | As a sales order. |
| Custom fields (every module above) | **Source Fields** on the record | By label; a picklist's value as its display value, several joined with "; ", a lookup as its name, a checkbox as Yes or No. |

## Planned and gaps

| Zoho CRM | Status |
|---|---|
| Contacts with no account | Operator decision: ERPNext's Contact needs a party; such a contact has none to be kept on. |
| Price books, product prices in several currencies | Planned (card 386): a **Price List** per price book and currency. |
| Cases, solutions, campaigns, visits, social, forecasts, territories, custom modules | Operator decision: outside the sales pipeline and the books, or no first-party home (**Issue** for cases is a candidate). |
| Emails (the mail add-on) and attachments | Planned (card 386): **Communication** (email) and **File**. |
| Deal stage history, contact roles | Planned (card 386): an opportunity's stage history and its contacts. |
| Record owners, roles and profiles | Operator decision: Zoho's users aren't this site's users. |
