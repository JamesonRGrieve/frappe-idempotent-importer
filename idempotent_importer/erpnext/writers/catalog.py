# SPDX-License-Identifier: AGPL-3.0-or-later
"""The catalog: Item Groups from collections; Items (a template with variants, or one item), their Item
Attributes, Brand, images (Files), price on the connection's Price List, a sale as a Pricing Rule, stock
as a Stock Reconciliation into the connection's warehouse, and the Website Item that sells them on the
Public Site's Webshop Store.

Stock is mirrored as the source's absolute count, at zero valuation: the source has no cost price, and a
zero-valued reconciliation moves no money, so the books stay exact (orders never move stock, see
``sales``)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import frappe
from erpnext.controllers.item_variant import create_variant
from frappe.utils import nowdate, nowtime

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Values
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import money, quantity
from idempotent_importer.erpnext.custom_fields import CUSTOM_VALUES
from idempotent_importer.erpnext.writers.base import PART_HASH, Writer, custom_json
from idempotent_importer.naming import unique_abbr

STOCK_UOM = "Nos"
# ERPNext's UOM names for the units mappers hand over.
WEIGHT_UOMS = {"g": "Gram", "kg": "Kg", "oz": "Ounce", "lbs": "Pound"}


class CollectionWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		parent = self.context.item_group
		parent_gid = values.optional_string("parent")
		if parent_gid:
			parent = self.dependency(Kind.COLLECTION, parent_gid)
			if not frappe.db.get_value("Item Group", parent, "is_group"):
				frappe.db.set_value("Item Group", parent, "is_group", 1)
		name = self.context.suffixed(values.string("name"))
		doc = self.existing(link)
		if doc is not None and doc.name != name:
			frappe.rename_doc("Item Group", doc.name, name, force=True)
			doc = frappe.get_doc("Item Group", name)
		if doc is None:
			doc = frappe.new_doc("Item Group")
			doc.item_group_name = name
		doc.update(
			{
				"parent_item_group": parent,
				"description": values.optional_string("description"),
				"image": self.media.file_url(values.optional_string("image")),
				"show_in_website": 1,
				"route": self.context.route(values.string("slug")),
			}
		)
		doc.save(ignore_permissions=True)
		return Target("Item Group", doc.name, doc.route)

	def retire(self, link: Link) -> None:
		frappe.db.set_value("Item Group", link.target.name, "show_in_website", 0)


class ProductWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		groups = [self.dependency(Kind.COLLECTION, gid) for gid in values.strings("collections")]
		images = [self.media.file_url(image.string("url")) for image in values.records("images")]
		template = self.existing(link)
		code = (
			template.name if template else self.context.item_code(values.optional_string("sku"), desired.gid)
		)
		variable = values.string("type") == "variable"
		if variable:
			for attribute in values.records("attributes"):
				self._attribute_values(attribute.string("name"), attribute.strings("values"))
		item = self._item(
			template,
			code,
			{
				"item_name": values.string("name"),
				"item_group": groups[0] if groups else self.context.item_group,
				"description": values.optional_string("description") or values.string("name"),
				"brand": self._brand(values.optional_string("brand")),
				"image": images[0] if images else None,
				"is_stock_item": int(values.flag("is_stock_item")),
				"has_variants": int(variable),
				"disabled": 0,
				CUSTOM_VALUES: custom_json(values),
				**self._weight(values),
			},
			[attribute.string("name") for attribute in values.records("attributes")] if variable else [],
		)
		if variable:
			self._variants(desired.gid, values, item)
		else:
			self._barcode(item, values.optional_string("barcode"))
			self._selling(desired.gid, item.name, values)
		self.tags(desired.gid, "Item", item.name, values.strings("tags"))
		route = self._website_item(item, values, groups, images)
		return Target("Item", item.name, route)

	def retire(self, link: Link) -> None:
		frappe.db.set_value("Item", link.target.name, "disabled", 1)
		for variant in self.links.children(Kind.VARIANT, f"{link.gid}#"):
			frappe.db.set_value("Item", variant.target.name, "disabled", 1)
		website_item = frappe.db.get_value("Website Item", {"item_code": link.target.name})
		if website_item:
			doc = frappe.get_doc("Website Item", website_item)
			doc.published = 0
			doc.save(ignore_permissions=True)

	def _item(self, doc: Any | None, code: str, fields: dict[str, Any], attributes: list[str]) -> Any:
		"""The template (``attributes`` are its variant attributes) or the one item of a product."""
		if doc is None:
			doc = frappe.new_doc("Item")
			doc.update({"item_code": code, "stock_uom": STOCK_UOM, "is_sales_item": 1})
		else:
			# ERPNext doesn't let an item stop or start tracking stock, or gain or lose variants, once it
			# has transactions; those stay as first mirrored.
			fields = {k: v for k, v in fields.items() if k not in ("is_stock_item", "has_variants")}
		doc.update(fields)
		if attributes and [row.attribute for row in doc.attributes] != attributes:
			doc.set("attributes", [{"attribute": name} for name in attributes])
		doc.save(ignore_permissions=True)
		return doc

	def _weight(self, values: Values) -> dict[str, Any]:
		weight = values.record("weight")
		if weight is None:
			return {"weight_per_unit": 0, "weight_uom": None}
		return {
			"weight_per_unit": quantity(weight.decimal("value")),
			"weight_uom": WEIGHT_UOMS[weight.string("unit")],
		}

	def _brand(self, name: str | None) -> str | None:
		if not name:
			return None
		if not frappe.db.exists("Brand", name):
			frappe.get_doc({"doctype": "Brand", "brand": name}).insert(ignore_permissions=True)
		return name

	def _barcode(self, item: Any, barcode: str | None) -> None:
		"""ERPNext keeps a barcode on one item only: one another item already holds stays there."""
		if not barcode or any(row.barcode == barcode for row in item.barcodes):
			return
		if frappe.db.exists("Item Barcode", {"barcode": barcode}):
			return
		item.append("barcodes", {"barcode": barcode})
		item.save(ignore_permissions=True)

	@staticmethod
	def _attribute_values(name: str, values: list[str]) -> None:
		"""An Item Attribute with every value (shared by every connection: "Size" is "Size")."""
		attribute = (
			frappe.get_doc("Item Attribute", name)
			if frappe.db.exists("Item Attribute", name)
			else frappe.get_doc({"doctype": "Item Attribute", "attribute_name": name})
		)
		known = {row.attribute_value for row in attribute.item_attribute_values}
		abbreviations = {row.abbr for row in attribute.item_attribute_values}
		missing = [value for value in values if value not in known]
		for value in missing:
			attribute.append(
				"item_attribute_values", {"attribute_value": value, "abbr": unique_abbr(value, abbreviations)}
			)
		if missing or attribute.is_new():
			attribute.save(ignore_permissions=True)

	def _variants(self, product_gid: str, values: Values, template: Any) -> None:
		wanted: set[str] = set()
		for variant in values.records("variants"):
			gid = f"{product_gid}#{variant.string('gid')}"
			wanted.add(gid)
			existing = self.links.target(Kind.VARIANT, gid)
			attributes = {
				name: str(value) for name, value in (variant.required_record("attributes").all()).items()
			}
			if existing and frappe.db.exists("Item", existing):
				doc = frappe.get_doc("Item", existing)
			else:
				doc = create_variant(template.name, attributes)
				doc.item_code = self.context.item_code(variant.optional_string("sku"), variant.string("gid"))
			doc.update(
				{
					"item_name": f"{template.item_name} - {', '.join(attributes.values())}",
					"item_group": template.item_group,
					"brand": template.brand,
					"disabled": int(not variant.flag("enabled")),
					"image": self.media.file_url(variant.optional_string("image")) or template.image,
					**self._weight(variant),
				}
			)
			doc.save(ignore_permissions=True)
			self.links.save(Kind.VARIANT, gid, Target("Item", doc.name), PART_HASH, 1)
			self._barcode(doc, variant.optional_string("barcode"))
			self._selling(gid, doc.name, variant)
		for link in self.links.children(Kind.VARIANT, f"{product_gid}#"):
			if link.gid not in wanted:
				frappe.db.set_value("Item", link.target.name, "disabled", 1)
				self.links.retire(Kind.VARIANT, link.gid)

	def _selling(self, gid: str, item_code: str, values: Values) -> None:
		"""A sellable item's price, sale and stock."""
		price = values.optional_decimal("price")
		if price is not None:
			self._price(item_code, price)
		self._sale(gid, item_code, values.record("sale"))
		stock = values.optional_decimal("stock_qty")
		if stock is not None and frappe.db.get_value("Item", item_code, "is_stock_item"):
			self._stock(item_code, stock)

	def _price(self, item_code: str, price: Decimal) -> None:
		name = frappe.db.get_value(
			"Item Price", {"item_code": item_code, "price_list": self.context.price_list}
		)
		doc = frappe.get_doc("Item Price", name) if name else frappe.new_doc("Item Price")
		if name and Decimal(str(doc.price_list_rate)) == price:
			return
		doc.update(
			{
				"item_code": item_code,
				"price_list": self.context.price_list,
				"uom": STOCK_UOM,
				"price_list_rate": money(price),
			}
		)
		doc.save(ignore_permissions=True)

	def _sale(self, gid: str, item_code: str, sale: Values | None) -> None:
		"""A sale price as a Pricing Rule setting the item's rate (with the sale's dates)."""
		key = f"{gid}#sale"
		existing = self.links.target(Kind.PRICE, key)
		if sale is None:
			if existing:
				frappe.db.set_value("Pricing Rule", existing, "disable", 1)
			return
		doc = frappe.get_doc("Pricing Rule", existing) if existing else frappe.new_doc("Pricing Rule")
		doc.update(
			{
				"title": f"Sale {item_code}",
				"apply_on": "Item Code",
				"price_or_product_discount": "Price",
				"selling": 1,
				"buying": 0,
				"company": self.context.company,
				"currency": self.context.currency,
				"for_price_list": self.context.price_list,
				"rate_or_discount": "Rate",
				"rate": money(sale.decimal("price")),
				"valid_from": (sale.optional_string("from") or "")[:10] or None,
				"valid_upto": (sale.optional_string("to") or "")[:10] or None,
				"disable": 0,
			}
		)
		doc.set("items", [{"item_code": item_code}])
		doc.save(ignore_permissions=True)
		self.links.save(Kind.PRICE, key, Target("Pricing Rule", doc.name), PART_HASH, 1)

	def _stock(self, item_code: str, count: Decimal) -> None:
		"""The source's count as the warehouse's absolute quantity (a reconciliation only when it differs)."""
		current = frappe.db.get_value(
			"Bin", {"item_code": item_code, "warehouse": self.context.warehouse}, "actual_qty"
		)
		if Decimal(str(current or 0)) == count:
			return
		reconciliation = frappe.get_doc(
			{
				"doctype": "Stock Reconciliation",
				"company": self.context.company,
				"purpose": "Stock Reconciliation",
				"expense_account": self._difference_account(),
				"cost_center": self.context.cost_center,
				"posting_date": nowdate(),
				"posting_time": nowtime(),
				"set_posting_time": 1,
				"items": [
					{
						"item_code": item_code,
						"warehouse": self.context.warehouse,
						"qty": quantity(count),
						"valuation_rate": 0,
						"allow_zero_valuation_rate": 1,
					}
				],
			}
		)
		reconciliation.insert(ignore_permissions=True)
		reconciliation.submit()

	def _difference_account(self) -> str:
		"""The company's temporary (opening) account: a mirrored count is valued at zero, so it moves no
		money, and a balance-sheet account keeps it out of profit and loss (and is what ERPNext requires
		of a site's first reconciliation)."""
		account = frappe.db.get_value(
			"Account", {"company": self.context.company, "account_type": "Temporary", "is_group": 0}
		)
		if not account:
			raise Unsupported(f"{self.context.company} has no Temporary account for zero-valued stock counts")
		return str(account)

	def _website_item(self, item: Any, values: Values, groups: list[str], images: list[str | None]) -> str:
		name = frappe.db.get_value("Website Item", {"item_code": item.name})
		doc = frappe.get_doc("Website Item", name) if name else frappe.new_doc("Website Item")
		doc.update(
			{
				"item_code": item.name,
				"web_item_name": values.string("name"),
				"item_group": item.item_group,
				"published": int(values.flag("published")),
				"route": self.context.route(values.string("slug")),
				"webshop_store": self.context.webshop_store,
				"brand": item.brand,
				"website_image": images[0] if images else None,
				"short_description": values.optional_string("short_description"),
				"web_long_description": values.optional_string("description"),
			}
		)
		doc.set("website_item_groups", [{"item_group": group} for group in groups[1:]])
		doc.set(
			"website_specifications",
			[
				{"label": spec.string("label"), "description": spec.string("value")}
				for spec in values.records("specifications")
			],
		)
		doc.save(ignore_permissions=True)
		return str(doc.route)
