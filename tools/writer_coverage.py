# SPDX-License-Identifier: AGPL-3.0-or-later
"""``python -m tools.writer_coverage <coverage.xml>``: the ERPNext writers' line coverage from the bench's
``run-tests --coverage`` report (Cobertura XML), held to the one-way ratchet in .writer-coverage-baseline.
Raise the baseline in the commit that raises coverage; it never goes down."""

from __future__ import annotations

import sys
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".writer-coverage-baseline"
WRITERS = "idempotent_importer/erpnext/writers/"
PERCENT = Decimal(100)


def writer_coverage(report: Path) -> tuple[Decimal, dict[str, Decimal]]:
	"""The writers' overall line coverage and each writer file's, in percent."""
	covered = total = 0
	per_file: dict[str, Decimal] = {}
	for element in ElementTree.parse(report).iter("class"):  # noqa: S314 - our own bench's report
		filename = element.get("filename", "")
		if WRITERS not in filename:
			continue
		lines = list(element.iter("line"))
		hits = sum(1 for line in lines if int(line.get("hits", "0")) > 0)
		covered += hits
		total += len(lines)
		per_file[filename[filename.index(WRITERS) :]] = PERCENT * hits / len(lines) if lines else PERCENT
	if total == 0:
		raise ValueError(f"{report} covers no writer")
	return PERCENT * covered / total, per_file


def main() -> int:
	overall, per_file = writer_coverage(Path(sys.argv[1]))
	for filename, percent in sorted(per_file.items()):
		sys.stdout.write(f"{filename}: {percent.quantize(Decimal('0.1'))}%\n")
	baseline = Decimal(BASELINE.read_text().strip())
	reached = overall.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
	if reached < baseline:
		sys.stderr.write(f"Writer coverage {reached}% is below the baseline {baseline}%.\n")
		return 1
	sys.stdout.write(f"Writer coverage {reached}% (baseline {baseline}%).\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
