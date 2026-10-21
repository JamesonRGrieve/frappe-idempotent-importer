# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the HighLevel fixtures (tests/fixtures/highlevel/<state>/*.json): a small studio's sub-account as API
v2 answers, shaped as HighLevel's OpenAPI documents define each response (pinned in tools/specs.py, with
the corrections recorded there), for exactly the requests the reader sends: the users, products and their
prices, the contact list and each contact with its notes, tasks and appointments, the businesses and their
contacts, the pipelines and opportunities, the conversations and their messages, the invoices and the
payment transactions. The sub-account's two states differ only by their records (v2: a user removed, a new
price, a won opportunity, a sent invoice, a new message).

``python -m tools.highlevel_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.sources.highlevel import reader

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "highlevel"
LOCATION = "loc0harbour0studio01"
ADDED = "2026-08-03T15:00:00.000Z"
UPDATED = "2026-09-15T15:00:00.000Z"
LATER = "2026-10-16T15:00:00.000Z"
ANY = ".+"
Record = dict[str, Any]
# What an invoice's item sells: its name, product and price.
SESSION = ("Design session", "product001", "price001")
TEE = ("Studio T-shirt", "product002", "price002")


def contact(n: int, first: str, last: str, **fields: Any) -> Record:
	body: Record = {
		"id": f"contact{n:03d}",
		"name": f"{first} {last}",
		"locationId": LOCATION,
		"firstName": first,
		"lastName": last,
		"email": f"{first.lower()}@example.com",
		"emailLowerCase": f"{first.lower()}@example.com",
		"phone": f"+1902555{n:04d}",
		"dnd": False,
		"type": "lead",
		"source": "website form",
		"tags": [],
		"dateAdded": ADDED,
		"dateUpdated": UPDATED,
	}
	return body | fields


def store_v1() -> dict[str, Any]:
	riley = contact(
		1,
		"Riley",
		"River",
		type="customer",
		companyName="River Co",
		address1="1 Harbour Rd",
		city="Halifax",
		state="NS",
		postalCode="B3H 1A1",
		country="CA",
		website="https://river.example",
		businessId="business001",
	)
	return {
		"users": [
			{
				"id": "user001",
				"name": "Morgan Sales",
				"firstName": "Morgan",
				"lastName": "Sales",
				"email": "morgan@studio.example",
				"phone": "+19025550100",
				"deleted": False,
			},
			{
				"id": "user002",
				"name": "Jamie Front",
				"firstName": "Jamie",
				"lastName": "Front",
				"email": "jamie@studio.example",
				"deleted": False,
			},
		],
		"products": [
			{
				"_id": "product001",
				"locationId": LOCATION,
				"name": "Consultation",
				"description": "One hour with a designer",
				"productType": "SERVICE",
				"availableInStore": True,
				"createdAt": ADDED,
				"updatedAt": UPDATED,
			},
			{
				"_id": "product002",
				"locationId": LOCATION,
				"name": "Studio T-shirt",
				"productType": "PHYSICAL",
				"image": "http://highlevel-fake:8080/media/studio-tee.png",
				"variants": [
					{
						"id": "variant01",
						"name": "Size",
						"options": [{"id": "opt0s", "name": "S"}, {"id": "opt0m", "name": "M"}],
					}
				],
				"availableInStore": True,
				"createdAt": ADDED,
				"updatedAt": UPDATED,
			},
			{
				"_id": "product003",
				"locationId": LOCATION,
				"name": "Coaching plan",
				"productType": "SERVICE",
				"createdAt": ADDED,
				"updatedAt": UPDATED,
			},
		],
		"prices": {
			"product001": [price("price001", "product001", "Standard", 150)],
			"product002": [
				price(
					"price002",
					"product002",
					"S",
					25,
					variantOptionIds=["opt0s"],
					trackInventory=True,
					availableQuantity=10,
				),
				price(
					"price003",
					"product002",
					"M",
					25,
					variantOptionIds=["opt0m"],
					trackInventory=True,
					availableQuantity=4,
				),
			],
			"product003": [
				price(
					"price004",
					"product003",
					"Monthly",
					90,
					type="recurring",
					recurring={"interval": "month", "intervalCount": 1},
				)
			],
		},
		"contacts": [riley, contact(2, "Sam", "Lead"), contact(3, "Quinn", "Quiet", dnd=True)],
		"notes": {
			"contact001": [
				{
					"id": "note001",
					"body": "Wants the spring catalogue",
					"title": "Follow-up",
					"userId": "user001",
					"dateAdded": "2026-09-02T14:30:00.000Z",
					"contactId": "contact001",
					"pinned": False,
				}
			]
		},
		"tasks": {
			"contact001": [
				{
					"id": "task001",
					"title": "Send samples",
					"body": "Two fabric swatches",
					"assignedTo": "user001",
					"dueDate": "2026-10-30T15:00:00.000Z",
					"completed": False,
					"contactId": "contact001",
				}
			]
		},
		"appointments": {
			"contact002": [
				{
					"id": "appointment001",
					"calendarId": "calendar001",
					"status": "booked",
					"title": "Discovery call",
					"assignedUserId": "user001",
					"notes": "Bring the portfolio",
					"startTime": "2026-10-22 10:00:00",
					"endTime": "2026-10-22 10:30:00",
					"locationId": LOCATION,
					"contactId": "contact002",
					"appointmentStatus": "confirmed",
					"users": [],
					"dateAdded": "2026-10-01 09:00:00",
					"dateUpdated": "2026-10-01 09:00:00",
				}
			]
		},
		"businesses": [
			{
				"id": "business001",
				"name": "River Co",
				"phone": "+19025550142",
				"email": "hello@river.example",
				"website": "https://river.example",
				"address": "1 Harbour Rd",
				"city": "Halifax",
				"state": "NS",
				"postalCode": "B3H 1A1",
				"country": "CA",
				"description": "Boutique river tours",
				"locationId": LOCATION,
				"createdAt": ADDED,
				"updatedAt": UPDATED,
			}
		],
		"business_contacts": {"business001": ["contact001"]},
		"pipelines": [{"id": "pipeline001", "name": "Sales", "locationId": LOCATION, "stages": []}],
		"opportunities": [
			{
				"id": "opportunity001",
				"name": "Spring rebrand",
				"monetaryValue": 1200,
				"pipelineId": "pipeline001",
				"pipelineStageId": "stage-proposal",
				"status": "open",
				"source": "website",
				"createdAt": "2026-09-05T13:00:00.000Z",
				"updatedAt": UPDATED,
				"contactId": "contact002",
				"locationId": LOCATION,
			}
		],
		"conversations": [
			conversation("conversation001", riley, "TYPE_EMAIL", "TYPE_EMAIL"),
			conversation("conversation002", contact(2, "Sam", "Lead"), "TYPE_PHONE", "TYPE_CALL"),
		],
		"messages": {
			"conversation001": [
				message(
					"message001",
					"conversation001",
					"contact001",
					"TYPE_EMAIL",
					"inbound",
					"Do you ship to Halifax?",
					"2026-09-01T12:00:00.000Z",
				),
				message(
					"message002",
					"conversation001",
					"contact001",
					"TYPE_SMS",
					"outbound",
					"We do: next day.",
					"2026-09-01T12:05:00.000Z",
				),
			],
			"conversation002": [
				message(
					"message003",
					"conversation002",
					"contact002",
					"TYPE_CALL",
					"inbound",
					None,
					"2026-09-03T16:00:00.000Z",
					meta={"callDuration": "185", "callStatus": "completed"},
				)
			],
		},
		"invoices": [
			invoice("invoice001", 1001, "sent", 2, 50, 13),
			invoice("invoice002", 1002, "draft", 1, 150, 0),
			invoice("invoice003", 1003, "void", 1, 25, 0, TEE),
		],
		"transactions": [
			transaction("transaction001", 113, "invoice", "invoice001"),
			transaction("transaction002", 50, "order", "order001", amountRefunded=10),
			transaction("transaction003", 75, "order", "order002", status="failed"),
		],
	}


def price(id_: str, product: str, name: str, amount: float, **fields: Any) -> Record:
	return {
		"_id": id_,
		"locationId": LOCATION,
		"product": product,
		"name": name,
		"type": "one_time",
		"currency": "CAD",
		"amount": amount,
		"createdAt": ADDED,
		"updatedAt": UPDATED,
	} | fields


def conversation(id_: str, person: Record, kind: str, last_type: str) -> Record:
	return {
		"id": id_,
		"contactId": person["id"],
		"locationId": LOCATION,
		"lastMessageBody": "",
		"lastMessageType": last_type,
		"type": kind,
		"unreadCount": 0,
		"fullName": person["name"],
		"contactName": person["name"],
		"email": person["email"],
		"phone": person["phone"],
	}


def message(
	id_: str,
	conversation_: str,
	person: str,
	kind: str,
	direction: str,
	body: str | None,
	added: str,
	**fields: Any,
) -> Record:
	record: Record = {
		"id": id_,
		"type": 3 if kind == "TYPE_EMAIL" else 1,
		"messageType": kind,
		"locationId": LOCATION,
		"contactId": person,
		"conversationId": conversation_,
		"dateAdded": added,
		"direction": direction,
		"status": "delivered",
		"contentType": "text/plain",
		"attachments": [],
	}
	if body is not None:
		record["body"] = body
	return record | fields


def invoice(
	id_: str,
	number: int,
	status: str,
	qty: int,
	amount: float,
	tax: float,
	item: tuple[str, str, str] = SESSION,
) -> Record:
	name, product, price_id = item
	subtotal = qty * amount
	return {
		"_id": id_,
		"status": status,
		"liveMode": True,
		"amountPaid": 0,
		"altId": LOCATION,
		"altType": "location",
		"name": f"Invoice {number}",
		"businessDetails": {"name": "Harbour Studio"},
		"invoiceNumber": number,
		"currency": "CAD",
		"contactDetails": {
			"id": "contact001",
			"name": "Riley River",
			"phoneNo": "+19025550001",
			"email": "riley@example.com",
			"customFields": [],
		},
		"issueDate": "2026-09-10",
		"dueDate": "2026-10-10",
		"invoiceItems": [
			{
				"_id": f"{id_}-item1",
				"name": name,
				"productId": product,
				"priceId": price_id,
				"currency": "CAD",
				"amount": amount,
				"qty": qty,
				"taxes": [],
			}
		],
		"total": subtotal + tax,
		"title": "INVOICE",
		"amountDue": subtotal + tax,
		"createdAt": ADDED,
		"updatedAt": UPDATED,
		"totalSummary": {"subTotal": subtotal, "discount": 0, "tax": tax},
	}


def transaction(
	id_: str, amount: float, entity: str, entity_id: str, status: str = "succeeded", **fields: Any
) -> Record:
	return {
		"_id": id_,
		"altId": LOCATION,
		"altType": "location",
		"contactId": "contact001",
		"contactName": "Riley River",
		"contactEmail": "riley@example.com",
		"currency": "CAD",
		"amount": amount,
		"status": status,
		"liveMode": True,
		"entityType": entity,
		"entityId": entity_id,
		"entitySourceType": "invoice" if entity == "invoice" else "funnel",
		"entitySourceName": "Spring funnel" if entity != "invoice" else "Invoice 1001",
		"chargeId": f"ch_{id_}",
		"paymentProviderType": "stripe",
		"createdAt": "2026-09-12T15:00:00.000Z",
		"updatedAt": "2026-09-12T15:00:00.000Z",
		"fulfilledAt": "2026-09-12T15:00:00.000Z",
	} | fields


def store_v2() -> dict[str, Any]:
	store = copy.deepcopy(store_v1())
	store["users"][1]["deleted"] = True
	store["prices"]["product001"][0] |= {"amount": 175, "updatedAt": LATER}
	store["opportunities"][0] |= {"status": "won", "updatedAt": LATER}
	store["invoices"][1] |= {"status": "sent", "updatedAt": LATER}
	store["messages"]["conversation001"].append(
		message(
			"message004",
			"conversation001",
			"contact001",
			"TYPE_EMAIL",
			"inbound",
			"Perfect, thanks!",
			"2026-10-02T09:00:00.000Z",
		)
	)
	return store


def fixture(path: str, query: Record, response: Record, patterns: Record | None = None) -> Record:
	request: Record = {"path": path, "query": {key: str(value) for key, value in query.items()}}
	if patterns:
		request["query_patterns"] = patterns
	return {"request": request, "response": response}


def fixtures(store: dict[str, Any]) -> dict[str, Record]:
	located = {"locationId": LOCATION}
	page = {"limit": reader.PAGE_SIZE}
	alt = {"altId": LOCATION, "altType": "location", "offset": 0, **page}
	written: dict[str, Record] = {
		"users": fixture(reader.USERS.path, located, {"users": store["users"]}),
		"products": fixture(
			reader.PRODUCTS.path,
			{**located, "offset": 0, **page},
			{"products": store["products"], "total": [{"total": len(store["products"])}]},
		),
		"contacts": fixture(
			"/contacts/",
			{**located, **page},
			{"contacts": [summary(c) for c in store["contacts"]], "count": len(store["contacts"])},
		),
		"businesses": fixture(
			reader.BUSINESSES.path, {**located, "skip": 0, **page}, {"businesses": store["businesses"]}
		),
		"pipelines": fixture("/opportunities/pipelines", located, {"pipelines": store["pipelines"]}),
		"opportunities": fixture(
			"/opportunities/search",
			{"location_id": LOCATION, **page},
			{
				"opportunities": store["opportunities"],
				"meta": {"total": len(store["opportunities"]), "currentPage": 1},
			},
		),
		"conversations": fixture(
			"/conversations/search",
			{**located, **page, "sort": "asc"},
			{"conversations": store["conversations"], "total": len(store["conversations"])},
			{"startDate": ANY, "endDate": ANY},
		),
		"invoices": fixture(
			reader.INVOICES.path, alt, {"invoices": store["invoices"], "total": len(store["invoices"])}
		),
		"transactions": fixture(
			reader.TRANSACTIONS.path,
			alt,
			{"data": store["transactions"], "totalCount": len(store["transactions"])},
		),
	}
	for product, prices in store["prices"].items():
		written[f"prices-{product}"] = fixture(
			f"/products/{product}/price",
			{**located, "offset": 0, **page},
			{"prices": prices, "total": len(prices)},
		)
	for person in store["contacts"]:
		identifier = person["id"]
		written[f"contact-{identifier}"] = fixture(f"/contacts/{identifier}", {}, {"contact": person})
		for part, key in (("notes", "notes"), ("tasks", "tasks"), ("appointments", "events")):
			written[f"contact-{identifier}-{part}"] = fixture(
				f"/contacts/{identifier}/{part}", {}, {key: store[part].get(identifier, [])}
			)
	for business, members in store["business_contacts"].items():
		people = [summary(c) for c in store["contacts"] if c["id"] in members]
		written[f"business-{business}-contacts"] = fixture(
			f"/contacts/business/{business}",
			{**located, "skip": 0, **page},
			{"contacts": people, "count": len(people)},
		)
	for conversation_, messages in store["messages"].items():
		written[f"messages-{conversation_}"] = fixture(
			f"/conversations/{conversation_}/messages",
			page,
			{"lastMessageId": messages[-1]["id"], "nextPage": False, "messages": messages},
		)
	return written


def summary(person: Record) -> Record:
	"""A contact as the contact list has it."""
	keys = ("id", "locationId", "email", "source", "dateAdded", "tags", "businessId", "country")
	return {key: person[key] for key in keys if key in person}


def write(state: str, store: dict[str, Any]) -> None:
	folder = FIXTURES / state
	shutil.rmtree(folder, ignore_errors=True)
	folder.mkdir(parents=True)
	for name, content in fixtures(store).items():
		(folder / f"{name}.json").write_text(json.dumps(content, indent="\t", ensure_ascii=False) + "\n")


def main() -> int:
	write("v1", store_v1())
	write("v2", store_v2())
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
