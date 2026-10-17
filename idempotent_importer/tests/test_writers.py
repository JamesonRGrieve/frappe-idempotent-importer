# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ERPNext writers on a real site, through the one map → hash → write path (ObjectSync) with the real
link store: each writer's create, skip (an unchanged object writes nothing), update and retire paths, and
that an update writes only mapped fields (what a person changed elsewhere on the record is kept).

Run by the end-to-end suite once its businesses exist (``bench run-tests --module
idempotent_importer.tests.test_writers``): every test works on its own connection ("writer-test", bound to
the first business's Public Site) and is rolled back afterwards."""

from __future__ import annotations

import re
import struct
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from typing import Any

import frappe
from frappe.desk.doctype.tag.tag import DocTags
from frappe.tests import IntegrationTestCase

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.sync import Handler, ObjectSync, Outcome
from idempotent_importer.erpnext.context import ConnectionContext
from idempotent_importer.erpnext.media import Media
from idempotent_importer.erpnext.stores import FrappeLinkStore
from idempotent_importer.erpnext.writers.base import Writer
from idempotent_importer.erpnext.writers.catalog import CollectionWriter, ProductWriter
from idempotent_importer.erpnext.writers.content import (
	ArticleWriter,
	AssetWriter,
	BlogCategoryWriter,
	CommentWriter,
	LegacyRedirects,
	PageWriter,
	RedirectWriter,
	ReviewWriter,
)
from idempotent_importer.erpnext.writers.discounts import DiscountWriter
from idempotent_importer.erpnext.writers.parties import CustomerWriter
from idempotent_importer.erpnext.writers.sales import DraftOrderWriter, OrderWriter
from idempotent_importer.erpnext.writers.shipping import DeliveryProfileWriter

TEMPLATE = "woo-a"
CONNECTION = "writer-test"
ABBREVIATION = "WT"
SOURCE = "woocommerce"


def png() -> bytes:
	"""A real 1x1 PNG: Webshop makes thumbnails of a Website Item's image."""

	def chunk(kind: bytes, data: bytes) -> bytes:
		return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

	header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
	pixels = zlib.compress(bytes([0, 200, 120, 40]))
	return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


def gid(type_: str, id_: str) -> str:
	return f"gid://{SOURCE}/{CONNECTION}/{type_}/{id_}"


class PngTransport:
	"""Media downloads, answered with a tiny binary image (no network)."""

	def get(self, url: str, headers: dict[str, str], timeout_seconds: float) -> Any:
		raise AssertionError("writers never read the source")

	def post(self, url: str, headers: dict[str, str], body: bytes, timeout_seconds: float) -> Any:
		raise AssertionError("writers never read the source")

	def download(self, url: str, timeout_seconds: float, max_bytes: int) -> bytes:
		return png()


class GivenMapper:
	"""Maps a node to the Desired the test handed it (writers are what is under test, not mappers)."""

	def __init__(self, kind: Kind) -> None:
		self.kind = kind
		self.version = 1
		self.desired: dict[str, Desired | None] = {}

	def map(self, node: Node) -> Desired | None:
		return self.desired[node.id()]


@contextmanager
def savepoint_scope() -> Iterator[None]:
	"""One object's writes, undone on failure, but never committed: the test rolls everything back."""
	frappe.db.savepoint("writer_test")
	try:
		yield
	except BaseException:
		frappe.db.rollback(save_point="writer_test")
		raise


def modified(doctype: str, name: str) -> Any:
	return frappe.db.get_value(doctype, name, "modified")


class TestWriters(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		self.addCleanup(frappe.db.rollback)
		template = frappe.get_doc("Importer Connection", TEMPLATE)
		doc = frappe.copy_doc(template)
		doc.update(
			{
				"connection_id": CONNECTION,
				"abbreviation": ABBREVIATION,
				"cursors": None,
				"last_run": None,
				"lock_run": None,
			}
		)
		doc.insert()
		self.context = ConnectionContext.of(doc, SOURCE)
		self.links = FrappeLinkStore(CONNECTION)
		self.media = Media(self.context, self.links, PngTransport())

	# -- helpers ----------------------------------------------------------------------------------------

	def sync_of(self, kind: Kind, writer: type[Writer]) -> tuple[ObjectSync, GivenMapper]:
		mapper = GivenMapper(kind)
		handler = Handler(mapper, writer(self.context, self.links, self.media))
		sync = ObjectSync(
			{kind: handler}, self.links, savepoint_scope, LegacyRedirects(self.context, self.links)
		)
		return sync, mapper

	def mirror(self, kind: Kind, writer: type[Writer], key: str, fields: Fields, *legacy: str) -> Outcome:
		sync, mapper = self.sync_of(kind, writer)
		mapper.desired[key] = Desired(kind, key, fields, legacy)
		return sync.sync(kind, Node({"id": key}), False)

	def target(self, kind: Kind, key: str) -> str:
		name = self.links.target(kind, key)
		self.assertIsNotNone(name, f"{key} is linked")
		return str(name)

	def assert_lifecycle(
		self, kind: Kind, writer: type[Writer], key: str, first: Fields, changed: Fields
	) -> tuple[str, str]:
		"""Created, skipped when unchanged (nothing written), updated when changed; returns the record."""
		self.assertEqual(self.mirror(kind, writer, key, first), Outcome.CREATED)
		link = self.links.find(kind, key)
		assert link is not None
		doctype, name = link.target.doctype, link.target.name
		before = modified(doctype, name)
		self.assertEqual(self.mirror(kind, writer, key, first), Outcome.SKIPPED)
		self.assertEqual(modified(doctype, name), before, "an unchanged object writes nothing")
		self.assertEqual(self.mirror(kind, writer, key, changed), Outcome.UPDATED)
		updated = self.links.find(kind, key)
		assert updated is not None
		return updated.target.doctype, updated.target.name

	def retire(self, kind: Kind, writer: type[Writer], key: str) -> Outcome:
		sync, _ = self.sync_of(kind, writer)
		return sync.retire(kind, key, False)

	# -- catalog ----------------------------------------------------------------------------------------

	def collection(self, name: str = "Hats", slug: str = "hats") -> Fields:
		return {"name": name, "slug": slug, "parent": None, "description": "<p>Warm</p>", "image": None}

	def test_collections(self) -> None:
		key = gid("ProductCategory", "1")
		_, name = self.assert_lifecycle(
			Kind.COLLECTION, CollectionWriter, key, self.collection(), self.collection("Toques", "toques")
		)
		self.assertEqual(name, "Toques - WT", "a renamed collection renames its Item Group")
		group = frappe.get_doc("Item Group", name)
		self.assertEqual(
			(group.parent_item_group, group.route, group.show_in_website),
			(self.context.item_group, f"{self.context.route_prefix}/toques", 1),
		)
		frappe.db.set_value("Item Group", name, "is_group", 1)
		self.mirror(
			Kind.COLLECTION,
			CollectionWriter,
			key,
			{**self.collection("Toques", "toques"), "image": None, "description": "New"},
		)
		self.assertEqual(frappe.db.get_value("Item Group", name, "is_group"), 1, "an unmapped field is kept")
		self.assertEqual(self.retire(Kind.COLLECTION, CollectionWriter, key), Outcome.RETIRED)
		self.assertEqual(frappe.db.get_value("Item Group", name, "show_in_website"), 0)

	def simple_product(self, price: str = "25", stock: str = "12", sale: Fields | None = None) -> Fields:
		return {
			"type": "simple",
			"name": "Wool Toque",
			"slug": "wool-toque",
			"description": "<p>Warm</p>",
			"short_description": "Warm",
			"sku": "WTEST-TOQUE",
			"barcode": "0000000000999",
			"brand": "Writer Test Brand",
			"collections": [],
			"images": [{"url": "https://media.example.test/toque.png", "alt": None}],
			"published": True,
			"is_stock_item": True,
			"weight": {"value": Decimal("0.2"), "unit": "kg"},
			"price": Decimal(price),
			"sale": sale,
			"stock_qty": Decimal(stock),
			"attributes": [],
			"specifications": [{"label": "Material", "value": "Wool"}],
			"variants": [],
		}

	def price_of(self, item: str) -> Decimal:
		rate = frappe.db.get_value(
			"Item Price", {"item_code": item, "price_list": self.context.price_list}, "price_list_rate"
		)
		return Decimal(str(rate))

	def stock_of(self, item: str) -> Decimal:
		qty = frappe.db.get_value(
			"Bin", {"item_code": item, "warehouse": self.context.warehouse}, "actual_qty"
		)
		return Decimal(str(qty or 0))

	def test_a_simple_product(self) -> None:
		key = gid("Product", "1")
		sale: Fields = {"price": Decimal("20"), "from": None, "to": None}
		_, item = self.assert_lifecycle(
			Kind.PRODUCT, ProductWriter, key, self.simple_product(), self.simple_product("27", "9", sale)
		)
		self.assertEqual(item, "WT-WTEST-TOQUE")
		self.assertEqual((self.price_of(item), self.stock_of(item)), (Decimal(27), Decimal(9)))
		rule = self.target(Kind.PRICE, f"{key}#sale")
		self.assertEqual(Decimal(str(frappe.db.get_value("Pricing Rule", rule, "rate"))), Decimal(20))
		doc = frappe.get_doc("Item", item)
		self.assertEqual(
			(doc.brand, doc.weight_uom, [row.barcode for row in doc.barcodes], bool(doc.image)),
			("Writer Test Brand", "Kg", ["0000000000999"], True),
		)
		website = frappe.get_doc("Website Item", {"item_code": item})
		self.assertEqual(
			(website.published, website.route, website.webshop_store),
			(1, f"{self.context.route_prefix}/wool-toque", self.context.webshop_store),
		)
		frappe.db.set_value("Item", item, "shelf_life_in_days", 30)
		self.mirror(Kind.PRODUCT, ProductWriter, key, self.simple_product("28", "9", None))
		self.assertEqual(
			frappe.db.get_value("Item", item, "shelf_life_in_days"), 30, "an unmapped field is kept"
		)
		self.assertEqual(
			frappe.db.get_value("Pricing Rule", rule, "disable"), 1, "an ended sale disables its rule"
		)
		self.assertEqual(self.retire(Kind.PRODUCT, ProductWriter, key), Outcome.RETIRED)
		self.assertEqual(frappe.db.get_value("Item", item, "disabled"), 1)
		self.assertEqual(frappe.db.get_value("Website Item", {"item_code": item}, "published"), 0)

	def variable_product(self, variants: list[str]) -> Fields:
		return {
			**self.simple_product(),
			"type": "variable",
			"name": "Writer Hoodie",
			"slug": "writer-hoodie",
			"sku": None,
			"barcode": None,
			"weight": None,
			"price": None,
			"stock_qty": None,
			"attributes": [{"name": "Writer Size", "values": ["S", "M"]}],
			"variants": [
				{
					"gid": gid("ProductVariation", size),
					"sku": f"WTEST-HOODIE-{size}",
					"barcode": None,
					"attributes": {"Writer Size": size},
					"price": Decimal(50),
					"sale": None,
					"stock_qty": Decimal(3),
					"weight": None,
					"image": None,
					"enabled": True,
				}
				for size in variants
			],
		}

	def test_a_product_with_variants(self) -> None:
		key = gid("Product", "2")
		_, template = self.assert_lifecycle(
			Kind.PRODUCT, ProductWriter, key, self.variable_product(["S", "M"]), self.variable_product(["S"])
		)
		self.assertEqual(frappe.db.get_value("Item", template, "has_variants"), 1)
		small = self.target(Kind.VARIANT, f"{key}#{gid('ProductVariation', 'S')}")
		self.assertEqual(
			(small, self.price_of(small), self.stock_of(small)),
			("WT-WTEST-HOODIE-S", Decimal(50), Decimal(3)),
		)
		medium = frappe.db.get_value("Item", "WT-WTEST-HOODIE-M", "disabled")
		self.assertEqual(medium, 1, "a variant removed at the source is disabled")
		self.retire(Kind.PRODUCT, ProductWriter, key)
		self.assertEqual(frappe.db.get_value("Item", small, "disabled"), 1)

	# -- parties ----------------------------------------------------------------------------------------

	def customer(self, name: str = "Avery Writer") -> Fields:
		first, last = name.split(" ", 1)
		return {
			"name": name,
			"type": "Individual",
			"first_name": first,
			"last_name": last,
			"company": None,
			"email": "avery.writer@example.test",
			"phone": "+1 403 555 0100",
			"addresses": [
				{
					"address_type": "Billing",
					"name": name,
					"company": None,
					"line1": "1 Main St",
					"line2": None,
					"city": "Calgary",
					"state": "AB",
					"postcode": "T2P 1J9",
					"country_code": "CA",
					"email": None,
					"phone": None,
				}
			],
		}

	def test_customers(self) -> None:
		key = gid("Customer", "1")
		_, name = self.assert_lifecycle(
			Kind.CUSTOMER, CustomerWriter, key, self.customer(), self.customer("Avery Writes")
		)
		customer = frappe.get_doc("Customer", name)
		self.assertEqual(customer.customer_name, "Avery Writes")
		self.assertTrue(customer.customer_primary_contact and customer.customer_primary_address)
		self.assertEqual(
			frappe.db.get_value("Address", customer.customer_primary_address, "country"), "Canada"
		)
		frappe.db.set_value("Customer", name, "tax_id", "123456789")
		self.mirror(Kind.CUSTOMER, CustomerWriter, key, self.customer("Avery Wrote"))
		self.assertEqual(
			frappe.db.get_value("Customer", name, "tax_id"), "123456789", "an unmapped field is kept"
		)
		self.retire(Kind.CUSTOMER, CustomerWriter, key)
		self.assertEqual(frappe.db.get_value("Customer", name, "disabled"), 1)

	# -- discounts --------------------------------------------------------------------------------------

	def discount(self, codes: list[Field], value: str = "10") -> Fields:
		return {
			"codes": codes,
			"description": "Writer test",
			"discount": "percentage",
			"applies": "transaction",
			"value": Decimal(value),
			"items": [],
			"groups": [],
			"min_amount": None,
			"valid_from": None,
			"valid_upto": None,
			"max_uses": 5,
		}

	def test_discounts(self) -> None:
		key = gid("Coupon", "1")
		_, rule = self.assert_lifecycle(
			Kind.DISCOUNT,
			DiscountWriter,
			key,
			self.discount(["WTEST10", "WTEST11"]),
			self.discount(["WTEST10"], "15"),
		)
		self.assertEqual(
			Decimal(str(frappe.db.get_value("Pricing Rule", rule, "discount_percentage"))), Decimal(15)
		)
		coupon = frappe.get_doc("Coupon Code", self.target(Kind.COUPON, f"{key}#WTEST10"))
		self.assertEqual(
			(coupon.coupon_name, coupon.webshop_store, coupon.pricing_rule, coupon.maximum_use),
			("WTEST10 - WT", self.context.webshop_store, rule, 5),
		)
		dropped = frappe.db.get_value("Coupon Code", {"coupon_name": "WTEST11 - WT"}, "valid_upto")
		self.assertIsNotNone(dropped, "a code dropped at the source expires")
		frappe.db.set_value("Pricing Rule", rule, "priority", "5")
		self.mirror(Kind.DISCOUNT, DiscountWriter, key, self.discount(["WTEST10"], "20"))
		self.assertEqual(
			frappe.db.get_value("Pricing Rule", rule, "priority"), "5", "an unmapped field is kept"
		)
		self.retire(Kind.DISCOUNT, DiscountWriter, key)
		self.assertEqual(frappe.db.get_value("Pricing Rule", rule, "disable"), 1)

	def test_a_code_held_on_the_store_is_not_taken_over(self) -> None:
		frappe.get_doc(
			{
				"doctype": "Coupon Code",
				"coupon_name": "WTESTHELD by hand",
				"coupon_code": "WTESTHELD",
				"coupon_type": "Promotional",
				"pricing_rule": self.make_rule(),
				"webshop_store": self.context.webshop_store,
			}
		).insert()
		self.assertEqual(
			self.mirror(Kind.DISCOUNT, DiscountWriter, gid("Coupon", "2"), self.discount(["WTESTHELD"])),
			Outcome.UNSUPPORTED,
		)

	def make_rule(self) -> str:
		rule = frappe.get_doc(
			{
				"doctype": "Pricing Rule",
				"title": "Writer test held",
				"apply_on": "Transaction",
				"price_or_product_discount": "Price",
				"coupon_code_based": 1,
				"selling": 1,
				"company": self.context.company,
				"rate_or_discount": "Discount Percentage",
				"discount_percentage": 5,
			}
		).insert()
		return str(rule.name)

	def test_a_connection_without_a_store_mirrors_no_codes(self) -> None:
		context = replace(self.context, webshop_store=None)
		writer = DiscountWriter(context, self.links, self.media)
		before = frappe.db.count("Pricing Rule")
		with self.assertRaisesRegex(Unsupported, "no Webshop Store"):
			writer.write(Desired(Kind.DISCOUNT, gid("Coupon", "3"), self.discount(["WTESTNOSTORE"])), None)
		self.assertEqual(frappe.db.count("Pricing Rule"), before, "refused before writing anything")

	# -- sales ------------------------------------------------------------------------------------------

	def order(self, refunds: list[Field] | None = None, qty: str = "2") -> Fields:
		line_total = Decimal(qty) * Decimal(25)
		return {
			"number": "WT-1001",
			"currency": self.context.currency,
			"created_at": "2026-06-01T18:00:00Z",
			"submitted": True,
			"customer": gid("Customer", "1"),
			"guest": None,
			"billing": None,
			"shipping": None,
			"lines": [
				{
					"source_line": gid("OrderItem", "1"),
					"product": gid("Product", "1"),
					"variant": None,
					"name": "Wool Toque",
					"qty": Decimal(qty),
					"rate": Decimal(25),
				}
			],
			"discount": Decimal(0),
			"charges": [{"kind": "tax", "description": "GST", "amount": Decimal("2.50")}],
			"total": line_total + Decimal("2.50"),
			"payment": {"method": "card", "reference": "ch_1", "date": "2026-06-01T18:01:00Z"},
			"refunds": refunds or [],
			"notes": [
				{
					"gid": gid("OrderNote", "1"),
					"date": "2026-06-01T18:02:00Z",
					"author": "Staff",
					"text": "Gift wrap please",
					"customer_note": False,
				}
			],
			"remarks": None,
		}

	def refund(self) -> Fields:
		return {
			"gid": gid("OrderRefund", "1"),
			"date": "2026-06-03T12:00:00Z",
			"reason": "Too small",
			"amount": Decimal("26.25"),
			"lines": [
				{
					"source_line": gid("OrderItem", "1"),
					"product": gid("Product", "1"),
					"variant": None,
					"name": "Wool Toque",
					"qty": Decimal(-1),
					"rate": Decimal(25),
				}
			],
			"charges": [{"kind": "tax", "description": "GST", "amount": Decimal("-1.25")}],
		}

	def test_orders(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		key = gid("Order", "1")
		_, invoice = self.assert_lifecycle(
			Kind.ORDER, OrderWriter, key, self.order(), self.order([self.refund()])
		)
		booked = frappe.get_doc("Sales Invoice", invoice)
		self.assertEqual(
			(
				booked.docstatus,
				Decimal(str(booked.grand_total)),
				Decimal(str(booked.outstanding_amount)),
				booked.update_stock,
			),
			(1, Decimal("52.5"), Decimal(0), 0),
		)
		self.assertIsNone(booked.amended_from, "a refund doesn't amend the invoice")
		credit = frappe.get_doc("Sales Invoice", self.target(Kind.REFUND, f"{key}#{gid('OrderRefund', '1')}"))
		self.assertEqual(
			(credit.is_return, credit.docstatus, Decimal(str(credit.grand_total))), (1, 1, Decimal("-26.25"))
		)
		self.assertEqual(
			frappe.db.get_value(
				"Payment Entry",
				self.target(Kind.REFUND_PAYMENT, f"{key}#{gid('OrderRefund', '1')}#payment"),
				"payment_type",
			),
			"Pay",
		)
		self.assertEqual(
			frappe.db.count(
				"Comment",
				{"reference_doctype": "Sales Invoice", "reference_name": invoice, "comment_type": "Comment"},
			),
			1,
		)

		self.assertEqual(
			self.mirror(Kind.ORDER, OrderWriter, key, self.order([self.refund()], qty="3")), Outcome.UPDATED
		)
		amended = self.target(Kind.ORDER, key)
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", amended, "amended_from"),
			invoice,
			"a changed sale is cancelled and amended",
		)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, "docstatus"), 2)

		self.retire(Kind.ORDER, OrderWriter, key)
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", amended, "docstatus"),
			2,
			"a cancelled order's invoice is cancelled",
		)
		payment = frappe.db.get_value(
			"Importer Link",
			{"connection": CONNECTION, "kind": Kind.PAYMENT.value, "gid": f"{key}#payment"},
			"target_name",
		)
		self.assertEqual(frappe.db.get_value("Payment Entry", payment, "docstatus"), 2)

	def test_an_order_whose_books_would_not_match_is_not_kept(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		before = frappe.db.count("Sales Invoice")
		wrong = {**self.order(), "total": Decimal("99.99")}
		self.assertEqual(self.mirror(Kind.ORDER, OrderWriter, gid("Order", "2"), wrong), Outcome.FAILED)
		self.assertEqual(frappe.db.count("Sales Invoice"), before, "rolled back")
		other = {**self.order(), "currency": "XAF"}
		self.assertEqual(self.mirror(Kind.ORDER, OrderWriter, gid("Order", "3"), other), Outcome.UNSUPPORTED)

	def test_an_order_waits_for_its_product(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.assertEqual(
			self.mirror(Kind.ORDER, OrderWriter, gid("Order", "4"), self.order()), Outcome.DEFERRED
		)

	def included_tax_order(self, tax: str = "13") -> Fields:
		return {
			**self.order(),
			"lines": [
				{
					"source_line": gid("OrderItem", "9"),
					"product": gid("Product", "1"),
					"variant": None,
					"name": "Wool Toque",
					"qty": Decimal(1),
					"rate": Decimal(113),
				}
			],
			"charges": [
				{"kind": "included_tax", "description": "HST", "amount": Decimal(tax), "rate": Decimal(13)},
				{"kind": "shipping", "description": "Standard", "amount": Decimal(10)},
				{
					"kind": "shipping_tax",
					"description": "HST (shipping)",
					"amount": Decimal("1.30"),
					"rate": Decimal(13),
				},
			],
			"total": Decimal("124.30"),
			"taxes_included": True,
		}

	def test_an_order_whose_prices_include_tax(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		key = gid("Order", "5")
		self.assertEqual(
			self.mirror(Kind.ORDER, OrderWriter, key, self.included_tax_order()), Outcome.CREATED
		)
		invoice = frappe.get_doc("Sales Invoice", self.target(Kind.ORDER, key))
		self.assertEqual(
			(invoice.docstatus, Decimal(str(invoice.grand_total)), Decimal(str(invoice.net_total))),
			(1, Decimal("124.3"), Decimal(100)),
		)
		self.assertEqual(
			[
				(row.charge_type, row.included_in_print_rate, Decimal(str(row.tax_amount)))
				for row in invoice.taxes
			],
			[
				("On Net Total", 1, Decimal(13)),
				("Actual", 0, Decimal(10)),
				("On Previous Row Amount", 0, Decimal("1.3")),
			],
		)
		wrong = gid("Order", "6")
		self.assertEqual(
			self.mirror(Kind.ORDER, OrderWriter, wrong, self.included_tax_order("12.99")),
			Outcome.FAILED,
			"an included tax ERPNext computes differently isn't kept",
		)

	def test_an_order_in_another_currency(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		frappe.db.set_value("Currency", "USD", "enabled", 1)
		no_account = {
			**self.order(),
			"currency": "USD",
			"exchange_rate": Decimal("1.4"),
			"base_total": Decimal("73.50"),
		}
		self.assertEqual(
			self.mirror(Kind.ORDER, OrderWriter, gid("Order", "9"), no_account),
			Outcome.UNSUPPORTED,
			"no receivable account in the order's currency",
		)
		frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": "Debtors USD",
				"parent_account": f"Accounts Receivable - {frappe.db.get_value('Company', self.context.company, 'abbr')}",
				"company": self.context.company,
				"account_type": "Receivable",
				"account_currency": "USD",
			}
		).insert()
		key = gid("Order", "7")
		usd = {
			**self.order(),
			"currency": "USD",
			"exchange_rate": Decimal("1.4"),
			"base_total": Decimal("73.50"),
		}
		self.assertEqual(self.mirror(Kind.ORDER, OrderWriter, key, usd), Outcome.CREATED)
		invoice = frappe.get_doc("Sales Invoice", self.target(Kind.ORDER, key))
		self.assertEqual(
			(invoice.currency, Decimal(str(invoice.grand_total)), Decimal(str(invoice.base_grand_total))),
			("USD", Decimal("52.5"), Decimal("73.5")),
		)
		self.assertEqual(Decimal(str(invoice.outstanding_amount)), Decimal(0), "paid")
		no_rate = {**self.order(), "currency": "USD"}
		self.assertEqual(
			self.mirror(Kind.ORDER, OrderWriter, gid("Order", "8"), no_rate), Outcome.UNSUPPORTED
		)

	def draft(self, qty: str = "2") -> Fields:
		order = self.order(qty=qty)
		return {
			key: order[key]
			for key in (
				"number",
				"currency",
				"created_at",
				"customer",
				"guest",
				"billing",
				"shipping",
				"lines",
				"discount",
				"charges",
				"total",
				"remarks",
			)
		}

	def test_draft_orders(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		key = gid("DraftOrder", "1")
		_, name = self.assert_lifecycle(
			Kind.DRAFT_ORDER, DraftOrderWriter, key, self.draft(), self.draft("3")
		)
		quotation = frappe.get_doc("Quotation", name)
		self.assertEqual(
			(quotation.docstatus, quotation.quotation_to, Decimal(str(quotation.grand_total))),
			(0, "Customer", Decimal("77.5")),
		)
		self.assertEqual(quotation.party_name, self.target(Kind.CUSTOMER, gid("Customer", "1")))
		self.retire(Kind.DRAFT_ORDER, DraftOrderWriter, key)
		self.assertTrue(
			str(frappe.db.get_value("Quotation", name, "title")).startswith("[Removed at source]")
		)

	def profile(self, *rules: tuple[str, str]) -> Fields:
		return {
			"name": "General profile",
			"rules": [
				{
					"gid": gid("Rate", rate),
					"label": f"Standard {rate}",
					"countries": ["CA"],
					"amount": Decimal(amount),
					"enabled": True,
				}
				for rate, amount in rules
			],
		}

	def test_shipping_rates(self) -> None:
		key = gid("DeliveryProfile", "1")
		self.assertEqual(
			self.mirror(
				Kind.DELIVERY_PROFILE, DeliveryProfileWriter, key, self.profile(("1", "10"), ("2", "25"))
			),
			Outcome.CREATED,
		)
		standard = self.target(Kind.SHIPPING_RULE, f"{key}#{gid('Rate', '1')}")
		rule = frappe.get_doc("Shipping Rule", standard)
		self.assertEqual(
			(
				rule.calculate_based_on,
				Decimal(str(rule.shipping_amount)),
				[c.country for c in rule.countries],
				rule.label,
			),
			("Fixed", Decimal(10), ["Canada"], "Standard 1 - WT"),
		)
		before = modified("Shipping Rule", standard)
		self.assertEqual(
			self.mirror(
				Kind.DELIVERY_PROFILE, DeliveryProfileWriter, key, self.profile(("1", "10"), ("2", "25"))
			),
			Outcome.SKIPPED,
		)
		self.mirror(Kind.DELIVERY_PROFILE, DeliveryProfileWriter, key, self.profile(("1", "12")))
		self.assertEqual(modified("Shipping Rule", standard) != before, True)
		self.assertEqual(
			Decimal(str(frappe.db.get_value("Shipping Rule", standard, "shipping_amount"))), Decimal(12)
		)
		self.assertEqual(
			frappe.db.get_value("Shipping Rule", "Standard 2 - WT", "disabled"),
			1,
			"a dropped rate is disabled",
		)
		self.retire(Kind.DELIVERY_PROFILE, DeliveryProfileWriter, key)
		self.assertEqual(frappe.db.get_value("Shipping Rule", standard, "disabled"), 1)

	def test_files(self) -> None:
		key = gid("File", "1")
		fields: Fields = {"url": "https://media.example.test/size-guide.png", "alt": None}
		self.assertEqual(self.mirror(Kind.ASSET, AssetWriter, key, fields), Outcome.CREATED)
		name = self.target(Kind.ASSET, key)
		self.assertEqual(
			frappe.db.get_value("File", name, ["file_name", "is_private"]), ("size-guide.png", 0)
		)
		self.assertEqual(self.mirror(Kind.ASSET, AssetWriter, key, fields), Outcome.SKIPPED)
		self.retire(Kind.ASSET, AssetWriter, key)
		self.assertTrue(frappe.db.exists("File", name), "a file removed at the source stays")

	def test_tags(self) -> None:
		key = gid("Product", "1")
		self.mirror(Kind.PRODUCT, ProductWriter, key, {**self.simple_product(), "tags": ["Wool", "Winter"]})
		item = self.target(Kind.PRODUCT, key)
		DocTags("Item").add(item, "Staff pick")
		self.mirror(Kind.PRODUCT, ProductWriter, key, {**self.simple_product(), "tags": ["Wool"]})
		self.assertEqual(
			sorted(DocTags("Item").get_tags(item).split(",")),
			["Staff pick", "Wool"],
			"a tag the source dropped is removed; one a person added is kept",
		)

	def test_images_in_pages_are_localised(self) -> None:
		key = gid("Page", "9")
		html = '<p><img src="https://media.example.test/workshop.png" alt="W"></p>'
		fields: Fields = {
			**self.page("Workshop", html),
			"slug": "workshop-writer",
			"images": ["https://media.example.test/workshop.png"],
		}
		self.mirror(Kind.PAGE, PageWriter, key, fields)
		stored = str(frappe.db.get_value("Web Page", self.target(Kind.PAGE, key), "main_section_html"))
		self.assertNotIn("media.example.test", stored)
		self.assertIn('src="/files/workshop', stored)

	# -- content ----------------------------------------------------------------------------------------

	def page(self, title: str, html: str = "<p>Hi</p>") -> Fields:
		return {"title": title, "slug": "about-writer", "parent": None, "published": True, "html": html}

	def test_pages_and_their_old_urls(self) -> None:
		key = gid("Page", "1")
		self.assertEqual(
			self.mirror(Kind.PAGE, PageWriter, key, self.page("About"), "/old-about"), Outcome.CREATED
		)
		name = self.target(Kind.PAGE, key)
		route = f"{self.context.route_prefix}/about-writer"
		self.assertEqual(
			(name, frappe.db.get_value("Web Page", name, "route")), (route.replace("/", "-"), route)
		)
		redirect = self.target(Kind.LEGACY_REDIRECT, f"{key}#legacy:/old-about")
		self.assertEqual(frappe.db.get_value("Website Route Redirect", redirect, "target"), f"/{route}")
		before = modified("Web Page", name)
		self.assertEqual(
			self.mirror(Kind.PAGE, PageWriter, key, self.page("About"), "/old-about"), Outcome.SKIPPED
		)
		self.assertEqual(modified("Web Page", name), before)
		frappe.db.set_value("Web Page", name, "meta_title", "Kept")
		self.assertEqual(self.mirror(Kind.PAGE, PageWriter, key, self.page("About us")), Outcome.UPDATED)
		self.assertEqual(frappe.db.get_value("Web Page", name, ["title", "meta_title"]), ("About us", "Kept"))
		self.assertFalse(
			frappe.db.exists("Website Route Redirect", redirect),
			"an old URL the source dropped stops redirecting",
		)
		self.retire(Kind.PAGE, PageWriter, key)
		self.assertEqual(frappe.db.get_value("Web Page", name, "published"), 0)

	def test_blogs_articles_and_comments(self) -> None:
		blog = gid("Category", "1")
		_, category = self.assert_lifecycle(
			Kind.BLOG,
			BlogCategoryWriter,
			blog,
			{"title": "News", "slug": "news", "description": None},
			{"title": "News", "slug": "news", "description": "<p>All the news</p>"},
		)
		self.assertEqual(frappe.db.get_value("Blog Category", category, "description"), "All the news")
		article = gid("Post", "1")
		fields: Fields = {
			"title": "Hello",
			"slug": "hello-writer",
			"category": blog,
			"author": {"key": "sam", "name": "Sam"},
			"published": True,
			"published_on": "2026-05-01",
			"html": "<p>Hello</p>",
			"intro": "<p>Hi</p>",
		}
		_, post = self.assert_lifecycle(
			Kind.ARTICLE, ArticleWriter, article, fields, {**fields, "html": "<p>Hello again</p>"}
		)
		self.assertEqual(
			frappe.db.get_value("Blogger", frappe.db.get_value("Blog Post", post, "blogger"), "short_name"),
			"wt-sam",
		)
		frappe.db.set_value("Blog Post", post, "disable_comments", 1)
		self.mirror(Kind.ARTICLE, ArticleWriter, article, {**fields, "title": "Hello there"})
		self.assertEqual(
			frappe.db.get_value("Blog Post", post, "disable_comments"), 1, "an unmapped field is kept"
		)
		comment = gid("Comment", "1")
		said: Fields = {
			"article": article,
			"author_name": "Rae",
			"author_email": None,
			"html": "Nice",
			"published_on": "2026-05-02",
		}
		_, name = self.assert_lifecycle(
			Kind.COMMENT, CommentWriter, comment, said, {**said, "html": "Very nice"}
		)
		self.assertEqual(
			frappe.db.get_value("Comment", name, ["reference_name", "content"]), (post, "Very nice")
		)
		for kind, writer, key, doctype, record in (
			(Kind.COMMENT, CommentWriter, comment, "Comment", name),
			(Kind.ARTICLE, ArticleWriter, article, "Blog Post", post),
			(Kind.BLOG, BlogCategoryWriter, blog, "Blog Category", category),
		):
			self.retire(kind, writer, key)
			self.assertEqual(frappe.db.get_value(doctype, record, "published"), 0)

	def test_reviews(self) -> None:
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		key = gid("ProductReview", "1")
		said: Fields = {
			"product": gid("Product", "1"),
			"author_name": "Rae",
			"author_email": None,
			"text": "Warm",
			"rating": Decimal("0.8"),
			"published_on": "2026-05-02",
		}
		_, review = self.assert_lifecycle(Kind.REVIEW, ReviewWriter, key, said, {**said, "text": "Very warm"})
		self.assertEqual(frappe.db.get_value("Item Review", review, "rating"), 0.8)
		self.retire(Kind.REVIEW, ReviewWriter, key)
		title = str(frappe.db.get_value("Item Review", review, "review_title"))
		self.assertTrue(title.startswith("[Removed at source]"), "a review is marked, never deleted")

	def test_redirects(self) -> None:
		key = gid("Redirect", "1")
		first: Fields = {"source": "/writer-old", "target": "/writer-new", "status": 301}
		_, row = self.assert_lifecycle(
			Kind.URL_REDIRECT,
			RedirectWriter,
			key,
			first,
			{**first, "target": "https://elsewhere.example.test/x"},
		)
		self.assertEqual(
			frappe.db.get_value("Website Route Redirect", row, ["source", "target"]),
			(re.escape(f"{self.context.route_prefix}/writer-old"), "https://elsewhere.example.test/x"),
		)
		self.retire(Kind.URL_REDIRECT, RedirectWriter, key)
		self.assertFalse(frappe.db.exists("Website Route Redirect", row))
