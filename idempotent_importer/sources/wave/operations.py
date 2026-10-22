# SPDX-License-Identifier: AGPL-3.0-or-later
"""The GraphQL operations the Wave adapter sends to Wave's public API (gql.waveapps.com/graphql/public), each
a named query of one business's records a page at a time (``page``/``pageSize``, ``pageInfo.totalPages``).
They are proven against Wave's schema (tests/schemas/wave.json, tools/wave_schema.py) by tools/prove.py."""

from __future__ import annotations

from dataclasses import dataclass

MONEY = "{ value currency { code } }"
ADDRESS = "{ addressLine1 addressLine2 city postalCode province { code name } country { code name } }"
PARTY = f"""id name firstName lastName displayId email mobile phone tollFree website internalNotes
	currency {{ code }} address {ADDRESS} isArchived modifiedAt"""
ITEM_TAXES = f"taxes {{ salesTax {{ id name abbreviation }} amount {MONEY} }}"
ITEMS = f"items {{ id description quantity unitPrice subtotal {MONEY} product {{ id name }} {ITEM_TAXES} }}"
DOCUMENT = f"""id status title currency {{ code }} exchangeRate customer {{ id }} memo footer poNumber
	subtotal {MONEY} discountTotal {MONEY} taxTotal {MONEY} total {MONEY} modifiedAt {ITEMS}"""
PAYMENTS = """payments { id paymentDate amount memo paymentMethod exchangeRate active account { id }
	customer { id } paymentCurrency { code } }"""
PAGE_INFO = "pageInfo { currentPage totalPages }"


@dataclass(frozen=True)
class Operation:
	"""A paged query: its name, the business field it lists, whether it filters by ``modifiedAtAfter``, and
	its document."""

	name: str
	field: str
	changed: bool
	document: str


def listing(name: str, field: str, arguments: str, selection: str, changed: bool = True) -> Operation:
	since = ", $since: DateTime" if changed else ""
	filtered = ", modifiedAtAfter: $since" if changed else ""
	document = f"""query {name}($business: ID!, $page: Int!, $pageSize: Int!{since}) {{
	business(id: $business) {{
		{field}(page: $page, pageSize: $pageSize{arguments}{filtered}) {{
			{PAGE_INFO}
			edges {{ node {{ {selection} }} }}
		}}
	}}
}}"""
	return Operation(name, field, changed, document)


ACCOUNTS = listing(
	"Accounts",
	"accounts",
	", isArchived: null",
	"""id name description displayId currency { code } type { value } subtype { value name }
	normalBalanceType isArchived""",
	changed=False,
)
SALES_TAXES = listing(
	"SalesTaxes",
	"salesTaxes",
	", isArchived: null",
	"id name abbreviation taxNumber rate isCompound isArchived",
)
PRODUCTS = listing(
	"Products",
	"products",
	", isArchived: null, sort: [CREATED_AT_ASC]",
	"id name description unitPrice isSold isBought isArchived incomeAccount { id } expenseAccount { id }",
)
CUSTOMERS = listing("Customers", "customers", ", sort: [CREATED_AT_ASC]", PARTY)
VENDORS = listing("Vendors", "vendors", "", PARTY)
ESTIMATES = listing(
	"Estimates",
	"estimates",
	", sort: CREATED_AT_ASC",
	f"{DOCUMENT} estimateNumber estimateDate dueDate",
)
INVOICES = listing(
	"Invoices",
	"invoices",
	", sort: [CREATED_AT_ASC]",
	f"{DOCUMENT} invoiceNumber invoiceDate dueDate",
)
INVOICE_PAYMENTS = listing(
	"InvoicePayments",
	"invoices",
	", sort: [CREATED_AT_ASC]",
	f"id customer {{ id }} currency {{ code }} {PAYMENTS}",
)
BUSINESSES = Operation(
	"Businesses",
	"businesses",
	False,
	"""query Businesses($page: Int!, $pageSize: Int!) {
	businesses(page: $page, pageSize: $pageSize) {
		pageInfo { currentPage totalPages }
		edges { node { id name currency { code } } }
	}
}""",
)
OPERATIONS = (
	ACCOUNTS,
	SALES_TAXES,
	PRODUCTS,
	CUSTOMERS,
	VENDORS,
	ESTIMATES,
	INVOICES,
	INVOICE_PAYMENTS,
	BUSINESSES,
)


def by_name(name: str) -> Operation:
	return next(operation for operation in OPERATIONS if operation.name == name)
