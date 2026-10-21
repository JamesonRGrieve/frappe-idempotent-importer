# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes tests/schemas/salesforce.json: the REST API answers the Salesforce adapter reads, as an OpenAPI
document transcribed from Salesforce's official references (Salesforce publishes no static OpenAPI document
for its standard objects; its sObjects OpenAPI generator is a per-org beta):

- the Query resource's answer (REST API Developer Guide, "Execute a SOQL Query",
  https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/api_rest/dome_query.htm): ``totalSize``,
  ``done``, ``nextRecordsUrl`` while not done, and ``records``, each with its ``attributes`` (``type``,
  ``url``) and the fields the query selects; a child relationship's records nest as a query result of their
  own (SOQL and SOSL Reference, "Relationship Queries");
- the sObject Describe resource's answer (https://developer.salesforce.com/docs/atlas.en-us.api_rest.meta/
  api_rest/resources_sobject_describe.htm, DescribeSObjectResult and Field in the SOAP API's
  "describeSObjects()" reference): the object's ``name``, ``label`` and ``custom``, and each field's
  ``name``, ``label``, ``type``, ``custom`` and ``picklistValues`` (``value``, ``label``, ``active``,
  ``defaultValue``);
- each object's fields as the Object Reference types them
  (https://developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference/sforce_api_objects_<object>.htm):
  a field without a value is null; custom fields (API names ending ``__c``) are the org's own, so any other
  field of a record is a string, number, boolean or null.

``python -m tools.salesforce_schema`` rewrites it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "tests" / "schemas" / "salesforce.json"
API = "/services/data/v62.0"
REFERENCE = "https://developer.salesforce.com/docs/atlas.en-us.object_reference.meta/object_reference/"
STRING, NUMBER, BOOLEAN, INTEGER = "string", "number", "boolean", "integer"
Schema = dict[str, Any]

# Each object's fields the adapter selects, by the Object Reference's type.
OBJECTS: dict[str, dict[str, str]] = {
	"User": {
		"FirstName": STRING,
		"LastName": STRING,
		"Email": STRING,
		"Phone": STRING,
		"MobilePhone": STRING,
		"Title": STRING,
		"Department": STRING,
		"EmployeeNumber": STRING,
		"ManagerId": STRING,
		"IsActive": BOOLEAN,
	},
	"Product2": {
		"Name": STRING,
		"ProductCode": STRING,
		"Description": STRING,
		"IsActive": BOOLEAN,
		"Family": STRING,
		"StockKeepingUnit": STRING,
	},
	"PricebookEntry": {"UnitPrice": NUMBER},
	"Account": {
		"Name": STRING,
		"Type": STRING,
		"Phone": STRING,
		"Website": STRING,
		"Industry": STRING,
		"Description": STRING,
		"AccountNumber": STRING,
		"BillingStreet": STRING,
		"BillingCity": STRING,
		"BillingState": STRING,
		"BillingPostalCode": STRING,
		"BillingCountry": STRING,
		"IsPersonAccount": BOOLEAN,
		"PersonEmail": STRING,
	},
	"Contact": {
		"FirstName": STRING,
		"LastName": STRING,
		"Email": STRING,
		"Phone": STRING,
		"MobilePhone": STRING,
		"Title": STRING,
	},
	"Note": {"Title": STRING, "Body": STRING, "CreatedDate": STRING},
	"Lead": {
		"FirstName": STRING,
		"LastName": STRING,
		"Company": STRING,
		"Email": STRING,
		"Phone": STRING,
		"MobilePhone": STRING,
		"Title": STRING,
		"Website": STRING,
		"Status": STRING,
		"IsConverted": BOOLEAN,
		"Description": STRING,
		"Street": STRING,
		"City": STRING,
		"State": STRING,
		"PostalCode": STRING,
		"Country": STRING,
	},
	"Opportunity": {
		"Name": STRING,
		"AccountId": STRING,
		"Amount": NUMBER,
		"CloseDate": STRING,
		"StageName": STRING,
		"Probability": NUMBER,
		"IsClosed": BOOLEAN,
		"IsWon": BOOLEAN,
		"Type": STRING,
		"Description": STRING,
		"CreatedDate": STRING,
		"CurrencyIsoCode": STRING,
	},
	"Task": {
		"WhoId": STRING,
		"WhatId": STRING,
		"AccountId": STRING,
		"Subject": STRING,
		"Description": STRING,
		"CreatedDate": STRING,
		"Status": STRING,
		"IsClosed": BOOLEAN,
		"ActivityDate": STRING,
		"TaskSubtype": STRING,
		"CallType": STRING,
		"CallDurationInSeconds": INTEGER,
		"CallDisposition": STRING,
	},
	"Event": {
		"WhoId": STRING,
		"WhatId": STRING,
		"AccountId": STRING,
		"Subject": STRING,
		"Description": STRING,
		"CreatedDate": STRING,
		"Location": STRING,
		"StartDateTime": STRING,
		"EndDateTime": STRING,
		"IsAllDayEvent": BOOLEAN,
		"ActivityDate": STRING,
	},
	"EmailMessage": {
		"Subject": STRING,
		"TextBody": STRING,
		"FromAddress": STRING,
		"ToAddress": STRING,
		"Incoming": BOOLEAN,
		"MessageDate": STRING,
		"RelatedToId": STRING,
		"Status": STRING,
	},
	"Quote": {
		"Name": STRING,
		"QuoteNumber": STRING,
		"Status": STRING,
		"AccountId": STRING,
		"ContactId": STRING,
		"ExpirationDate": STRING,
		"CreatedDate": STRING,
		"Subtotal": NUMBER,
		"TotalPrice": NUMBER,
		"Tax": NUMBER,
		"ShippingHandling": NUMBER,
		"GrandTotal": NUMBER,
		"CurrencyIsoCode": STRING,
	},
	"QuoteLineItem": {
		"Product2Id": STRING,
		"Description": STRING,
		"Quantity": NUMBER,
		"UnitPrice": NUMBER,
		"TotalPrice": NUMBER,
	},
	"Order": {
		"OrderNumber": STRING,
		"AccountId": STRING,
		"EffectiveDate": STRING,
		"Status": STRING,
		"StatusCode": STRING,
		"TotalAmount": NUMBER,
		"Description": STRING,
		"PoNumber": STRING,
		"CurrencyIsoCode": STRING,
	},
	"OrderItem": {
		"Product2Id": STRING,
		"Description": STRING,
		"Quantity": NUMBER,
		"UnitPrice": NUMBER,
		"TotalPrice": NUMBER,
	},
	"Organization": {"DefaultCurrencyIsoCode": STRING},
}
# Each object's child relationships, and the object of their records.
CHILDREN: dict[str, dict[str, str]] = {
	"Product2": {"PricebookEntries": "PricebookEntry"},
	"Account": {"Contacts": "Contact", "Notes": "Note"},
	"Lead": {"Notes": "Note"},
	"Opportunity": {"Notes": "Note"},
	"Quote": {"QuoteLineItems": "QuoteLineItem"},
	"Order": {"OrderItems": "OrderItem"},
}
# Each line's product, as the relationship field Product2.Name brings it.
PRODUCT_REFERENCE = ("QuoteLineItem", "OrderItem")
CUSTOM_VALUE: Schema = {"type": [STRING, NUMBER, BOOLEAN, "null"]}


def record(name: str) -> Schema:
	properties: dict[str, Schema] = {
		"attributes": {
			"type": "object",
			"required": ["type"],
			"properties": {"type": {"type": STRING, "enum": [name]}, "url": {"type": STRING}},
		},
		"Id": {"type": STRING},
	}
	for field, kind in OBJECTS[name].items():
		properties[field] = {"type": [kind, "null"]}
	for relationship, child in CHILDREN.get(name, {}).items():
		properties[relationship] = {
			"oneOf": [{"$ref": f"#/components/schemas/{child}Result"}, {"type": "null"}]
		}
	if name in PRODUCT_REFERENCE:
		properties["Product2"] = {
			"oneOf": [
				{
					"type": "object",
					"required": ["attributes"],
					"properties": {
						"attributes": {
							"type": "object",
							"properties": {"type": {"type": STRING}, "url": {"type": STRING}},
						},
						"Name": {"type": STRING},
					},
				},
				{"type": "null"},
			]
		}
	required = ["attributes"] if name == "Organization" else ["attributes", "Id"]
	return {
		"type": "object",
		"description": f"{REFERENCE}sforce_api_objects_{name.lower()}.htm",
		"required": required,
		"properties": properties,
		"additionalProperties": CUSTOM_VALUE,
	}


def result(items: Schema) -> Schema:
	return {
		"type": "object",
		"required": ["totalSize", "done", "records"],
		"properties": {
			"totalSize": {"type": INTEGER, "minimum": 0},
			"done": {"type": BOOLEAN},
			"nextRecordsUrl": {"type": STRING},
			"records": {"type": "array", "items": items},
		},
	}


def ok(schema: str) -> Schema:
	return {
		"get": {
			"responses": {
				"200": {
					"description": "OK",
					"content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}},
				}
			}
		}
	}


def document() -> Schema:
	schemas: dict[str, Schema] = {name: record(name) for name in OBJECTS}
	for name in OBJECTS:
		schemas[f"{name}Result"] = result({"$ref": f"#/components/schemas/{name}"})
	schemas["QueryResult"] = result({"oneOf": [{"$ref": f"#/components/schemas/{name}"} for name in OBJECTS]})
	schemas["PicklistEntry"] = {
		"type": "object",
		"required": ["value", "label", "active"],
		"properties": {
			"value": {"type": STRING},
			"label": {"type": STRING},
			"active": {"type": BOOLEAN},
			"defaultValue": {"type": BOOLEAN},
		},
	}
	schemas["Field"] = {
		"type": "object",
		"required": ["name", "label", "type", "custom", "picklistValues"],
		"properties": {
			"name": {"type": STRING},
			"label": {"type": STRING},
			"type": {"type": STRING},
			"custom": {"type": BOOLEAN},
			"picklistValues": {"type": "array", "items": {"$ref": "#/components/schemas/PicklistEntry"}},
		},
	}
	schemas["DescribeSObjectResult"] = {
		"type": "object",
		"required": ["name", "label", "custom", "fields"],
		"properties": {
			"name": {"type": STRING},
			"label": {"type": STRING},
			"custom": {"type": BOOLEAN},
			"fields": {"type": "array", "items": {"$ref": "#/components/schemas/Field"}},
		},
	}
	return {
		"openapi": "3.0.3",
		"info": {
			"title": "Salesforce REST API v62.0: the answers read by frappe-idempotent-importer",
			"version": "2026-10-21",
			"description": (__doc__ or "").strip(),
		},
		"paths": {
			f"{API}/query": ok("QueryResult"),
			f"{API}/query/{{locator}}": ok("QueryResult"),
			f"{API}/sobjects/{{sObject}}/describe": ok("DescribeSObjectResult"),
		},
		"components": {"schemas": schemas},
	}


def main() -> int:
	TARGET.write_text(json.dumps(document(), indent=1, ensure_ascii=False) + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
