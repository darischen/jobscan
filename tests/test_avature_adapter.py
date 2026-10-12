"""Avature portal adapter, offline against a fake board.

The fake board serves SearchJobs pages in the shape jobs.ea.com returned on
2026-10-12: a "N results" line, then <article class="article--result"> cards
with a JobDetail link and a subtitle of list-item-* spans. Like EA it serves
a fixed page size whatever jobRecordsPerPage asks for.
"""
from __future__ import annotations

import httpx
import pytest

from jobscan import adapters
from jobscan.adapters import KNOWN, TIER_A, TIER_B, avature

HOST = "jobs.example.com"
SITE = "en_US/careers"


def _card(n: int) -> str:
    return f"""
<article class="article article--result article--non-toggle" id="article--{n}">
  <div class="article__header"><div class="article__header__text">
    <h3 class="article__header__text__title title title--04 " data-au="ag-h3-6">
      <a class="link link_result" href="https://{HOST}/{SITE}/JobDetail/Game-Designer-{n}/{5000 + n}" data-au="ag-a-10">
        Game Designer &amp; Builder {n}
      </a>
    </h3>
    <div class="article__header__text__subtitle">
      <span class="list-item-location">Vancouver, Canada</span>
      <span class="separator" aria-hidden="true">&nbsp;&#8226;&nbsp;</span>
      <span class="list-item-id">Role ID {5000 + n}</span>
      <span class="list-item-workerType">Regular Employee</span>
      <span class="list-item-department">EA Studios - SPORTS</span>
    </div>
  </div></div>
  <div class="article__footer">
    <a class="button button--primary" href="https://{HOST}/{SITE}/JobDetail/Game-Designer-{n}/{5000 + n}">More Information</a>
  </div>
</article>"""


def _board(total: int, per_page: int = 20, calls: list | None = None,
           loop_last: bool = False, show_total: bool = True):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/{SITE}/SearchJobs/"
        offset = int(request.url.params["jobOffset"])
        if calls is not None:
            calls.append(offset)
        ids = list(range(offset, min(offset + per_page, total)))
        if loop_last and not ids:
            ids = list(range(total - per_page, total))  # re-serves the last page
        head = f"<p>{total:,} results</p>" if show_total else ""
        html = (f"<html><body><div class=\"jobs\">{head}"
                f"<div class=\"results\">{''.join(_card(i) for i in ids)}</div>"
                f"</div></body></html>")
        return httpx.Response(200, text=html)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(adapters, "AVATURE_DELAY", 0)


async def test_pages_by_cards_received_to_the_boards_total():
    calls: list[int] = []
    async with _board(45, calls=calls) as c:
        jobs = await avature(c, "EA", {"host": HOST, "site": SITE})
    assert len(jobs) == 45
    assert len({j.raw_id for j in jobs}) == 45
    # The portal ignores jobRecordsPerPage; offsets follow what it served.
    assert calls == [0, 20, 40]


async def test_parses_card_fields():
    async with _board(1) as c:
        [job] = await avature(c, "EA", {"host": HOST, "site": SITE})
    assert job.title == "Game Designer & Builder 0"
    assert job.url == f"https://{HOST}/{SITE}/JobDetail/Game-Designer-0/5000"
    assert job.raw_id == "5000"
    assert job.location == "Vancouver, Canada"
    assert job.department == "EA Studios - SPORTS"
    assert job.ats == "avature"
    assert job.posted_at is None


async def test_large_total_with_thousands_separator():
    async with _board(1234, per_page=100) as c:
        jobs = await avature(c, "EA", {"host": HOST, "site": SITE})
    assert len(jobs) == 1234


async def test_stops_when_a_page_repeats_without_a_total():
    calls: list[int] = []
    async with _board(30, calls=calls, loop_last=True, show_total=False) as c:
        jobs = await avature(c, "EA", {"host": HOST, "site": SITE})
    assert len(jobs) == 30
    assert len(calls) == 3      # 0, 20, then a page of repeats ends it


async def test_empty_board():
    async with _board(0) as c:
        assert await avature(c, "EA", {"host": HOST, "site": SITE}) == []


async def test_requires_host_and_site():
    async with _board(1) as c:
        with pytest.raises(ValueError):
            await avature(c, "EA", {"host": HOST})


def test_registered_as_tier_a():
    assert TIER_A["avature"] is avature
    assert "avature" not in TIER_B and "avature" in KNOWN
