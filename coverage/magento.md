<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Magento / Adobe Commerce → ERPNext

**Status: listed, not built.** Connections to Magento / Adobe Commerce can be created, but they refuse to run ("The
Magento / Adobe Commerce adapter is not built yet"). A later phase builds it on the shared engine: the same identities,
change hashes, writers, lockout safety and retirement as every built source.

- **Source API:** Built in wp-woo-idempotent-importer (REST API V1): its coverage/magento.md lists the source's objects.
- **Planned first-party homes:** Item Groups, Items (templates and variants), Item Prices, sales as Pricing Rules, stock as Stock Reconciliations, Website Items, Item Reviews, Customers with Contacts and Addresses, coupons as Pricing Rules + Coupon Codes, orders as Sales Invoices + Payment Entries with credit notes for refunds, and the store's pages, posts and redirects as Web Pages, Blog Posts and Website Route Redirects.
- **Gaps:** recorded here, object by object, when the adapter is built: anything without a first-party
  home is listed as a gap, never approximated.
