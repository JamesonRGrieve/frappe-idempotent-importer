# SPDX-License-Identifier: AGPL-3.0-or-later
"""What Zoho Books and Zoho Inventory share: Zoho's data centres, its OAuth 2 (www.zoho.com/books/api/v3/
oauth), the organisation every request names, and reading a module's records page by page.

The connection's Store URL is the product's web address in the organisation's data centre
(``https://books.zoho.eu``, ``https://inventory.zoho.com.au``): the data centre decides where tokens are
issued (``accounts.zoho.<dc>``) and where the API answers (``www.zohoapis.<dc>``). Tokens: the secret in
the form, an access token for an hour (at most ten issued per refresh token in ten minutes), and a refresh
token that never changes (none comes back on a refresh). Requests carry ``Authorization: Zoho-oauthtoken``
and ``organization_id``.

A module lists its records ``per_page`` (200 at most) from ``page`` 1, saying whether there are more
(``page_context.has_more_page``; the modules whose list has no page context end on a short page). A
document's list entry is a summary, so each one is read whole by its id. Incremental runs ask for
``last_modified_time`` where the module takes it; the others are read whole and skipped by their hash.

Limits (www.zoho.com/books/api/v3/introduction/#api-call-limit): 100 requests a minute per organisation, a
daily allowance by plan (1,000 to 10,000), and 5 or 10 at once; past either it answers 429, which pauses
the run (a spent day's allowance keeps pausing it until the next day)."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from idempotent_importer.core.errors import CredentialsRejected, SourceError
from idempotent_importer.core.http import HTTP_OK, HttpTransport, RateLimit
from idempotent_importer.core.identity import Identity
from idempotent_importer.core.kind import Kind
from idempotent_importer.core.node import JsonValue, Node
from idempotent_importer.core.oauth import ClientAuth, OAuthApp, OAuthClient, OAuthProvider, TokenGrant
from idempotent_importer.core.rest import REQUEST_TIMEOUT_SECONDS, QueryValue, RestClient
from idempotent_importer.core.source import ConnectionConfig, Page
from idempotent_importer.sources.common import chained

# The data centres www.zoho.com/books/api/v3/introduction/#multidc lists, by their domain suffix.
DATA_CENTRES = frozenset({"com", "eu", "in", "com.au", "jp", "ca", "com.cn", "sa", "ae"})
ZOHO_DOMAIN = "zoho."
AUTHORIZE_PATH = "/oauth/v2/auth"
GRANT_PATH = "/oauth/v2/token"
SCHEME = "Zoho-oauthtoken"
PER_PAGE = 200
FIRST_PAGE = 1
REQUESTS_PER_MINUTE = 100
RATE_LIMIT = RateLimit(requests_per_second=RateLimit.per_period(REQUESTS_PER_MINUTE, 60.0))
MODIFIED_SINCE_FORMAT = "%Y-%m-%dT%H:%M:%S+0000"
PAGE_CONTEXT = "page_context"
ORGANIZATIONS = "/organizations"
# The callback's query names the data centre the account lives in.
ACCOUNTS_SERVER_PARAMETER = "accounts-server"


def every(node: Node, key: str) -> list[Node]:
	"""The objects under ``key``: Zoho's reference documents some as one object where others are lists (a
	vendor credit's ``line_items``, a list's ``page_context``, a sales order read by its id), so either is
	read as the list it means."""
	value = node.get(key)
	if isinstance(value, dict):
		return [Node(value, f"{node.path}.{key}")]
	return node.children(key)


def one(node: Node, key: str) -> Node | None:
	"""The one object under ``key``, whether Zoho sends it as an object or as a list of one."""
	found = every(node, key)
	if len(found) > 1:
		raise SourceError(f"{node.path}.{key} holds {len(found)} objects where one was expected.")
	return found[0] if found else None


def more_pages(listed: Node, count: int) -> bool:
	"""Whether a module's list has a page after this one: its page context says, or (a module whose page
	context doesn't) a full page."""
	context = one(listed, PAGE_CONTEXT)
	has_more = context.optional_flag("has_more_page") if context is not None else None
	return has_more if has_more is not None else count == PER_PAGE


@dataclass(frozen=True)
class Product:
	"""One Zoho product's API: its label, the path its API answers under, and its read-only scopes."""

	label: str
	api_path: str
	scopes: tuple[str, ...]


def data_centre(config: ConnectionConfig) -> str:
	"""The data centre the Store URL names (``books.zoho.eu`` → ``eu``)."""
	host = urlsplit(config.store_url if "//" in config.store_url else f"//{config.store_url}").hostname or ""
	_, found, suffix = host.partition(ZOHO_DOMAIN)
	if not found or suffix not in DATA_CENTRES:
		raise SourceError(
			f"The Store URL must be the product's address in the organisation's Zoho data centre "
			f"(e.g. https://books.zoho.com or https://books.zoho.eu), not {config.store_url or 'blank'}."
		)
	return suffix


def accounts_url(config: ConnectionConfig) -> str:
	return config.endpoint_override or f"https://accounts.zoho.{data_centre(config)}"


def api_url(config: ConnectionConfig) -> str:
	return config.endpoint_override or f"https://www.zohoapis.{data_centre(config)}"


def provider(product: Product, config: ConnectionConfig) -> OAuthProvider:
	accounts = accounts_url(config)
	return OAuthProvider(
		label=product.label,
		authorize_url=accounts + AUTHORIZE_PATH,
		token_url=accounts + GRANT_PATH,
		scopes=product.scopes,
		client_auth=ClientAuth.FORM,
		reconnect="connect the organisation again",
		rotates_refresh_token=False,
		scope_separator=",",
		authorize_params=(("access_type", "offline"), ("prompt", "consent")),
	)


def oauth(product: Product, config: ConnectionConfig, transport: HttpTransport) -> OAuthClient:
	if not config.api_user or not config.api_secret:
		raise SourceError(f"{product.label} is not configured: enter the client's ID and secret.")
	if config.save_refresh_token is None:
		raise SourceError(f"{product.label} needs somewhere to keep its refresh token.")
	return OAuthClient(
		provider(product, config),
		OAuthApp(config.api_user, config.api_secret),
		transport,
		config.refresh_token,
		config.save_refresh_token,
	)


def same_data_centre(config: ConnectionConfig, params: Mapping[str, str]) -> None:
	"""An approval from another data centre than the Store URL names can't be used there (the callback's
	``accounts-server`` says where the account lives)."""
	server = params.get(ACCOUNTS_SERVER_PARAMETER)
	if server and not config.endpoint_override and server.rstrip("/") != accounts_url(config):
		raise SourceError(
			f"The Zoho account is in the data centre of {server}: set the Store URL to that data centre."
		)


def connected(
	product: Product,
	config: ConnectionConfig,
	transport: HttpTransport,
	grant: TokenGrant,
	params: Mapping[str, str],
) -> dict[str, str]:
	"""The organisation the approval opened (GET /organizations): the account, unless there are several and
	the connection doesn't already name one of them. An approval from another data centre than the Store
	URL names can't be used there."""
	same_data_centre(config, params)
	response = transport.get(
		api_url(config) + product.api_path + ORGANIZATIONS,
		{"Authorization": f"{SCHEME} {grant.access_token}", "Accept": "application/json"},
		REQUEST_TIMEOUT_SECONDS,
	)
	if response.status != HTTP_OK:
		raise CredentialsRejected(f"{product.label} didn't list the organisations (HTTP {response.status}).")
	try:
		listed = json.loads(response.body)
	except ValueError:
		raise SourceError(f"{product.label} returned an unreadable list of organisations.") from None
	organizations = {
		str(entry["organization_id"]): str(entry.get("name") or entry["organization_id"])
		for entry in (listed.get("organizations") or [] if isinstance(listed, dict) else [])
		if isinstance(entry, dict) and entry.get("organization_id")
	}
	if config.account in organizations:
		return {}
	if len(organizations) == 1:
		return {"api_account": next(iter(organizations))}
	names = ", ".join(f"{name} ({id_})" for id_, name in organizations.items()) or "none"
	raise SourceError(
		f"{product.label} opened several organisations or none ({names}): enter the one to mirror as the account."
	)


@dataclass(frozen=True)
class Module:
	"""One Zoho module a kind reads: its path, the key its list is under and the id field, the key of one
	record read whole (None: the list entry is the whole record), the identity type, its fixed query,
	whether it takes ``last_modified_time``, and which records the kind takes (None: all)."""

	path: str
	key: str
	id_field: str
	type: str
	detail: str | None = None
	query: tuple[tuple[str, QueryValue], ...] = ()
	incremental: bool = False
	paged: bool = True
	take: Callable[[Node], bool] | None = field(default=None, compare=False)
	# Read as another identity type (a sales receipt's payment): the record, under this type.
	as_type: str | None = None
	# The key of one record read by its id when the list is the whole record ("" for a response that is the
	# record itself; None, with no detail either, for a module that has no read by id: its list is scanned).
	single: str | None = None


class ZohoReader:
	def __init__(
		self,
		product: Product,
		modules: Mapping[Kind, tuple[Module, ...]],
		config: ConnectionConfig,
		client: RestClient,
	) -> None:
		if not config.account:
			raise SourceError(f"{product.label} is not connected: use Connect to {product.label} first.")
		self._product = product
		self._modules = modules
		self._config = config
		self._client = client

	@property
	def account(self) -> str:
		"""The organisation every request names."""
		return self._config.account

	def is_incremental(self, kind: Kind) -> bool:
		return any(module.incremental for module in self._modules.get(kind, ()))

	def page(self, kind: Kind, cursor: str | None, since: datetime | None, filter_: str) -> Page:
		modules = self._modules.get(kind)
		if modules is None:
			raise SourceError(f"{self._product.label} has no {kind.value}.")
		return chained(
			len(modules), cursor, lambda feed, position: self._module(modules[feed], position, since)
		)

	def node(self, kind: Kind, gid: str) -> Node | None:
		identity = Identity.parse(gid)
		module = next(
			(m for m in self._modules.get(kind, ()) if (m.as_type or m.type) == identity.type), None
		)
		if module is None:
			raise SourceError(f"{self._product.label} can't read a {identity.type} as a {kind.value}.")
		key = module.detail if module.detail is not None else module.single
		if key is None:
			listed = self._get(module.path, dict(module.query))
			entries = listed.children(module.key) if listed is not None else []
			record = next((entry for entry in entries if entry.string(module.id_field) == identity.id), None)
		else:
			record = self._record(module.path, key, identity.id)
		return None if record is None else self._taken(module, record)

	def _module(self, module: Module, position: str | None, since: datetime | None) -> Page:
		page = int(position) if position else FIRST_PAGE
		query: dict[str, QueryValue] = dict(module.query)
		if module.paged:
			query |= {"page": page, "per_page": PER_PAGE}
		if since is not None and module.incremental:
			query["last_modified_time"] = since.astimezone(UTC).strftime(MODIFIED_SINCE_FORMAT)
		listed = self._get(module.path, query)
		if listed is None:
			raise SourceError(f"{self._product.label} has no {module.path} for this organisation.")
		entries = listed.children(module.key)
		nodes: list[Node] = []
		for entry in entries:
			record = (
				entry
				if module.detail is None
				else self._record(module.path, module.detail, entry.string(module.id_field))
			)
			taken = None if record is None else self._taken(module, record)
			if taken is not None:
				nodes.append(taken)
		more = module.paged and more_pages(listed, len(entries))
		return Page(nodes, more, str(page + 1) if more else None)

	def _record(self, path: str, key: str, id_: str) -> Node | None:
		"""One record read whole by its id: under ``key`` in the response, or the response itself."""
		found = self._get(f"{path}/{id_}", {})
		if found is None or key == "":
			return found
		return one(found, key)

	def _get(self, path: str, query: Mapping[str, QueryValue]) -> Node | None:
		result = self._client.get(
			self._product.api_path + path, {**query, "organization_id": self._config.account}
		)
		return None if result is None else result.node

	def _taken(self, module: Module, record: Node) -> Node | None:
		if module.take is not None and not module.take(record):
			return None
		gid = self._config.identity(module.as_type or module.type, record.string(module.id_field))
		fields: dict[str, JsonValue] = {**record.raw(), **self.looked_up(module, record), "id": gid}
		return Node(fields, gid)

	def looked_up(self, module: Module, record: Node) -> dict[str, JsonValue]:
		"""What a product's mapping needs of a record that Zoho keeps elsewhere (none by default)."""
		return {}

	def listed(self, module: Module) -> list[Node]:
		"""Every record of a module (for the lookups ``looked_up`` makes), page by page."""
		records: list[Node] = []
		page = FIRST_PAGE
		while True:
			query: dict[str, QueryValue] = dict(module.query)
			if module.paged:
				query |= {"page": page, "per_page": PER_PAGE}
			listed = self._get(module.path, query)
			entries = listed.children(module.key) if listed is not None else []
			records += entries
			if listed is None or not module.paged or not more_pages(listed, len(entries)):
				return records
			page += 1
