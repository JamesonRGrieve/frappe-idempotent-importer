# SPDX-License-Identifier: AGPL-3.0-or-later
"""How one PeopleSoft installation answers for each kind: which query it runs and which of the query's
columns hold what. Every PeopleSoft installation is the customer's own (records extended, renamed and added,
trees, effective dating, setIDs), so nothing is assumed: the connection's field map (JSON, its "Field Map")
names, per kind, a public or private PeopleSoft Query that the Integration Broker's Query Access Service
runs, and the columns that answer each role:

    {
      "node": "PSFT_HR",
      "owner": "PUBLIC",
      "max_rows": 5000,
      "kinds": {
        "employee": {
          "query": "ERP_EMPLOYEES",
          "key": "EMPLID",
          "fields": {"first_name": "FIRST_NAME", "last_name": "LAST_NAME", "status": "EMPL_STATUS"},
          "values": {"status": {"A": "Active", "L": "Active", "T": "Left"}},
          "after_prompt": "AFTER_EMPLID",
          "since_prompt": "CHANGED_SINCE"
        },
        "payment_entry": [
          {"query": "ERP_AP_PAYMENTS", "type": "ApPayment", "key": ["BANK_CD", "PYMNT_ID"], "fields": {...},
           "fixed": {"payment_type": "Pay", "party_type": "Supplier"}},
          {"query": "ERP_AR_PAYMENTS", "type": "ArPayment", ...}
        ]
      }
    }

``node`` is the Integration Broker node the REST listening connector answers for (the default local node);
``owner`` the queries' owner type (PUBLIC or PRIVATE); ``max_rows`` the most rows one request asks for. A
kind is one query, or several read in turn (a kind PeopleSoft keeps in several places: AP and AR payments),
each its own identity ``type``. A role is one column or several (a composite key: SETID and DEPTID); a column
is matched case-insensitively, with or without its record alias (``A.EMPLID`` is ``EMPLID``). ``values``
translates a role's codes into the contract's vocabulary (PeopleSoft's status codes are translate values
of the customer's own), ``fixed`` gives a role one value for every row. A document kind's query answers one
row per line, its header columns repeated: the rows of one key are one document. ``after_prompt`` pages a
query by key (the query's prompt for "key after", its rows ordered by key); ``since_prompt`` narrows an
incremental run (the query's prompt for "changed on or after", a YYYY-MM-DD date). A kind the map doesn't
name is unavailable: the run skips it and says so."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from idempotent_importer.core.errors import KindUnavailable, SourceError
from idempotent_importer.core.identity import TYPE
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue

ALIAS_SEPARATOR = "."
KEY_SEPARATOR = ":"
DEFAULT_OWNER = "PUBLIC"
OWNERS = frozenset({"PUBLIC", "PRIVATE"})
DEFAULT_MAX_ROWS = 5000
LINE_ROLES = ("line_name", "line_qty", "line_amount", "line_account")
PARTY_ROLES = (
	"type",
	"first_name",
	"last_name",
	"email",
	"phone",
	"line1",
	"line2",
	"city",
	"state",
	"postcode",
	"country",
	"tax_id",
	"currency",
	"disabled",
)
DOCUMENT_ROLES = ("number", "due_date", "reference", "remarks", "tax", "posted", *LINE_ROLES)


@dataclass(frozen=True)
class Roles:
	"""What a kind's query must answer, what it may, and the identity type it mints by default."""

	type: str
	required: tuple[str, ...]
	optional: tuple[str, ...] = ()


ROLES: dict[Kind, Roles] = {
	Kind.DEPARTMENT: Roles("Department", ("name",), ("parent", "disabled")),
	Kind.DESIGNATION: Roles("Job", ("name",)),
	Kind.EMPLOYEE: Roles(
		"Employee",
		("first_name", "status"),
		(
			"middle_name",
			"last_name",
			"gender",
			"date_of_birth",
			"date_of_joining",
			"relieving_date",
			"employee_number",
			"department",
			"designation",
			"reports_to",
			"company_email",
			"personal_email",
			"phone",
		),
	),
	Kind.CUSTOMER: Roles("Customer", ("name",), PARTY_ROLES),
	Kind.SUPPLIER: Roles("Supplier", ("name",), PARTY_ROLES),
	Kind.ACCOUNT: Roles(
		"Account", ("name", "root_type"), ("number", "account_type", "parent", "is_group", "currency")
	),
	Kind.SALES_INVOICE: Roles(
		"ArInvoice", ("date", "party", "currency", "total", *LINE_ROLES[1:3]), DOCUMENT_ROLES
	),
	Kind.PURCHASE_INVOICE: Roles(
		"Voucher", ("date", "party", "currency", "total", *LINE_ROLES[1:3]), DOCUMENT_ROLES
	),
	Kind.PAYMENT_ENTRY: Roles(
		"Payment",
		("date", "party", "amount", "currency", "payment_type", "party_type"),
		("reference", "remarks", "posted", "allocation_document", "allocation_amount"),
	),
	Kind.JOURNAL_ENTRY: Roles(
		"Journal",
		("date", "line_account"),
		("number", "remarks", "posted", "line_amount", "line_debit", "line_credit"),
	),
}
KINDS: tuple[Kind, ...] = tuple(ROLES)
# The kinds whose query answers a row per line, its header repeated.
DOCUMENT_KINDS = frozenset({Kind.SALES_INVOICE, Kind.PURCHASE_INVOICE, Kind.JOURNAL_ENTRY})


@dataclass(frozen=True)
class QueryMap:
	"""One query a kind is read from: its name, identity type, key columns, each role's columns, value
	translations and fixed values, and its paging and change prompts."""

	kind: Kind
	query: str
	type: str
	key: tuple[str, ...]
	fields: Mapping[str, tuple[str, ...]]
	values: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
	fixed: Mapping[str, str] = field(default_factory=dict)
	after_prompt: str | None = None
	since_prompt: str | None = None


@dataclass(frozen=True)
class FieldMap:
	node: str
	owner: str
	max_rows: int
	kinds: Mapping[Kind, tuple[QueryMap, ...]]

	def queries(self, kind: Kind) -> tuple[QueryMap, ...]:
		"""The kind's queries, or KindUnavailable naming what the operator must map."""
		maps = self.kinds.get(kind)
		if not maps:
			raise KindUnavailable(
				f"PeopleSoft's {kind.value} isn't in the connection's field map: name its query and columns "
				"(see coverage/peoplesoft.md)."
			)
		return maps

	def type_of(self, kind: Kind) -> str:
		"""The identity type a reference to the kind names (its first query's)."""
		maps = self.kinds.get(kind)
		return maps[0].type if maps else ROLES[kind].type


def field_map(text: str) -> FieldMap:
	try:
		configured = json.loads(text) if text.strip() else None
	except ValueError:
		raise SourceError("PeopleSoft's field map isn't JSON.") from None
	if not isinstance(configured, dict):
		raise SourceError(
			"PeopleSoft needs a field map naming its Integration Broker node and each kind's query."
		)
	node = configured.get("node")
	if not isinstance(node, str) or not node:
		raise SourceError('PeopleSoft\'s field map names no Integration Broker "node".')
	owner = str(configured.get("owner") or DEFAULT_OWNER).upper()
	if owner not in OWNERS:
		raise SourceError(f"PeopleSoft's query owner is PUBLIC or PRIVATE, not {owner}.")
	max_rows = configured.get("max_rows", DEFAULT_MAX_ROWS)
	if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
		raise SourceError("PeopleSoft's max_rows is a positive whole number.")
	kinds_given = configured.get("kinds") or {}
	if not isinstance(kinds_given, dict):
		raise SourceError("PeopleSoft's field map's kinds are an object of kinds.")
	kinds: dict[Kind, tuple[QueryMap, ...]] = {}
	for name, given in kinds_given.items():
		kind = Kind.parse(name)
		if kind not in ROLES:
			raise SourceError(f"PeopleSoft can't map {name} (it reads {', '.join(k.value for k in ROLES)}).")
		entries = given if isinstance(given, list) else [given]
		maps = tuple(query_map(kind, entry) for entry in entries)
		if len({m.type for m in maps}) != len(maps):
			raise SourceError(f"PeopleSoft's {name} queries each need a type of their own.")
		kinds[kind] = maps
	return FieldMap(node, owner, max_rows, kinds)


def query_map(kind: Kind, given: JsonValue) -> QueryMap:
	what = f"PeopleSoft's {kind.value}"
	if not isinstance(given, dict):
		raise SourceError(f"{what} is a query map (an object).")
	query = given.get("query")
	if not isinstance(query, str) or not query:
		raise SourceError(f"{what} names no query.")
	roles = ROLES[kind]
	type_ = str(given.get("type") or roles.type)
	if not TYPE.match(type_):
		raise SourceError(f"{what}'s type {type_!r} is letters and digits.")
	fields = {
		role: columns(columns_given, f"{what}'s {role}")
		for role, columns_given in mapping(given, "fields", what).items()
	}
	fixed = {role: str(value) for role, value in mapping(given, "fixed", what).items()}
	unknown = (set(fields) | set(fixed)) - set(roles.required) - set(roles.optional)
	if unknown:
		raise SourceError(f"{what} has no role {', '.join(sorted(unknown))}.")
	missing = [role for role in roles.required if role not in fields and role not in fixed]
	if missing:
		raise SourceError(f"{what} needs the field map to name {', '.join(missing)}.")
	values: dict[str, dict[str, str]] = {}
	for role, translations in mapping(given, "values", what).items():
		if not isinstance(translations, dict):
			raise SourceError(f"{what}'s {role} values are codes and what each means.")
		values[role] = {str(code): str(meaning) for code, meaning in translations.items()}
	key = columns(given.get("key"), f"{what}'s key")
	after = optional_text(given.get("after_prompt"), f"{what}'s after_prompt")
	if after and len(key) != 1:
		raise SourceError(f"{what} pages by its key, which must be one column for an after_prompt.")
	return QueryMap(
		kind,
		query,
		type_,
		key,
		fields,
		values,
		fixed,
		after,
		optional_text(given.get("since_prompt"), f"{what}'s since_prompt"),
	)


def mapping(given: dict[str, JsonValue], name: str, what: str) -> dict[str, JsonValue]:
	value = given.get(name) or {}
	if not isinstance(value, dict):
		raise SourceError(f"{what}'s {name} is an object of roles.")
	return value


def columns(value: JsonValue, what: str) -> tuple[str, ...]:
	if isinstance(value, str) and value:
		return (value,)
	if isinstance(value, list) and value and all(isinstance(item, str) and item for item in value):
		return tuple(item for item in value if isinstance(item, str))
	raise SourceError(f"{what} is a column, or a list of columns.")


def optional_text(value: JsonValue, what: str) -> str | None:
	if value is None or value == "":
		return None
	if not isinstance(value, str):
		raise SourceError(f"{what} is a prompt's unique name.")
	return value


def column_name(column: str) -> str:
	"""A column as a query answers it, without its record alias, in upper case."""
	return column.rpartition(ALIAS_SEPARATOR)[2].upper()
