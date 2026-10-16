<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Zoho → ERPNext

**Status: listed, not built.** Connections to Zoho can be created, but they refuse to run ("The
Zoho adapter is not built yet"). A later phase builds it on the shared engine: the same identities,
change hashes, writers, lockout safety and retirement as every built source.

- **Source API:** Zoho Books / Inventory / CRM APIs.
- **Planned first-party homes:** Customers, Suppliers, Contacts and Addresses; Items, Item Groups and Item Prices; Sales and Purchase Invoices with their Payment Entries and credit notes; and, where the source keeps them, Accounts (mapped onto the company's chart of accounts) and Journal Entries; Zoho CRM's Leads and Deals as CRM Leads and Opportunities.
- **Gaps:** recorded here, object by object, when the adapter is built: anything without a first-party
  home is listed as a gap, never approximated.
