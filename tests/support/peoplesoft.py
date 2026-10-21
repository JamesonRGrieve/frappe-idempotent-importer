# SPDX-License-Identifier: AGPL-3.0-or-later
"""The test installation's PeopleSoft field map (tests/fixtures/peoplesoft/field-map.json: its queries and
their columns, as its operator chose them; the end-to-end connection reads the same file) and its user."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIELD_MAP_FILE = Path(__file__).resolve().parents[1] / "fixtures" / "peoplesoft" / "field-map.json"
FIELD_MAP_TEXT = FIELD_MAP_FILE.read_text()
FIELD_MAP: dict[str, Any] = json.loads(FIELD_MAP_TEXT)
NODE = str(FIELD_MAP["node"])
MAX_ROWS = int(FIELD_MAP["max_rows"])
PS_USER = "e2e-ps-reader"
PS_PASSWORD = "e2e-ps-password"
