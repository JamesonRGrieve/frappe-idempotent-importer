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
	"admin-management-orders": "b1022156b66d9a0dd89f3f418f0e51dec626686c87906a8f42c417cdcc56c5c2",
	"admin-management-order-operations": "85058cb818f3da2d56022ded545b6c8ea7730747bf62bfeeabce602164c4bb34",
	"admin-content-pages": "97008b6fc2af709f8b49477e6a49eb6e33f6e1d0108b22ffcaf229cc1d57ddfd",
	"admin-content-store-content": "29859d12a29def2e2881faee7dd5d312af3dd9936d8842da40453dc68e756dbc",
	"admin-management-redirects": "c06587f9727a44d2a8e4192fc304f90dfd8f42abf8a71d27eaaad08c9f6e5df0",
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
		return ApiSpec.of(decode(self.format, self.document(body)), self.prefix_pattern)


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
	for document in catalog():
		if only is None or document.source == only:
			sys.stdout.write(f"{document.source}/{document.name}: {document.fetch()}\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
