# SPDX-License-Identifier: AGPL-3.0-or-later
"""Website content: Web Pages, Blog Categories, Blog Posts (and their Bloggers), Blog Post comments, Item
Reviews, and Website Route Redirects (the source's own redirects, and 301s from every mirrored object's
old URL). Everything routed lives under the Public Site's route prefix."""

from __future__ import annotations

import re
from typing import Any

import frappe
from frappe.query_builder.functions import Max

from idempotent_importer.core.desired import Desired
from idempotent_importer.core.errors import Unsupported
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.paths import slugify
from idempotent_importer.core.sync import Link, Target
from idempotent_importer.erpnext.context import ConnectionContext
from idempotent_importer.erpnext.stores import FrappeLinkStore
from idempotent_importer.erpnext.writers.base import Writer
from idempotent_importer.naming import named_after, plain_text

REVIEW_TITLE_LENGTH = 140
WEBSITE_SETTINGS = "Website Settings"
ROUTE_REDIRECT = "Website Route Redirect"
REDIRECTS_FIELD = "route_redirects"
REDIRECT_HASH = "redirect"
RETIRED_MARK = "[Removed at source] "


class PageWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		segments = [values.string("slug")]
		parent_gid = values.optional_string("parent")
		if parent_gid:
			parent = self.dependency(Kind.PAGE, parent_gid)
			parent_route = str(frappe.db.get_value("Web Page", parent, "route"))
			segments = [parent_route.removeprefix(self.context.route_prefix).strip("/"), *segments]
		doc = self.existing(link) or frappe.new_doc("Web Page")
		doc.update(
			{
				"title": values.string("title"),
				"route": self.context.route(*segments),
				"published": int(values.flag("published")),
				"content_type": "HTML",
				"main_section_html": values.string("html"),
			}
		)
		self.save(doc, named_after(doc.route))
		return Target("Web Page", doc.name, doc.route)

	def retire(self, link: Link) -> None:
		frappe.db.set_value("Web Page", link.target.name, "published", 0)


class BlogCategoryWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		doc = self.existing(link) or frappe.new_doc("Blog Category")
		doc.update(
			{
				"title": self.context.suffixed(values.string("title")),
				"published": 1,
				"description": plain_text(values.optional_string("description")) or None,
			}
		)
		doc.save(ignore_permissions=True)
		return Target("Blog Category", doc.name, doc.route)

	def retire(self, link: Link) -> None:
		frappe.db.set_value("Blog Category", link.target.name, "published", 0)


class ArticleWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		category = self.dependency(Kind.BLOG, values.string("category"))
		author = values.required_record("author")
		doc = self.existing(link) or frappe.new_doc("Blog Post")
		doc.update(
			{
				"title": values.string("title"),
				"blog_category": category,
				"blogger": self._blogger(author.string("key"), author.string("name")),
				"route": self.context.route(values.string("slug")),
				"published": int(values.flag("published")),
				"published_on": values.string("published_on"),
				"content_type": "HTML",
				"content_html": values.string("html"),
				"blog_intro": plain_text(values.optional_string("intro")) or None,
			}
		)
		self.save(doc, named_after(doc.route))
		return Target("Blog Post", doc.name, doc.route)

	def retire(self, link: Link) -> None:
		frappe.db.set_value("Blog Post", link.target.name, "published", 0)

	def _blogger(self, key: str, full_name: str) -> str:
		"""The connection's Blogger for one source author (created once, named after the author)."""
		gid = self.context.identity("Author", slugify(key) or "author")
		existing = self.links.target(Kind.BLOGGER, gid)
		if existing and frappe.db.exists("Blogger", existing):
			if frappe.db.get_value("Blogger", existing, "full_name") != full_name:
				frappe.db.set_value("Blogger", existing, "full_name", full_name)
			return existing
		short_name = f"{self.context.abbreviation.lower()}-{slugify(key) or 'author'}"
		blogger = frappe.get_doc({"doctype": "Blogger", "short_name": short_name, "full_name": full_name})
		blogger.insert(ignore_permissions=True)
		self.links.save(Kind.BLOGGER, gid, Target("Blogger", blogger.name), REDIRECT_HASH, 1)
		return str(blogger.name)


class CommentWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		post = self.dependency(Kind.ARTICLE, values.string("article"))
		doc = self.existing(link) or frappe.new_doc("Comment")
		doc.update(
			{
				"comment_type": "Comment",
				"reference_doctype": "Blog Post",
				"reference_name": post,
				"comment_by": values.string("author_name"),
				"comment_email": values.optional_string("author_email"),
				"content": values.string("html"),
				"published": 1,
			}
		)
		doc.save(ignore_permissions=True)
		return Target("Comment", doc.name)

	def retire(self, link: Link) -> None:
		frappe.db.set_value("Comment", link.target.name, "published", 0)


class ReviewWriter(Writer):
	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		item = self.dependency(Kind.PRODUCT, values.string("product"))
		website_item = frappe.db.get_value("Website Item", {"item_code": item})
		if not website_item:
			raise Unsupported(f"{item} has no Website Item to hold its reviews")
		text = plain_text(values.string("text"))
		doc = self.existing(link) or frappe.new_doc("Item Review")
		doc.update(
			{
				"website_item": website_item,
				"item": item,
				"review_title": (text or values.string("author_name"))[:REVIEW_TITLE_LENGTH],
				"comment": f"{text}\n— {values.string('author_name')}",
				"rating": float(values.decimal("rating")),
				"published_on": values.string("published_on"),
			}
		)
		doc.save(ignore_permissions=True)
		return Target("Item Review", doc.name)

	def retire(self, link: Link) -> None:
		# Item Review has no unpublished state, and nothing is ever deleted: the review is marked.
		title = str(frappe.db.get_value("Item Review", link.target.name, "review_title") or "")
		if not title.startswith(RETIRED_MARK):
			frappe.db.set_value(
				"Item Review",
				link.target.name,
				"review_title",
				f"{RETIRED_MARK}{title}"[:REVIEW_TITLE_LENGTH],
			)


class RouteRedirects:
	"""Website Route Redirect rows of Website Settings, one per (connection, owner identity). A rule's source
	is the old path under the Public Site's prefix (so two sites' old paths never collide); its target is
	a new route under the prefix (the Public Site router serves it at its clean URL) or an outside URL."""

	def __init__(self, context: ConnectionContext, links: FrappeLinkStore) -> None:
		self.context = context
		self.links = links

	def source(self, path: str) -> str:
		return re.escape(self.context.route(path))

	def target(self, target: str) -> str:
		outside = re.match(r"^[a-z][a-z0-9+.-]*://", target, re.IGNORECASE)
		location = target if outside else "/" + self.context.route(target)
		return location.replace("\\", "\\\\")

	def upsert(self, kind: Kind, gid: str, source: str, target: str, status: int) -> Target:
		"""Writes this rule's own row only: saving Website Settings would rewrite every other connection's
		rows with it."""
		validate_rule(source, target)
		existing = self.links.target(kind, gid)
		if existing and not frappe.db.exists(ROUTE_REDIRECT, existing):
			existing = None
		clash = frappe.db.get_value(ROUTE_REDIRECT, {"parent": WEBSITE_SETTINGS, "source": source})
		if clash and clash != existing:
			raise Unsupported(f"another redirect already answers {source}")
		values = {"source": source, "target": target, "redirect_http_status": str(status)}
		if existing:
			current = frappe.db.get_value(ROUTE_REDIRECT, existing, list(values), as_dict=True)
			if any(str(current.get(field) or "") != value for field, value in values.items()):
				frappe.db.set_value(ROUTE_REDIRECT, existing, values)
				clear_redirect_cache()
			return Target(ROUTE_REDIRECT, existing)
		row = frappe.get_doc(
			{
				"doctype": ROUTE_REDIRECT,
				"parent": WEBSITE_SETTINGS,
				"parenttype": WEBSITE_SETTINGS,
				"parentfield": REDIRECTS_FIELD,
				"idx": last_rule_index() + 1,
				**values,
			}
		)
		row.db_insert()
		clear_redirect_cache()
		return Target(ROUTE_REDIRECT, row.name)

	def remove(self, name: str) -> None:
		if frappe.db.exists(ROUTE_REDIRECT, name):
			frappe.db.delete(ROUTE_REDIRECT, {"name": name})
			clear_redirect_cache()


def validate_rule(source: str, target: str) -> None:
	"""The check Website Settings makes of a rule (its source is a regular expression)."""
	pattern = source.strip("/ ") + "$"
	try:
		re.sub(pattern, target, "")
	except re.error as e:
		raise Unsupported(f"the redirect {source} → {target} isn't a valid rule: {e}") from e


def last_rule_index() -> int:
	rule = frappe.qb.DocType(ROUTE_REDIRECT)
	rows = frappe.qb.from_(rule).select(Max(rule.idx)).where(rule.parent == WEBSITE_SETTINGS).run()
	return int(rows[0][0] or 0) if rows else 0


def clear_redirect_cache() -> None:
	frappe.get_single(WEBSITE_SETTINGS).clear_cache()


class RedirectWriter(Writer):
	"""The source's own URL redirects. A rule has no disabled flag, so a retired redirect's row is removed
	(it is site configuration, not a record)."""

	def __init__(self, *args: Any, **kwargs: Any) -> None:
		super().__init__(*args, **kwargs)
		self.redirects = RouteRedirects(self.context, self.links)

	def write(self, desired: Desired, link: Link | None) -> Target:
		values = desired.values()
		return self.redirects.upsert(
			Kind.URL_REDIRECT,
			desired.gid,
			self.redirects.source(values.string("source")),
			self.redirects.target(values.string("target")),
			values.integer("status"),
		)

	def retire(self, link: Link) -> None:
		self.redirects.remove(link.target.name)


class LegacyRedirects:
	"""301s from each mirrored object's old storefront paths to its new route (``Redirects``)."""

	def __init__(self, context: ConnectionContext, links: FrappeLinkStore) -> None:
		self.links = links
		self.redirects = RouteRedirects(context, links)

	def sync(self, kind: Kind, gid: str, legacy_paths: tuple[str, ...], route: str | None) -> None:
		new_path = "/" + (route or "").strip("/")
		# An old path that is already the new route (a page that kept its slug) needs no redirect.
		wanted = (
			{
				f"{gid}#legacy:{path}": path
				for path in legacy_paths
				if "/" + self.redirects.context.route(path) != new_path
			}
			if route
			else {}
		)
		for existing in self.links.children(Kind.LEGACY_REDIRECT, f"{gid}#legacy:"):
			if existing.gid not in wanted:
				self.redirects.remove(existing.target.name)
				self.links.retire(Kind.LEGACY_REDIRECT, existing.gid)
		for key, path in wanted.items():
			source = self.redirects.source(path)
			try:
				row = self.redirects.upsert(
					Kind.LEGACY_REDIRECT, key, source, new_path.replace("\\", "\\\\"), 301
				)
			except Unsupported:
				# The source's own redirect for this path wins (it is what the old store answered).
				continue
			self.links.save(Kind.LEGACY_REDIRECT, key, row, REDIRECT_HASH, 1)
