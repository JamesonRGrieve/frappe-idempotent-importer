<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Coverage: Webflow → ERPNext

The single source of truth for what the Webflow adapter mirrors. **Not yet run against a live site:** there
are no live credentials; every fixture (`tests/fixtures/webflow`, written by `python -m tools.webflow_fixtures`)
is shaped as Webflow's Data API v2 OpenAPI document defines each response, and is validated against it by
`tools/prove.py`. The document is the one Webflow publishes for its API reference
(https://github.com/webflow/openapi-spec, `openapi/v2.yml`, MIT, pinned to commit `f6db6073` and its SHA-256 in
`tools/specs.py`); where it types a field against its own example (a collection field's `validations`), the
example's shape is used, the correction recorded in `tools/specs.py`. OAuth follows
https://developers.webflow.com/data/reference/oauth-app, limits https://developers.webflow.com/data/reference/rate-limits.

Access: one connection per site. Register a Data Client app in a Webflow workspace (Apps & Integrations →
Develop) with the scopes `sites:read`, `cms:read`, `pages:read`, `assets:read` and `ecommerce:read` and this
site's `/api/method/idempotent_importer.api.oauth_callback` as its redirect URI. Enter its client ID and secret on
the connection with the site's address as the Store URL, save, and click **Connect to Webflow**, authorising the
site: the site the token opens becomes the connection's account (with several, enter the one to mirror as the
account first). Webflow's access token lasts until the app is uninstalled and comes with no refresh token, so
it is the credential the connection keeps. A refused token stops the run and holds scheduled runs.

Reading: every list pages by `offset` and `limit` (100). The site's e-commerce collections (Products, SKUs,
Categories) are told from its own CMS collections by their slugs. A product carries its SKUs and each SKU's
inventory; a page its content nodes (the page DOM) and the site's assets; a CMS item its collection's field
definitions. Incremental runs ask for the CMS items (categories and posts) changed since the last clean run
(`lastUpdated[gte]`); nothing else filters by change, so the rest is read whole and skipped by its hash.
Deletions come only from a completed, unfiltered full pass. Limits: 60 requests a minute per token (120 on CMS,
e-commerce and Business plans); the importer paces one a second, quiets when `X-RateLimit-Remaining` runs out,
backs off on 429 after `Retry-After`, and pauses the run when Webflow keeps refusing. Money is in the
currency's smallest unit (`value`), read exactly.

## Mirrored

| Webflow | ERPNext home | Notes |
|---|---|---|
| Assets | **File** (public) | The hosted file, with its alt text. |
| E-commerce categories | **Item Group** under the connection's | Name, slug, parent, description and image; the old category page redirects to it. Drafts and archived ones aren't kept. |
| Products and their SKUs | **Item** (+ **Website Item**); SKUs as **Item** variants | One SKU without options; else a template with a variant per SKU (its options from the product's SKU properties). A compare-at price above the price is the price and the price the sale; finite inventory is the stock, infinite isn't counted. Drafts are unpublished, archived products disabled; the old product page redirects to the item. The site's own product fields are kept in **Source Fields**. |
| The customers orders name | **Customer**, **Contact**, **Address** | Webflow keeps no customer record: each email an order names is a customer, with its name and billing address. |
| Orders | **Sales Invoice** with its **Payment Entry**, credit note and **Shipment** | Items at their row totals, the order's and shipping discounts as the discount, shipping and tax as charges, which must make the total; a pending order is a draft, any other submitted with its payment (card brand and Stripe charge or PayPal capture). A refunded order (or a dispute lost) is refunded in full; a fulfilled one has its shipment (provider and tracking). The customer's comment is its remarks, the site's own comment a note. |
| Static pages | **Web Page** under the connection's route prefix | Their text (as Webflow's HTML) and images (from the site's assets, copied); drafts unpublished; the old path redirects. A collection's template page has no content of its own. |
| CMS collections | **Blog Category** | Name and slug. |
| CMS items | **Blog Post** in its collection's category | Rich text fields as its body (in field order), image fields as its images (copied), the first plain text field as its introduction, every other field (options as their names, switches as Yes or No) in its **Source Fields**; drafts unpublished, archived items not kept. |
| Redirects | **Website Route Redirect** | To a path on the site (redirecting on to the mirrored record) or a full URL. A pattern redirect is Unsupported. |

## Planned and gaps

| Webflow | Status |
|---|---|
| Subscription SKUs | Planned (card 386): **Subscription Plan**. |
| Partial refunds | Webflow's orders say only that an order was refunded; a refund is booked in full. |
| Forms and their submissions | Planned (card 386): **Web Form** and its records; a page's form elements aren't part of its mirrored content. |
| Components, custom code, comments, custom fonts, localised content (secondary locales) | Operator decision: the site's design and translations belong to the site router's theme, or have no first-party home. |
| User accounts (memberships) | Webflow's v2 API no longer exposes site users; customers come from orders. |
| A product's other images (beyond each SKU's main image), digital downloads | Planned (card 386): further **Website Item** images and downloadable files. |
