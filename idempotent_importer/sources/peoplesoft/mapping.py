# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure PeopleSoft → desired ERPNext state, every value read through the connection's field map: a role's
columns (joined by ``:`` when several, as keys are), translated by the map's ``values``, or the map's
``fixed`` value. Departments, jobs and employees land in HRMS; customers, suppliers, the chart of accounts,
AR and AP documents, payments and journals in the books. Dates are a column's YYYY-MM-DD (or the date of its
date and time); flags are Y/N (or 1/0, true/false); amounts exact as PeopleSoft answers them. What the
mapped values can't make exactly is Unsupported, never approximated."""

from __future__ import annotations

from decimal import Decimal

from idempotent_importer.contract import (
	Address,
	Allocation,
	Employee,
	JournalLine,
	LedgerAccount,
	Party,
	Payment,
	TradeDocument,
	TradeLine,
	TradeTax,
	account_fields,
	address_fields,
	employee_fields,
	journal_fields,
	order_line,
	party_fields,
	payment_fields,
	trade_fields,
	tree_fields,
)
from idempotent_importer.core.desired import Desired, Mapper
from idempotent_importer.core.errors import SourceError, Unsupported
from idempotent_importer.core.fields import Fields
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.money import decimal_of
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.source import ConnectionConfig
from idempotent_importer.sources.ledger import country_code, day
from idempotent_importer.sources.peoplesoft.fieldmap import (
	KEY_SEPARATOR,
	FieldMap,
	QueryMap,
	column_name,
	field_map,
)
from idempotent_importer.sources.peoplesoft.reader import key_text

TRUE = frozenset({"Y", "YES", "1", "TRUE", "T"})
FALSE = frozenset({"N", "NO", "0", "FALSE", "F", ""})
PAYMENT_DOCUMENTS = {"Customer": Kind.SALES_INVOICE, "Supplier": Kind.PURCHASE_INVOICE}


class Values:
	"""One document's (or record's) rows read through its query map."""

	def __init__(self, query: QueryMap, rows: list[dict[str, JsonValue]]) -> None:
		self.query = query
		self.rows = rows

	def raw(self, role: str, row: dict[str, JsonValue] | None = None) -> str | None:
		if role in self.query.fixed:
			return self.query.fixed[role]
		columns = self.query.fields.get(role)
		if not columns:
			return None
		source = row if row is not None else self.rows[0]
		found = [source.get(column_name(column)) for column in columns]
		if all(value is None or str(value).strip() == "" for value in found):
			return None
		text = key_text(found) if len(found) > 1 else str(found[0]).strip()
		return self.query.values.get(role, {}).get(text, text)

	def text(self, role: str, row: dict[str, JsonValue] | None = None) -> str | None:
		return self.raw(role, row) or None

	def required(self, role: str, row: dict[str, JsonValue] | None = None) -> str:
		value = self.text(role, row)
		if value is None:
			raise Unsupported(f"its {role} is empty")
		return value

	def number(self, role: str, row: dict[str, JsonValue] | None = None) -> Decimal | None:
		value = self.text(role, row)
		if value is None:
			return None
		try:
			return decimal_of(value)
		except ValueError:
			raise SourceError(f"PeopleSoft's {self.query.query} {role} is not a number: {value!r}") from None

	def required_number(self, role: str, row: dict[str, JsonValue] | None = None) -> Decimal:
		value = self.number(role, row)
		if value is None:
			raise Unsupported(f"its {role} is empty")
		return value

	def date(self, role: str, row: dict[str, JsonValue] | None = None) -> str | None:
		return day(self.text(role, row))

	def flag(self, role: str, default: bool = False) -> bool:
		value = self.raw(role)
		if value is None:
			return default
		if value.upper() in TRUE:
			return True
		if value.upper() in FALSE:
			return False
		raise SourceError(f"PeopleSoft's {self.query.query} {role} is not Y or N: {value!r}")


class Context:
	def __init__(self, config: ConnectionConfig) -> None:
		self.config = config
		self.map: FieldMap = field_map(config.field_map)

	def values(self, kind: Kind, node: Node) -> Values:
		type_ = node.string("type")
		query = next((q for q in self.map.kinds.get(kind, ()) if q.type == type_), None)
		if query is None:
			raise SourceError(f"PeopleSoft's field map has no {kind.value} query of type {type_}.")
		rows = [row.raw() for row in node.children("rows")]
		if not rows:
			raise SourceError(f"{node.path} has no rows")
		return Values(query, rows)

	def gid(self, kind: Kind, key: str | None) -> str | None:
		return self.config.identity(self.map.type_of(kind), key) if key else None


class PeopleSoftMapper:
	kind: Kind
	version = 1

	def __init__(self, context: Context) -> None:
		self.context = context


class DepartmentMapper(PeopleSoftMapper):
	"""A department under its parent department (in the department tree the query flattens)."""

	kind = Kind.DEPARTMENT

	def map(self, node: Node) -> Desired | None:
		values = self.context.values(self.kind, node)
		fields = tree_fields(
			values.required("name"),
			self.context.gid(Kind.DEPARTMENT, values.text("parent")),
			disabled=values.flag("disabled"),
		)
		return Desired(self.kind, node.id(), fields)


class DesignationMapper(PeopleSoftMapper):
	"""A job code."""

	kind = Kind.DESIGNATION

	def map(self, node: Node) -> Desired | None:
		values = self.context.values(self.kind, node)
		return Desired(self.kind, node.id(), {"name": values.required("name")})


class EmployeeMapper(PeopleSoftMapper):
	"""A person's current job: names, gender, dates, status (translated by the map), department, job and
	supervisor, emails and phone."""

	kind = Kind.EMPLOYEE

	def map(self, node: Node) -> Desired | None:
		values = self.context.values(self.kind, node)
		gid = self.context.gid
		employee = Employee(
			first_name=values.required("first_name"),
			status=values.required("status"),
			middle_name=values.text("middle_name"),
			last_name=values.text("last_name"),
			gender=values.text("gender"),
			date_of_birth=values.date("date_of_birth"),
			date_of_joining=values.date("date_of_joining"),
			relieving_date=values.date("relieving_date"),
			employee_number=values.text("employee_number") or node.string("source_id"),
			department=gid(Kind.DEPARTMENT, values.text("department")),
			designation=gid(Kind.DESIGNATION, values.text("designation")),
			reports_to=gid(Kind.EMPLOYEE, values.text("reports_to")),
			company_email=values.text("company_email"),
			personal_email=values.text("personal_email"),
			phone=values.text("phone"),
		)
		return Desired(self.kind, node.id(), employee_fields(employee))


class PartyMapper(PeopleSoftMapper):
	"""A customer or supplier: its name and type (Company unless the map says Individual), contact, address
	(when it has a street, a city and a country the map makes an ISO code), tax id, currency and status."""

	def __init__(self, context: Context, kind: Kind) -> None:
		super().__init__(context)
		self.kind = kind

	def map(self, node: Node) -> Desired | None:
		values = self.context.values(self.kind, node)
		name = values.required("name")
		party = Party(
			name=name,
			type=values.text("type") or "Company",
			first_name=values.text("first_name"),
			last_name=values.text("last_name"),
			company=name,
			email=values.text("email"),
			phone=values.text("phone"),
			addresses=self._address(values, name),
			tax_id=values.text("tax_id"),
			currency=values.text("currency"),
			disabled=values.flag("disabled"),
		)
		return Desired(self.kind, node.id(), party_fields(party))

	@staticmethod
	def _address(values: Values, name: str) -> list[Fields]:
		line1, city = values.text("line1"), values.text("city")
		country = country_code(values.text("country"))
		if not (line1 and city and country):
			return []
		address = Address(
			"Billing",
			name,
			name,
			line1,
			values.text("line2"),
			city,
			values.text("state"),
			values.text("postcode"),
			country,
			None,
			None,
		)
		return [address_fields(address)]


class AccountMapper(PeopleSoftMapper):
	"""A ChartField account, its root type (translated by the map from PeopleSoft's account types) and
	parent (in the account tree the query flattens)."""

	kind = Kind.ACCOUNT

	def map(self, node: Node) -> Desired | None:
		values = self.context.values(self.kind, node)
		account = LedgerAccount(
			number=values.text("number") or node.string("source_id"),
			name=values.required("name"),
			root_type=values.required("root_type"),
			account_type=values.text("account_type"),
			parent=self.context.gid(Kind.ACCOUNT, values.text("parent")),
			is_group=values.flag("is_group"),
			currency=values.text("currency"),
		)
		return Desired(self.kind, node.id(), account_fields(account))


class InvoiceMapper(PeopleSoftMapper):
	"""An AR invoice or an AP voucher: its rows are its lines (name, quantity, extended amount, account),
	its header tax one amount; posted ones are submitted. Lines and tax must make its total exactly."""

	def __init__(self, context: Context, kind: Kind, party: Kind) -> None:
		super().__init__(context)
		self.kind = kind
		self.party = party

	def map(self, node: Node) -> Desired | None:
		values = self.context.values(self.kind, node)
		currency = values.required("currency")
		number = values.text("number") or node.string("source_id")
		lines = [
			TradeLine(
				order_line(
					str(index),
					None,
					None,
					values.text("line_name", row) or number,
					values.required_number("line_qty", row),
					values.required_number("line_amount", row),
					currency,
				),
				account=self.context.gid(Kind.ACCOUNT, values.text("line_account", row)),
			)
			for index, row in enumerate(values.rows)
		]
		tax = values.number("tax")
		document = TradeDocument(
			number=number,
			date=_required(values.date("date"), "date"),
			party=_required(self.context.gid(self.party, values.text("party")), "party"),
			currency=currency,
			lines=lines,
			taxes=[TradeTax("Tax", tax)] if tax else [],
			total=values.required_number("total"),
			submitted=values.flag("posted", default=True),
			due_date=values.date("due_date"),
			reference=values.text("reference"),
			remarks=values.text("remarks"),
			is_return=values.required_number("total") < 0,
		)
		return Desired(self.kind, node.id(), trade_fields(document))


class PaymentMapper(PeopleSoftMapper):
	"""A payment from a customer or to a supplier (the map's party type and direction), allocated to the
	invoices its rows name."""

	kind = Kind.PAYMENT_ENTRY

	def map(self, node: Node) -> Desired | None:
		values = self.context.values(self.kind, node)
		party_type = values.required("party_type")
		document_kind = PAYMENT_DOCUMENTS.get(party_type)
		if document_kind is None:
			raise Unsupported(f"a payment of a {party_type} (a Customer or a Supplier)")
		party_kind = Kind.CUSTOMER if party_type == "Customer" else Kind.SUPPLIER
		allocations = [
			Allocation(
				document_kind,
				_required(
					self.context.gid(document_kind, values.text("allocation_document", row)), "allocation"
				),
				values.required_number("allocation_amount", row),
			)
			for row in values.rows
			if values.text("allocation_document", row)
		]
		payment = Payment(
			payment_type=values.required("payment_type"),
			party_type=party_type,
			party=_required(self.context.gid(party_kind, values.text("party")), "party"),
			date=_required(values.date("date"), "date"),
			amount=values.required_number("amount"),
			currency=values.required("currency"),
			allocations=allocations,
			reference=values.text("reference") or node.string("source_id"),
			remarks=values.text("remarks"),
			submitted=values.flag("posted", default=True),
		)
		return Desired(self.kind, node.id(), payment_fields(payment))


class JournalMapper(PeopleSoftMapper):
	"""A journal: its rows are its lines, each an account and a signed amount (a debit when positive, as
	PeopleSoft's monetary amounts are) or a debit and a credit; posted ones are submitted."""

	kind = Kind.JOURNAL_ENTRY

	def map(self, node: Node) -> Desired | None:
		values = self.context.values(self.kind, node)
		lines = [self._line(values, row) for row in values.rows]
		number = values.text("number") or node.string("source_id").split(KEY_SEPARATOR)[-1]
		fields = journal_fields(
			number,
			_required(values.date("date"), "date"),
			lines,
			values.flag("posted", default=True),
			values.text("remarks"),
		)
		return Desired(self.kind, node.id(), fields)

	def _line(self, values: Values, row: dict[str, JsonValue]) -> JournalLine:
		account = _required(self.context.gid(Kind.ACCOUNT, values.text("line_account", row)), "line account")
		signed = values.number("line_amount", row)
		if signed is not None:
			return JournalLine(account, max(signed, Decimal(0)), max(-signed, Decimal(0)))
		return JournalLine(
			account,
			values.number("line_debit", row) or Decimal(0),
			values.number("line_credit", row) or Decimal(0),
		)


def _required(value: str | None, what: str) -> str:
	if value is None:
		raise Unsupported(f"its {what} is empty")
	return value


def mappers(config: ConnectionConfig) -> list[Mapper]:
	context = Context(config)
	return [
		DepartmentMapper(context),
		DesignationMapper(context),
		EmployeeMapper(context),
		PartyMapper(context, Kind.CUSTOMER),
		PartyMapper(context, Kind.SUPPLIER),
		AccountMapper(context),
		InvoiceMapper(context, Kind.SALES_INVOICE, Kind.CUSTOMER),
		InvoiceMapper(context, Kind.PURCHASE_INVOICE, Kind.SUPPLIER),
		PaymentMapper(context),
		JournalMapper(context),
	]
