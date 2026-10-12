"""Oracle Cloud HCM boards, offline against a fake requisitions API.

JPMorgan Chase reports TotalJobsCount 7,302 (2026-10-12) and pages cleanly
past offset 5,000, but the loop stopped there, so it read as exactly 5,100 on
every scan: the round number in BACKLOG section 5 that looked like a cap.
"""
from __future__ import annotations

import httpx

from jobscan.adapters import oracle

ROW = {"host": "acme.fa.oraclecloud.com", "site": "CX_1"}


def _req(i: int) -> dict:
    return {"Id": str(300000 + i), "Title": f"Engineer {i}",
            "PrimaryLocation": "New York, NY, United States",
            "PostedDate": "2026-10-01"}


def _board(n: int, repeat_from: int | None = None):
    """A board of n requisitions, 100 a page. Past `repeat_from` it replays
    page one forever, the failure the replay guard exists for."""
    asked: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        finder = request.url.params["finder"]
        off = int(finder.split("offset=")[1].split(",")[0])
        asked.append(off)
        if repeat_from is not None and off >= repeat_from:
            off = 0
        reqs = [_req(i) for i in range(off, min(off + 100, n))]
        return httpx.Response(200, json={"items": [
            {"TotalJobsCount": n, "requisitionList": reqs}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), asked


async def test_pages_past_offset_5000_to_the_reported_total():
    client, _ = _board(7302)
    async with client:
        jobs = await oracle(client, "Acme", ROW)
    assert len(jobs) == 7302
    assert len({j.raw_id for j in jobs}) == 7302


async def test_a_board_replaying_itself_stops_instead_of_spinning():
    client, asked = _board(100_000, repeat_from=300)
    async with client:
        jobs = await oracle(client, "Acme", ROW)
    assert len(jobs) == 300
    assert len(asked) <= 5
