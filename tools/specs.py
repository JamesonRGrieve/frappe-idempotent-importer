# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every REST source's published API documents, which its recorded fixtures are validated against, pinned
as wp-woo-idempotent-importer's tools/SpecCache.php pins them: a versioned URL, a repository commit, or
(for a vendor that only publishes an unversioned URL) the SHA-256 of the document as fetched, so a changed
document fails loudly instead of silently changing what the fixtures are proven against. A source with no
machine-readable spec has one transcribed from its official reference and committed under tests/schemas
(copied from that project). Fetched documents are cached in .cache/specs; ``python -m tools.specs`` fetches
them (the proofs fetch a missing one themselves)."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
import yaml

from tools.dtd import Dtd
from tools.openapi import ApiSpec
from tools.spectacle import swagger_of
from tools.xsd import REF_PREFIX as XSD_REF_PREFIX
from tools.xsd import XsdSchemas

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".cache" / "specs"
FETCH_TIMEOUT_SECONDS = 120
USER_AGENT = "frappe-idempotent-importer spec fetch"
# NetSuite's help center redirects any client that doesn't present itself as a browser to "page not found".
BROWSER_USER_AGENT = f"Mozilla/5.0 (compatible; {USER_AGENT})"
JSON, YAML = "json", "yaml"
# An OpenAPI description published only as its Spectacle rendering (tools/spectacle.py).
SPECTACLE = "html"
# NetSuite's REST API Browser for the Record API, release 2026.1 (the latest published on 2026-10-19), in
# the 28 parts its page loads, pinned to the bytes fetched that day. Its paths are relative to
# /services/rest/record/v1.
NETSUITE_RECORD_BROWSER = (
	"https://system.netsuite.com/help/helpcenter/en_US/APIs/REST_API_Browser/record/v1/2026.1/"
)
NETSUITE_RECORD_PARTS = tuple(f"chunks/data{index:02d}.part" for index in range(28))
NETSUITE_RECORD_SHA256 = "85a7d589100084de8b307e0ef08d91e4fbbb2452234df37cbe1d29087c7b3507"
BIGCOMMERCE_BASE = "https://docs.bigcommerce.com/openapi/"
# BigCommerce publishes one OpenAPI 3.1 document per API area (indexed at
# https://docs.bigcommerce.com/openapi.json; every one has info.version 1.0.0) at an unversioned URL, so each
# is pinned to the bytes fetched on 2026-10-16. Their paths carry the whole /stores/{store_hash}/v2|v3
# prefix and their server is the bare API host, so no prefix is stripped.
BIGCOMMERCE_DOCUMENTS = {
	"admin-management-store-information-v2": "840c3c9d072a27c5f7e228641fefdb4c4378833856b9a1de36b0490b961e7990",
	"admin-catalog-category-trees": "243c23b1150565f600f319e97c548a262f4cc10cfd24dc8f9f5f485ad36d6ca4",
	"admin-catalog-products": "ba4baf2999f51fe30119af42729a36ca425b417be76e1eb4fc503fbb109096a5",
	"admin-catalog-product-variants": "8597a077b2fd0aac9b98dfa121c8af3f00d77a86fa8460fdef9fdee3d0b9fb8a",
	"admin-catalog-brands": "66d4560b56f7ad6f09b58a159af44d708715014c15bcd658c072602f8a6b067c",
	"admin-management-customers-v3": "5576697361ae159a2e6e18c10feca7ec8420912652c97456414cdbf40da1518f",
	# Customer groups and their discount rules (v2), pinned to the bytes fetched on 2026-10-19.
	"admin-management-customers-v2": "a59720bb538cada12a843dde91be47adbcb15673cbf1d0665443d8c2a804d761",
	"admin-content-marketing": "c2469589c1752db9ca39e7eab7191312fa9c36889b1447f6b33cb4c5a49ddf34",
	"admin-management-promotions": "8c4c0e7e2ca7f4bb226a21563c77ac71e8313d6f6a57c422b804d76ce0999d1b",
	# Re-pinned to the bytes fetched on 2026-10-21: BigCommerce reworded how a pickup consignment deducts
	# inventory (order creation's description, pickupConsignment_Post.pickup_method_id); no schema changed.
	"admin-management-orders": "d0506f86d6414d9a08691ff313e9a6f8c9747af67e9ea9748b5096df6bf9c9ba",
	"admin-management-order-operations": "85058cb818f3da2d56022ded545b6c8ea7730747bf62bfeeabce602164c4bb34",
	"admin-content-pages": "97008b6fc2af709f8b49477e6a49eb6e33f6e1d0108b22ffcaf229cc1d57ddfd",
	"admin-content-store-content": "29859d12a29def2e2881faee7dd5d312af3dd9936d8842da40453dc68e756dbc",
	"admin-management-redirects": "c06587f9727a44d2a8e4192fc304f90dfd8f42abf8a71d27eaaad08c9f6e5df0",
}


HIGHLEVEL_BASE = "https://raw.githubusercontent.com/GoHighLevel/highlevel-api-docs/0af86a4cbd48c66a4071c7e509d1079f9f10ed17/apps/"
HIGHLEVEL_DOCUMENTS = {
	"users": "a83b463c0bd7e9cc3d0602d5a8edc44ce27a207eec965b7bbfa77855c672c408",
	"products": "ef17a2dfad950838791cfdd5416d5b71cb512919e3350903207fb3a962cbfb18",
	"contacts": "d251defab9b0d3ae2feb393b27c071bed7e964c63826af0131bb8e888ecfd6cb",
	"businesses": "5f1061a259128cabecd50884094a649442fa5b3d579c2ac4c2c061f6b9e54888",
	"opportunities": "bd2c63ca4ef9929fb996a04c873e7300b7d3fe5737200270e7f2270dce26e1ba",
	"conversations": "d8e08fcf4249f4f0ba190da0b46a440ffcdf18b95f11a0e2331f82a0df7d14bb",
	"invoices": "3b90fc6561ae9ca4b9ff00213dbde6f6e947d34a27a8067caa113169cacbd770",
	"payments": "93b1dc8d05fa8fa87b1b3cf72d82cc3cd71b4b9846818bcb7835cc65d14a4bfa",
}
# Where a HighLevel document types a field against its own example, the example's shape: an invoice's items
# are the InvoiceItemDto it takes on create (with the ``_id`` its response example shows) and its contact
# the ContactDetailsDto, not strings and a bare object; a transaction's status is the string its example
# shows ("succeeded"), not an object.
INVOICE_RESPONSE = "/components/schemas/GetInvoiceResponseDto/properties/"
HIGHLEVEL_CORRECTIONS: dict[str, tuple[tuple[str, Any], ...]] = {
	"invoices": (
		(
			INVOICE_RESPONSE + "invoiceItems/items",
			{
				"allOf": [
					{"$ref": "#/components/schemas/InvoiceItemDto"},
					{"properties": {"_id": {"type": "string"}}},
				]
			},
		),
		(INVOICE_RESPONSE + "contactDetails", {"$ref": "#/components/schemas/ContactDetailsDto"}),
	),
	"payments": (("/components/schemas/TxnResponseSchema/properties/status", {"type": "string"}),),
}
# HubSpot's CRM API v3 OpenAPI documents (HubSpot/HubSpot-public-api-spec-collection, PublicApiSpecs/CRM, the
# commit of 2026-10-21), one per object type the adapter reads, each pinned to its SHA-256.
HUBSPOT_BASE = (
	"https://raw.githubusercontent.com/HubSpot/HubSpot-public-api-spec-collection/"
	"dd4246cd4b82fefd93b374547fec352f443fc4ae/PublicApiSpecs/CRM/"
)
HUBSPOT_DOCUMENTS = {
	"Calls/Rollouts/424/v3/calls.json": "a642f888dce2dc2dbb05dd5156147dd08a87765c98986408655ca5a8b6468af8",
	"Commerce Payments/Rollouts/424/v3/commercePayments.json": (
		"06a3f94e332035fcf7950a20e938e8c5b3dc0bfd0b3e5c257cc941b6fa8118b9"
	),
	"Companies/Rollouts/424/v3/companies.json": "42ba01ce31a1a47ce16325804057659e15548a8a44f979c3d68b116eb480720c",
	"Contacts/Rollouts/424/v3/contacts.json": "05f32b926eb2d90cb00b11b93498983f5489553cb74e424f954f03612683e9e1",
	"Crm Owners/Rollouts/146888/v3/crmOwners.json": "354d43093879400a9cae4a5bcec72bf4bbca0d5e6129901e19e92d2f308ea85b",
	"Deals/Rollouts/424/v3/deals.json": "e4710a20196e71511826c82b24c8c2ef34a498a46913d9738f751ada85844b6e",
	"Discounts/Rollouts/424/v3/discounts.json": "431507d901cf8e5cc73bfeeeac43443f4945877aea63fb0357d1c65ef5d6c9d3",
	"Emails/Rollouts/424/v3/emails.json": "923068decd72579b41c5215dd9b9ad9234bbda92f74b3d6ea66606c6b0982b72",
	"Fees/Rollouts/424/v3/fees.json": "f4794b7dcdc084801627024924ed66f7ff5daa324f1b7b8cebcb72c318ed3ec3",
	"Invoices/Rollouts/424/v3/invoices.json": "2bfeed713636c603bcc2276ad12abc2213e21087c9c704a2a78d89acc3875c7f",
	"Line Items/Rollouts/424/v3/lineItems.json": "fce93b4b7ae8783f93312556d1b205079a1140e0fffd1f3e6741e63f4b596de9",
	"Meetings/Rollouts/424/v3/meetings.json": "60ad248add2e65c1c20e226e29992c14ea9281146a2d897330bb6d6f30362e03",
	"Notes/Rollouts/424/v3/notes.json": "d6da9731122655efa05c52e5f21e736392806aa7ee63b9c9ff28ca45b3d4de3b",
	"Orders/Rollouts/424/v3/orders.json": "9eac30562c21d4f00a91ae8811486afad10c697c115a5015e8534ffae30863f5",
	"Pipelines/Rollouts/145896/v3/pipelines.json": "dd0fc775d2dcd73df03471e354876e8416acf48b1a35fe41c85651874b209ce7",
	"Products/Rollouts/424/v3/products.json": "f08e032c5015bec92bd3b05f5a86ed27e830037736a7be10426e53626457cd5a",
	"Properties/Rollouts/145899/v3/properties.json": "62693afb6b06fe189a2ac0db2da6d863624919839aa07d5cd68090eb6526f788",
	"Quotes/Rollouts/424/v3/quotes.json": "ab0f0fb28f5c37871ca96ed25f398e5a89acce7349420c8ea0b6a42fb5f90d98",
	"Tasks/Rollouts/424/v3/tasks.json": "41eba2b831ed3706f845212e3b2c287fcc0fc38b4d4078024406e7e721a79c9c",
	"Taxes/Rollouts/424/v3/taxes.json": "334e4742f2cad9896c28127b55deb51d69bd89e13656b0b76cd0d5e086bf67b9",
}
QUICKBOOKS_XSD_FILES = (
	"IntuitRestServiceDef.xsd",
	"Finance.xsd",
	"IntuitBaseTypes.xsd",
	"IntuitNamesTypes.xsd",
	"SalesTax.xsd",
	"Report.xsd",
)
# SAP publishes the Service Layer's data model only as each installation's OData $metadata; these OpenAPI
# documents are generated from it (namespace SAPB1, OData v3 at /b1s/v1) by WSO2's Ballerina connector
# (ballerina-platform/module-ballerinax-sap.businessone, Apache-2.0, the commit of 2026-07-10), one per
# module the adapter reads.
SAP_B1_BASE = (
	"https://raw.githubusercontent.com/ballerina-platform/module-ballerinax-sap.businessone/"
	"f06691e0e4e9e571e0c1855c69ed6e224e125b07/docs/spec/"
)
SAP_B1_MODULES = (
	"administration",
	"banking",
	"businesspartners",
	"crm",
	"financials",
	"humanresources",
	"inventory",
	"projects",
	"purchasing",
	"sales",
)
XERO_BASE = "https://raw.githubusercontent.com/XeroAPI/Xero-OpenAPI/5a115f378321eab57cae540a5540797bc5e9ec71/"
XERO_DOCUMENTS = (
	("xero_accounting", "/api.xro/2.0"),
	("xero-projects", "/projects.xro/2.0"),
	("xero-identity", ""),
)
# The modules whose responses the Zoho adapters read, in the order a path is looked up (Books' debit notes
# module also documents /invoices, so invoices come first).
ZOHO_BOOKS_MEMBERS = (
	"organizations.yml",
	"chart-of-accounts.yml",
	"contacts.yml",
	"items.yml",
	"invoices.yml",
	"credit-notes.yml",
	"customer-payments.yml",
	"estimates.yml",
	"sales-order.yml",
	"sales-receipt.yml",
	"bills.yml",
	"vendor-credits.yml",
	"vendor-payments.yml",
	"purchase-order.yml",
	"journals.yml",
	"expenses.yml",
	"projects.yml",
	"locations.yml",
	"reporting-tags.yml",
	"bank-transactions.yml",
)
ZOHO_INVENTORY_MEMBERS = (
	"organizations.yml",
	"locations.yml",
	"categories.yml",
	"itemgroups.yml",
	"items.yml",
	"compositeitems.yml",
	"contacts.yml",
	"salesorders.yml",
	"invoices.yml",
	"credit-notes.yml",
	"customer-payments.yml",
	"purchaseorders.yml",
	"bills.yml",
	"vendor-credits.yml",
	"inventoryadjustments.yml",
	"transferorders.yml",
	"packages.yml",
	"salesreturns.yml",
	"purchasereceives.yml",
)


@dataclass(frozen=True)
class SpecDocument:
	"""One published API document: where it is (a URL, or a committed file under the repository), the
	server path prefix its paths sit under (a regular expression), and whether the API returns every
	declared property (``complete_objects``)."""

	source: str
	name: str
	url: str
	prefix_pattern: str
	complete_objects: bool
	format: str = JSON
	sha256: str | None = None
	committed: Path | None = None
	# A document published inside a zip archive (Zoho's): the archive is fetched and pinned, the document
	# is this member of it.
	member: str | None = None
	# A document published in parts (NetSuite's REST API Browser): each relative to ``url``, concatenated in
	# order, the pin covering the whole.
	parts: tuple[str, ...] = ()
	user_agent: str = USER_AGENT
	# Where the published document contradicts its own response examples (HighLevel's types an invoice's
	# items as strings, its example and its InvoiceItemDto make them objects): each correction replaces
	# the schema at a JSON pointer, its reason recorded beside it in the catalog.
	corrections: tuple[tuple[str, Any], ...] = ()

	def path(self) -> Path:
		if self.member is not None:
			return CACHE / self.source / Path(urlsplit(self.url).path).name
		return self.committed or CACHE / self.source / f"{self.name}.{self.format}"

	def verify(self, body: bytes) -> None:
		actual = hashlib.sha256(body).hexdigest()
		if self.sha256 is not None and actual != self.sha256:
			raise RuntimeError(
				f"{self.source}/{self.name} ({self.url}) has changed: sha256 {actual}, pinned {self.sha256}. "
				"Review the new document, re-validate the fixtures and update the pin in tools/specs.py."
			)

	def fetch(self) -> Path:
		if self.committed is not None:
			return self.committed
		body = b"".join(download(self.url + part, self.user_agent) for part in self.parts or ("",))
		self.verify(body)
		decode(self.format, self.document(body))
		target = self.path()
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_bytes(body)
		return target

	def document(self, body: bytes) -> bytes:
		"""The document's own bytes: ``body``, or its member when ``body`` is the archive."""
		if self.member is None:
			return body
		with zipfile.ZipFile(io.BytesIO(body)) as archive:
			return archive.read(self.member)

	def load(self) -> ApiSpec:
		path = self.path()
		if not path.is_file():
			path = self.fetch()
		body = path.read_bytes()
		self.verify(body)
		document = decode(self.format, self.document(body))
		for pointer, schema in self.corrections:
			corrected(document, pointer, schema)
		return ApiSpec.of(document, self.prefix_pattern)


def corrected(document: dict[str, Any], pointer: str, schema: Any) -> None:
	"""Replaces the value at a JSON pointer (RFC 6901) that must already exist."""
	*parents, last = [part.replace("~1", "/").replace("~0", "~") for part in pointer.lstrip("/").split("/")]
	node: Any = document
	for part in parents:
		node = node[part]
	if last not in node:
		raise KeyError(f"{pointer} is not in the document: the correction no longer applies")
	node[last] = schema


@dataclass(frozen=True)
class XsdDocument:
	"""A source whose only machine-readable spec is an XML Schema (tools/xsd.py): its files, fetched from a
	pinned commit, and the REST operations whose JSON responses are its types (path template → the type a
	GET returns)."""

	source: str
	name: str
	base_url: str
	files: tuple[str, ...]
	operations: tuple[tuple[str, str], ...]
	prefix_pattern: str = ""
	complete_objects: bool = False

	def path(self) -> Path:
		return CACHE / self.source / self.name

	def fetch(self) -> Path:
		target = self.path()
		target.mkdir(parents=True, exist_ok=True)
		for file in self.files:
			response = requests.get(
				self.base_url + file, timeout=FETCH_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
			)
			response.raise_for_status()
			(target / file).write_bytes(response.content)
		return target

	def load(self) -> ApiSpec:
		folder = self.path()
		if not all((folder / file).is_file() for file in self.files):
			folder = self.fetch()
		schemas = XsdSchemas(folder / file for file in self.files).schemas()
		paths = {
			template: {"get": {"responses": {"200": {"schema": {"$ref": f"{XSD_REF_PREFIX}{type_}"}}}}}
			for template, type_ in self.operations
		}
		return ApiSpec(schemas, XSD_REF_PREFIX, paths, self.prefix_pattern)


@dataclass(frozen=True)
class DtdDocument:
	"""A source whose only machine-readable spec is a DTD (tools/dtd.py): its files, fetched from a pinned
	commit and read as one, which the source's XML responses are proven against."""

	source: str
	name: str
	base_url: str
	files: tuple[str, ...]

	def path(self) -> Path:
		return CACHE / self.source / self.name

	def fetch(self) -> Path:
		target = self.path()
		target.mkdir(parents=True, exist_ok=True)
		for file in self.files:
			(target / file).write_bytes(download(self.base_url + file, USER_AGENT))
		return target

	def dtd(self) -> Dtd:
		folder = self.path()
		if not all((folder / file).is_file() for file in self.files):
			folder = self.fetch()
		return Dtd(*((folder / file).read_text() for file in self.files))


# Sage Intacct's XML gateway answers as its 2.1 response DTD declares (github.com/intacct/intacct_dtd,
# Apache-2.0, the commit of 2025-04-02; Sage's REST API is its successor, documented only behind
# developer.sage.com).
INTACCT_RESPONSE = DtdDocument(
	"sage_intacct",
	"response-2.1",
	"https://raw.githubusercontent.com/intacct/intacct_dtd/5e7ff4c65e897fa3c293ea6d15539a695d93896e/v2.1/",
	("intacct_response.v2.1.dtd",),
)
DTD_DOCUMENTS = (INTACCT_RESPONSE,)


def committed(source: str, name: str, relative: str, prefix_pattern: str) -> SpecDocument:
	return SpecDocument(source, name, f"file://{relative}", prefix_pattern, False, committed=ROOT / relative)


def download(url: str, user_agent: str) -> bytes:
	response = requests.get(url, timeout=FETCH_TIMEOUT_SECONDS, headers={"User-Agent": user_agent})
	response.raise_for_status()
	return response.content


def decode(format_: str, body: bytes) -> dict[str, Any]:
	if format_ == SPECTACLE:
		return swagger_of(body.decode())
	document = yaml.safe_load(body) if format_ == YAML else json.loads(body)
	if not isinstance(document, dict):
		raise TypeError("An API document is not an object.")
	return document


type Document = SpecDocument | XsdDocument


def zoho(
	product: str, prefix: str, archive_url: str, sha256: str, members: tuple[str, ...]
) -> list[Document]:
	"""Zoho's OpenAPI 3 documents for one product, one per module, all in one published archive."""
	return [
		SpecDocument(
			product, member.removesuffix(".yml"), archive_url, prefix, False, YAML, sha256, member=member
		)
		for member in members
	]


def catalog() -> list[Document]:
	return [
		SpecDocument(
			"etsy", "open-api-v3", "https://www.etsy.com/openapi/generated/oas/3.0.0.json", "", True
		),
		# Square API 2026-09-16 (square/connect-api-specification, "Release 2026-09-16").
		SpecDocument(
			"square_online",
			"connect-api-2026-09-16",
			"https://raw.githubusercontent.com/square/connect-api-specification/"
			"79ccc3f89e1527f7b943cc2f6628b6098572583e/api.json",
			"",
			False,
		),
		# The same document for the Square account gift cards and loyalty are written to (any source).
		SpecDocument(
			"square_stored_value",
			"connect-api-2026-09-16",
			"https://raw.githubusercontent.com/square/connect-api-specification/"
			"79ccc3f89e1527f7b943cc2f6628b6098572583e/api.json",
			"",
			False,
		),
		*(
			SpecDocument("bigcommerce", name, f"{BIGCOMMERCE_BASE}{name}.json", "", False, JSON, sha256)
			for name, sha256 in BIGCOMMERCE_DOCUMENTS.items()
		),
		# Squarespace's Commerce APIs (OpenAPI 3.1.1, linked from the docs' schemas page), published at an
		# unversioned URL, so pinned to the bytes fetched on 2026-10-16 (last modified 2026-10-12). Its paths
		# carry their /1.0, /v1 or /v2 prefix and its server is the bare API host.
		SpecDocument(
			"squarespace",
			"commerce-apis",
			"https://developers.squarespace.com/commerce-apis/latest/schema-processor-version-version-latest.json",
			"",
			False,
			JSON,
			"5dd4b1b7cd387d7db65f2f25418dac62c822ded5d90f4d41bba152fa305db368",
		),
		# Wix publishes no OpenAPI document: the response shapes the adapter reads, transcribed from Wix's
		# official REST reference (each schema cites its page). Its paths are full.
		committed("wix", "rest-reference", "tests/schemas/wix.json", ""),
		# Adobe Commerce / Magento Open Source 2.4.9 admin REST schema, "All inclusive" (Swagger 2.0,
		# AdobeDocs/commerce-webapi at the commit of 2026-06-02). Its basePath is /rest/default; a store
		# answers under /rest/<store view code>, so any store code is stripped.
		SpecDocument(
			"magento",
			"admin-schema-2.4.9",
			"https://raw.githubusercontent.com/AdobeDocs/commerce-webapi/"
			"ed5aedf78cb2dd1a28a39f3d03f7e5aabab65fb4/src/openapi/admin-schema-2.4.9.yaml",
			"/rest/[^/]+",
			False,
			YAML,
		),
		# Lightspeed eCom publishes no OpenAPI document: the responses the adapter reads, transcribed from the
		# official endpoint reference. Requests go to <cluster>/<language>/…, so the language is stripped.
		committed("lightspeed", "endpoint-reference", "tests/schemas/lightspeed.json", "/[a-z]{2}"),
		# Ecwid publishes no OpenAPI document: the responses the adapter reads, transcribed from the official
		# REST API reference. Requests go to /api/v3/<store id>/…, so that prefix is stripped.
		committed("ecwid", "rest-api-reference", "tests/schemas/ecwid.json", r"/api/v3/\d+"),
		# Odoo publishes no OpenAPI document (each database describes itself at /doc): the JSON-2 answers the
		# adapter reads, transcribed by tools/odoo_schema.py from Odoo 19's model definitions at a pinned commit.
		committed("odoo", "json2-model-reference", "tests/schemas/odoo.json", ""),
		# Business Central publishes no OpenAPI document for API v2.0 (only v1.0's): the entities the adapter
		# reads, transcribed by tools/bc_schema.py from the API (v2.0) resource-type pages.
		committed(
			"dynamics_business_central",
			"api-v2.0-reference",
			"tests/schemas/dynamics_business_central.json",
			"",
		),
		# Dataverse publishes no static OpenAPI document: the tables the Dynamics 365 Sales adapter reads,
		# transcribed by tools/dataverse_schema.py from Microsoft's table reference pages.
		committed("dynamics_sales", "dataverse-table-reference", "tests/schemas/dynamics_sales.json", ""),
		# Finance and operations apps publish no static OpenAPI document: the documented fields of the data
		# entities the adapter reads by default, transcribed by tools/fo_schema.py from the entity reference.
		committed(
			"dynamics_finance_operations",
			"data-entity-reference",
			"tests/schemas/dynamics_finance_operations.json",
			"",
		),
		# QuickBooks Online publishes its data model as XML Schemas (the JSON API returns the same types), in
		# its official Java SDK (intuit/QuickBooks-V3-Java-SDK, the commit of 2026-07-17). A query and a read
		# by id both answer an IntuitResponse.
		XsdDocument(
			"quickbooks_online",
			"v3-schema",
			"https://raw.githubusercontent.com/intuit/QuickBooks-V3-Java-SDK/"
			"c4d5dfad23ffebf0302356876d0dc389708513a9/ipp-v3-java-data/src/main/xsd/",
			QUICKBOOKS_XSD_FILES,
			(
				("/v3/company/{realmId}/query", "IntuitResponse"),
				("/v3/company/{realmId}/{entity}/{entityId}", "IntuitResponse"),
			),
		),
		# Xero's OpenAPI 3 documents (XeroAPI/Xero-OpenAPI, the commit of 2026-10-15; accounting 19.1.0). Each
		# API answers under its own path on api.xero.com; the identity API's /connections names the tenants.
		*(
			SpecDocument(
				"xero",
				name,
				f"{XERO_BASE}{name}.yaml",
				prefix,
				False,
				YAML,
			)
			for name, prefix in XERO_DOCUMENTS
		),
		# Zoho publishes its Books and Inventory OpenAPI documents as one archive each at an unversioned URL,
		# so each archive is pinned to the bytes fetched on 2026-10-19 (built 2026-10-08).
		*zoho(
			"zoho_books",
			"/books/v3",
			"https://www.zoho.com/books/api/v3/openapi-all.zip",
			"89b337bacfb1c3a37f08144536218cb3009102af48f3dcf848eef02934c42438",
			ZOHO_BOOKS_MEMBERS,
		),
		*zoho(
			"zoho_inventory",
			"/inventory/v1",
			"https://www.zoho.com/inventory/api/v1/openapi-all.zip",
			"ec5435338b235f18425c23db383ea2199feffbeb19f491fe94a8f0ca2b583863",
			ZOHO_INVENTORY_MEMBERS,
		),
		SpecDocument(
			"netsuite",
			"rest-record-2026.1",
			NETSUITE_RECORD_BROWSER,
			"/services/rest/record/v1",
			False,
			SPECTACLE,
			NETSUITE_RECORD_SHA256,
			parts=NETSUITE_RECORD_PARTS,
			user_agent=BROWSER_USER_AGENT,
		),
		*(
			SpecDocument("sap_business_one", module, f"{SAP_B1_BASE}{module}.json", "/b1s/v1", False)
			for module in SAP_B1_MODULES
		),
		# Acumatica publishes the Default endpoint's OpenAPI document only from each instance: the entities the
		# adapter reads, transcribed by tools/acumatica_schema.py from the official REST client's generated
		# models of Default 24.200.001 at a pinned commit.
		committed("acumatica", "default-24.200.001", "tests/schemas/acumatica.json", ""),
		# Agendrix's OpenAPI 3 document (agendrix/openapi, en/open_api.json, version 1.14.4, the commit of
		# 2026-10-15), which generates its developer portal's reference. Its server is /v1 on the API host.
		SpecDocument(
			"agendrix",
			"open-api-1.14.4",
			"https://raw.githubusercontent.com/agendrix/openapi/"
			"5a61151915d82b5ba1b26d1688fc3d4eb9256634/en/open_api.json",
			"/v1",
			False,
			JSON,
			"d118ecab8c953a90d96128f08b065bf1e42642ebcaab8fd5d9a7aa522f3277ff",
		),
		# HighLevel's API v2 OpenAPI documents (GoHighLevel/highlevel-api-docs, CC0, the commit of 2026-06-19),
		# one per app, each pinned to its SHA-256. Their paths are full on services.leadconnectorhq.com.
		*(
			SpecDocument(
				"highlevel",
				name,
				f"{HIGHLEVEL_BASE}{name}.json",
				"",
				False,
				JSON,
				sha256,
				corrections=HIGHLEVEL_CORRECTIONS.get(name, ()),
			)
			for name, sha256 in HIGHLEVEL_DOCUMENTS.items()
		),
		# Salesforce publishes no static OpenAPI document for its standard objects: the REST API answers the
		# adapter reads, transcribed by tools/salesforce_schema.py from the REST API Developer Guide and the
		# Object Reference.
		committed("salesforce", "rest-api-reference", "tests/schemas/salesforce.json", ""),
		# HubSpot's CRM v3 documents; their paths are full on api.hubapi.com.
		*(
			SpecDocument(
				"hubspot",
				Path(member).stem,
				HUBSPOT_BASE + member.replace(" ", "%20"),
				"",
				False,
				JSON,
				sha256,
			)
			for member, sha256 in HUBSPOT_DOCUMENTS.items()
		),
		# PeopleSoft publishes no OpenAPI document for the Integration Broker: the Query Access Service's REST
		# answer, transcribed from PeopleBooks ("Executing the Query").
		committed("peoplesoft", "qas-rest-reference", "tests/schemas/peoplesoft-qas.json", ""),
		# SuiteQL's endpoint is outside the Record API: its response, transcribed from the official page.
		committed("netsuite", "suiteql", "tests/schemas/netsuite-suiteql.json", ""),
	]


def for_source(source: str) -> list[Document]:
	return [document for document in catalog() if document.source == source]


def sources() -> list[str]:
	return list(dict.fromkeys(document.source for document in catalog()))


def main() -> int:
	"""``python -m tools.specs [<source>]``: fetches the pinned documents into .cache/specs."""
	only = sys.argv[1] if len(sys.argv) > 1 else None
	for document in [*catalog(), *DTD_DOCUMENTS]:
		if only is None or document.source == only:
			sys.stdout.write(f"{document.source}/{document.name}: {document.fetch()}\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
