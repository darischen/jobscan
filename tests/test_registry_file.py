"""The committed registry parses one record per line.

A note opened with a quote and never closed swallows every row after it into
that note's text. csv reports no error, registry.load drops nothing it can
see, and the swallowed companies silently stop being scanned. On 2026-10-01 a
hand edit to Meta's note hid 13 companies this way until a row count was
checked by eye.
"""
from __future__ import annotations

import csv
from pathlib import Path

from jobscan import registry
from jobscan.registry import COLUMNS

REGISTRY = Path(__file__).resolve().parents[1] / "registry" / "companies.csv"


def test_one_record_per_line():
    lines = [l for l in REGISTRY.read_text(encoding="utf-8-sig").splitlines() if l.strip()]
    with REGISTRY.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == len(lines) - 1
    swallowed = [r["company"] for r in rows if "\n" in (r.get("notes") or "")]
    assert not swallowed


def test_every_row_has_exactly_the_registry_columns():
    with REGISTRY.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        assert next(reader) == COLUMNS
        bad = [r[0] for r in reader if r and len(r) != len(COLUMNS)]
    assert not bad


def test_registry_loads_and_validates():
    rows = registry.load(REGISTRY)
    assert rows
