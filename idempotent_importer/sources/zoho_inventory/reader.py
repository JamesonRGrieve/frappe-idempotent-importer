# SPDX-License-Identifier: AGPL-3.0-or-later
"""What Zoho Inventory holds, module by module (www.zoho.com/inventory/api/v1), read through the shared Zoho
reader (``sources.zoho.client``): categories, locations (warehouses), item groups (each with its variants)
and the items in no group, contacts as customers and vendors, sales orders, invoices and credit notes,
purchase orders, bills and vendor credits, and customer payments. Every node carries its identity as ``id``
(``gid://zoho_inventory/<connection>/<Type>/<id>``)."""

from __future__ import annotations

from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.zoho.client import Module, Product, ZohoReader, every

SOURCE_ID = "zoho_inventory"
PRODUCT = Product(
	label="Zoho Inventory",
	api_path="/inventory/v1",
	scopes=(
		"ZohoInventory.settings.READ",
		"ZohoInventory.items.READ",
		"ZohoInventory.compositeitems.READ",
		"ZohoInventory.contacts.READ",
		"ZohoInventory.salesorders.READ",
		"ZohoInventory.invoices.READ",
		"ZohoInventory.creditnotes.READ",
		"ZohoInventory.customerpayments.READ",
		"ZohoInventory.purchaseorders.READ",
		"ZohoInventory.bills.READ",
		"ZohoInventory.vendorcredits.READ",
		"ZohoInventory.inventoryadjustments.READ",
		"ZohoInventory.transferorders.READ",
	),
)
ALL = (("filter_by", "Status.All"),)


def is_customer(contact: Node) -> bool:
	return contact.optional_string("contact_type") != "vendor"


def is_vendor(contact: Node) -> bool:
	return contact.optional_string("contact_type") == "vendor"


def is_ungrouped(item: Node) -> bool:
	"""An item in no item group (a group's items are its variants, read with the group)."""
	return not item.optional_string("group_id")


MODULES: dict[Kind, tuple[Module, ...]] = {
	Kind.WAREHOUSE: (
		Module("/locations", "locations", "location_id", "Location", paged=False, single="location"),
	),
	Kind.COLLECTION: (
		Module("/categories", "categories", "category_id", "Category", incremental=True, single="category"),
	),
	Kind.PRODUCT: (
		Module("/itemgroups", "itemgroups", "group_id", "ItemGroup", detail=""),
		Module(
			"/items",
			"items",
			"item_id",
			"Item",
			query=ALL,
			incremental=True,
			take=is_ungrouped,
			single="item",
		),
	),
	Kind.CUSTOMER: (
		Module("/contacts", "contacts", "contact_id", "Contact", "contact", ALL, take=is_customer),
	),
	Kind.SUPPLIER: (
		Module("/contacts", "contacts", "contact_id", "Contact", "contact", ALL, take=is_vendor),
	),
	Kind.SALES_ORDER: (Module("/salesorders", "salesorders", "salesorder_id", "SalesOrder", "salesorder"),),
	Kind.SALES_INVOICE: (
		Module("/invoices", "invoices", "invoice_id", "Invoice", "invoice"),
		Module("/creditnotes", "creditnotes", "creditnote_id", "CreditNote", "creditnote"),
	),
	Kind.PURCHASE_ORDER: (
		Module("/purchaseorders", "purchaseorders", "purchaseorder_id", "PurchaseOrder", "purchase_order"),
	),
	Kind.PURCHASE_INVOICE: (
		Module("/bills", "bills", "bill_id", "Bill", "bill"),
		Module("/vendorcredits", "vendorcredits", "vendor_credit_id", "VendorCredit", "vendor_credit"),
	),
	Kind.PAYMENT_ENTRY: (
		Module("/customerpayments", "customerpayments", "payment_id", "CustomerPayment", "payment"),
	),
}
ITEMS = MODULES[Kind.PRODUCT][1]
LINED = frozenset({"SalesOrder", "Invoice", "CreditNote", "PurchaseOrder", "Bill", "VendorCredit"})


class ZohoInventoryReader(ZohoReader):
	"""The shared Zoho reader, with the one lookup Inventory needs: a document's line names its item, not
	the item group it is a variant of, so each line gets its item's ``group_id`` (from the items, read
	once)."""

	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		super().__init__(PRODUCT, MODULES, config, client)
		self._groups: dict[str, str] | None = None

	def looked_up(self, module: Module, record: Node) -> dict[str, JsonValue]:
		if module.type not in LINED:
			return {}
		lines = every(record, "line_items")
		if not any(line.optional_string("item_id") for line in lines):
			return {}
		if self._groups is None:
			self._groups = {
				item.string("item_id"): item.string("group_id")
				for item in self.listed(ITEMS)
				if item.optional_string("group_id")
			}
		groups = self._groups
		return {
			"line_items": [
				{**line.raw(), "group_id": groups[line.text("item_id")]}
				if line.text("item_id") in groups
				else line.raw()
				for line in lines
			]
		}
