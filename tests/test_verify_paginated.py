"""verify.py's paginated check: postings returned against the board's total.

The docstring promised this check from the start and nothing implemented it.
Every truncation found by hand in 2026-10 would have tripped it: Google
stopping at page 27 (539 of 3,414), Lowe's at offset 5,000 (5,020 of 12,603),
JPMorgan the same way (5,100 of 7,302), Apple on an empty page (20 of 4,395).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

import tools.verify as verify
from jobscan.adapters import _board_total
from jobscan.core import Job


def _jobs(n: int) -> list[Job]:
    return [Job(company="Acme", title=f"Engineer {i}", url="", location="",
                ats="fake", raw_id=str(i), posted_at="2026-10-01T00:00:00+00:00")
            for i in range(n)]


async def _check(monkeypatch, returned: int, total: int | None):
    async def fake(c, company, row):
        if total is not None:
            _board_total(row, total)
        return _jobs(returned)
    monkeypatch.setitem(verify.TIER_A, "fake", fake)
    row = {"company": "Acme", "ats": "fake"}
    async with httpx.AsyncClient() as c:
        return await verify.check(c, asyncio.Semaphore(1), row, 0, {})


async def test_a_truncated_board_warns_with_both_numbers(monkeypatch):
    res = await _check(monkeypatch, returned=5020, total=12603)
    assert res["status"] == "WARN"
    assert any("5020 of board total 12603" in w for w in res["warnings"])


async def test_a_board_read_to_its_total_is_ok(monkeypatch):
    res = await _check(monkeypatch, returned=7302, total=7302)
    assert res["status"] == "ok"


async def test_a_few_postings_of_churn_do_not_warn(monkeypatch):
    # Boards change mid-sweep: JPMorgan read 7,301 of 7,302, Microsoft 2,236
    # of 2,237. That is not truncation.
    res = await _check(monkeypatch, returned=7301, total=7302)
    assert res["status"] == "ok"


async def test_more_postings_than_the_total_also_warns(monkeypatch):
    # Repeats counted as postings: Lowe's-style replay inflating a count.
    res = await _check(monkeypatch, returned=5000, total=2000)
    assert res["status"] == "WARN"


async def test_no_reported_total_is_not_a_warning(monkeypatch):
    # Lever, Ashby and other single-list boards have no total to compare.
    res = await _check(monkeypatch, returned=40, total=None)
    assert res["status"] == "ok"


def test_board_total_ignores_missing_or_zero_counts():
    row: dict = {}
    _board_total(row, None)
    _board_total(row, 0)
    assert "_board_total" not in row
    _board_total(row, "1,234")
    assert row["_board_total"] == 1234
