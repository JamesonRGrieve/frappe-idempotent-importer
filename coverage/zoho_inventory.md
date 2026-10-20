<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Zoho Inventory → ERPNext

**Status: listed, not built.** Connections to Zoho Inventory can be created, but they refuse to run ("The
Zoho Inventory adapter is not built yet"). A later phase builds it on the shared engine: the same
identities, change hashes, writers, lockout safety and retirement as every built source.

- **Source API:** the Zoho Inventory API v1.
- **Planned first-party homes:** Items, Item Groups and Item Prices; Warehouses and stock; Customers and
  Suppliers; Sales and Purchase Orders, Invoices and Bills with their payments; transfer orders and
  adjustments as Stock Entries.
- **Gaps:** recorded here, object by object, when the adapter is built: anything without a first-party
  home is listed as a gap, never approximated.
