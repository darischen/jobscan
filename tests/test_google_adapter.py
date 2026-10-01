"""Google board pagination, offline against a fake board.

Two regressions measured live on 2026-10-01, when the full board returned 539
of ~3,400 postings:

- The board reorders results between requests, so a page can repeat a posting
  from the page before. The loop treated "fewer than 20 new ids" as the end of
  the board and stopped at page 27.
- The total moved from "3,617 jobs matched" to "Showing 1 to 20 of 3414 rows",
  so the total-driven stop condition silently never fired.
"""
from __future__ import annotations

import json

import httpx
import pytest

from jobscan.adapters import GOOGLE_PAGE_SIZE, google


def _page(ids: list[str], total: int | None, wording: str = "rows") -> str:
    records = [[i, f"Job {i}", None, None, None, None, None, "Google",
                None, [["Mountain View, CA, USA"]], None, None, [1767225600, 0]]
               for i in ids]
    blob = "{key: 'ds:1', data: " + json.dumps([records]) + ", sideChannel: {}}"
    if total is None:
        status = ""
    elif wording == "rows":
        status = f"<span>Showing 1 to {len(ids)} of {total} rows</span>"
    else:
        status = f"<span>{total:,} jobs matched</span>"
    return f"<html>{status}<script>AF_initDataCallback({blob});</script></html>"


def _board(pages: dict[int, list[str]], total: int | None, wording: str = "rows"):
    """A fake board serving `pages`, plus the list of pages it was asked for."""
    asked: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.params["page"])
        asked.append(n)
        return httpx.Response(200, text=_page(pages.get(n, []), total, wording))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), asked


def _ids(start: int, n: int = GOOGLE_PAGE_SIZE) -> list[str]:
    return [str(100000 + i) for i in range(start, start + n)]


async def test_a_repeated_posting_does_not_end_the_scan():
    page2 = _ids(20, 19) + [_ids(0)[5]]  # full page, one id repeated from page 1
    pages = {1: _ids(0), 2: page2, 3: _ids(39)}
    client, asked = _board(pages, total=None)
    async with client:
        jobs = await google(client, "Google", {})
    assert len(jobs) == 59
    assert 3 in asked


async def test_stops_at_the_total_in_the_current_wording():
    pages = {1: _ids(0), 2: _ids(20), 3: _ids(40)}
    client, asked = _board(pages, total=40)
    async with client:
        jobs = await google(client, "Google", {})
    assert len(jobs) == 40
    assert asked == [1, 2]


async def test_still_reads_the_old_total_wording():
    pages = {1: _ids(0), 2: _ids(20), 3: _ids(40)}
    client, asked = _board(pages, total=40, wording="matched")
    async with client:
        jobs = await google(client, "Google", {})
    assert len(jobs) == 40
    assert asked == [1, 2]


async def test_a_short_page_is_still_the_end():
    pages = {1: _ids(0), 2: _ids(20, 7)}
    client, asked = _board(pages, total=None)
    async with client:
        jobs = await google(client, "Google", {})
    assert len(jobs) == 27
    assert asked == [1, 2]
