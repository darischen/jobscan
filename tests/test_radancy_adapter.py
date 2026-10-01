"""Radancy TalentBrew adapter, offline against a fake board.

The results endpoint returns JSON whose `results` is an HTML fragment of job
cards plus the board's own total. The fake board below serves that shape in
the two card layouts seen live on 2026-10-01: Intuit (category attributes on
the <li>, no requisition span) and UnitedHealth Group (requisition number and
brand spans inside the link).
"""
from __future__ import annotations

import httpx
import pytest

from jobscan.adapters import radancy
from jobscan import adapters

HOST = "careers.example.com"


def _intuit_card(n: int) -> str:
    return f"""
<li data-remote="{n}" data-intuit-jobid="{n}" data-category-id="68357" data-category='Data' data-orig-location="">
  <a href="/job/mountain-view/software-engineer-{n}/27595/{1000 + n}" data-job-id="{n}" class="sr-item">
    <h2>Software Engineer &amp; Builder {n}</h2>
    <span class="job-location">Mountain View, California</span>
  </a>
  <button type="button" class="js-save-job-btn" data-job-id="{1000 + n}" data-org-id="27595"><span class="wai">Save </span></button>
</li>"""


def _uhg_card(n: int) -> str:
    return f"""
<li>
<a href="/job/gallatin/cna-gallatin/34088/{1000 + n}" data-job-id="{1000 + n}" class="brand-facet brand-facet__optum">
<div>
<h2>CNA - Gallatin {n}</h2>
    <span class="job-id job-info">23929{n:02d}</span>
    <span class="job-divider"> | </span>
    <span class="job-location 1">Gallatin, Tennessee</span>
    <span class="job-divider"> | </span>
    <span class="job-info job-entity">LHC Group</span>
</div>
</a>
<button type="button" class="js-save-job-btn" aria-label="Save job" data-job-id="{1000 + n}" data-org-id="34088"></button>
</li>"""


def _board(total: int, card, per_page: int | None = None, repeat_first: bool = False,
           calls: list | None = None):
    """Serve `total` cards, paginated by the RecordsPerPage the client asks for."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search-jobs/results"
        # The facet HTML is ~8.7 MB per request on UHG; it must stay off.
        assert request.url.params["SearchFiltersModuleName"] == ""
        page = int(request.url.params["CurrentPage"])
        per = per_page or int(request.url.params["RecordsPerPage"])
        if calls is not None:
            calls.append(page)
        pages = -(-total // per)
        start = (page - 1) * per
        ids = list(range(start, min(start + per, total)))
        if repeat_first and page > 1:
            ids = [start - 1] + ids     # the board re-serves a card
        cards = "".join(card(i) for i in ids)
        html = (f'<section id="search-results" data-total-results="{total}" '
                f'data-total-pages="{pages}" data-current-page="{page}">'
                f'<ul>{cards}</ul>'
                f'<nav class="pagination"><ul><li><a href="/search-jobs?p=2">2</a></li></ul></nav>'
                f'</section>')
        return httpx.Response(200, json={"filters": "", "results": html,
                                         "hasJobs": bool(ids), "hasContent": True})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_paginates_to_the_boards_own_total(monkeypatch):
    monkeypatch.setattr(adapters, "RADANCY_PAGE", 10)
    calls: list[int] = []
    async with _board(25, _intuit_card, calls=calls) as c:
        jobs = await radancy(c, "Intuit", {"host": HOST})
    assert len(jobs) == 25
    assert len({j.raw_id for j in jobs}) == 25
    assert calls == [1, 2, 3]       # stops at data-total-pages, no extra request


async def test_dedupes_cards_the_board_serves_twice(monkeypatch):
    monkeypatch.setattr(adapters, "RADANCY_PAGE", 10)
    async with _board(25, _intuit_card, repeat_first=True) as c:
        jobs = await radancy(c, "Intuit", {"host": HOST})
    assert sorted(int(j.raw_id) for j in jobs) == list(range(1000, 1025))


async def test_maps_intuit_card():
    async with _board(1, _intuit_card) as c:
        [job] = await radancy(c, "Intuit", {"host": HOST})
    assert job.raw_id == "1000"     # the URL's job id, not the Avature data-job-id
    assert job.url == f"https://{HOST}/job/mountain-view/software-engineer-0/27595/1000"
    assert job.title == "Software Engineer & Builder 0"
    assert job.location == "Mountain View, California"
    assert job.department == ""     # data-category is unreliable, so ignored
    assert job.ats == "radancy"
    # The listing carries no date; first_seen in the store is the fallback.
    assert job.posted_at is None and job.posted_source == ""


async def test_maps_uhg_card():
    async with _board(1, _uhg_card) as c:
        [job] = await radancy(c, "UnitedHealth Group", {"host": HOST})
    assert job.raw_id == "1000"
    assert job.title == "CNA - Gallatin 0"
    assert job.location == "Gallatin, Tennessee"
    assert job.department == "LHC Group"


async def test_passes_query_as_keywords():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["kw"] = request.url.params["Keywords"]
        return httpx.Response(200, json={"results": '<section data-total-results="0" '
                                                    'data-total-pages="0"></section>'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await radancy(c, "Intuit", {"host": HOST, "query": "engineer"}) == []
    assert seen["kw"] == "engineer"


async def test_requires_host():
    async with httpx.AsyncClient() as c:
        with pytest.raises(ValueError):
            await radancy(c, "Intuit", {})
