# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine's persistence on Frappe, scoped to one Importer Connection: links (Importer Link), the full
pass's seen identities (Importer Seen), the run in progress (Importer Run), cursors, the run lock and the
credential hold (fields of the connection). Times are stored as naive UTC."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import frappe

from idempotent_importer.core.kind import Kind
from idempotent_importer.core.run import Mode, RunCounts, RunState
from idempotent_importer.core.sync import Link, Target

CONNECTION = "Importer Connection"
LINK = "Importer Link"
SEEN = "Importer Seen"
RUN = "Importer Run"
LOCK_TTL = timedelta(minutes=10)
RUNNING = "Running"


def naive_utc(moment: datetime) -> datetime:
	return moment.astimezone(UTC).replace(tzinfo=None)


def aware_utc(value: datetime | str | None) -> datetime | None:
	if value is None or value == "":
		return None
	moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
	return moment.replace(tzinfo=UTC)


def now_utc() -> datetime:
	return datetime.now(UTC)


class FrappeLinkStore:
	def __init__(self, connection: str) -> None:
		self.connection = connection

	def find(self, kind: Kind, gid: str) -> Link | None:
		row = frappe.db.get_value(
			LINK,
			{"connection": self.connection, "kind": kind.value, "gid": gid},
			["target_doctype", "target_name", "hash", "mapper_version", "retired"],
			as_dict=True,
		)
		if row is None:
			return None
		return Link(
			kind,
			gid,
			Target(row.target_doctype, row.target_name),
			row.hash or "",
			int(row.mapper_version or 0),
			bool(row.retired),
		)

	def save(self, kind: Kind, gid: str, target: Target, hash_: str, mapper_version: int) -> None:
		values = {
			"target_doctype": target.doctype,
			"target_name": target.name,
			"hash": hash_,
			"mapper_version": mapper_version,
			"retired": 0,
			"synced_at": naive_utc(now_utc()),
		}
		name = self._name(kind, gid)
		if name:
			frappe.db.set_value(LINK, name, values, update_modified=False)
			return
		frappe.get_doc(
			{"doctype": LINK, "connection": self.connection, "kind": kind.value, "gid": gid, **values}
		).db_insert()

	def retire(self, kind: Kind, gid: str) -> None:
		name = self._name(kind, gid)
		if name:
			frappe.db.set_value(LINK, name, {"retired": 1, "hash": ""}, update_modified=False)

	def unseen(self, kind: Kind, run_id: str) -> list[Link]:
		seen = frappe.qb.DocType(SEEN)
		link = frappe.qb.DocType(LINK)
		rows = (
			frappe.qb.from_(link)
			.left_join(seen)
			.on((seen.run == run_id) & (seen.kind == link.kind) & (seen.gid == link.gid))
			.select(link.gid, link.target_doctype, link.target_name, link.hash, link.mapper_version)
			.where(
				(link.connection == self.connection)
				& (link.kind == kind.value)
				& (link.retired == 0)
				& seen.gid.isnull()
			)
			.orderby(link.creation)
		).run(as_dict=True)
		return [
			Link(
				kind,
				row.gid,
				Target(row.target_doctype, row.target_name),
				row.hash or "",
				int(row.mapper_version or 0),
			)
			for row in rows
		]

	def target(self, kind: Kind, gid: str) -> str | None:
		"""The live record a sub-object or dependency was mirrored to, or None."""
		link = self.find(kind, gid)
		return None if link is None or link.retired else link.target.name

	def ending(self, kind: Kind, gid_suffix: str) -> str | None:
		"""The live record of the one part of ``kind`` whose identity ends with ``gid_suffix`` (a variant by
		its own identity, when the source doesn't name its product), or None."""
		names = frappe.get_all(
			LINK,
			filters={
				"connection": self.connection,
				"kind": kind.value,
				"gid": ["like", f"%{escape_like(gid_suffix)}"],
				"retired": 0,
			},
			pluck="target_name",
		)
		return str(names[0]) if len(names) == 1 else None

	def children(self, kind: Kind, gid_prefix: str) -> list[Link]:
		"""Live links of ``kind`` whose identity starts with ``gid_prefix`` (an object's parts)."""
		rows = frappe.get_all(
			LINK,
			filters={
				"connection": self.connection,
				"kind": kind.value,
				"gid": ["like", f"{escape_like(gid_prefix)}%"],
				"retired": 0,
			},
			fields=["gid", "target_doctype", "target_name", "hash", "mapper_version"],
			order_by="creation asc",
		)
		return [
			Link(
				kind,
				row.gid,
				Target(row.target_doctype, row.target_name),
				row.hash or "",
				int(row.mapper_version or 0),
			)
			for row in rows
		]

	def _name(self, kind: Kind, gid: str) -> str | None:
		name = frappe.db.get_value(LINK, {"connection": self.connection, "kind": kind.value, "gid": gid})
		return str(name) if name else None


def escape_like(text: str) -> str:
	return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class FrappeSeenStore:
	def mark(self, run_id: str, kind: Kind, gids: Sequence[str]) -> None:
		if not gids:
			return
		now = frappe.utils.now()
		frappe.db.bulk_insert(
			SEEN,
			["name", "run", "kind", "gid", "creation", "modified", "owner", "modified_by"],
			[
				(
					frappe.generate_hash(length=16),
					run_id,
					kind.value,
					gid,
					now,
					now,
					"Administrator",
					"Administrator",
				)
				for gid in gids
			],
		)
		frappe.db.commit()

	def clear(self, run_id: str) -> None:
		frappe.db.delete(SEEN, {"run": run_id})
		frappe.db.commit()


class FrappeRunStore:
	"""The connection's run in progress is its newest Importer Run whose status is Running."""

	def __init__(self, connection: str, log_lines: list[str]) -> None:
		self.connection = connection
		self._log_lines = log_lines

	def load(self) -> RunState | None:
		name = frappe.db.get_value(
			RUN, {"connection": self.connection, "status": RUNNING}, order_by="creation desc"
		)
		if not name:
			return None
		run = frappe.db.get_value(
			RUN,
			name,
			[
				"mode",
				"dry_run",
				"kinds",
				"started_at",
				"kind_index",
				"cursor",
				"counts",
				"resume_at",
				"pauses",
			],
			as_dict=True,
		)
		started = aware_utc(run.started_at)
		if started is None:
			return None
		return RunState(
			id=str(name),
			mode=Mode(run.mode),
			dry_run=bool(run.dry_run),
			kinds=[Kind(kind) for kind in json.loads(run.kinds or "[]")],
			started_at=started,
			index=int(run.kind_index or 0),
			cursor=run.cursor or None,
			counts=RunCounts.from_json(json.loads(run.counts or "{}")),
			resume_at=aware_utc(run.resume_at),
			pauses=int(run.pauses or 0),
		)

	def start(self, state: RunState) -> None:
		doc = frappe.get_doc(
			{
				"doctype": RUN,
				"connection": self.connection,
				"mode": state.mode.value,
				"dry_run": int(state.dry_run),
				"status": RUNNING,
				"started_at": naive_utc(state.started_at),
				"kinds": json.dumps([kind.value for kind in state.kinds]),
				"kind_index": 0,
				"counts": "{}",
			}
		)
		doc.insert(ignore_permissions=True, set_name=state.id)
		frappe.db.set_value(CONNECTION, self.connection, "last_run", doc.name, update_modified=False)
		frappe.db.commit()

	def save(self, state: RunState) -> None:
		frappe.db.set_value(RUN, state.id, self._values(state), update_modified=False)
		frappe.db.commit()

	def finish(self, state: RunState, status: str) -> None:
		values = self._values(state) | {
			"status": status,
			"finished_at": naive_utc(now_utc()),
			"summary": state.counts.summary(),
		}
		frappe.db.set_value(RUN, state.id, values, update_modified=False)
		frappe.db.commit()

	def _values(self, state: RunState) -> dict[str, object]:
		log = frappe.db.get_value(RUN, state.id, "log") or ""
		lines = "\n".join(self._log_lines)
		self._log_lines.clear()
		return {
			"kind_index": state.index,
			"cursor": state.cursor,
			"counts": json.dumps(state.counts.to_json(), sort_keys=True),
			"resume_at": naive_utc(state.resume_at) if state.resume_at else None,
			"pauses": state.pauses,
			"log": f"{log}\n{lines}".strip() if lines else log,
		}


class FrappeCursors:
	def __init__(self, connection: str) -> None:
		self.connection = connection

	def last_clean_start(self, kind: Kind) -> datetime | None:
		return aware_utc(self._all().get(kind.value))

	def advance(self, kind: Kind, run_started_at: datetime) -> None:
		cursors = self._all()
		cursors[kind.value] = naive_utc(run_started_at).isoformat()
		self._store(cursors)

	def reset(self) -> None:
		self._store({})

	def _all(self) -> dict[str, str]:
		stored = frappe.db.get_value(CONNECTION, self.connection, "cursors")
		cursors = json.loads(stored) if stored else {}
		return {str(k): str(v) for k, v in cursors.items()} if isinstance(cursors, dict) else {}

	def _store(self, cursors: dict[str, str]) -> None:
		frappe.db.set_value(
			CONNECTION, self.connection, "cursors", json.dumps(cursors, sort_keys=True), update_modified=False
		)
		frappe.db.commit()


class FrappeRunLock:
	"""A lease on the connection row: taken under SELECT … FOR UPDATE, so only one caller can take a free
	(or expired) lease."""

	def __init__(self, connection: str) -> None:
		self.connection = connection

	def acquire(self, run_id: str) -> bool:
		holder, expires = frappe.db.get_value(
			CONNECTION, self.connection, ["lock_run", "lock_expires"], for_update=True
		)
		expiry = aware_utc(expires)
		if holder and expiry is not None and expiry > now_utc():
			frappe.db.rollback()
			return False
		self._set(run_id, now_utc() + LOCK_TTL)
		return True

	def refresh(self, run_id: str) -> None:
		self.hold_until(run_id, now_utc())

	def hold_until(self, run_id: str, until: datetime) -> None:
		if self.holder() == run_id:
			self._set(run_id, until + LOCK_TTL)

	def release(self, run_id: str) -> None:
		if self.holder() == run_id:
			frappe.db.set_value(
				CONNECTION, self.connection, {"lock_run": None, "lock_expires": None}, update_modified=False
			)
			frappe.db.commit()

	def holder(self) -> str | None:
		holder, expires = frappe.db.get_value(CONNECTION, self.connection, ["lock_run", "lock_expires"])
		expiry = aware_utc(expires)
		return holder if holder and expiry is not None and expiry > now_utc() else None

	def _set(self, run_id: str, expires: datetime) -> None:
		frappe.db.set_value(
			CONNECTION,
			self.connection,
			{"lock_run": run_id, "lock_expires": naive_utc(expires)},
			update_modified=False,
		)
		frappe.db.commit()


class FrappeCredentialHold:
	def __init__(self, connection: str) -> None:
		self.connection = connection

	def hold(self, reason: str) -> None:
		frappe.db.set_value(
			CONNECTION,
			self.connection,
			{"credential_hold": reason, "credential_hold_since": naive_utc(now_utc())},
			update_modified=False,
		)
		frappe.db.commit()

	def reason(self) -> str | None:
		reason, since = frappe.db.get_value(
			CONNECTION, self.connection, ["credential_hold", "credential_hold_since"]
		)
		return f"{reason} (since {since} UTC)" if reason else None

	def release(self) -> None:
		frappe.db.set_value(
			CONNECTION,
			self.connection,
			{"credential_hold": None, "credential_hold_since": None},
			update_modified=False,
		)
		frappe.db.commit()
