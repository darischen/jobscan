"""verify.py's dated check and baseline upkeep, offline.

Radancy, Meta, Avature and two SuccessFactors boards publish no posting date,
so "only 0% carry a date" fired on every run and trained the eye to skip it.
The useful signal is a board that used to carry dates and stopped. Baselines
also only ever grew: keys for removed or rekeyed rows (the single-row "SAP"
after it became two rows) stayed forever.
"""
from __future__ import annotations

import asyncio

import httpx

import tools.verify as verify
from jobscan.core import Job


def _jobs(n: int, dated: bool) -> list[Job]:
    return [Job(company="Acme", title=f"Engineer {i}", url="", location="",
                ats="fake", raw_id=str(i),
                posted_at="2026-10-01T00:00:00+00:00" if dated else None)
            for i in range(n)]


async def _check(monkeypatch, dated: bool, baseline: dict):
    async def fake(c, company, row):
        return _jobs(50, dated)
    monkeypatch.setitem(verify.TIER_A, "fake", fake)
    async with httpx.AsyncClient() as c:
        return await verify.check(c, asyncio.Semaphore(1),
                                  {"company": "Acme", "ats": "fake"}, 0, baseline)


async def test_an_undated_board_with_no_baseline_still_warns(monkeypatch):
    res = await _check(monkeypatch, dated=False, baseline={})
    assert any("carry a date" in w for w in res["warnings"])


async def test_a_board_known_to_be_undated_does_not_warn(monkeypatch):
    res = await _check(monkeypatch, dated=False,
                       baseline={"Acme": {"count": 50, "dated": 0}})
    assert res["status"] == "ok"


async def test_a_board_that_loses_its_dates_warns(monkeypatch):
    res = await _check(monkeypatch, dated=False,
                       baseline={"Acme": {"count": 50, "dated": 100}})
    assert any("100% -> 0%" in w for w in res["warnings"])


def test_a_full_run_prunes_keys_with_no_row():
    old = {"SAP": {"count": 788}, "Acme": {"count": 40}, "Gone Inc": {"count": 9}}
    snap = {"Acme": {"count": 50}, "SAP [careers.sap.com]": {"count": 730}}
    live = {"Acme", "SAP [careers.sap.com]", "Failing Co"}
    merged = verify.merge_baseline(old, snap, live, full_run=True)
    assert set(merged) == {"Acme", "SAP [careers.sap.com]"}
    assert merged["Acme"]["count"] == 50


def test_a_full_run_keeps_the_old_baseline_of_a_row_that_failed_today():
    # A board that is down today is still in the registry; losing its
    # baseline would blind the next run to drift.
    old = {"Failing Co": {"count": 300}}
    merged = verify.merge_baseline(old, {}, {"Failing Co"}, full_run=True)
    assert merged == old


def test_a_filtered_run_prunes_nothing():
    old = {"Acme": {"count": 40}, "Other": {"count": 9}}
    merged = verify.merge_baseline(old, {"Acme": {"count": 50}}, {"Acme"},
                                   full_run=False)
    assert set(merged) == {"Acme", "Other"}


async def test_link_checks_send_browser_navigation_headers():
    # metacareers.com answers 400 to a request without Sec-Fetch headers and
    # 200 with them, so every Meta link read as dead.
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        ok = request.headers.get("sec-fetch-mode") == "navigate"
        return httpx.Response(200 if ok else 400)

    job = Job(company="Acme", title="Engineer", url="https://jobs.test/1",
              location="", ats="fake", raw_id="1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await verify.sample_live(c, [job], 1, retry_delay=0)
    assert (r.ok, r.dead) == (1, 0)
    assert "text/html" in seen["accept"]
