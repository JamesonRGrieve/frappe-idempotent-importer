<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# frappe-idempotent-importer — Agent Operating Guide

AGPL-3.0-or-later Frappe v16 app (`idempotent_importer`): a one-way, idempotent mirror of other stores and
business systems into ERPNext, the ERPNext port of wp-woo-idempotent-importer. README.md is the
user-facing contract (and its "Decisions to confirm"); `coverage/<source>.md` is the single source of
truth for what each source mirrors and what is a gap. Python rules:
`/home/jameson/Source/ai-prompts/python.md`.

## Layout

- `core/` — the pure engine, no Frappe: `identity` (`gid://<source>/<connection>/<Type>/<id>`), `node`
  (validated reads), `money` (Decimal), `times` (UTC), `desired` (Desired, Mapper), `fields` (Values),
  `hasher` (the change signal), `http` (RateLimit, RequestPacer, RetryPolicy), `rest` (RestClient),
  `transport` (requests), `source` (Adapter, SourceReader, ConnectionConfig), `sync` (ObjectSync, the one
  map → hash → write path, and the LinkStore/Writer/WriteScope/Redirects protocols), `run` (Runner,
  RunState, PausePolicy, and its store protocols).
- `contract.py` — the shared field contract every mapper produces and every writer reads; `naming.py` —
  how globally named ERPNext records and routes are scoped to a connection.
- `sources/<source>/` — one adapter: `adapter`, `reader`, `mapping` (one mapper per kind), and for
  Shopify `client` + `queries` + `graphql/fragments`. `sources/registry.py` is the single list of sources;
  listed-but-unbuilt ones are `ListedAdapter`s that refuse to run.
- `erpnext/` — the Frappe side: `stores` (Importer Link/Seen/Run, cursors, lock, credential hold),
  `context` (a connection's targets), `media`, `writers/` (one writer per kind: catalog, parties,
  discounts, sales, content), `engine` (builds and drives a run), `jobs` (scheduler + step jobs).
- `api.py` (Sync Now), `commands.py` (`bench idempotent-import`), `hooks.py`, doctypes under
  `idempotent_importer/doctype/` (Importer Connection, Importer Link, Importer Run, Importer Seen).

## Rules

- Idempotent by construction: identity is (connection, kind, gid) in Importer Link; the change signal is
  the hash of the mapped fields and the mapper's `version`. Bump a mapper's version whenever its output
  changes. Never key on titles, slugs or handles.
- Mappers speak the shared contract, never ERPNext; writers speak ERPNext, never a source's vocabulary.
  Pure logic (mapping, naming, contract helpers) stays out of `erpnext/` so it is unit-tested.
- A connection's runs only retire that connection's links. Retire = disable, cancel, unpublish or mark;
  never delete (README lists the exceptions as decisions).
- Writers write only mapped fields; a part of an object with its own record (a variant, a refund, a
  payment, a contact) has its own link and hash. A record shared by connections (Website Settings) is
  never re-saved whole: write its own rows only.
- Something ERPNext can't represent exactly is `Unsupported` (and a gap in coverage/), never approximated.
  A missing dependency is `UnresolvedDependency` (deferred and retried), never skipped.
- Every object's writes are one transaction (`engine.write_scope`): mail muted, committed or rolled back
  as a whole. Sales never move stock; invoices must total the source's total exactly.
- Lockout safety: every client declares its documented limits (cited) and goes through RequestPacer +
  RetryPolicy; pushing back is `RetryLater` (the run pauses in place), rejected credentials are
  `CredentialsRejected` (the run stops, the credential hold blocks scheduled runs). Never retry in a loop of
  your own.
- Credentials live in Password fields; never logged, never in errors or reprs. Test endpoints come from the
  `idempotent_importer_test_endpoints` site config only.

## Testing

`scripts/check.sh` is the pre-commit gate (lockfile, SPDX, ruff format + lint, mypy --strict, pytest over
the recorded fixtures with the pure layers' coverage ratchet in `.coverage-baseline`, `python -m
tools.prove` against Shopify's schema and the transcribed WooCommerce OpenAPI document). `tests/e2e/run.sh` proves the Frappe side on a real bench in podman (two WooCommerce
connections bound to two Public Sites plus Shopify): first import, zero-write re-runs (content tables
byte-identical), changes and deletions, isolation between connections, lockout pause and hold, Sync Now on
the job queue, no mail. Every change to a writer is proven there; every pure change gets a unit test.
