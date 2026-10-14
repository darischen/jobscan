"""Filling posted dates from the posting's own page. Offline.

Radancy, Meta and SuccessFactors listings carry no date, but each posting's
page does: schema.org JSON-LD on the first two, a microdata attribute on
the third. One request per posting is too many for a whole board (5,300 at
UnitedHealth Group) and fine for the postings a scan actually shows (108).
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from jobscan import enrich
from jobscan.core import Job
from jobscan.store import Store

JSON_LD = '<script type="application/ld+json">{"@type":"JobPosting","datePosted":"2026-08-25","title":"x"}</script>'
JSON_LD_TIME = '<script>{"datePosted": "2025-06-10T13:24:53-07:00"}</script>'
MICRO = '<meta itemprop="datePosted" content="Mon Oct 05 00:00:00 UTC 2026">'
MICRO_ISO = '<span content="2026-10-05" itemprop="datePosted">'


@pytest.mark.parametrize("html,want", [
    (JSON_LD, "2026-08-25T00:00:00+00:00"),
    (JSON_LD_TIME, "2025-06-10T20:24:53+00:00"),
    (MICRO_ISO, "2026-10-05T00:00:00+00:00"),
    (MICRO, "2026-10-05T00:00:00+00:00"),
    ("<html>no date here</html>", None),
    ('{"datePosted": "soon"}', None),
])
def test_parse_posted(html, want):
    assert enrich.parse_posted(html) == want


def _job(i: int, ats: str, bucket: str = "unleveled", posted: str | None = None) -> Job:
    j = Job(company="Acme", title=f"Software Engineer {i}", url=f"https://acme.example/job/{i}",
            ats=ats, raw_id=str(i), posted_at=posted)
    j.bucket = bucket
    return j


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "jobs.db"
    st = Store(path)
    st.upsert([
        _job(1, "radancy"),                          # page has JSON-LD
        _job(2, "successfactors"),                   # page has microdata
        _job(3, "radancy"),                          # page has no date
        _job(4, "radancy", bucket="role_miss"),      # not shown: never fetched
        _job(5, "greenhouse"),                       # family not on the list
        _job(6, "radancy", posted="2026-01-01T00:00:00+00:00"),   # already dated
        _job(7, "meta"),                             # page errors
    ], {"Acme"})
    st.close()
    return path


def _client(asked: list[str]) -> httpx.AsyncClient:
    pages = {"1": JSON_LD, "2": MICRO_ISO, "3": "<html></html>"}

    def handler(request: httpx.Request) -> httpx.Response:
        n = request.url.path.rsplit("/", 1)[-1]
        asked.append(n)
        if n == "7":
            return httpx.Response(503)
        return httpx.Response(200, text=pages[n])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _row(db: Path, n: int):
    st = Store(db)
    r = st.db.execute("SELECT posted_at, posted_source FROM jobs WHERE url LIKE ?",
                      (f"%/job/{n}",)).fetchone()
    st.close()
    return (r["posted_at"], r["posted_source"])


async def test_fill_dates_fetches_only_shown_undated_postings_on_listed_boards(db: Path):
    asked: list[str] = []
    st = Store(db)
    async with _client(asked) as c:
        out = await enrich.fill_dates(st, c)
    st.close()
    assert sorted(asked) == ["1", "2", "3", "7"]
    assert out == {"tried": 4, "dated": 2, "no_date": 1, "failed": 1}
    assert _row(db, 1) == ("2026-08-25T00:00:00+00:00", "detail_page")
    assert _row(db, 2) == ("2026-10-05T00:00:00+00:00", "detail_page")
    assert _row(db, 3) == (None, "detail_none")
    assert _row(db, 7) == (None, "")             # a failed fetch is tried again


async def test_a_page_with_no_date_is_not_fetched_twice_but_a_failure_is(db: Path):
    first: list[str] = []
    second: list[str] = []
    st = Store(db)
    async with _client(first) as c:
        await enrich.fill_dates(st, c)
    async with _client(second) as c:
        out = await enrich.fill_dates(st, c)
    st.close()
    assert second == ["7"] and out["tried"] == 1


async def test_the_cap_limits_one_run(db: Path):
    asked: list[str] = []
    st = Store(db)
    async with _client(asked) as c:
        out = await enrich.fill_dates(st, c, limit=2)
    st.close()
    assert len(asked) == 2 and out["tried"] == 2


async def test_a_rescan_keeps_the_filled_date(db: Path):
    st = Store(db)
    async with _client([]) as c:
        await enrich.fill_dates(st, c)
    st.upsert([_job(1, "radancy")], {"Acme"})      # the board still sends no date
    st.close()
    assert _row(db, 1) == ("2026-08-25T00:00:00+00:00", "detail_page")
