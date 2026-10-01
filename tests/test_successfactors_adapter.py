"""SuccessFactors Career Site Builder boards, offline against fake boards.

Two front ends share one adapter. The classic listing is server rendered
HTML paged by `startrow`, in a table or a tile theme. The unify listing is a
JSON POST that needs the search page's CSRF token, and it reorders between
requests unless sorted by date, so it is deduped on id.
"""
from __future__ import annotations

import json

import httpx
import pytest

import jobscan.adapters as adapters
from jobscan.adapters import successfactors
from jobscan.registry import RegistryError, load
from tests.conftest import write_registry


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    monkeypatch.setattr(adapters, "SF_PAGE_DELAY", 0)


def _table_row(rid: int, title: str, loc: str, date: str, sub: str = "") -> str:
    href = f"{sub}/job/Somewhere-{title.replace(' ', '-')}-%26-Co/{rid}/"
    return f"""
    <tr class="data-row">
      <td class="colTitle"><span class="jobTitle hidden-phone">
        <a href="{href.replace('%26', '&amp;')}" class="jobTitle-link">{title} &amp; Co</a></span>
        <div class="jobdetail-phone visible-phone">
          <span class="jobLocation visible-phone"><span class="jobLocation">
            {loc}
          </span></span>
          <span class="jobDate visible-phone">{date}
          </span></div></td>
      <td class="colLocation hidden-phone"><span class="jobLocation">
            {loc}
        </span></td>
      <td class="colDate hidden-phone"><span class="jobDate">{date}
        </span></td>
      <td class="colDepartment"><span class="jobDepartment">Software</span></td>
    </tr>"""


def _table_page(total: int, rows: list[str]) -> str:
    return (f'<span class="paginationLabel">Results <b>1 – 25</b> of <b>{total:,}</b></span>'
            f'<table>{"".join(rows)}</table>')


def _tile(rid: int, title: str, loc: str, date: str) -> str:
    return f"""
    <li class="job-tile job-id-{rid}" data-url="/job/LA-{title}/{rid}/">
      <a class="jobTitle-link fontcolor1" href="/job/LA-{title}/{rid}/">
          {title}
      </a>
      <div id="job-{rid}-desktop-section-location-value">{loc}
      </div>
      <div id="job-{rid}-desktop-section-date-value">{date}
      </div>
    </li>"""


def _classic_client(pages: dict[int, str], seen: list[dict]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/"
        seen.append(dict(request.url.params))
        return httpx.Response(200, text=pages.get(int(request.url.params["startrow"]), ""))
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_classic_table_paginates_to_the_board_total():
    # Two rows a page: the adapter advances startrow by rows actually served,
    # not by an assumed page size, and stops once it reaches the total.
    pages = {
        0: _table_page(3, [_table_row(11, "Engineer", "Austin, TX, US, 78701", "Oct 1, 2026", "/hatci"),
                              _table_row(12, "Analyst", "Bade, Taiwan, TW", "Sep 30, 2026")]),
        2: _table_page(3, [_table_row(13, "Intern", "Berlin, DE", "")]),
    }
    seen: list[dict] = []
    async with _classic_client(pages, seen) as c:
        jobs = await successfactors(c, "Hyundai", {"host": "careers.example.com"})
    assert [s["startrow"] for s in seen] == ["0", "2"]
    assert [j.raw_id for j in jobs] == ["11", "12", "13"]
    first = jobs[0]
    assert first.title == "Engineer & Co"
    assert first.url == ("https://careers.example.com/hatci/job/"
                         "Somewhere-Engineer-&-Co/11/")
    assert first.location == "Austin, TX, US, 78701"
    assert first.posted_at == "2026-10-01T00:00:00+00:00"
    assert first.posted_source == "jobDate"
    assert first.department == "Software"
    assert first.ats == "successfactors"
    assert jobs[2].posted_at is None  # a board with no date says nothing


def test_classic_total_reads_thousands_separators():
    m = adapters.SF_CLASSIC_TOTAL.search(_table_page(1037, []))
    assert m.group(1) == "1,037"


async def test_classic_stops_at_total_even_when_the_last_page_is_full():
    pages = {0: _table_page(2, [_table_row(1, "A", "X", "Oct 1, 2026"),
                                _table_row(2, "B", "Y", "Oct 1, 2026")])}
    seen: list[dict] = []
    async with _classic_client(pages, seen) as c:
        jobs = await successfactors(c, "Co", {"host": "h.example", "query": "engineer"})
    assert len(seen) == 1 and seen[0]["q"] == "engineer"
    assert len(jobs) == 2


async def test_classic_tile_layout():
    page = ('<span id="tile-search-results-label">Showing 1 to 25 of 2 Jobs</span>'
            + _tile(1363949300, "Lead Analyst", "Los Angeles, CA, US, 90038", "Oct 1, 2026")
            + _tile(1406061700, "Account Executive", "Chicago, IL, US", "Sep 30, 2026"))
    async with _classic_client({0: page}, []) as c:
        jobs = await successfactors(c, "Paramount", {"host": "careers.paramount.example"})
    assert [(j.raw_id, j.title, j.location) for j in jobs] == [
        ("1363949300", "Lead Analyst", "Los Angeles, CA, US, 90038"),
        ("1406061700", "Account Executive", "Chicago, IL, US"),
    ]
    assert jobs[1].posted_at == "2026-09-30T00:00:00+00:00"
    assert jobs[0].url == "https://careers.paramount.example/job/LA-Lead Analyst/1363949300/"


async def test_classic_mode_on_a_unify_site_fails_loudly():
    page = '<script src="/platform/js/j2w/min/j2w.searchResultsUnify.min.js"></script>'
    async with _classic_client({0: page}, []) as c:
        with pytest.raises(ValueError, match="site=unify"):
            await successfactors(c, "Altria", {"host": "careers.altria.example"})


def _unify_job(rid: str, title: str, start: str) -> dict:
    return {"response": {"id": rid, "unifiedStandardTitle": title,
                         "urlTitle": title.replace(" ", "-"),
                         "unifiedStandardStart": start,
                         "jobLocationShort": ["Richmond, VA-23230, United States    "]}}


async def test_unify_uses_csrf_sorts_by_date_and_dedupes():
    # Page 1 repeats a row from page 0, as the live board does under an
    # unstable sort. The adapter must keep paging until it has the total.
    pages = [
        [_unify_job("1", "Safety Engineer I", "10/1/26"), _unify_job("2", "Mechanic", "5/12/26")],
        [_unify_job("2", "Mechanic", "5/12/26"), _unify_job("3", "Electrician", "")],
        [_unify_job("4", "Controls Engineer", "3/13/26")],
    ]
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, text="var CSRFToken = \"tok-123\"; currentLocale: 'en_US',",
                headers={"Set-Cookie": "JSESSIONID=abc; Path=/"})
        assert request.url.path == "/services/recruiting/v1/jobs"
        assert request.headers["X-CSRF-Token"] == "tok-123"
        assert "JSESSIONID=abc" in request.headers.get("cookie", "")
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json={"totalJobs": 4,
                                         "jobSearchResult": pages[body["pageNumber"]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        jobs = await successfactors(c, "Altria", {"host": "careers.altria.example",
                                                  "site": "unify"})
    assert [b["pageNumber"] for b in bodies] == [0, 1, 2]
    assert all(b["sortBy"] == "date" and b["locale"] == "en_US" for b in bodies)
    assert [j.raw_id for j in jobs] == ["1", "2", "3", "4"]
    assert jobs[0].url == "https://careers.altria.example/job/Safety-Engineer-I/1-en_US/"
    assert jobs[0].location == "Richmond, VA-23230, United States"
    assert jobs[0].posted_at == "2026-10-01T00:00:00+00:00"
    assert jobs[1].posted_at == "2026-05-12T00:00:00+00:00"  # M/D/YY in en_US
    assert jobs[2].posted_at is None


async def test_unify_sweeps_again_when_a_pass_comes_up_short():
    # The live board reorders even under sortBy=date, so one full pass can
    # skip a posting. A second pass picks it up; the adapter stops as soon as
    # it holds the board's own total.
    passes = [
        [[_unify_job("1", "A", "")], [_unify_job("1", "A", "")], []],
        [[_unify_job("2", "B", "")], [_unify_job("1", "A", "")], []],
    ]
    calls: list[tuple[str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text='var CSRFToken = "t";')
        body = json.loads(request.content)
        n = sum(1 for _, p in calls if p == 0) - (body["pageNumber"] != 0)
        calls.append((body["sortBy"], body["pageNumber"]))
        return httpx.Response(200, json={"totalJobs": 2,
                                         "jobSearchResult": passes[n][body["pageNumber"]]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        jobs = await successfactors(c, "Altria", {"host": "h.example", "site": "unify"})
    assert sorted(j.raw_id for j in jobs) == ["1", "2"]
    assert calls == [("date", 0), ("date", 1), ("date", 2), ("date", 0)]


def test_registry_requires_host_and_a_known_site(tmp_path):
    ok = write_registry(tmp_path / "ok.csv", [
        {"company": "A", "ats": "successfactors", "host": "a.example"},
        {"company": "B", "ats": "successfactors", "host": "b.example", "site": "unify"},
    ])
    assert len(load(ok)) == 2
    for bad in ({"company": "C", "ats": "successfactors"},
                {"company": "D", "ats": "successfactors", "host": "d.example", "site": "rss"}):
        with pytest.raises(RegistryError):
            load(write_registry(tmp_path / "bad.csv", [bad]))
