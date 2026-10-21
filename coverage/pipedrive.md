<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Pipedrive → ERPNext

The single source of truth for what the Pipedrive adapter mirrors. **Not yet run against a live company:**
there are no live credentials; every fixture (`tests/fixtures/pipedrive`, written by
`python -m tools.pipedrive_fixtures`) is shaped as Pipedrive's OpenAPI documents define each response, and is
validated against them by `tools/prove.py`. The documents are the ones Pipedrive's API reference is generated
from (https://developers.pipedrive.com/docs/api/v1/openapi.yaml and `openapi-v2.yaml`), published at unversioned
URLs and so pinned to the SHA-256 of the bytes fetched on 2026-10-21 in `tools/specs.py`. OAuth follows
pipedrive.readme.io/docs/marketplace-oauth-authorization, limits pipedrive.readme.io/docs/core-api-concepts-rate-limiting.

Access: one connection per Pipedrive company. Create an app in Pipedrive's Developer Hub with this site's
`/api/method/idempotent_importer.api.oauth_callback` as its callback URL and the scopes `base`, `users:read`,
`deals:read`, `contacts:read`, `leads:read`, `activities:read`, `products:read` and `phone-integration` (call
logs). Enter its client ID and secret on the connection (Store URL your `https://<company>.pipedrive.com`),
save, and click **Connect to Pipedrive**: the token's `api_domain` becomes the connection's account, the host
the API is read from. OAuth 2: the client authenticated by HTTP Basic, an hour's access token and a refresh
token that stays the same; unused for 60 days it lapses and the company connects again. A refused token stops
the run and holds scheduled runs.

Reading: API v2 for users' objects where it has them (products, persons, organizations, pipelines, stages,
deals, activities and the field lists), paging after each answer's `next_cursor`; v1 for users, leads,
activity types, notes and call logs, by `start` while `more_items_in_collection`. Incremental runs ask for
what changed since the last clean run (`updated_since`) where a list takes it (deals, persons, organizations,
products, activities, leads, notes); users, pipelines, call logs, archived leads and the customers are read
whole. Deletions come only from a completed, unfiltered full pass. Limits: a burst limit per token over two
seconds (80 requests for an OAuth app on the smallest plan) and a daily token budget for the company; the
importer paces ten requests a second, quiets when `x-ratelimit-remaining` runs out until `x-ratelimit-reset`,
backs off on 429 and pauses the run when the day's budget is spent.

## Mirrored

| Pipedrive | ERPNext home | Notes |
|---|---|---|
| Users | **Employee** | Name, email and phone; a deactivated or deleted user is inactive. Pipedrive keeps no gender or dates: they stay empty, never invented. |
| Products | **Item** with **Item Price** | Name, code, description and its price; a product with variations has an **Item** variant per variation (attribute *Variation*) at its own price. Deleted products aren't kept. |
| Persons | **Lead** | Name, primary email and phone, job title and notes. |
| Organizations | **Prospect** with its **Contact**, **Address** and leads | Named with the connection's abbreviation; its persons are its leads; address and website. |
| Organizations (else persons) of won deals | **Customer** with its **Contacts** and **Address** | An organization as a company with its persons as its contacts; a person as an individual. |
| Pipelines | **Opportunity Type** | Named with the connection's abbreviation. |
| Deals (archived too) | **Opportunity** with its organization (a prospect) or person (a lead), at its stage's **Sales Stage** | Title, value and currency, probability (else the stage's), expected close date, lost reason; open, won (Converted) or lost; an archived open deal is Closed. Deleted deals aren't kept. |
| Leads (archived too) | **Opportunity** (at no stage) | Pipedrive's deals in waiting: title, value, expected close date; archived is Closed. |
| Done call and email activities | **Communication** (phone or email) | Subject, note and description; on the person, else the deal or lead, else the organization. |
| Notes | **Communication** (other) | The note's text, on what it is attached to. |
| Call logs | **Communication** (phone) | Subject, duration, outcome and note, the number called. |
| Meetings and lunches | **Event** (private) | At the due time (UTC) for the duration; done is Completed; with its persons, deal, lead and organization; note, location and conference link. |
| Every other activity (tasks, deadlines, calls and emails not done, the company's own types) | **ToDo** | Type and subject, note, due date; done is Closed. |
| Custom fields (deals, leads, persons, organizations, products, activities) | **Source Fields** on the record | By name; an option as its label, several joined with "; ", a monetary value with its currency, an address as its text. |

## Planned and gaps

| Pipedrive | Status |
|---|---|
| Deal products and their discounts | Planned (card 386): an opportunity's items (**Opportunity Item**); Pipedrive has no quotes or orders of its own. |
| Products billed on a schedule, deal installments | Planned (card 386): **Subscription Plan**, **Payment Schedule**. |
| Prices in several currencies | Planned (card 386): a **Price List** per currency. |
| Mail threads and messages | Planned (card 386): **Communication** (email) from the mailbox API, read per thread. |
| Projects and their tasks | Planned (card 386): **Project** and **Task**. |
| Files, labels, filters, goals, followers, permission sets | Operator decision: outside the sales pipeline, or no first-party home (labels as tags is a candidate). |
| Owners of each record, teams | Operator decision: Pipedrive's users aren't this site's users. |
