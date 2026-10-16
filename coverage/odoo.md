<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Odoo → ERPNext

**Status: listed, not built.** Connections to Odoo can be created, but they refuse to run ("The
Odoo adapter is not built yet"). A later phase builds it on the shared engine: the same identities,
change hashes, writers, lockout safety and retirement as every built source.

- **Source API:** Odoo's external API (JSON-RPC/XML-RPC).
- **Planned first-party homes:** Customers, Suppliers, Contacts and Addresses; Items, Item Groups and Item Prices; Sales and Purchase Invoices with their Payment Entries and credit notes; and, where the source keeps them, Accounts (mapped onto the company's chart of accounts) and Journal Entries; Odoo's website and eCommerce modules as for a store.
- **Gaps:** recorded here, object by object, when the adapter is built: anything without a first-party
  home is listed as a gap, never approximated.
