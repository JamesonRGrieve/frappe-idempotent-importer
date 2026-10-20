# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the Finance & Operations fixtures (tests/fixtures/dynamics_finance_operations/<state>/*.json): one
legal entity's answers for the documented entities the adapter reads by default, keyed by the fields'
documented data management names (the reader matches fields case-insensitively), in two states (v2: a
price and a cancelled order line's order removed). ``ORDERDATE`` is the test environment's own order-date
field, which its connection's field map names (the reference documents none). The customers list arrives in
two pages.

``python -m tools.fo_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.sources.dynamics_finance_operations.reader import DATA_PATH
from tests.support.dynamics import FO_ENVIRONMENT, FO_LEGAL_ENTITY

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "dynamics_finance_operations"
QUERY = {"cross-company": "true", "$filter": f"dataAreaId eq '{FO_LEGAL_ENTITY}'"}
NEXT_PAGE = "1000001"

type Row = dict[str, Any]


def row(**fields: Any) -> Row:
	return {
		"@odata.etag": 'W/"JzEsNTYzNzE0NDU3NjsxLDU2MzcxNDQ1NzYn"',
		"dataAreaId": FO_LEGAL_ENTITY,
		**fields,
	}


def store_v1() -> dict[str, list[Row]]:
	return {
		"ReleasedProductsV2": [
			row(
				ItemNumber="D0001",
				PRODUCTNUMBER="D0001",
				SEARCHNAME="MidRangeSpeaker",
				PRODUCTSUBTYPE="Product",
				SALESPRICE=180.0,
				PURCHASEPRICE=90.0,
				ITEMGROUP="Audio",
			),
			row(
				ItemNumber="D0004",
				PRODUCTNUMBER="D0004",
				SEARCHNAME="HighEndSpeaker",
				PRODUCTSUBTYPE="ProductMaster",
				SALESPRICE=0.0,
				PURCHASEPRICE=0.0,
				ITEMGROUP="Audio",
			),
		],
		"CustomersV3": [
			row(
				CUSTOMERACCOUNT="US-001",
				ORGANIZATIONNAME="Contoso Retail San Diego",
				PARTYTYPE="Organization",
				PERSONFIRSTNAME="",
				PERSONMIDDLENAME="",
				PERSONLASTNAME="",
				SALESCURRENCYCODE="CAD",
				ADDRESSCITY="Calgary",
				ADDRESSSTATE="AB",
				ADDRESSZIPCODE="T2P 1J9",
				ADDRESSCOUNTRYREGIONID="CAN",
				CUSTOMERGROUPID="30",
			),
			row(
				CUSTOMERACCOUNT="US-027",
				ORGANIZATIONNAME="",
				PARTYTYPE="Person",
				PERSONFIRSTNAME="Birch",
				PERSONMIDDLENAME="",
				PERSONLASTNAME="Lake",
				SALESCURRENCYCODE="CAD",
				ADDRESSCITY="Edmonton",
				ADDRESSSTATE="AB",
				ADDRESSZIPCODE="T5J 0N3",
				ADDRESSCOUNTRYREGIONID="CAN",
				CUSTOMERGROUPID="10",
			),
		],
		"SalesOrderHeadersV2": [
			row(
				SALESORDERNUMBER="000701",
				ORDERINGCUSTOMERACCOUNTNUMBER="US-001",
				INVOICECUSTOMERACCOUNTNUMBER="US-001",
				CURRENCYCODE="CAD",
				REQUESTEDSHIPPINGDATE="2026-03-20T12:00:00Z",
				ORDERDATE="2026-03-02T12:00:00Z",
			),
			row(
				SALESORDERNUMBER="000702",
				ORDERINGCUSTOMERACCOUNTNUMBER="US-027",
				INVOICECUSTOMERACCOUNTNUMBER="US-027",
				CURRENCYCODE="CAD",
				REQUESTEDSHIPPINGDATE="1900-01-01T12:00:00Z",
				ORDERDATE="2026-03-04T12:00:00Z",
			),
		],
		"SalesOrderLines": [
			row(
				SALESORDERNUMBER="000701",
				ITEMNUMBER="D0001",
				ORDEREDSALESQUANTITY=2.0,
				SALESPRICE=180.0,
				LINEAMOUNT=360.0,
				SALESUNITSYMBOL="ea",
			),
			row(
				SALESORDERNUMBER="000702",
				ITEMNUMBER="D0001",
				ORDEREDSALESQUANTITY=1.0,
				SALESPRICE=180.0,
				LINEAMOUNT=180.0,
				SALESUNITSYMBOL="ea",
			),
		],
	}


def store_v2() -> dict[str, list[Row]]:
	db = copy.deepcopy(store_v1())
	db["ReleasedProductsV2"][0]["SALESPRICE"] = 189.0
	db["SalesOrderHeadersV2"] = db["SalesOrderHeadersV2"][:1]
	db["SalesOrderLines"] = db["SalesOrderLines"][:1]
	return db


def answers(db: dict[str, list[Row]]) -> list[tuple[str, dict[str, str], Any]]:
	found: list[tuple[str, dict[str, str], Any]] = []
	for entity, rows in db.items():
		path = f"{DATA_PATH}/{entity}"
		context = f"{FO_ENVIRONMENT}{DATA_PATH}/$metadata#{entity}"
		if entity == "CustomersV3":
			following = f"{FO_ENVIRONMENT}{path}?cross-company=true&%24filter=dataAreaId%20eq%20%27{FO_LEGAL_ENTITY}%27&%24skiptoken={NEXT_PAGE}"
			found.append((path, {"$skiptoken": NEXT_PAGE}, {"@odata.context": context, "value": rows[1:]}))
			found.append(
				(path, QUERY, {"@odata.context": context, "value": rows[:1], "@odata.nextLink": following})
			)
			continue
		found.append((path, QUERY, {"@odata.context": context, "value": rows}))
	return found


def write(state: str, db: dict[str, list[Row]]) -> None:
	folder = FIXTURES / state
	shutil.rmtree(folder, ignore_errors=True)
	folder.mkdir(parents=True)
	for index, (path, query, response) in enumerate(answers(db), 1):
		fixture = {"request": {"path": path, "query": query}, "response": response}
		(folder / f"{index:03d}-{path.rsplit('/', 1)[-1]}.json").write_text(
			json.dumps(fixture, indent="\t", ensure_ascii=False) + "\n"
		)


def main() -> int:
	write("v1", store_v1())
	write("v2", store_v2())
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
