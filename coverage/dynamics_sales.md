<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Dynamics 365 Sales → ERPNext

**Status: listed, not built.** Connections to Dynamics 365 Sales can be created, but they refuse to run ("The
Dynamics 365 Sales adapter is not built yet"). A later phase builds it on the shared engine: the same identities,
change hashes, writers, lockout safety and retirement as every built source.

- **Source API:** The Dataverse Web API.
- **Planned first-party homes:** CRM Leads, Opportunities, Customers, Contacts and Addresses, and Quotations and Sales Orders.
- **Gaps:** recorded here, object by object, when the adapter is built: anything without a first-party
  home is listed as a gap, never approximated.
