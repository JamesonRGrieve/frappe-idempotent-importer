# SPDX-License-Identifier: AGPL-3.0-or-later
"""``bench --site <site> idempotent-import …``: runs in the foreground, status, single-object syncs and
cursor resets. A run started here releases the connection's credential hold (one deliberate attempt)."""

from __future__ import annotations

import json
from typing import Any

import click
from frappe.commands import get_site, pass_context


def connect(context: Any) -> None:
	import frappe

	frappe.init(site=get_site(context))
	frappe.connect()
	frappe.set_user("Administrator")


@click.group("idempotent-import")
def idempotent_import() -> None:
	"""Mirror Importer Connections into this site."""


@idempotent_import.command("run")
@click.option(
	"--connection",
	"connections",
	multiple=True,
	help="Importer Connection (repeatable; default: every enabled one).",
)
@click.option("--full", is_flag=True, help="Read everything and retire what the source no longer has.")
@click.option("--dry-run", is_flag=True, help="Count what would change; write nothing.")
@click.option("--kinds", default="", help="Only these kinds, comma-separated (e.g. product,order).")
@pass_context
def run(context: Any, connections: tuple[str, ...], full: bool, dry_run: bool, kinds: str) -> None:
	"""Run connections now, in the foreground, until each ends or pauses for its source."""
	import frappe

	from idempotent_importer.core.kind import Kind
	from idempotent_importer.erpnext import engine
	from idempotent_importer.erpnext.stores import CONNECTION

	connect(context)
	try:
		names = list(connections) or frappe.get_all(CONNECTION, filters={"enabled": 1}, pluck="name")
		only = [Kind.parse(kind.strip()) for kind in kinds.split(",") if kind.strip()] or None
		for name in names:
			click.echo(f"{name}: {json.dumps(engine.run_by_hand(name, full, dry_run, only), sort_keys=True)}")
	finally:
		frappe.destroy()


@idempotent_import.command("status")
@click.option("--connection", "connections", multiple=True)
@pass_context
def status(context: Any, connections: tuple[str, ...]) -> None:
	"""The last run of each connection, and any credential hold."""
	import frappe

	from idempotent_importer.erpnext import engine
	from idempotent_importer.erpnext.stores import CONNECTION

	connect(context)
	try:
		for name in list(connections) or frappe.get_all(CONNECTION, pluck="name"):
			click.echo(json.dumps(engine.status(name), sort_keys=True))
	finally:
		frappe.destroy()


@idempotent_import.command("sync")
@click.option("--connection", required=True)
@click.argument("kind")
@click.argument("gid")
@pass_context
def sync(context: Any, connection: str, kind: str, gid: str) -> None:
	"""Re-read one object (by its gid://… identity) and mirror it now."""
	import frappe

	from idempotent_importer.core.kind import Kind
	from idempotent_importer.erpnext import engine

	connect(context)
	try:
		click.echo(engine.sync_one(connection, Kind.parse(kind), gid).value)
	finally:
		frappe.destroy()


@idempotent_import.command("reset-cursors")
@click.option("--connection", required=True)
@pass_context
def reset_cursors(context: Any, connection: str) -> None:
	"""Forget a connection's incremental cursors, so its next run reads everything."""
	import frappe

	from idempotent_importer.erpnext.stores import FrappeCursors

	connect(context)
	try:
		FrappeCursors(connection).reset()
		click.echo(f"{connection}: cursors reset")
	finally:
		frappe.destroy()


commands = [idempotent_import]
