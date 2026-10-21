<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Salesforce → ERPNext

The single source of truth for what the Salesforce adapter mirrors. **Not yet run against a live org:**
there are no live credentials; every fixture (`tests/fixtures/salesforce`, written by
`python -m tools.salesforce_fixtures`) is shaped as the REST API answers, and is validated by `tools/prove.py`
against `tests/schemas/salesforce.json`. Salesforce publishes no static OpenAPI document for its standard
objects (its sObjects OpenAPI generator is a per-org beta), so that schema is transcribed by
`tools/salesforce_schema.py` from the REST API Developer Guide (the Query and sObject Describe resources,
https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest) and the Object Reference
(https://developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference). At run time each
object's describe metadata says which fields the org has: the standard fields the mappers read that exist
in it, and every custom field.

Access: one connection per org, through its My Domain (`https://<domain>.my.salesforce.com`). Create an
External Client App (or a connected app) with OAuth, the `api` and `refresh_token` scopes, this site's
`/api/method/idempotent_importer.api.oauth_callback` as its callback URL and a secret required for the web
server flow. Enter its consumer key and secret on the connection with the My Domain URL as the Store URL, save,
and click **Connect to Salesforce**, approving as a user who can read these records. OAuth 2 (the web server
flow, help.salesforce.com `remoteaccess_oauth_web_server_flow`): the consumer secret in the token form, and a
refresh token kept until revoked (replaced on each refresh when the app rotates them; the newest is kept). A
refused token stops the run and holds scheduled runs.

Reading: SOQL through `/services/data/v62.0/query`, each page `ORDER BY Id LIMIT 200` after the last id read, so
a paused run carries on where it was (query locators expire after 15 minutes); child relationships (contacts,
notes, price book entries, quote and order lines) come with their parent, the rest of a long list from its
`nextRecordsUrl`. Incremental runs read what changed since the last clean run (`SystemModstamp >=`); the
customers and the opportunity types are read whole. Deletions come only from a completed, unfiltered full pass.
Limits: an org's API requests are counted over 24 hours against its allocation and reported in
`Sforce-Limit-Info` on every answer; the importer paces five requests a second, pauses the run for an hour once
90% of the allocation is used (leaving the rest to the org's other integrations), and treats a refusal for the
limit (403 `REQUEST_LIMIT_EXCEEDED`) as a pause, never as rejected credentials. An org with one currency keeps
its amounts in its `Organization.DefaultCurrencyIsoCode`; a multi-currency org's records name theirs.

## Mirrored

| Salesforce | ERPNext home | Notes |
|---|---|---|
| Users (standard) | **Employee** | Names, email, phone, employee number and manager; an inactive user is inactive. Salesforce keeps no gender or dates of a user: they stay empty, never invented. |
| Products | **Item** with **Item Price** | Name, SKU (or product code), description, the active standard price book price; an inactive product is disabled. |
| Accounts | **Prospect** with its **Contacts**, **Address** and **CRM Notes** | Named with the connection's abbreviation; its contacts as Contacts linked to it (name, email, phone, title); billing address, website, description. |
| Customer accounts (a `Customer…` type) and the accounts orders are for | **Customer** with its **Contacts** and **Address** | A business account as a company with its contacts; a person account as an individual. |
| Leads | **Lead** with its **CRM Notes** | Names, company, email, phone, mobile, title, website, description; converted is Converted, a working status Replied, a closed one closes the lead (disabled), any other Open. |
| Opportunity types (the Type picklist) | **Opportunity Type** | By label, named with the connection's abbreviation. |
| Opportunities | **Opportunity** with its account (a prospect), at its **Sales Stage** | Name, amount and currency, probability, close date, type, description and notes; won is Converted, closed otherwise Lost. |
| Logged calls (tasks of the Call subtype) | **Communication** (phone) | Subject, description, duration and disposition, inbound or outbound; on the lead, else the opportunity, else the account. |
| Logged emails (tasks of an email subtype) and email messages | **Communication** (email) | Subject and body, incoming or sent, the address; on the lead, opportunity or account it is about. |
| Other tasks | **ToDo** about the lead, opportunity or account | Subject and description, due date, closed or open. |
| Events | **Event** (private) with its lead, opportunity and account | Subject, start and end (shown on the site's clock), an all-day event on its day, description and location. Salesforce keeps no event status: every event is Open. |
| Notes (classic) | **CRM Notes** on the lead, prospect or opportunity they are on | Title and body. |
| Quotes | Draft **Quotation** to the account (a prospect) | Lines at list price, line discounts as the discount, tax and shipping and handling, which must make the grand total exactly; rejected and denied quotes aren't kept. |
| Orders | **Sales Order** to the account (a customer) | Products at their unit prices, which must make the total amount exactly; a draft stays a draft, activated is submitted, cancelled isn't kept. The PO number and description are its remarks. |
| Custom fields (every object above) | **Source Fields** on the record | By label; a picklist's value as its label, several joined with "; ", a checkbox as Yes or No. |

## Planned and gaps

| Salesforce | Status |
|---|---|
| Contacts with no account (private contacts) | Operator decision: ERPNext's Contact needs a party; such a contact has none to be kept on. |
| Enhanced notes (ContentNote) and files | Planned (card 386): read through ContentDocumentLink and ContentVersion as **CRM Notes** and **File**. |
| Price books other than the standard one, and prices in several currencies | Planned (card 386): a **Price List** per price book and currency. |
| Cases, campaigns and campaign members, contracts, assets, entitlements, forecasts, territories | Operator decision: outside the sales pipeline and the books, or with no first-party home (**Issue** for cases is a candidate). |
| Opportunity products (OpportunityLineItem), opportunity contact roles and team members | Planned (card 386): an opportunity's items (**Opportunity Item**) and contacts. |
| Billing and CPQ packages (invoices, payments, CPQ quotes) | Operator decision: packages with their own objects, read only where an org installs them. |
| Record owners, queues and roles | Operator decision: the org's users aren't this site's users. |
