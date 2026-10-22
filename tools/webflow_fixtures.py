# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the Webflow fixtures (tests/fixtures/webflow/<state>/*.json): a small studio's site as the Data API
v2 answers, shaped as Webflow's OpenAPI document defines each response (pinned in tools/specs.py), for exactly
the requests the reader sends: the sites the token opens, the site's assets, collections and each one's
fields, the categories, products and each SKU's inventory, the orders, the pages and each one's content, the
journal's items and the redirects. The two states differ only by their records (v2: a new price, an order
refunded, a page edited, a new post, a redirect removed).

``python -m tools.webflow_fixtures`` rewrites both states."""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any

from idempotent_importer.sources.webflow import reader

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "webflow"
SITE = "580e63e98c9a982ac9b8b741"
CDN = "http://webflow-fake:8080/media/580e63e98c9a982ac9b8b741"
CREATED = "2026-08-03T15:00:00.000Z"
UPDATED = "2026-09-15T15:00:00.000Z"
LATER = "2026-10-16T15:00:00.000Z"
COLLECTIONS = {
	"category": "6a10000000000000000000c1",
	"product": "6a10000000000000000000c2",
	"sku": "6a10000000000000000000c3",
	"journal": "6a10000000000000000000c4",
}
Record = dict[str, Any]


def collection(slug: str, name: str) -> Record:
	return {
		"id": COLLECTIONS[slug],
		"displayName": name,
		"singularName": name.rstrip("s"),
		"slug": slug,
		"createdOn": CREATED,
		"lastUpdated": UPDATED,
	}


def field(id_: str, slug: str, name: str, type_: str, options: list[tuple[str, str]] | None = None) -> Record:
	body: Record = {
		"id": id_,
		"isRequired": slug == "name",
		"isEditable": True,
		"type": type_,
		"slug": slug,
		"displayName": name,
	}
	if options:
		body["validations"] = {"options": [{"id": option, "name": label} for option, label in options]}
	return body


FIELDS = {
	"category": [
		field("f1", "name", "Name", "PlainText"),
		field("f2", "slug", "Slug", "PlainText"),
		field("f3", "description", "Description", "PlainText"),
	],
	"product": [
		field("p1", "name", "Name", "PlainText"),
		field("p2", "slug", "Slug", "PlainText"),
		field("p3", "description", "Description", "PlainText"),
		field("p4", "care-instructions", "Care instructions", "PlainText"),
	],
	"sku": [field("s1", "name", "Name", "PlainText"), field("s2", "slug", "Slug", "PlainText")],
	"journal": [
		field("j1", "name", "Name", "PlainText"),
		field("j2", "slug", "Slug", "PlainText"),
		field("j3", "post-summary", "Post summary", "PlainText"),
		field("j4", "post-body", "Post body", "RichText"),
		field("j5", "main-image", "Main image", "Image"),
		field("j6", "featured", "Featured", "Switch"),
		field("j7", "topic", "Topic", "Option", [("t1", "Process"), ("t2", "News")]),
	],
}


def money(cents: int, currency: str = "CAD") -> Record:
	return {"unit": currency, "value": str(cents), "string": f"${cents // 100}.{cents % 100:02d} {currency}"}


def item(id_: str, name: str, slug: str, **fields: Any) -> Record:
	return {
		"id": id_,
		"cmsLocaleId": "653ad57de882f528b32e810e",
		"lastPublished": UPDATED,
		"lastUpdated": UPDATED,
		"createdOn": CREATED,
		"isArchived": False,
		"isDraft": False,
		"fieldData": {"name": name, "slug": slug, **fields},
	}


def sku(
	id_: str,
	name: str,
	slug: str,
	cents: int,
	code: str,
	values: dict[str, str] | None = None,
	compare: int | None = None,
) -> Record:
	data: Record = {
		"name": name,
		"slug": slug,
		"price": {"value": cents, "unit": "CAD", "currency": "CAD"},
		"sku": code,
		"main-image": f"{CDN}/{slug}.png",
		"ec-sku-billing-method": "one-time",
	}
	if values:
		data["sku-values"] = values
	if compare is not None:
		data["compare-at-price"] = {"value": compare, "unit": "CAD"}
	return {
		"id": id_,
		"cmsLocaleId": "653ad57de882f528b32e810e",
		"lastPublished": UPDATED,
		"lastUpdated": UPDATED,
		"createdOn": CREATED,
		"fieldData": data,
	}


def address(kind: str, name: str) -> Record:
	return {
		"type": kind,
		"addressee": name,
		"line1": "1 Harbour Rd",
		"line2": "",
		"city": "Halifax",
		"state": "NS",
		"country": "CA",
		"postalCode": "B3H 1A1",
	}


def order(
	id_: str, status: str, items: list[Record], extras: list[Record], total: int, **fields: Any
) -> Record:
	subtotal = sum(int(i["rowTotal"]["value"]) for i in items)
	body: Record = {
		"orderId": id_,
		"status": status,
		"comment": "",
		"orderComment": "",
		"acceptedOn": "2026-09-14T15:00:00.000Z",
		"fulfilledOn": None,
		"refundedOn": None,
		"disputedOn": None,
		"disputeUpdatedOn": None,
		"disputeLastStatus": None,
		"customerPaid": money(total),
		"netAmount": money(total - 100),
		"applicationFee": money(0),
		"allAddresses": [address("shipping", "Riley River"), address("billing", "Riley River")],
		"shippingAddress": address("shipping", "Riley River"),
		"billingAddress": address("billing", "Riley River"),
		"shippingProvider": None,
		"shippingTracking": None,
		"shippingTrackingURL": None,
		"customerInfo": {"fullName": "Riley River", "email": "Riley@River.example"},
		"purchasedItems": items,
		"purchasedItemsCount": len(items),
		"stripeDetails": {
			"paymentMethod": "pm_1",
			"paymentIntentId": "pi_1",
			"customerId": "cus_1",
			"chargeId": f"ch_{id_}",
		},
		"stripeCard": {
			"last4": "4242",
			"brand": "Visa",
			"ownerName": "Riley River",
			"expires": {"year": 2030, "month": 12},
		},
		"customData": [],
		"metadata": {"isBuyNow": False},
		"isCustomerDeleted": False,
		"isShippingRequired": True,
		"hasDownloads": False,
		"paymentProcessor": "stripe",
		"totals": {"subtotal": money(subtotal), "extras": extras, "total": money(total)},
		"downloadFiles": [],
	}
	return body | fields


def purchased(product: str, sku_id: str, name: str, variant: str, count: int, unit: int) -> Record:
	return {
		"count": count,
		"rowTotal": money(unit * count),
		"productId": product,
		"productName": name,
		"productSlug": name.lower().replace(" ", "-"),
		"variantId": sku_id,
		"variantName": variant,
		"variantSlug": variant.lower().replace(" ", "-"),
		"variantSKU": variant.upper(),
		"variantPrice": money(unit),
		"weight": None,
		"width": None,
		"height": None,
		"length": None,
	}


def extra(kind: str, name: str, cents: int) -> Record:
	return {"type": kind, "name": name, "description": "", "price": money(cents)}


PRINT, MUG = "6a10000000000000000000a1", "6a10000000000000000000a2"
PRINT_A4, PRINT_A3, MUG_SKU = (
	"6a10000000000000000000b1",
	"6a10000000000000000000b2",
	"6a10000000000000000000b3",
)
CATEGORY = "6a10000000000000000000d1"


def store_v1() -> dict[str, Any]:
	return {
		"assets": [
			{
				"id": "6a10000000000000000000e1",
				"contentType": "image/png",
				"size": 1024,
				"siteId": SITE,
				"hostedUrl": f"{CDN}/studio-hero.png",
				"originalFileName": "studio-hero.png",
				"displayName": "studio-hero.png",
				"lastUpdated": UPDATED,
				"createdOn": CREATED,
				"variants": [],
				"altText": "The studio",
			}
		],
		"categories": [item(CATEGORY, "Prints", "prints", description="Art prints")],
		"products": [
			{
				"product": item(
					PRINT,
					"Harbour print",
					"harbour-print",
					description="A print of the harbour",
					shippable=True,
					category=[CATEGORY],
					**{
						"sku-properties": [
							{
								"id": "size",
								"name": "Size",
								"enum": [
									{"id": "a4", "name": "A4", "slug": "a4"},
									{"id": "a3", "name": "A3", "slug": "a3"},
								],
							}
						],
						"default-sku": PRINT_A4,
						"care-instructions": "Keep out of direct sun",
					},
				),
				"skus": [
					sku(PRINT_A4, "Harbour print A4", "harbour-print-a4", 4500, "PRINT-A4", {"size": "a4"}),
					sku(
						PRINT_A3,
						"Harbour print A3",
						"harbour-print-a3",
						6000,
						"PRINT-A3",
						{"size": "a3"},
						compare=7500,
					),
				],
			},
			{
				"product": item(
					MUG,
					"Studio mug",
					"studio-mug",
					description="Stoneware",
					shippable=True,
					category=[CATEGORY],
					**{"default-sku": MUG_SKU},
				),
				"skus": [sku(MUG_SKU, "Studio mug", "studio-mug", 2200, "MUG")],
			},
		],
		"inventory": {
			PRINT_A4: {"id": PRINT_A4, "quantity": 12, "inventoryType": "finite"},
			PRINT_A3: {"id": PRINT_A3, "quantity": 4, "inventoryType": "finite"},
			MUG_SKU: {"id": MUG_SKU, "quantity": 0, "inventoryType": "infinite"},
		},
		"orders": [
			order(
				"fc7-128",
				"fulfilled",
				[purchased(PRINT, PRINT_A4, "Harbour print", "A4", 2, 4500)],
				[
					extra("discount", "Discount (WELCOME10)", -900),
					extra("shipping", "Flat rate", 1000),
					extra("tax", "HST", 1215),
				],
				10315,
				fulfilledOn="2026-09-15T15:00:00.000Z",
				shippingProvider="Canada Post",
				shippingTracking="CP123",
				shippingTrackingURL="https://www.canadapost-postescanada.ca/track/CP123",
				comment="Please wrap it",
			),
			order(
				"fc7-129",
				"unfulfilled",
				[purchased(MUG, MUG_SKU, "Studio mug", "Studio mug", 1, 2200)],
				[extra("tax", "HST", 330)],
				2530,
				orderComment="Gift for a client",
			),
			order(
				"fc7-130",
				"pending",
				[purchased(MUG, MUG_SKU, "Studio mug", "Studio mug", 2, 2200)],
				[],
				4400,
				customerInfo={"fullName": "Sam Shore", "email": "sam@shore.example"},
			),
		],
		"pages": [
			page("6a10000000000000000000f1", "About", "about", "/about"),
			page(
				"6a10000000000000000000f2",
				"Journal template",
				"detail_journal",
				"/journal/item",
				collectionId=COLLECTIONS["journal"],
			),
		],
		"dom": {
			"6a10000000000000000000f1": [
				{
					"id": "n1",
					"type": "text",
					"text": {"html": "<h1>About the studio</h1>", "text": "About the studio"},
				},
				{
					"id": "n2",
					"type": "text",
					"text": {
						"html": "<p>We design for the harbour.</p>",
						"text": "We design for the harbour.",
					},
				},
				{
					"id": "n3",
					"type": "image",
					"image": {"alt": "The studio", "assetId": "6a10000000000000000000e1"},
				},
			]
		},
		"journal": [
			item(
				"6a10000000000000000000g1",
				"Printing the harbour",
				"printing-the-harbour",
				**{
					"post-summary": "How the prints are made",
					"post-body": "<p>Every print starts on the press.</p>",
					"main-image": {
						"fileId": "6a10000000000000000000e2",
						"url": f"{CDN}/press.png",
						"alt": "The press",
					},
					"featured": True,
					"topic": "t1",
				},
			)
		],
		"redirects": [
			{"id": "6a10000000000000000000h1", "fromUrl": "/old-about", "toUrl": "/about"},
			{"id": "6a10000000000000000000h2", "fromUrl": "/shop", "toUrl": "https://shop.example.com/"},
		],
	}


def page(id_: str, title: str, slug: str, path: str, **fields: Any) -> Record:
	body: Record = {
		"id": id_,
		"siteId": SITE,
		"title": title,
		"slug": slug,
		"createdOn": CREATED,
		"lastUpdated": UPDATED,
		"archived": False,
		"draft": False,
		"canBranch": True,
		"isBranch": False,
		"seo": {"title": title, "description": ""},
		"openGraph": {"title": title, "titleCopied": True, "description": "", "descriptionCopied": True},
		"localeId": None,
		"publishedPath": path,
	}
	return body | fields


def store_v2() -> dict[str, Any]:
	store = copy.deepcopy(store_v1())
	store["products"][1]["skus"][0]["fieldData"]["price"]["value"] = 2400
	store["orders"][1] |= {"status": "refunded", "refundedOn": "2026-10-01T15:00:00.000Z"}
	store["dom"]["6a10000000000000000000f1"][1]["text"] = {
		"html": "<p>We design for the harbour and the hills.</p>",
		"text": "We design for the harbour and the hills.",
	}
	store["journal"].append(
		item(
			"6a10000000000000000000g2",
			"Studio news",
			"studio-news",
			**{"post-body": "<p>New prints in October.</p>", "topic": "t2"},
		)
	)
	store["redirects"].pop(1)
	return store


def fixture(path: str, query: Record, response: Any) -> Record:
	return {
		"request": {"path": f"{reader.API_PATH}{path}", "query": {k: str(v) for k, v in query.items()}},
		"response": response,
	}


def paged(key: str, records: list[Record]) -> Record:
	return {key: records, "pagination": {"limit": reader.PAGE_SIZE, "offset": 0, "total": len(records)}}


def fixtures(store: dict[str, Any]) -> dict[str, Record]:
	page_query = {"offset": 0, "limit": reader.PAGE_SIZE}
	site = f"/sites/{SITE}"
	collections = [
		collection("category", "Categories"),
		collection("product", "Products"),
		collection("sku", "SKUs"),
		collection("journal", "Journal"),
	]
	written: dict[str, Record] = {
		"sites": fixture(
			"/sites",
			{},
			{
				"sites": [
					{
						"id": SITE,
						"workspaceId": "6a1000000000000000000001",
						"displayName": "Harbour Studio",
						"shortName": "harbour-studio",
						"createdOn": CREATED,
						"lastUpdated": UPDATED,
						"customDomains": [{"id": "6a1000000000000000000002", "url": "harbour.example"}],
					}
				]
			},
		),
		"assets": fixture(f"{site}/assets", page_query, paged("assets", store["assets"])),
		"collections": fixture(f"{site}/collections", {}, {"collections": collections}),
		"categories": fixture(
			f"/collections/{COLLECTIONS['category']}/items", page_query, paged("items", store["categories"])
		),
		"products": fixture(f"{site}/products", page_query, paged("items", store["products"])),
		"orders": fixture(f"{site}/orders", page_query, paged("orders", store["orders"])),
		"pages": fixture(f"{site}/pages", page_query, paged("pages", store["pages"])),
		"journal": fixture(
			f"/collections/{COLLECTIONS['journal']}/items", page_query, paged("items", store["journal"])
		),
		"redirects": fixture(f"{site}/redirects", page_query, paged("redirects", store["redirects"])),
	}
	for entry in collections:
		written[f"collection-{entry['slug']}"] = fixture(
			f"/collections/{entry['id']}", {}, entry | {"fields": FIELDS[entry["slug"]]}
		)
	for sku_id, inventory in store["inventory"].items():
		written[f"inventory-{sku_id}"] = fixture(
			f"/collections/{COLLECTIONS['sku']}/items/{sku_id}/inventory", {}, inventory
		)
	for page_id, nodes in store["dom"].items():
		written[f"dom-{page_id}"] = fixture(
			f"/pages/{page_id}/dom",
			page_query,
			{
				"pageId": page_id,
				"branchId": None,
				"nodes": nodes,
				"pagination": {"limit": reader.PAGE_SIZE, "offset": 0, "total": len(nodes)},
				"lastUpdated": UPDATED,
			},
		)
	return written


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
