# SPDX-License-Identifier: AGPL-3.0-or-later
"""What Zoho Books holds, module by module (www.zoho.com/books/api/v3), read through the shared Zoho reader
(``sources.zoho.client``): the chart of accounts, locations, items, contacts as customers and vendors,
projects, estimates, sales orders, invoices, credit notes and sales receipts, purchase orders, bills and
vendor credits, customer and vendor payments (and the payment taken on a sales receipt), manual journals and
expenses. Every node carries its identity as ``id`` (``gid://zoho_books/<connection>/<Type>/<id>``)."""

from __future__ import annotations

from idempotent_importer.core.errors import SourceError
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.rest import RestClient
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.zoho.client import ORGANIZATIONS, Module, Product, ZohoReader

SOURCE_ID = "zoho_books"
PRODUCT = Product(
	label="Zoho Books",
	api_path="/books/v3",
	scopes=(
		"ZohoBooks.settings.READ",
		"ZohoBooks.contacts.READ",
		"ZohoBooks.estimates.READ",
		"ZohoBooks.salesorders.READ",
		"ZohoBooks.invoices.READ",
		"ZohoBooks.creditnotes.READ",
		"ZohoBooks.customerpayments.READ",
		"ZohoBooks.purchaseorders.READ",
		"ZohoBooks.bills.READ",
		"ZohoBooks.debitnotes.READ",
		"ZohoBooks.vendorpayments.READ",
		"ZohoBooks.expenses.READ",
		"ZohoBooks.projects.READ",
		"ZohoBooks.accountants.READ",
	),
)
ALL = (("filter_by", "Status.All"),)


def is_customer(contact: Node) -> bool:
	return contact.optional_string("contact_type") != "vendor"


def is_vendor(contact: Node) -> bool:
	return contact.optional_string("contact_type") == "vendor"


MODULES: dict[Kind, tuple[Module, ...]] = {
	Kind.ACCOUNT: (
		Module(
			"/chartofaccounts",
			"chartofaccounts",
			"account_id",
			"Account",
			query=(("filter_by", "AccountType.All"),),
			incremental=True,
			single="",
		),
	),
	Kind.WAREHOUSE: (Module("/locations", "locations", "location_id", "Location", paged=False),),
	Kind.PRODUCT: (Module("/items", "items", "item_id", "Item", query=ALL, single="item"),),
	Kind.CUSTOMER: (
		Module("/contacts", "contacts", "contact_id", "Contact", "contact", ALL, take=is_customer),
	),
	Kind.SUPPLIER: (
		Module("/contacts", "contacts", "contact_id", "Contact", "contact", ALL, take=is_vendor),
	),
	Kind.PROJECT: (
		Module("/projects", "projects", "project_id", "Project", "project", (("filter_by", "Status.All"),)),
	),
	Kind.DRAFT_ORDER: (
		Module("/estimates", "estimates", "estimate_id", "Estimate", "estimate", incremental=True),
	),
	Kind.SALES_ORDER: (
		Module("/salesorders", "salesorders", "salesorder_id", "SalesOrder", "salesorder", incremental=True),
	),
	Kind.SALES_INVOICE: (
		Module("/invoices", "invoices", "invoice_id", "Invoice", "invoice", incremental=True),
		Module("/creditnotes", "creditnotes", "creditnote_id", "CreditNote", "creditnote", incremental=True),
		Module("/salesreceipts", "salesreceipts", "sales_receipt_id", "SalesReceipt", "salesreceipt"),
	),
	Kind.PURCHASE_ORDER: (
		Module(
			"/purchaseorders",
			"purchaseorders",
			"purchaseorder_id",
			"PurchaseOrder",
			"purchaseorder",
			incremental=True,
		),
	),
	Kind.PURCHASE_INVOICE: (
		Module("/bills", "bills", "bill_id", "Bill", "bill", incremental=True),
		Module(
			"/vendorcredits",
			"vendorcredits",
			"vendor_credit_id",
			"VendorCredit",
			"vendor_credit",
			incremental=True,
		),
	),
	Kind.PAYMENT_ENTRY: (
		Module("/customerpayments", "customer_payments", "payment_id", "CustomerPayment", "payment"),
		Module(
			"/vendorpayments",
			"vendorpayments",
			"payment_id",
			"VendorPayment",
			"vendorpayment",
			incremental=True,
		),
		Module(
			"/salesreceipts",
			"salesreceipts",
			"sales_receipt_id",
			"SalesReceipt",
			"salesreceipt",
			as_type="SalesReceiptPayment",
		),
	),
	Kind.JOURNAL_ENTRY: (
		Module("/journals", "journals", "journal_id", "Journal", "journal", incremental=True),
		Module("/expenses", "expenses", "expense_id", "Expense", "expense"),
	),
}


class ZohoBooksReader(ZohoReader):
	"""The shared Zoho reader, with the lookups Books needs: an expense names the account it was paid
	through by name only (``paid_through_account_name``), so its id is looked up in the chart of accounts
	(an account's name is unique in an organisation); a vendor payment names its currency by id only, so the
	organisation's own currency (from /organizations) is its code when the ids match."""

	def __init__(self, config: ConnectionConfig, client: RestClient) -> None:
		super().__init__(PRODUCT, MODULES, config, client)
		self._account_ids: dict[str, str] | None = None
		self._organization: Node | None = None

	def looked_up(self, module: Module, record: Node) -> dict[str, JsonValue]:
		if module.type == "VendorPayment" and not record.optional_string("currency_code"):
			return self._currency(record)
		name = record.optional_string("paid_through_account_name")
		if module.type != "Expense" or record.optional_string("paid_through_account_id") or not name:
			return {}
		if self._account_ids is None:
			chart = MODULES[Kind.ACCOUNT][0]
			self._account_ids = {
				account.string("account_name"): account.string("account_id") for account in self.listed(chart)
			}
		found = self._account_ids.get(name)
		if found is None:
			raise SourceError(f"Zoho Books has no account named {name} (an expense is paid through it).")
		return {"paid_through_account_id": found}

	def _currency(self, record: Node) -> dict[str, JsonValue]:
		if self._organization is None:
			listed = self.listed(
				Module(ORGANIZATIONS, "organizations", "organization_id", "Organization", paged=False)
			)
			self._organization = next(
				(org for org in listed if org.optional_string("organization_id") == self.account), Node({})
			)
		if record.optional_string("currency_id") != self._organization.optional_string("currency_id"):
			return {}
		code = self._organization.optional_string("currency_code")
		return {"currency_code": code} if code else {}
