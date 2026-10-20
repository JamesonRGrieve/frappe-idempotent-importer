<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Zoho Books → ERPNext

**Status: listed, not built.** Connections to Zoho Books can be created, but they refuse to run ("The
Zoho Books adapter is not built yet"). A later phase builds it on the shared engine: the same identities,
change hashes, writers, lockout safety and retirement as every built source.

- **Source API:** the Zoho Books API v3.
- **Planned first-party homes:** Customers, Suppliers, Contacts and Addresses; Items and Item Prices; Sales
  and Purchase Invoices with their Payment Entries and credit notes; Accounts (mapped onto the company's
  chart of accounts) and Journal Entries.
- **Gaps:** recorded here, object by object, when the adapter is built: anything without a first-party
  home is listed as a gap, never approximated.
