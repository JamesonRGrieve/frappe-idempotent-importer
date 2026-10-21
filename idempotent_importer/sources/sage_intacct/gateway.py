# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sage Intacct's XML Web Services gateway (``POST https://api.intacct.com/ia/xml/xmlgw.phtml``), version 2.1
of its request and response schemas (the DTDs Sage publishes at github.com/intacct/intacct_dtd, pinned in
tools/specs.py). Every request carries the Web Services sender's credentials (``control``) and logs in as a
company user (``login``); a refused sender or login stops the run and holds scheduled runs. Responses are
read into plain nested values (``xml_value``): an element with children becomes an object, repeated
children a list, text a string, attributes ``@name`` keys.

Intacct governs the gateway by the company's performance tier (concurrent requests); the client sends one
request at a time, at most REQUESTS_PER_SECOND, backs off on 429 and 5xx, and pauses the run when the
gateway keeps pushing back. Passwords never appear in errors or reprs."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field

from idempotent_importer.core.errors import CredentialsRejected, RetryLater, SourceError
from idempotent_importer.core.http import (
	HTTP_OK,
	HttpResponse,
	HttpTransport,
	RateLimit,
	RequestPacer,
	RetryPolicy,
	Sleeper,
)
from idempotent_importer.core.node import JsonValue, Node

GATEWAY = "https://api.intacct.com"
GATEWAY_PATH = "/ia/xml/xmlgw.phtml"
DTD_VERSION = "2.1"
TIMEOUT_SECONDS = 120.0
REQUESTS_PER_SECOND = 2.0
RATE_LIMIT = RateLimit(requests_per_second=REQUESTS_PER_SECOND, cooldown_seconds=60.0)
SUCCESS = "success"
CONTROL_ID = "idempotent-importer"
FUNCTION_ID = "f1"
LABEL = "Sage Intacct"
CONTENT_TYPE = "x-intacct-xml-request"

type Builder = Callable[[ET.Element], None]


@dataclass(frozen=True)
class Credentials:
	"""The Web Services sender (its ID and password) and the company user it logs in as."""

	sender_id: str
	sender_password: str = field(repr=False)
	company_id: str = ""
	user_id: str = ""
	user_password: str = field(default="", repr=False)

	def missing(self) -> list[str]:
		named = {
			"the sender ID": self.sender_id,
			"the sender password": self.sender_password,
			"the company ID": self.company_id,
			"the user ID": self.user_id,
			"the user password": self.user_password,
		}
		return [name for name, value in named.items() if not value]


def xml_value(element: ET.Element) -> JsonValue:
	"""An element as plain values: an object of its attributes (``@name``) and children (repeated ones as a
	list), or its text when it has neither."""
	children = list(element)
	if not children and not element.attrib:
		return (element.text or "").strip()
	value: dict[str, JsonValue] = {f"@{name}": text for name, text in element.attrib.items()}
	if not children and (element.text or "").strip():
		value["#text"] = (element.text or "").strip()
	for child in children:
		converted = xml_value(child)
		if child.tag in value:
			existing = value[child.tag]
			value[child.tag] = [*existing, converted] if isinstance(existing, list) else [existing, converted]
		else:
			value[child.tag] = converted
	return value


class Gateway:
	def __init__(
		self, credentials: Credentials, transport: HttpTransport, sleeper: Sleeper, base_url: str = GATEWAY
	) -> None:
		missing = credentials.missing()
		if missing:
			raise SourceError(f"Sage Intacct is not configured: enter {', '.join(missing)}.")
		self._credentials = credentials
		self._transport = transport
		self._sleeper = sleeper
		self._url = base_url.rstrip("/") + GATEWAY_PATH
		self._pacer = RequestPacer(RATE_LIMIT, sleeper)

	def call(self, function: Builder) -> Node:
		"""One function's result (``status``, ``function``, ``listtype``, ``data``…) as a node."""
		body = self.request(function)
		attempt = 1
		while True:
			self._pacer.before_request(LABEL)
			response = self._transport.post(
				self._url, {"Content-Type": CONTENT_TYPE, "Accept": "application/xml"}, body, TIMEOUT_SECONDS
			)
			wait = RetryPolicy.wait(response, attempt, RATE_LIMIT.cooldown_seconds)
			if wait is None:
				return self._result(response)
			if not RetryPolicy.waits_inline(wait, attempt):
				raise RetryLater(
					wait, f"{LABEL} is rate limiting or failing (HTTP {response.status}, attempt {attempt})."
				)
			self._sleeper.sleep(wait)
			attempt += 1

	def request(self, function: Builder) -> bytes:
		credentials = self._credentials
		root = ET.Element("request")
		control = ET.SubElement(root, "control")
		for tag, text in (
			("senderid", credentials.sender_id),
			("password", credentials.sender_password),
			("controlid", CONTROL_ID),
			("uniqueid", "false"),
			("dtdversion", DTD_VERSION),
		):
			ET.SubElement(control, tag).text = text
		operation = ET.SubElement(root, "operation")
		login = ET.SubElement(ET.SubElement(operation, "authentication"), "login")
		for tag, text in (
			("userid", credentials.user_id),
			("companyid", credentials.company_id),
			("password", credentials.user_password),
		):
			ET.SubElement(login, tag).text = text
		holder = ET.SubElement(ET.SubElement(operation, "content"), "function", controlid=FUNCTION_ID)
		function(holder)
		return b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root)

	def _result(self, response: HttpResponse) -> Node:
		if response.status != HTTP_OK:
			raise SourceError(f"{LABEL} returned HTTP {response.status}.")
		try:
			root = ET.fromstring(response.body)  # noqa: S314 - see the import
		except ET.ParseError:
			raise SourceError(f"{LABEL} returned a response that isn't XML.") from None
		document = Node({"response": xml_value(root)}).required_child("response")
		control = document.required_child("control")
		if control.text("status") != SUCCESS:
			raise CredentialsRejected(
				f"{LABEL} refused the Web Services sender: {errors_of(document)}; check the sender ID and password."
			)
		operation = document.required_child("operation")
		authentication = operation.required_child("authentication")
		if authentication.text("status") != SUCCESS:
			raise CredentialsRejected(
				f"{LABEL} refused the login: {errors_of(operation)}; check the company ID, user ID and password."
			)
		result = operation.child("result")
		if result is None:
			raise SourceError(f"{LABEL} answered no result: {errors_of(operation)}.")
		if result.text("status") != SUCCESS:
			raise SourceError(f"{LABEL} refused the request: {errors_of(result)}.")
		return result

	def __repr__(self) -> str:
		return "Gateway([redacted])"


def errors_of(node: Node) -> str:
	"""The error numbers and descriptions an errormessage holds."""
	holder = node.child("errormessage")
	if holder is None:
		return "no reason given"
	raw = holder.get("error")
	errors = raw if isinstance(raw, list) else [raw] if raw is not None else []
	described = [
		" ".join(
			str(e.get(part)) for part in ("errorno", "description2") if isinstance(e, dict) and e.get(part)
		)
		for e in errors
	]
	return "; ".join(text for text in described if text) or "no reason given"
