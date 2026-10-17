# SPDX-License-Identifier: AGPL-3.0-or-later
"""``python -m tools.spdx``: every source file carries the AGPL-3.0-or-later SPDX header (JSON has no
comments, so data and doctype files are exempt; the LICENSE is the licence itself)."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEADER = "SPDX-License-Identifier: AGPL-3.0-or-later"
EXEMPT_SUFFIXES = frozenset({".json", ".lock", ".pyc"})
# The licence itself, Frappe's plain-text module lists, and data files with no comment syntax.
EXEMPT_NAMES = frozenset(
	{
		"LICENSE",
		"modules.txt",
		"patches.txt",
		".gitignore",
		".coverage",
		".coverage-baseline",
		".writer-coverage-baseline",
	}
)
# Generated, cached or third-party trees, never part of the source.
SKIPPED_DIRS = frozenset(
	{".git", ".venv", ".cache", "__pycache__", ".ruff_cache", ".mypy_cache", ".pytest_cache"}
)
HEAD_LINES = 3


def source_files(root: Path = ROOT) -> Iterator[Path]:
	for path in sorted(root.rglob("*")):
		relative = path.relative_to(root)
		if (
			path.is_file()
			and not SKIPPED_DIRS.intersection(relative.parts)
			and not relative.parts[0].endswith(".egg-info")
		):
			yield relative


def missing_header(root: Path, path: Path) -> bool:
	if path.suffix in EXEMPT_SUFFIXES or path.name in EXEMPT_NAMES:
		return False
	head = (root / path).read_text(errors="replace").splitlines()[:HEAD_LINES]
	return not any(HEADER in line for line in head)


def main() -> int:
	missing = [str(path) for path in source_files() if missing_header(ROOT, path)]
	if missing:
		sys.stderr.write("Files without the SPDX header:\n  " + "\n  ".join(missing) + "\n")
		return 1
	sys.stdout.write("Every source file carries the SPDX header.\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
