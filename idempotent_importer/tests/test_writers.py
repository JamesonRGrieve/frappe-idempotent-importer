# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ERPNext writers on a real site, through the one map → hash → write path (ObjectSync) with the real
link store: each writer's create, skip (an unchanged object writes nothing), update and retire paths, and
that an update writes only mapped fields (what a person changed elsewhere on the record is kept).

Run by the end-to-end suite once its businesses exist (``bench run-tests --module
idempotent_importer.tests.test_writers``): every test works on its own connection ("writer-test", bound to
the first business's Public Site) and is rolled back afterwards."""

from __future__ import annotations

import json
import re
import struct
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import frappe
from erpnext.accounts.doctype.pricing_rule.utils import validate_coupon_code
from frappe.desk.doctype.tag.tag import DocTags
from frappe.tests import IntegrationTestCase
from frappe.utils import add_days, nowdate

from idempotent_importer import contract
from idempotent_importer.contract import (
	Allocation,
	Code,
	Condition,
	ContactPerson,
	Discount,
	Employee,
	JournalLine,
	Lead,
	LedgerAccount,
	Opportunity,
	Party,
	Payment,
	Tier,
	TradeDocument,
	TradeLine,
	TradeTax,
	account_fields,
	discount_fields,
	employee_fields,
	journal_fields,
	lead_fields,
	opportunity_fields,
	order_line,
	party_fields,
	payment_fields,
	project_fields,
	trade_fields,
	tree_fields,
	warehouse_fields,
)
from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.fields import Field, Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import Node
from idempotent_importer.core.sync import Handler, ObjectSync, Outcome, Target
from idempotent_importer.erpnext.context import ConnectionContext
from idempotent_importer.erpnext.media import Media
from idempotent_importer.erpnext.stores import FrappeLinkStore
from idempotent_importer.erpnext.writers import stored_value
from idempotent_importer.erpnext.writers.base import Writer
from idempotent_importer.erpnext.writers.books import (
	AccountWriter,
	CostCenterWriter,
	JournalEntryWriter,
	PaymentEntryWriter,
	ProjectWriter,
	WarehouseWriter,
)
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
from idempotent_importer.erpnext.writers.crm import LeadWriter, OpportunityWriter
from idempotent_importer.erpnext.writers.discounts import DiscountWriter
from idempotent_importer.erpnext.writers.hr import DepartmentWriter, DesignationWriter, EmployeeWriter
from idempotent_importer.erpnext.writers.parties import CustomerGroupWriter, CustomerWriter, SupplierWriter
from idempotent_importer.erpnext.writers.sales import MULTI_CURRENCY_SETTING, DraftOrderWriter, OrderWriter
from idempotent_importer.erpnext.writers.shipping import DeliveryProfileWriter
from idempotent_importer.erpnext.writers.trade import (
	PurchaseInvoiceWriter,
	PurchaseOrderWriter,
	SalesInvoiceWriter,
	SalesOrderWriter,
)

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


def address_of(party: Fields) -> Fields:
	"""A party's first address."""
	addresses = party["addresses"]
	assert isinstance(addresses, list) and isinstance(addresses[0], dict)
	return addresses[0]


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
		self.assertEqual(
			(
				frappe.db.get_value("Pricing Rule", rule, "disable"),
				coupon.importer_hold,
				coupon.importer_conditions,
			),
			(0, None, None),
			"a discount with no unenforced condition is enabled",
		)
		validate_coupon_code(coupon.name)
		dropped = frappe.db.get_value("Coupon Code", {"coupon_name": "WTEST11 - WT"}, "valid_upto")
		self.assertIsNotNone(dropped, "a code dropped at the source expires")
		frappe.db.set_value("Pricing Rule", rule, "priority", "5")
		self.mirror(Kind.DISCOUNT, DiscountWriter, key, self.discount(["WTEST10"], "20"))
		self.assertEqual(
			frappe.db.get_value("Pricing Rule", rule, "priority"), "5", "an unmapped field is kept"
		)
		self.retire(Kind.DISCOUNT, DiscountWriter, key)
		self.assertEqual(frappe.db.get_value("Pricing Rule", rule, "disable"), 1)

	def test_automatic_discounts_for_a_customer_group(self) -> None:
		group = gid("CustomerGroup", "1")
		self.assertEqual(
			self.mirror(Kind.CUSTOMER_GROUP, CustomerGroupWriter, group, {"name": "Wholesale"}),
			Outcome.CREATED,
		)
		self.assertEqual(self.target(Kind.CUSTOMER_GROUP, group), "Wholesale - WT")
		customer = {**self.customer(), "group": group}
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), customer)
		self.assertEqual(
			frappe.db.get_value(
				"Customer", self.target(Kind.CUSTOMER, gid("Customer", "1")), "customer_group"
			),
			"Wholesale - WT",
		)
		automatic: Fields = {
			**self.discount([], "12"),
			"applies": "all_items",
			"automatic": True,
			"min_qty": Decimal(3),
			"mixed": True,
			"customer_groups": [group],
			"max_uses": None,
		}
		_, rule = self.assert_lifecycle(
			Kind.DISCOUNT,
			DiscountWriter,
			gid("Discount", "2"),
			automatic,
			{**automatic, "value": Decimal(15)},
		)
		doc = frappe.get_doc("Pricing Rule", rule)
		self.assertEqual(
			(
				doc.coupon_code_based,
				doc.apply_on,
				[row.item_group for row in doc.item_groups],
				doc.applicable_for,
				doc.customer_group,
				doc.min_qty,
				doc.mixed_conditions,
				Decimal(str(doc.discount_percentage)),
			),
			(
				0,
				"Item Group",
				[self.context.item_group],
				"Customer Group",
				"Wholesale - WT",
				3,
				1,
				Decimal(15),
			),
		)

	def test_a_free_item_and_a_discount_on_other_items(self) -> None:
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		third_free: Fields = {
			**self.discount([], "0"),
			"applies": "items",
			"items": [gid("Product", "1")],
			"automatic": True,
			"min_qty": Decimal(2),
			"free": {"qty": Decimal(1), "recursive": True, "recurse_for": Decimal(2)},
			"max_uses": None,
		}
		self.assertEqual(
			self.mirror(Kind.DISCOUNT, DiscountWriter, gid("Discount", "3"), third_free), Outcome.CREATED
		)
		rule = frappe.get_doc("Pricing Rule", self.target(Kind.DISCOUNT, gid("Discount", "3")))
		self.assertEqual(
			(
				rule.price_or_product_discount,
				rule.same_item,
				rule.free_qty,
				rule.is_recursive,
				rule.recurse_for,
			),
			("Product", 1, 1, 1, 2),
		)
		other: Fields = {
			**self.discount(["WTOTHER"], "50"),
			"applies": "items",
			"items": [gid("Product", "1")],
			"other": {"applies": "items", "target": gid("Product", "1")},
		}
		self.assertEqual(
			self.mirror(Kind.DISCOUNT, DiscountWriter, gid("Discount", "4"), other), Outcome.CREATED
		)
		rule = frappe.get_doc("Pricing Rule", self.target(Kind.DISCOUNT, gid("Discount", "4")))
		self.assertEqual(
			(rule.apply_rule_on_other, rule.other_item_code, rule.coupon_code_based),
			("Item Code", self.target(Kind.PRODUCT, gid("Product", "1")), 1),
		)

	def test_tiers_and_several_customers_are_a_promotional_scheme(self) -> None:
		for number in ("1", "2"):
			self.mirror(
				Kind.CUSTOMER, CustomerWriter, gid("Customer", number), self.customer(f"Avery Writer{number}")
			)
		first: Fields = {
			"discount": "percentage",
			"value": Decimal(5),
			"min_qty": Decimal(10),
			"max_qty": Decimal(49),
		}
		second: Fields = {"discount": "percentage", "value": Decimal(10), "min_qty": Decimal(50)}
		tiered: Fields = {
			**self.discount([], "0"),
			"applies": "all_items",
			"automatic": True,
			"customers": [gid("Customer", "1"), gid("Customer", "2")],
			"tiers": [first, second],
			"max_uses": None,
		}
		key = gid("Discount", "5")
		_, scheme = self.assert_lifecycle(
			Kind.DISCOUNT, DiscountWriter, key, tiered, {**tiered, "tiers": [first]}
		)
		self.assertEqual(frappe.db.get_value("Promotional Scheme", scheme, "applicable_for"), "Customer")
		rules = frappe.get_all(
			"Pricing Rule",
			filters={"promotional_scheme": scheme},
			fields=["customer", "discount_percentage", "disable"],
		)
		live = sorted((r.customer, Decimal(str(r.discount_percentage))) for r in rules if not r.disable)
		self.assertEqual(
			len(live), 2, "one rule per customer for the tier left; the dropped tier is disabled"
		)
		self.retire(Kind.DISCOUNT, DiscountWriter, key)
		self.assertEqual(frappe.db.get_value("Promotional Scheme", scheme, "disable"), 1)

	def test_a_discount_moving_between_a_rule_and_a_scheme(self) -> None:
		for number in ("1", "2"):
			self.mirror(
				Kind.CUSTOMER, CustomerWriter, gid("Customer", number), self.customer(f"Avery Writer{number}")
			)
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		one: Fields = {
			**self.discount([], "10"),
			"automatic": True,
			"customers": [gid("Customer", "1")],
			"free": {"item": gid("Product", "1"), "qty": Decimal(1), "rate": Decimal(5)},
			"applies": "items",
			"items": [gid("Product", "1")],
			"max_uses": None,
		}
		key = gid("Discount", "6")
		self.assertEqual(self.mirror(Kind.DISCOUNT, DiscountWriter, key, one), Outcome.CREATED)
		rule = frappe.get_doc("Pricing Rule", self.target(Kind.DISCOUNT, key))
		self.assertEqual(
			(rule.applicable_for, rule.customer, rule.free_item, rule.free_item_rate, rule.same_item),
			(
				"Customer",
				self.target(Kind.CUSTOMER, gid("Customer", "1")),
				self.target(Kind.PRODUCT, gid("Product", "1")),
				5,
				0,
			),
		)
		both: Fields = {**one, "customers": [gid("Customer", "1"), gid("Customer", "2")]}
		self.assertEqual(self.mirror(Kind.DISCOUNT, DiscountWriter, key, both), Outcome.UPDATED)
		moved = self.links.find(Kind.DISCOUNT, key)
		assert moved is not None
		self.assertEqual(moved.target.doctype, "Promotional Scheme")
		self.assertEqual(
			frappe.db.get_value("Pricing Rule", rule.name, "disable"), 1, "the old rule is disabled"
		)
		self.assertEqual(
			self.mirror(Kind.DISCOUNT, DiscountWriter, gid("Discount", "7"), {**both, "codes": ["WTSCHEME"]}),
			Outcome.UNSUPPORTED,
			"a code applies one Pricing Rule",
		)
		group = gid("CustomerGroup", "2")
		self.mirror(Kind.CUSTOMER_GROUP, CustomerGroupWriter, group, {"name": "Retail"})
		self.assertEqual(
			self.mirror(
				Kind.DISCOUNT, DiscountWriter, gid("Discount", "8"), {**one, "customer_groups": [group]}
			),
			Outcome.UNSUPPORTED,
			"customers and groups at once",
		)

	def held_discount(self, codes: list[str], *conditions: Condition, **change: Any) -> Fields:
		"""A discount's fields as the contract maps them, with conditions ERPNext's pricing doesn't enforce."""
		base = Discount(
			codes=codes,
			description="Writer test held",
			discount="percentage",
			applies="transaction",
			value=Decimal(10),
			items=[],
			groups=[],
			min_amount=None,
			valid_from="2026-01-01",
			valid_upto=None,
			max_uses=5,
			conditions=list(conditions),
		)
		return discount_fields(replace(base, **change))

	def test_a_discount_with_unenforced_conditions_is_held(self) -> None:
		key = gid("Coupon", "10")
		limit = Condition("per_customer_limit", {"uses": 1})
		fields = self.held_discount(["WTHELD"], limit)
		self.assertEqual(self.mirror(Kind.DISCOUNT, DiscountWriter, key, fields), Outcome.CREATED)
		rule = frappe.get_doc("Pricing Rule", self.target(Kind.DISCOUNT, key))
		self.assertEqual(
			(rule.disable, rule.importer_hold, json.loads(rule.importer_conditions)),
			(
				1,
				"held: awaiting enforcement (card 405)",
				[{"card": "card 405", "kind": "per_customer_limit", "terms": {"uses": 1}}],
			),
		)
		coupon = frappe.get_doc("Coupon Code", self.target(Kind.COUPON, f"{key}#WTHELD"))
		self.assertEqual(
			(str(coupon.valid_upto), coupon.valid_from, coupon.importer_hold, coupon.maximum_use),
			(str(add_days(nowdate(), -1)), None, "held: awaiting enforcement (card 405)", 5),
		)
		with self.assertRaisesRegex(frappe.ValidationError, "expired"):
			validate_coupon_code(coupon.name)
		self.assertEqual(self.mirror(Kind.DISCOUNT, DiscountWriter, key, fields), Outcome.SKIPPED)
		with patch.object(contract, "ENFORCED", frozenset({"per_customer_limit"})):
			released = self.held_discount(["WTHELD"], limit)
		self.assertNotIn("held", released)
		self.assertEqual(
			self.mirror(Kind.DISCOUNT, DiscountWriter, key, released),
			Outcome.UPDATED,
			"once the fork enforces the condition, the next run enables the discount",
		)
		rule.reload()
		coupon.reload()
		self.assertEqual((rule.disable, rule.importer_hold), (0, None))
		self.assertEqual((coupon.valid_upto, str(coupon.valid_from)), (None, "2026-01-01"))
		self.assertIn("per_customer_limit", coupon.importer_conditions, "still recorded for the fork")
		validate_coupon_code(coupon.name)

	def test_a_held_scheme_and_a_code_held_alone(self) -> None:
		tiered = self.held_discount(
			[],
			Condition("cap", {"amount": Decimal("40")}),
			automatic=True,
			tiers=[
				Tier("percentage", Decimal(5), min_qty=Decimal(10)),
				Tier("percentage", Decimal(10), min_qty=Decimal(20)),
			],
			max_uses=None,
		)
		key = gid("Discount", "11")
		self.assertEqual(self.mirror(Kind.DISCOUNT, DiscountWriter, key, tiered), Outcome.CREATED)
		scheme = self.target(Kind.DISCOUNT, key)
		self.assertEqual(
			frappe.db.get_value("Promotional Scheme", scheme, ["disable", "importer_hold"]),
			(1, "held: awaiting enforcement (card 407)"),
		)
		rules = frappe.get_all("Pricing Rule", filters={"promotional_scheme": scheme}, pluck="disable")
		self.assertTrue(rules and all(rules), "every rule of a held scheme is disabled")
		code_held = self.held_discount(
			["WTOPEN", "WTOWN"],
			code_limits={"WTOWN": Code(max_uses=2, conditions=[Condition("once_per_customer")])},
		)
		key = gid("Coupon", "12")
		self.assertEqual(self.mirror(Kind.DISCOUNT, DiscountWriter, key, code_held), Outcome.CREATED)
		self.assertEqual(frappe.db.get_value("Pricing Rule", self.target(Kind.DISCOUNT, key), "disable"), 0)
		validate_coupon_code(self.target(Kind.COUPON, f"{key}#WTOPEN"))
		own = frappe.get_doc("Coupon Code", self.target(Kind.COUPON, f"{key}#WTOWN"))
		self.assertEqual((own.maximum_use, own.importer_hold), (2, "held: awaiting enforcement (card 405)"))
		with self.assertRaisesRegex(frappe.ValidationError, "expired"):
			validate_coupon_code(own.name)

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

	def shipment(self, number: str = "CP1") -> Field:
		return {
			"gid": gid("Fulfillment", "1"),
			"date": "2026-06-02T15:00:00Z",
			"carrier": "Canada Post",
			"service": "Expedited",
			"tracking_number": number,
			"tracking_url": f"https://track.example/{number}",
			"delivered": False,
			"contents": "2 x Wool Toque",
		}

	def test_an_orders_fulfilments_are_shipments(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		key = gid("Order", "14")
		shipped: Fields = {
			**self.order(),
			"shipping": {
				"address_type": "Shipping",
				"name": "Avery Writer",
				"company": None,
				"line1": "9 Ship St",
				"line2": None,
				"city": "Banff",
				"state": "AB",
				"postcode": "T1L 1A1",
				"country_code": "CA",
				"email": None,
				"phone": None,
			},
			"shipments": [self.shipment()],
		}
		self.assertEqual(self.mirror(Kind.ORDER, OrderWriter, key, shipped), Outcome.CREATED)
		part = f"{key}#{gid('Fulfillment', '1')}"
		shipment = frappe.get_doc("Shipment", self.target(Kind.SHIPMENT, part))
		invoice = frappe.get_doc("Sales Invoice", self.target(Kind.ORDER, key))
		self.assertEqual(
			(
				shipment.docstatus,
				shipment.delivery_customer,
				frappe.db.get_value("Address", shipment.delivery_address_name, "city"),
				shipment.carrier,
				shipment.carrier_service,
				shipment.awb_number,
				shipment.tracking_status,
				Decimal(str(shipment.value_of_goods)),
				shipment.description_of_content,
			),
			(
				0,
				invoice.customer,
				"Banff",
				"Canada Post",
				"Expedited",
				"CP1",
				"In Progress",
				Decimal("52.5"),
				"2 x Wool Toque",
			),
		)
		before = modified("Shipment", shipment.name)
		self.assertEqual(self.mirror(Kind.ORDER, OrderWriter, key, shipped), Outcome.SKIPPED)
		self.assertEqual(modified("Shipment", shipment.name), before, "an unchanged shipment writes nothing")
		retracked: Fields = {**shipped, "shipments": [self.shipment("CP2")]}
		self.assertEqual(self.mirror(Kind.ORDER, OrderWriter, key, retracked), Outcome.UPDATED)
		self.assertEqual(
			frappe.db.get_value("Shipment", shipment.name, "awb_number"), "CP2", "updated in place"
		)
		self.assertEqual(
			self.mirror(Kind.ORDER, OrderWriter, key, {**shipped, "shipments": []}), Outcome.UPDATED
		)
		self.assertEqual(
			frappe.db.get_value("Shipment", shipment.name, "docstatus"),
			2,
			"a dropped fulfilment is discarded",
		)

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

	def test_an_order_whose_lines_include_tax_at_different_rates(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		mixed: Fields = {
			**self.order(),
			"lines": [
				{
					"source_line": gid("OrderItem", "11"),
					"product": gid("Product", "1"),
					"variant": None,
					"name": "Wool Toque",
					"qty": Decimal(1),
					"rate": Decimal(112),
					"taxes": [
						{"title": "GST", "rate": Decimal(5)},
						{"title": "PST", "rate": Decimal(7)},
					],
				},
				{
					"source_line": gid("OrderItem", "12"),
					"product": None,
					"variant": None,
					"name": "Gift wrap",
					"qty": Decimal(1),
					"rate": Decimal(105),
					"taxes": [{"title": "GST", "rate": Decimal(5)}],
				},
			],
			"charges": [
				{"kind": "included_tax", "description": "GST", "amount": Decimal(10)},
				{"kind": "included_tax", "description": "PST", "amount": Decimal(7)},
			],
			"total": Decimal(217),
			"taxes_included": True,
		}
		key = gid("Order", "11")
		self.assertEqual(self.mirror(Kind.ORDER, OrderWriter, key, mixed), Outcome.CREATED)
		invoice = frappe.get_doc("Sales Invoice", self.target(Kind.ORDER, key))
		self.assertEqual(
			(invoice.docstatus, Decimal(str(invoice.grand_total)), Decimal(str(invoice.net_total))),
			(1, Decimal(217), Decimal(200)),
		)
		self.assertEqual(
			[(row.description, Decimal(str(row.tax_amount))) for row in invoice.taxes],
			[("GST", Decimal(10)), ("PST", Decimal(7))],
		)
		self.assertEqual(len({row.account_head for row in invoice.taxes}), 2, "each tax on its own account")
		self.assertEqual(
			[
				frappe.db.get_value("Item Tax Template", line.item_tax_template, "title")
				for line in invoice.items
			],
			[f"GST at 5, PST at 7 - {ABBREVIATION}", f"GST at 5 - {ABBREVIATION}"],
		)
		before = modified("Sales Invoice", invoice.name)
		self.assertEqual(self.mirror(Kind.ORDER, OrderWriter, key, mixed), Outcome.SKIPPED)
		self.assertEqual(modified("Sales Invoice", invoice.name), before)

	def test_an_order_in_another_currency(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		frappe.db.set_value("Currency", "USD", "enabled", 1)
		frappe.db.set_single_value("Accounts Settings", MULTI_CURRENCY_SETTING, 0)
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
		other_base = {**usd, "base_currency": "EUR"}
		self.assertEqual(
			self.mirror(Kind.ORDER, OrderWriter, gid("Order", "10"), other_base),
			Outcome.UNSUPPORTED,
			"a base total in another currency than the company's",
		)

	def test_orders_in_several_currencies_on_one_customer(self) -> None:
		"""With multi-currency invoices against a single party account, a customer's orders in other
		currencies are booked at the source's rate on its own receivable, and paid exactly."""
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		frappe.db.set_value("Currency", "USD", "enabled", 1)
		frappe.db.set_single_value("Accounts Settings", MULTI_CURRENCY_SETTING, 1)
		self.assertEqual(
			self.mirror(Kind.ORDER, OrderWriter, gid("Order", "12"), self.order()), Outcome.CREATED
		)
		key = gid("Order", "13")
		usd = {**self.order(), "currency": "USD", "exchange_rate": Decimal("1.2")}
		self.assertEqual(self.mirror(Kind.ORDER, OrderWriter, key, usd), Outcome.CREATED)
		invoice = frappe.get_doc("Sales Invoice", self.target(Kind.ORDER, key))
		self.assertEqual(
			(
				invoice.currency,
				Decimal(str(invoice.grand_total)),
				Decimal(str(invoice.base_grand_total)),
				frappe.db.get_value("Account", invoice.debit_to, "account_currency"),
				invoice.docstatus,
			),
			("USD", Decimal("52.5"), Decimal(63), self.context.currency, 1),
		)
		self.assertEqual(Decimal(str(invoice.outstanding_amount)), Decimal(0), "paid")

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

	# -- the books and people (ERP and HR sources) ------------------------------------------------------

	def supplier(self, name: str = "Writer Supply Co") -> Fields:
		return party_fields(
			Party(
				name,
				"Company",
				first_name="Sam",
				last_name="Supplier",
				company=name,
				email="sam@supply.example.test",
				addresses=[address_of(self.customer())],
				tax_id="RT0001",
			)
		)

	def test_suppliers(self) -> None:
		key = gid("Vendor", "1")
		_, name = self.assert_lifecycle(
			Kind.SUPPLIER, SupplierWriter, key, self.supplier(), self.supplier("Writer Supplies Ltd")
		)
		supplier = frappe.get_doc("Supplier", name)
		self.assertEqual(
			(supplier.supplier_name, supplier.supplier_type, supplier.tax_id),
			("Writer Supplies Ltd", "Company", "RT0001"),
		)
		self.assertTrue(supplier.supplier_primary_contact and supplier.supplier_primary_address)
		frappe.db.set_value("Supplier", name, "on_hold", 1)
		self.mirror(Kind.SUPPLIER, SupplierWriter, key, self.supplier("Writer Supplies Inc"))
		self.assertEqual(frappe.db.get_value("Supplier", name, "on_hold"), 1, "an unmapped field is kept")
		self.retire(Kind.SUPPLIER, SupplierWriter, key)
		self.assertEqual(frappe.db.get_value("Supplier", name, "disabled"), 1)

	def ledger(self, number: str, name: str, root_type: str, account_type: str | None = None) -> str:
		"""A mirrored account (by its number), for the documents that book to it."""
		key = gid("Account", number)
		self.mirror(
			Kind.ACCOUNT,
			AccountWriter,
			key,
			account_fields(LedgerAccount(number, name, root_type, account_type)),
		)
		return key

	def test_accounts_and_cost_centers(self) -> None:
		group = gid("Account", "4")
		self.mirror(
			Kind.ACCOUNT,
			AccountWriter,
			group,
			account_fields(LedgerAccount("4", "Writer Revenue", "Income", None, None, True)),
		)
		key = gid("Account", "4100")
		first = account_fields(LedgerAccount("4100", "Writer Sales", "Income", "Income Account", group))
		_, name = self.assert_lifecycle(
			Kind.ACCOUNT, AccountWriter, key, first, {**first, "name": "Writer Product Sales"}
		)
		account = frappe.get_doc("Account", name)
		self.assertEqual(
			(account.account_name, account.account_number, account.parent_account, account.root_type),
			("Writer Product Sales - WT", "4100", self.target(Kind.ACCOUNT, group), "Income"),
		)
		root = frappe.db.get_value("Account", self.target(Kind.ACCOUNT, group), "parent_account")
		self.assertEqual(
			frappe.db.get_value("Account", root, "root_type"), "Income", "under the company's root"
		)
		self.retire(Kind.ACCOUNT, AccountWriter, key)
		self.assertEqual(frappe.db.get_value("Account", name, "disabled"), 1)

		center = gid("AnalyticAccount", "1")
		_, cost_center = self.assert_lifecycle(
			Kind.COST_CENTER,
			CostCenterWriter,
			center,
			tree_fields("Workshop", None),
			tree_fields("Studio", None),
		)
		self.assertEqual(frappe.db.get_value("Cost Center", cost_center, "cost_center_name"), "Studio - WT")
		self.retire(Kind.COST_CENTER, CostCenterWriter, center)
		self.assertEqual(frappe.db.get_value("Cost Center", cost_center, "disabled"), 1)

	def test_warehouses(self) -> None:
		group = gid("Warehouse", "1")
		self.mirror(
			Kind.WAREHOUSE, WarehouseWriter, group, warehouse_fields("Writer Sites", None, True, None)
		)
		key = gid("Warehouse", "2")
		address = address_of(self.customer())
		_, name = self.assert_lifecycle(
			Kind.WAREHOUSE,
			WarehouseWriter,
			key,
			warehouse_fields("Writer Depot", group, False, address),
			warehouse_fields("Writer Depot", group, False, {**address, "city": "Edmonton"}),
		)
		warehouse = frappe.get_doc("Warehouse", name)
		self.assertEqual(
			(warehouse.warehouse_name, warehouse.parent_warehouse, warehouse.city),
			("Writer Depot - WT", self.target(Kind.WAREHOUSE, group), "Edmonton"),
		)
		self.retire(Kind.WAREHOUSE, WarehouseWriter, key)
		self.assertEqual(frappe.db.get_value("Warehouse", name, "disabled"), 1)

	def test_projects_leads_and_opportunities(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		key = gid("Project", "1")
		first = project_fields("Writer Fit-out", "Open", gid("Customer", "1"), "2026-03-01", "2026-04-30")
		_, project = self.assert_lifecycle(
			Kind.PROJECT, ProjectWriter, key, first, {**first, "status": "Completed"}
		)
		self.assertEqual(
			frappe.db.get_value("Project", project, ["status", "customer"]),
			("Completed", self.target(Kind.CUSTOMER, gid("Customer", "1"))),
		)
		self.retire(Kind.PROJECT, ProjectWriter, key)
		self.assertEqual(frappe.db.get_value("Project", project, "is_active"), "No")

		lead_key = gid("Lead", "1")
		lead = lead_fields(
			Lead("Open", "Rae", "Lead", "Writer Prospects", "rae.lead@example.test", notes="Met at the expo")
		)
		_, name = self.assert_lifecycle(Kind.LEAD, LeadWriter, lead_key, lead, {**lead, "status": "Replied"})
		self.assertEqual(
			frappe.db.get_value("Lead", name, ["status", "company_name"]), ("Replied", "Writer Prospects")
		)
		opportunity_key = gid("Opportunity", "1")
		opportunity = opportunity_fields(
			Opportunity(
				Kind.LEAD,
				lead_key,
				"Writer showroom",
				"Open",
				"2026-03-02",
				"CAD",
				Decimal("1200.00"),
				Decimal(40),
			)
		)
		_, deal = self.assert_lifecycle(
			Kind.OPPORTUNITY,
			OpportunityWriter,
			opportunity_key,
			opportunity,
			{**opportunity, "probability": Decimal(60)},
		)
		self.assertEqual(
			frappe.db.get_value("Opportunity", deal, ["opportunity_from", "party_name", "probability"]),
			("Lead", name, 60.0),
		)
		self.retire(Kind.OPPORTUNITY, OpportunityWriter, opportunity_key)
		self.assertEqual(frappe.db.get_value("Opportunity", deal, "status"), "Closed")
		self.retire(Kind.LEAD, LeadWriter, lead_key)
		self.assertEqual(frappe.db.get_value("Lead", name, "disabled"), 1)

	def test_departments_designations_and_employees(self) -> None:
		department = gid("Department", "1")
		_, team = self.assert_lifecycle(
			Kind.DEPARTMENT,
			DepartmentWriter,
			department,
			tree_fields("Writers", None),
			tree_fields("Writing", None),
		)
		self.assertEqual(frappe.db.get_value("Department", team, "parent_department"), "All Departments")
		job = gid("Job", "1")
		self.assertEqual(
			self.mirror(Kind.DESIGNATION, DesignationWriter, job, {"name": "Writer"}), Outcome.CREATED
		)
		self.assertEqual(self.target(Kind.DESIGNATION, job), "Writer - WT")
		manager = gid("Employee", "1")
		self.mirror(
			Kind.EMPLOYEE,
			EmployeeWriter,
			manager,
			employee_fields(
				Employee(
					"Morgan",
					"Active",
					gender="Female",
					date_of_birth="1980-01-02",
					date_of_joining="2020-01-06",
				)
			),
		)
		key = gid("Employee", "2")
		first = employee_fields(
			Employee(
				"Avery",
				"Active",
				last_name="Writer",
				department=department,
				designation=job,
				reports_to=manager,
			)
		)
		_, employee = self.assert_lifecycle(
			Kind.EMPLOYEE, EmployeeWriter, key, first, {**first, "last_name": "Wright"}
		)
		doc = frappe.get_doc("Employee", employee)
		self.assertEqual(
			(doc.employee_name, doc.department, doc.designation, doc.reports_to, doc.date_of_birth),
			("Avery Wright", team, "Writer - WT", self.target(Kind.EMPLOYEE, manager), None),
			"what the source doesn't keep (a date of birth) stays empty",
		)
		self.retire(Kind.EMPLOYEE, EmployeeWriter, key)
		self.assertEqual(frappe.db.get_value("Employee", employee, "status"), "Inactive")
		self.retire(Kind.DEPARTMENT, DepartmentWriter, department)
		self.assertEqual(frappe.db.get_value("Department", team, "disabled"), 1)

	def trade(
		self, party: str, qty: str = "2", submitted: bool = True, account: str | None = None
	) -> TradeDocument:
		line = order_line(
			gid("Line", "1"), gid("Product", "1"), None, "Wool Toque", Decimal(qty), Decimal(qty) * 25, "CAD"
		)
		return TradeDocument(
			number="WT/2026/0001",
			date="2026-06-01",
			party=party,
			currency="CAD",
			lines=[TradeLine(line, account=account)],
			taxes=[TradeTax("GST 5%", Decimal(qty) * Decimal("1.25"))],
			total=Decimal(qty) * Decimal("26.25"),
			submitted=submitted,
			due_date="2026-07-01",
			reference="PO-77",
		)

	def test_sales_and_purchase_orders(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.SUPPLIER, SupplierWriter, gid("Vendor", "1"), self.supplier())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		for kind, writer, doctype, party, planned in (
			(Kind.SALES_ORDER, SalesOrderWriter, "Sales Order", gid("Customer", "1"), "delivery_date"),
			(Kind.PURCHASE_ORDER, PurchaseOrderWriter, "Purchase Order", gid("Vendor", "1"), "schedule_date"),
		):
			key = gid("Order", doctype.replace(" ", ""))
			_, order = self.assert_lifecycle(
				kind,
				writer,
				key,
				trade_fields(self.trade(party, submitted=False)),
				trade_fields(self.trade(party)),
			)
			doc = frappe.get_doc(doctype, order)
			self.assertEqual(
				(doc.docstatus, Decimal(str(doc.grand_total)), str(doc.get(planned)), doc.title),
				(1, Decimal("52.5"), "2026-07-01", "WT/2026/0001"),
				f"a confirmed {doctype}",
			)
			self.assertEqual(
				self.stock_of(self.target(Kind.PRODUCT, gid("Product", "1"))),
				Decimal(12),
				"orders move no stock",
			)
			self.mirror(kind, writer, key, trade_fields(self.trade(party, qty="3")))
			amended = self.target(kind, key)
			self.assertEqual(
				frappe.db.get_value(doctype, amended, "amended_from"), order, "changed after confirming"
			)
			self.retire(kind, writer, key)
			self.assertEqual(frappe.db.get_value(doctype, amended, "docstatus"), 2)

	def test_invoices_returns_and_payments(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.SUPPLIER, SupplierWriter, gid("Vendor", "1"), self.supplier())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		income = self.ledger("4200", "Writer Income", "Income", "Income Account")
		expense = self.ledger("5200", "Writer Purchases", "Expense", "Expense Account")
		for kind, writer, doctype, party, account, field, payment_type, party_type in (
			(
				Kind.SALES_INVOICE,
				SalesInvoiceWriter,
				"Sales Invoice",
				gid("Customer", "1"),
				income,
				"income_account",
				"Receive",
				"Customer",
			),
			(
				Kind.PURCHASE_INVOICE,
				PurchaseInvoiceWriter,
				"Purchase Invoice",
				gid("Vendor", "1"),
				expense,
				"expense_account",
				"Pay",
				"Supplier",
			),
		):
			key = gid("Move", doctype.replace(" ", ""))
			_, invoice = self.assert_lifecycle(
				kind,
				writer,
				key,
				trade_fields(self.trade(party, submitted=False, account=account)),
				trade_fields(self.trade(party, account=account)),
			)
			doc = frappe.get_doc(doctype, invoice)
			# A stock item bought without a receipt books where ERPNext's perpetual inventory puts it.
			booked_to = (
				self.target(Kind.ACCOUNT, account)
				if kind is Kind.SALES_INVOICE
				else frappe.get_cached_value("Company", self.context.company, "stock_received_but_not_billed")
			)
			self.assertEqual(
				(doc.docstatus, doc.update_stock, Decimal(str(doc.grand_total)), doc.items[0].get(field)),
				(1, 0, Decimal("52.5"), booked_to),
				f"a posted {doctype}, on its account",
			)
			returned = gid("Move", f"{doctype.replace(' ', '')}Return")
			note = replace(
				self.trade(party, qty="-1", account=account),
				number="WT/RET/1",
				is_return=True,
				return_against=key,
			)
			self.assertEqual(self.mirror(kind, writer, returned, trade_fields(note)), Outcome.CREATED)
			credit = frappe.get_doc(doctype, self.target(kind, returned))
			self.assertEqual(
				(
					credit.is_return,
					credit.return_against,
					Decimal(str(credit.grand_total)),
					credit.update_outstanding_for_self,
				),
				(1, invoice, Decimal("-26.25"), 0),
				"a return against the invoice, settling it",
			)
			payment_key = gid("Payment", doctype.replace(" ", ""))
			paid = payment_fields(
				Payment(
					payment_type,
					party_type,
					party,
					"2026-06-05",
					Decimal("26.25"),
					"CAD",
					[Allocation(kind, key, Decimal("26.25"))],
					reference="CHQ-1",
				)
			)
			_, entry = self.assert_lifecycle(
				Kind.PAYMENT_ENTRY, PaymentEntryWriter, payment_key, paid, {**paid, "reference": "CHQ-2"}
			)
			self.assertEqual(
				Decimal(str(frappe.db.get_value(doctype, invoice, "outstanding_amount"))),
				Decimal(0),
				"the return and the payment settle the invoice",
			)
			self.assertEqual(
				frappe.db.get_value("Payment Entry", entry, ["docstatus", "reference_no"]), (1, "CHQ-2")
			)
			self.retire(Kind.PAYMENT_ENTRY, PaymentEntryWriter, payment_key)
			self.assertEqual(frappe.db.get_value("Payment Entry", entry, "docstatus"), 2)
			self.retire(kind, writer, returned)
			self.retire(kind, writer, key)
			self.assertEqual(frappe.db.get_value(doctype, invoice, "docstatus"), 2)

	def test_a_refund_paid_out_on_a_credit_note_settles_it(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		note_key = gid("Move", "RefundReceipt")
		note = replace(self.trade(gid("Customer", "1"), qty="-1"), number="RR/1", is_return=True)
		self.mirror(Kind.SALES_INVOICE, SalesInvoiceWriter, note_key, trade_fields(note))
		credit = self.target(Kind.SALES_INVOICE, note_key)
		refund = payment_fields(
			Payment(
				"Pay",
				"Customer",
				gid("Customer", "1"),
				"2026-06-06",
				Decimal("26.25"),
				"CAD",
				[Allocation(Kind.SALES_INVOICE, note_key, Decimal("26.25"))],
			)
		)
		self.assertEqual(
			self.mirror(Kind.PAYMENT_ENTRY, PaymentEntryWriter, gid("Payment", "Refund"), refund),
			Outcome.CREATED,
		)
		self.assertEqual(
			Decimal(str(frappe.db.get_value("Sales Invoice", credit, "outstanding_amount"))),
			Decimal(0),
			"the refund settles the credit note's negative balance",
		)

	def test_a_document_of_a_retired_party_is_still_cancelled(self) -> None:
		customer = gid("Customer", "1")
		self.mirror(Kind.CUSTOMER, CustomerWriter, customer, self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		key = gid("Move", "OfRetired")
		self.mirror(Kind.SALES_INVOICE, SalesInvoiceWriter, key, trade_fields(self.trade(customer)))
		invoice = self.target(Kind.SALES_INVOICE, key)
		name = self.target(Kind.CUSTOMER, customer)
		self.assertEqual(self.retire(Kind.CUSTOMER, CustomerWriter, customer), Outcome.RETIRED)
		self.assertEqual(self.retire(Kind.SALES_INVOICE, SalesInvoiceWriter, key), Outcome.RETIRED)
		self.assertEqual(frappe.db.get_value("Sales Invoice", invoice, "docstatus"), 2)
		self.assertEqual(
			frappe.db.get_value("Customer", name, "disabled"),
			1,
			"the retired customer stays disabled",
		)

	def test_a_payment_of_a_draft_invoice_is_not_booked(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		key = gid("Move", "Draft")
		self.mirror(
			Kind.SALES_INVOICE,
			SalesInvoiceWriter,
			key,
			trade_fields(self.trade(gid("Customer", "1"), submitted=False)),
		)
		paid = payment_fields(
			Payment(
				"Receive",
				"Customer",
				gid("Customer", "1"),
				"2026-06-05",
				Decimal(10),
				"CAD",
				[Allocation(Kind.SALES_INVOICE, key, Decimal(10))],
			)
		)
		self.assertEqual(
			self.mirror(Kind.PAYMENT_ENTRY, PaymentEntryWriter, gid("Payment", "D"), paid),
			Outcome.UNSUPPORTED,
		)
		draft = self.target(Kind.SALES_INVOICE, key)
		self.retire(Kind.SALES_INVOICE, SalesInvoiceWriter, key)
		self.assertIn(
			"Retired",
			str(frappe.db.get_value("Sales Invoice", draft, "remarks")),
			"a retired draft is marked",
		)

	def test_journal_entries(self) -> None:
		self.mirror(Kind.SUPPLIER, SupplierWriter, gid("Vendor", "1"), self.supplier())
		rent = self.ledger("6100", "Writer Rent", "Expense", "Expense Account")
		bank = self.ledger("1010", "Writer Bank", "Asset", "Bank")
		key = gid("Move", "Journal")

		def entry(amount: str) -> Fields:
			lines = [
				JournalLine(rent, Decimal(amount), Decimal(0)),
				JournalLine(bank, Decimal(0), Decimal(amount)),
			]
			return journal_fields("MISC/2026/0001", "2026-06-10", lines, True, "June rent")

		_, journal = self.assert_lifecycle(
			Kind.JOURNAL_ENTRY, JournalEntryWriter, key, entry("900"), entry("950")
		)
		doc = frappe.get_doc("Journal Entry", journal)
		self.assertEqual(
			(doc.docstatus, Decimal(str(doc.total_debit)), doc.user_remark),
			(1, Decimal(950), "MISC/2026/0001: June rent"),
		)
		self.assertIsNotNone(doc.amended_from, "a posted entry that changed is cancelled and amended")
		self.retire(Kind.JOURNAL_ENTRY, JournalEntryWriter, key)
		self.assertEqual(frappe.db.get_value("Journal Entry", journal, "docstatus"), 2)

	def test_a_journal_keeps_a_party_on_receivable_and_payable_lines_only(self) -> None:
		self.mirror(Kind.SUPPLIER, SupplierWriter, gid("Vendor", "1"), self.supplier())
		supplies = self.ledger("6200", "Writer Supplies", "Expense", "Expense Account")
		card = self.ledger("2100", "Writer Card", "Liability")
		lines = [
			JournalLine(supplies, Decimal("40"), Decimal(0), "Supplier", gid("Vendor", "1")),
			JournalLine(card, Decimal(0), Decimal("40")),
		]
		key = gid("Move", "Spend")
		self.assertEqual(
			self.mirror(
				Kind.JOURNAL_ENTRY,
				JournalEntryWriter,
				key,
				journal_fields("SPEND/1", "2026-06-11", lines, True),
			),
			Outcome.CREATED,
		)
		rows = frappe.get_all(
			"Journal Entry Account",
			filters={"parent": self.target(Kind.JOURNAL_ENTRY, key)},
			fields=["party_type", "party"],
		)
		self.assertEqual({(row.party_type, row.party) for row in rows}, {(None, None)})

	def test_a_journal_settles_an_invoice_on_the_sources_receivable(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		receivable = self.ledger("1200", "Writer Receivable", "Asset", "Receivable")
		bank = self.ledger("1010", "Writer Bank", "Asset", "Bank")
		invoice_key = gid("Move", "Receivable")
		invoice = replace(self.trade(gid("Customer", "1")), party_account=receivable)
		self.mirror(Kind.SALES_INVOICE, SalesInvoiceWriter, invoice_key, trade_fields(invoice))
		invoice_name = self.target(Kind.SALES_INVOICE, invoice_key)
		self.assertEqual(
			frappe.db.get_value("Sales Invoice", invoice_name, "debit_to"),
			self.target(Kind.ACCOUNT, receivable),
		)
		lines = [
			JournalLine(bank, Decimal("52.50"), Decimal(0)),
			JournalLine(
				receivable,
				Decimal(0),
				Decimal("52.50"),
				"Customer",
				gid("Customer", "1"),
				against=(Kind.SALES_INVOICE, invoice_key),
			),
		]
		key = gid("Move", "Statement")
		self.assertEqual(
			self.mirror(
				Kind.JOURNAL_ENTRY,
				JournalEntryWriter,
				key,
				journal_fields("BNK/1", "2026-06-12", lines, True),
			),
			Outcome.CREATED,
		)
		self.assertEqual(
			Decimal(str(frappe.db.get_value("Sales Invoice", invoice_name, "outstanding_amount"))),
			Decimal(0),
			"a bank statement's entry settles the invoice it was reconciled with",
		)

	def test_a_partys_people_and_archiving(self) -> None:
		key = gid("Partner", "7")
		people = [
			ContactPerson(gid("Partner", "8"), "Douglas Fletcher", "douglas@example.test", None, "Buyer")
		]
		first = party_fields(Party("Writer Interiors", "Company", contacts=people))
		_, customer = self.assert_lifecycle(
			Kind.CUSTOMER,
			CustomerWriter,
			key,
			first,
			party_fields(Party("Writer Interiors", "Company", disabled=True)),
		)
		person = frappe.db.get_value(
			"Importer Link",
			{"connection": CONNECTION, "gid": f"{key}#person:{gid('Partner', '8')}"},
			"target_name",
		)
		self.assertEqual(frappe.db.get_value("Contact", person, "designation"), "Buyer")
		self.assertFalse(
			frappe.db.exists(
				"Dynamic Link", {"parent": person, "link_doctype": "Customer", "link_name": customer}
			),
			"a person the source drops is no longer the customer's",
		)
		self.assertEqual(
			frappe.db.get_value("Customer", customer, "disabled"), 1, "archived: disabled, still live"
		)
		self.assertEqual(self.target(Kind.CUSTOMER, key), customer)
		self.mirror(Kind.CUSTOMER, CustomerWriter, key, first)
		self.assertEqual(frappe.db.get_value("Customer", customer, "disabled"), 0, "restored at the source")
		department = gid("Department", "9")
		self.mirror(Kind.DEPARTMENT, DepartmentWriter, department, tree_fields("Archive", None, False, True))
		self.assertEqual(
			frappe.db.get_value("Department", self.target(Kind.DEPARTMENT, department), "disabled"), 1
		)
		lead = gid("Lead", "9")
		self.mirror(
			Kind.LEAD, LeadWriter, lead, lead_fields(Lead("Open", email="closed@example.test", disabled=True))
		)
		self.assertEqual(frappe.db.get_value("Lead", self.target(Kind.LEAD, lead), "disabled"), 1)

	def test_a_documents_discount_and_freight(self) -> None:
		self.mirror(Kind.CUSTOMER, CustomerWriter, gid("Customer", "1"), self.customer())
		self.mirror(Kind.SUPPLIER, SupplierWriter, gid("Vendor", "1"), self.supplier())
		self.mirror(Kind.PRODUCT, ProductWriter, gid("Product", "1"), self.simple_product())
		freight = TradeTax("Freight", Decimal("10.00"), role="shipping")
		document = replace(
			self.trade(gid("Customer", "1")),
			taxes=[*self.trade(gid("Customer", "1")).taxes, freight],
			discount=Decimal("5.00"),
			total=Decimal("57.50"),
		)
		key = gid("Order", "Discounted")
		self.assertEqual(
			self.mirror(Kind.SALES_ORDER, SalesOrderWriter, key, trade_fields(document)), Outcome.CREATED
		)
		order = frappe.get_doc("Sales Order", self.target(Kind.SALES_ORDER, key))
		self.assertEqual(
			(
				Decimal(str(order.discount_amount)),
				order.taxes[-1].account_head,
				Decimal(str(order.grand_total)),
			),
			(Decimal(5), self.context.shipping_account, Decimal("57.5")),
			"the discount on the net total, freight on the connection's shipping account",
		)
		bought = replace(document, party=gid("Vendor", "1"))
		self.assertEqual(
			self.mirror(
				Kind.PURCHASE_ORDER, PurchaseOrderWriter, gid("Order", "Freight"), trade_fields(bought)
			),
			Outcome.UNSUPPORTED,
			"a purchase's freight needs the source's account",
		)

	# -- gift cards and loyalty (Square) ------------------------------------------------------------------

	@contextmanager
	def fake_square(self) -> Iterator[None]:
		"""The writer-test connection writes to the e2e fake Square (tests/fixtures/square_stored_value), and
		the importer's commit after each Square write stays inside the test's rollback."""
		endpoints = dict(frappe.conf.get(stored_value.TEST_ENDPOINTS) or {})
		square = endpoints.get(f"{TEMPLATE}:square")
		self.assertTrue(square, "the e2e connection writes to the fake Square")
		with (
			patch.dict(
				frappe.conf, {stored_value.TEST_ENDPOINTS: {**endpoints, f"{CONNECTION}:square": square}}
			),
			patch.object(stored_value.StoredValueWriter, "commit", lambda self: None),
		):
			yield

	def certificate(
		self, balance: str, ledger: list[contract.LedgerEntry], load_increases: bool = False
	) -> Fields:
		return contract.gift_card_fields(
			contract.GiftCard(
				kind=contract.GIFT_CARD,
				code="4KZ-X0O-UHI-JHC",
				code_hint=None,
				currency="CAD",
				balance=Decimal(balance),
				state=contract.ACTIVE,
				customer=None,
				email="ingrid@example.com",
				given_name="Ingrid",
				family_name="Larsen",
				ledger=ledger,
				details={"to_name": "Ingrid Larsen"},
				load_increases=load_increases,
				new_numbers=True,
			)
		)

	def test_gift_cards_are_loaded_once_then_follow_only_the_source_changes(self) -> None:
		# The e2e BigCommerce certificate's identity, so the fake Square answers its recorded requests.
		key = "gid://bigcommerce/bigcommerce-c/GiftCertificate/901"
		issue = contract.LedgerEntry("issue-901", "2026-09-01T16:00:00Z", "issue", Decimal(50), Decimal(50))
		spent = contract.LedgerEntry("spent-901", None, "observed", Decimal(-20), Decimal(30))
		with self.fake_square():
			self.assertEqual(
				self.mirror(
					Kind.GIFT_CARD, stored_value.GiftCardWriter, key, self.certificate("50", [issue])
				),
				Outcome.CREATED,
			)
			card = frappe.get_doc("Importer Gift Card", self.target(Kind.GIFT_CARD, key))
			self.assertEqual(
				(card.gan, card.square_id, card.loaded_balance), ("4KZX0OUHIJHC", "GC_BC901", "50")
			)
			self.assertEqual(
				[(r.origin, r.entry_kind) for r in card.entries],
				[("source", "issue"), ("square", "activate")],
			)
			self.assertEqual(
				self.mirror(
					Kind.GIFT_CARD, stored_value.GiftCardWriter, key, self.certificate("50", [issue])
				),
				Outcome.SKIPPED,
			)
			self.assertEqual(
				self.mirror(
					Kind.GIFT_CARD, stored_value.GiftCardWriter, key, self.certificate("30", [issue, spent])
				),
				Outcome.UPDATED,
			)
			card.reload()
			self.assertEqual(card.loaded_balance, "30", "spent at the source: deducted in Square")
			self.assertEqual(
				[r.entry_kind for r in card.entries], ["issue", "observed", "activate", "adjust_decrement"]
			)
			# A rise at the source costs Square's load fee: held while the connection doesn't allow it.
			self.mirror(Kind.GIFT_CARD, stored_value.GiftCardWriter, key, self.certificate("40", [issue]))
			card.reload()
			self.assertEqual(card.loaded_balance, "30")
			self.assertIn("load fee", card.held)
		self.assertEqual(self.retire(Kind.GIFT_CARD, stored_value.GiftCardWriter, key), Outcome.RETIRED)
		self.assertEqual(frappe.db.get_value("Importer Gift Card", card.name, "state"), "retired")

	def test_a_card_whose_code_square_cant_use_is_held_without_new_numbers(self) -> None:
		fields = {**self.certificate("10", []), "code": "SHORT1", "new_numbers": False}
		with self.fake_square():
			self.assertEqual(
				self.mirror(Kind.GIFT_CARD, stored_value.GiftCardWriter, gid("GiftCard", "short"), fields),
				Outcome.FAILED,
			)

	def loyalty(self, points: int, customer: str | None) -> Fields:
		return contract.loyalty_fields(
			contract.LoyaltyAccount(
				points=points,
				customer=customer,
				email="maya.thunderchild@example.com",
				given_name="Maya",
				family_name="Thunderchild",
				ledger=[contract.LedgerEntry("earn-1", "2026-08-20T10:00:00Z", "earn", 100)],
				details={},
			)
		)

	def test_loyalty_accounts_enrol_by_phone_and_follow_the_source(self) -> None:
		customer = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": "Maya Thunderchild WT",
				"customer_group": "Individual",
				"territory": "Canada",
				"mobile_no": "+1 306 555 0177",
			}
		).insert()
		contact = gid("Customer", "maya")
		self.links.save(Kind.CUSTOMER, contact, Target("Customer", customer.name), "given", 1)
		key = "gid://wix/wix-x/LoyaltyAccount/5b0e7c40-0001-4a1b-8c2d-3e4f5a6b0001"
		with self.fake_square():
			self.assertEqual(
				self.mirror(
					Kind.LOYALTY_ACCOUNT, stored_value.LoyaltyWriter, key, self.loyalty(120, contact)
				),
				Outcome.CREATED,
			)
			account = frappe.get_doc("Importer Gift Card", self.target(Kind.LOYALTY_ACCOUNT, key))
			self.assertEqual(
				(account.square_id, account.loaded_balance, account.customer),
				("LA_MAYA", "120", customer.name),
			)
			self.assertEqual(
				self.mirror(Kind.LOYALTY_ACCOUNT, stored_value.LoyaltyWriter, key, self.loyalty(70, contact)),
				Outcome.UPDATED,
			)
			account.reload()
			self.assertEqual(account.loaded_balance, "70")
			self.assertEqual(
				[r.entry_kind for r in account.entries], ["earn", "adjust_points", "adjust_points"]
			)

	def test_a_loyalty_account_without_a_phone_waits(self) -> None:
		with self.fake_square():
			self.assertEqual(
				self.mirror(
					Kind.LOYALTY_ACCOUNT,
					stored_value.LoyaltyWriter,
					gid("LoyaltyAccount", "none"),
					self.loyalty(5, None),
				),
				Outcome.DEFERRED,
			)
