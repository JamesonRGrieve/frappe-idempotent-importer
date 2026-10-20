<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Dynamics 365 Finance & Operations → ERPNext

The single source of truth for what the Finance & Operations adapter mirrors. It reads one legal entity of a
finance and operations environment through its OData data entities (`https://<environment>/data/<entity>`,
with `cross-company=true` and the legal entity's `dataAreaId`;
[OData reference](https://learn.microsoft.com/dynamics365/fin-ops-core/dev-itpro/data-entities/odata)).

**Not yet run against a live finance and operations environment.** Each environment's data model is the
customer's own: entities are extended, renamed and added, and Microsoft's entity reference documents only a
few standard entities' fields (by their data management names, without types). So the adapter reads every
kind through a **field map** (the connection's "Field Map", JSON: per kind, its entity, its lines' entity and
role → field names), starting from defaults that name only what the reference documents, and matches each
field case-insensitively (the reference spells `CUSTOMERACCOUNT`, OData `CustomerAccount`). A kind whose
required fields the map doesn't give is skipped with a warning naming them, until the operator maps them.
`tests/schemas/dynamics_finance_operations.json` is transcribed by `tools/fo_schema.py` from the reference
pages (their documented fields; an entity's other fields are allowed); the fixtures
(`tests/fixtures/dynamics_finance_operations`, written by `tools/fo_fixtures.py`) are proven against it.

Access: an app registered in Microsoft Entra ID, added on the environment's "Microsoft Entra applications"
page mapped to a user with read access. It reads as itself through OAuth 2's client credentials grant for
`https://<environment>/.default`. The connection holds the environment's URL, `<tenant>/<legal entity>` as
the account, the app's client ID and secret, and the field map. Lists are paged by `@odata.nextLink` (200 at
a time; the service's maximum page size is 10,000). Data entities share no documented modification field,
so every kind is read in full and unchanged records are skipped by their hash. Requests are paced under the
documented 6,000 requests per user in a five-minute sliding window and a 429 waits out its Retry-After
([service protection limits](https://learn.microsoft.com/dynamics365/fin-ops-core/dev-itpro/data-entities/service-protection-api-limits)).

## Mirrored

| Finance & Operations | Default entity and fields (documented) | ERPNext home | Notes |
|---|---|---|---|
| Customers | `CustomersV3` ([reference](https://learn.microsoft.com/dynamics365/fin-ops-core/dev-itpro/data-entities/entity-customers-v3-customerv3)): CUSTOMERACCOUNT, ORGANIZATIONNAME, PARTYTYPE, PERSONFIRSTNAME/MIDDLENAME/LASTNAME, SALESCURRENCYCODE, ADDRESSCITY/STATE/ZIPCODE/COUNTRYREGIONID | **Customer** (organisation or person) | An address needs its street, which the reference doesn't document: map `street` (and `email`, `phone`). |
| Released products | `ReleasedProductsV2` ([reference](https://learn.microsoft.com/dynamics365/fin-ops-core/dev-itpro/data-entities/entity-released-products-v2-releasedproductv2)): ItemNumber, SEARCHNAME, SALESPRICE, PRODUCTSUBTYPE | **Item** at its sales price | The reference documents the search name, not the product name: map `name` to the environment's. |
| Sales orders and their lines | `SalesOrderHeadersV2`, `SalesOrderLines` ([reference](https://learn.microsoft.com/dynamics365/fin-ops-core/dev-itpro/data-entities/entity-sales-order-headers-v2-salesorderheaderv2)): SALESORDERNUMBER, ORDERINGCUSTOMERACCOUNTNUMBER, CURRENCYCODE, REQUESTEDSHIPPINGDATE; lines ITEMNUMBER, ORDEREDSALESQUANTITY, LINEAMOUNT | Draft **Sales Order** | No order date is documented: map `date`. The documented entities hold no tax or confirmation status, so the order is a draft totalling its lines. |
| Vendors | (none documented) map `supplier`: `entity`, `key`, `name`, … | **Supplier** | As customers. |
| Purchase orders | (none documented) map `purchase_order`: `entity`, `lines`, `key`, `supplier`, `currency`, `date`; lines `order`, `item`, `quantity`, `amount` | Draft **Purchase Order** | As sales orders. |

## Gaps

| Finance & Operations | Why it isn't mirrored |
|---|---|
| Vendors and purchase orders, until mapped | Operator decision: no entity reference documents them; the field map names the environment's entities. |
| The chart of accounts (main accounts), financial dimensions, posted customer and vendor invoices, payments, general journals and ledger entries, workers, positions and departments, warehouses, projects | Operator decision: their entities aren't documented field by field (general journals are, but post by ledger dimension strings that need the chart first). Planned (card 386): the field map extended to these kinds, through the shared ERP writers. |
| Product masters and variants, on-hand stock | Planned (card 386): `ReleasedProductVariantsV2`, the on-hand entities. |
| Order taxes, charges and confirmation | Planned (card 386): the sales tax and charges entities. |
| Addresses with three-letter country ids | Finance & Operations' country/region ids are usually ISO alpha-3; ERPNext takes alpha-2: planned (card 386) as a map. |
