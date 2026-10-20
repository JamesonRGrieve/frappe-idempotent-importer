# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which data entity and which of its fields each kind is read from. A finance and operations environment's
data model is the customer's own (entities extended, renamed, added), and Microsoft documents only a few
standard entities' fields, so the connection carries a field map (JSON, the connection's "Field Map"):

    {"customer": {"entity": "CustomersV3", "fields": {"email": "PrimaryContactEmail"}}, ...}

Each kind starts from the defaults below, which name only what Microsoft's entity reference documents
(learn.microsoft.com/dynamics365/fin-ops-core/dev-itpro/data-entities/…, cited per entity); the map adds or
replaces entities and fields. A field is matched case-insensitively: the reference spells fields as their
data management names (``CUSTOMERACCOUNT``), OData as the entity's (``CustomerAccount``). A kind whose
required fields the map doesn't give is unavailable until the operator gives them."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from idempotent_importer.core.errors import KindUnavailable, SourceError
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node

DOCS = "https://learn.microsoft.com/en-us/dynamics365/fin-ops-core/dev-itpro/data-entities/"


@dataclass(frozen=True)
class EntityMap:
	"""A kind's data entity (and, for a document, its lines' entity), the fields it is read by (role →
	field name), and the roles it can't be mirrored without."""

	entity: str
	fields: Mapping[str, str]
	required: tuple[str, ...]
	lines: str | None = None
	line_fields: Mapping[str, str] = field(default_factory=dict)
	line_required: tuple[str, ...] = ()
	cited: str = ""

	def missing(self) -> list[str]:
		entities = [name for name, given in (("entity", self.entity), ("lines", self.lines)) if given == ""]
		return (
			entities
			+ [role for role in self.required if not self.fields.get(role)]
			+ [f"lines.{role}" for role in self.line_required if not self.line_fields.get(role)]
		)


DEFAULTS: dict[Kind, EntityMap] = {
	# entity-customers-v3-customerv3
	Kind.CUSTOMER: EntityMap(
		"CustomersV3",
		{
			"key": "CUSTOMERACCOUNT",
			"name": "ORGANIZATIONNAME",
			"party_type": "PARTYTYPE",
			"first_name": "PERSONFIRSTNAME",
			"middle_name": "PERSONMIDDLENAME",
			"last_name": "PERSONLASTNAME",
			"currency": "SALESCURRENCYCODE",
			"city": "ADDRESSCITY",
			"state": "ADDRESSSTATE",
			"postcode": "ADDRESSZIPCODE",
			"country": "ADDRESSCOUNTRYREGIONID",
		},
		("key", "name"),
		cited=DOCS + "entity-customers-v3-customerv3",
	),
	# entity-released-products-v2-releasedproductv2: the documented fields name no product name but its
	# search alias.
	Kind.PRODUCT: EntityMap(
		"ReleasedProductsV2",
		{
			"key": "ITEMNUMBER",
			"name": "SEARCHNAME",
			"price": "SALESPRICE",
			"subtype": "PRODUCTSUBTYPE",
		},
		("key", "name"),
		cited=DOCS + "entity-released-products-v2-releasedproductv2",
	),
	# entity-sales-order-headers-v2-salesorderheaderv2 and entity-sales-order-lines-v2-salesorderline:
	# neither documents an order date or a tax, so the order date must be mapped.
	Kind.SALES_ORDER: EntityMap(
		"SalesOrderHeadersV2",
		{
			"key": "SALESORDERNUMBER",
			"customer": "ORDERINGCUSTOMERACCOUNTNUMBER",
			"currency": "CURRENCYCODE",
			"delivery_date": "REQUESTEDSHIPPINGDATE",
		},
		("key", "customer", "currency", "date"),
		lines="SalesOrderLines",
		line_fields={
			"order": "SALESORDERNUMBER",
			"item": "ITEMNUMBER",
			"quantity": "ORDEREDSALESQUANTITY",
			"amount": "LINEAMOUNT",
		},
		line_required=("order", "quantity", "amount"),
		cited=DOCS + "entity-sales-order-headers-v2-salesorderheaderv2",
	),
	# No vendor or purchase order entity is documented: the operator maps their environment's.
	Kind.SUPPLIER: EntityMap("", {}, ("key", "name")),
	Kind.PURCHASE_ORDER: EntityMap(
		"",
		{},
		("key", "supplier", "currency", "date"),
		lines="",
		line_required=("order", "quantity", "amount"),
	),
}


def entity_maps(field_map: str) -> dict[Kind, EntityMap]:
	"""Each kind's entity map: the defaults, with the connection's field map laid over them."""
	try:
		configured = json.loads(field_map) if field_map.strip() else {}
	except ValueError:
		raise SourceError("Finance & Operations' field map isn't JSON.") from None
	if not isinstance(configured, dict):
		raise SourceError("Finance & Operations' field map is an object of kinds.")
	maps = dict(DEFAULTS)
	for kind_name, given in configured.items():
		kind = Kind.parse(kind_name)
		if kind not in maps or not isinstance(given, dict):
			raise SourceError(
				f"Finance & Operations can't map {kind_name} (it reads {', '.join(k.value for k in maps)})."
			)
		base = maps[kind]
		maps[kind] = EntityMap(
			str(given.get("entity") or base.entity),
			{**base.fields, **strings(given.get("fields"))},
			base.required,
			str(given.get("lines") or base.lines) if base.lines is not None else None,
			{**base.line_fields, **strings(given.get("line_fields"))},
			base.line_required,
			base.cited,
		)
	return maps


def strings(value: JsonValue) -> dict[str, str]:
	if value is None:
		return {}
	if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
		raise SourceError("A Finance & Operations field map's fields are role → field name strings.")
	return {role: name for role, name in value.items() if isinstance(name, str)}


def usable(maps: Mapping[Kind, EntityMap], kind: Kind) -> EntityMap:
	"""The kind's map, or KindUnavailable naming what the operator must still map."""
	entity_map = maps[kind]
	missing = entity_map.missing()
	if missing:
		raise KindUnavailable(
			f"Finance & Operations' {kind.value} needs the field map to name {', '.join(missing)} "
			f"({'the entity reference documents none: ' + entity_map.cited if entity_map.cited else 'no entity reference documents them'})."
		)
	return entity_map


class Fields:
	"""One record's fields by role, matched case-insensitively."""

	def __init__(self, node: Node, roles: Mapping[str, str]) -> None:
		self._node = node
		self._roles = roles
		self._keys = {key.lower(): key for key, _ in node.items()}

	def name(self, role: str) -> str | None:
		"""The record's own spelling of the role's field, or None when unmapped or absent."""
		field_name = self._roles.get(role)
		return self._keys.get(field_name.lower()) if field_name else None

	def get(self, role: str) -> JsonValue:
		key = self.name(role)
		return self._node.get(key) if key else None
