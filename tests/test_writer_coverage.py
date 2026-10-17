# SPDX-License-Identifier: AGPL-3.0-or-later
"""The writers' coverage ratchet over a bench's Cobertura report (tools/writer_coverage.py)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tools.writer_coverage import writer_coverage

REPORT = """<?xml version="1.0" ?>
<coverage><packages><package><classes>
<class filename="idempotent_importer/erpnext/writers/catalog.py"><lines>
<line number="1" hits="1"/><line number="2" hits="0"/><line number="3" hits="4"/><line number="4" hits="1"/>
</lines></class>
<class filename="idempotent_importer/erpnext/writers/sales.py"><lines><line number="1" hits="0"/></lines></class>
<class filename="idempotent_importer/erpnext/engine.py"><lines><line number="1" hits="0"/></lines></class>
</classes></package></packages></coverage>
"""


def test_only_the_writers_count(tmp_path: Path) -> None:
	report = tmp_path / "coverage.xml"
	report.write_text(REPORT)
	overall, per_file = writer_coverage(report)
	assert overall == Decimal(60)
	assert per_file == {
		"idempotent_importer/erpnext/writers/catalog.py": Decimal(75),
		"idempotent_importer/erpnext/writers/sales.py": Decimal(0),
	}


def test_a_report_without_writers_is_an_error(tmp_path: Path) -> None:
	report = tmp_path / "coverage.xml"
	report.write_text("<coverage/>")
	with pytest.raises(ValueError, match="covers no writer"):
		writer_coverage(report)
