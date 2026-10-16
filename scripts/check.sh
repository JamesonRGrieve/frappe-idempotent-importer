#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# The pre-commit gate, mirrored in CI: lockfile, SPDX headers, format, lint, strict types, unit tests, and
# the proofs of every Shopify operation and fixture against the sources' published API descriptions.
# The end-to-end suite (tests/e2e/run.sh) runs on its own: it stands up a real bench in podman.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

run() {
	echo "== $*"
	uv run --frozen --no-sync "$@"
}

echo "== uv lock --check"
uv lock --check
uv sync --frozen --group dev --no-install-project --quiet
run python -m tools.spdx
run ruff format --check .
run ruff check .
run mypy
run python -I -m pytest -q
run python -m tools.prove
echo "== All checks passed"
