<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Zoho Inventory (API v1) → ERPNext

The single source of truth for what the Zoho Inventory adapter mirrors. **Not yet run against a live
organisation:** there are no live credentials; every fixture (`tests/fixtures/zoho_inventory`) is shaped as
Zoho's published OpenAPI modules define it (`https://www.zoho.com/inventory/api/v1/openapi-all.zip`, pinned
by SHA-256 in `tools/specs.py`) and proven against them strictly. An item group's response documents its
items, attributes and options as single objects; the reader takes one or a list.

Access, OAuth, reading and limits are Zoho Books' (see [zoho_books.md](zoho_books.md)), with the
organisation's Inventory address as the Store URL (`https://inventory.zoho.com`, …), **Connect to Zoho
Inventory**, the Inventory read-only scopes, and the `/inventory/v1` API. Items and categories are
incremental (`last_modified_time`). A document's line names its item, not the item group it is a variant of,
so each line gets its item's group from the items list (read once).

An organisation on both Zoho Books and Zoho Inventory shares contacts, items, invoices, bills and payments
between them: mirror those from one connection only (switch them off on the other), or they are booked
twice.

## Mirrored

| Zoho Inventory | ERPNext home | Notes |
|---|---|---|
| Locations | **Warehouse** | Address, parent location. |
| Categories | **Item Group** `<name> - <abbr>` | Parent category. |
| Item groups | **Item** template with a variant **Item** per item, **Item Attribute** | The group's attribute and each item's option; each item's rate, SKU and stock on hand. |
| Items in no group | **Item**, **Item Price**, stock | |
| Contacts | **Customer** or **Supplier**, **Contact**, **Address** | |
| Sales orders | **Sales Order** | Lines on the variant they sold. A void order has none. |
| Invoices, credit notes | **Sales Invoice** and returns | |
| Purchase orders, bills, vendor credits | **Purchase Order**, **Purchase Invoice** and debit notes | A bill's total from its sub-total and tax (the module documents none). |
| Customer payments | **Payment Entry** | Into the connection's deposit account (Inventory has no chart of accounts to read; documents book to their items' accounts), allocated to its invoices. |

## Planned and gaps

| Zoho Inventory | Status |
|---|---|
| Stock per location | Planned (card 386): one **Bin** per mirrored warehouse (stock is mirrored as the total on hand). |
| Composite items (bundles and assemblies) | Planned (card 386): **Product Bundle** / **BOM**. |
| Transfer orders, inventory adjustments, inventory counts | Planned (card 386): **Stock Entry** (Material Transfer), **Stock Reconciliation**. |
| Packages, shipments, sales returns, purchase receives, picklists, putaways | Planned (card 386): **Shipment** / **Delivery Note**, **Sales Invoice** return with stock, **Purchase Receipt**, **Pick List**, **Putaway Rule**. |
| Batches, serial numbers, storage locations, units of measure, price lists, reporting tags | Planned (card 386): **Batch**, **Serial No**, **Warehouse** tree, **UOM Conversion**, **Price List**, **Accounting Dimension**. |
| Tax-inclusive documents, line discounts, credit-note line rates | As Zoho Books. |
