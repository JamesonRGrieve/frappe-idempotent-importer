<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Agendrix → ERPNext (HRMS)

The single source of truth for what the Agendrix adapter mirrors. **Not yet run against a live
organisation:** there are no live credentials; every fixture (`tests/fixtures/agendrix`, written by
`python -m tools.agendrix_fixtures`) is shaped as Agendrix's own OpenAPI document defines each record, and is
validated against it by `tools/prove.py`. That document is the one Agendrix publishes and generates its
developer reference from (https://github.com/agendrix/openapi, `en/open_api.json`, version 1.14.4, pinned to
commit `5a611519` and its SHA-256 in `tools/specs.py`); the OAuth flow follows its "OAuth 2.0" section and
Agendrix's own samples (https://github.com/agendrix/public-api-integration-examples). Agendrix is read only
through this official public API (the operator decided against scraping).

Access: Agendrix's API is for organisations on its Plus plan, through an application created in its
developer portal (https://developers.agendrix.com, a developer account of its own). Add this site's
`/api/method/idempotent_importer.api.oauth_callback` as the application's redirect URI, enter its client ID
and secret on the connection with the Store URL `https://app.agendrix.com` (or `https://sandbox.agendrix.net`
for the sandbox), save, and click **Connect to Agendrix** signed in as an administrator of the organisation:
the API answers with what the token's owner may see, so a manager's token would see only their sites and
positions. OAuth 2: the authorization code grant with the `read` scope; token requests are JSON and a
refresh names the redirect URI too; an access token lasts two hours and each refresh token works once (the
new one is kept before the access token is used). A refused token stops the run and holds scheduled runs.

Reading: every list pages by `page` and `page_size` (100) until `pagination.last_page`. Archived sites,
positions and members are read too (`search[with_deleted]`). Shifts are read by their last update
(`search[updated_since]`/`search[updated_until]`): a full pass covers the organisation's whole history, an
incremental run the time since the last clean run with archived shifts included, so a removed shift is
retired. Clock logs never change: an incremental run reads only those created since. Members, positions,
sites, time off types and requests have no change filter and are read whole; the change hash skips what
didn't change. Deletions come only from a completed, unfiltered full pass. The API allows 360 requests a
minute: the importer paces under it, waits out a 429's `RateLimit-Reset` (or pauses the run when it is
long) and backs off on server errors.

## Mirrored

| Agendrix | ERPNext home | Notes |
|---|---|---|
| Sites | **Branch** | Named with the connection's abbreviation; renamed with the site. A Branch has no disabled state, so an archived site stays for the employees that name it. |
| Positions and sub-positions | **Designation** | A sub-position is named after its position ("Kitchen - Prep"): sub-positions of different positions may share a name. |
| Members | **Employee** | Profile names, birthdate, email and first phone; seniority date as date of joining; identification number; main position (designation) and main site (branch). An archived member has left, on the day archived. Agendrix keeps no gender: it stays empty, never invented. |
| Time off types | **Leave Type** | Leave without pay when unpaid. Agendrix keeps the balances, so the type allows a negative balance and counts holidays: ERPNext never refuses a leave Agendrix granted. |
| Shifts | **Shift Assignment** on the **Shift Type** of its hours | The day it starts; the Shift Type is named by its hours as Agendrix's clock shows them ("08:00-16:00", one ending earlier runs past midnight) and made the first time a shift has them. Published shifts are submitted, drafts stay drafts; a moved shift is amended; a removed one is cancelled (a draft made inactive). |
| Clock logs | **Employee Checkin** | In or out, at its time in the site's time zone, the device type. A failed clock attempt is no punch. |
| Time entries | **Attendance**, one per member and day | Present for the regular and overtime minutes the day's entries worked, from the first start to the last end (none while an entry is still open); submitted once every entry is approved. A day of time off only is its leave's: HRMS marks it from the Leave Application. |
| Time off requests | **Leave Application** | Type, first and last day, justification, the day it was asked. Approved and declined ones are submitted (HRMS then marks an approved leave's days On Leave), pending and cancelled ones stay drafts. No mail goes out. |

## Planned and gaps

| Agendrix | Status |
|---|---|
| A time off request for part of a single day | Planned (card 386): ERPNext books leave in whole or half days; reported Unsupported, never rounded. |
| A time off shift scheduled without a request | Planned (card 386): its home would be a Leave Application of its own. |
| Open shifts (no member yet) and open shift requests | Planned (card 386): a Shift Assignment needs an employee; **Shift Request** is the likely home. |
| A day with worked time that HRMS already marked from an approved leave | Kept as HRMS marked it; the worked time is reported Unsupported. |
| Member time banks and their transactions | Planned (card 386): **Leave Allocation** per leave type, once the operator decides which banks are leave. |
| Payrolls, premiums and premium entries, member rates, overtime thresholds | Planned (card 386): HRMS payroll (**Salary Structure Assignment**, **Additional Salary**); an operator decision. |
| Breaks, shift tasks and activities, shift resources and resources | Planned (card 386); no first-party home yet. |
| Members' HR data and private HR data (the organisation's own HR fields) | Operator decision: custom fields on Employee, per organisation. |
| Availability lists and requests, transfer requests, scheduler projections, publications | Operator decision: no first-party home. |
| The billboard (threads, tags, comments), the drive (files, folders, signatures) | Operator decision: no first-party home in HRMS. |
