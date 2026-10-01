"""Apple board, offline against a fake jobs.apple.com.

Measured live on 2026-10-01: the search page is server rendered and embeds
its results as window.__staticRouterHydrationData, 20 per page, with the
board's own totalRecords. The old POST /api/role/search and /api/csrfToken
both 404, so no token is involved. A full sweep at sort=newest returned
4,516 rows for 4,514 unique ids, the repeats being postings published
mid-sweep, so the adapter must dedupe and stop on the reported total.
"""
from __future__ import annotations

import json

import httpx
import pytest

import jobscan.adapters as adapters
from jobscan.adapters import APPLE_PAGE_SIZE, apple
from jobscan.runner import is_us_location


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(adapters, "APPLE_PAGE_DELAY", 0)
    monkeypatch.setattr(adapters, "APPLE_RETRY_DELAY", 0)


def _job(jid: str, **over) -> dict:
    j = {
        "id": jid,
        "positionId": jid.split("-")[0],
        "postingTitle": f"Software Engineer {jid}",
        "transformedPostingTitle": f"software-engineer-{jid.lower()}",
        "postDateInGMT": "2026-09-30T14:54:15.777Z",
        "postingDate": "Sep 30, 2026",
        "team": {"teamName": "Software and Services", "teamCode": "SFTWR"},
        "locations": [{"name": "Cupertino",
                       "countryName": "United States of America", "level": 5}],
        "type": "REQ",
    }
    j.update(over)
    return j


def _page(results: list[dict], total: int) -> str:
    data = {"loaderData": {"root": {"locale": "en-us"},
                           "search": {"searchResults": results,
                                      "totalRecords": total}}}
    # The page embeds a JSON *string* literal passed to JSON.parse.
    literal = json.dumps(json.dumps(data))
    return ("<html><body><main></main><script nonce=\"x\">"
            f"window.__staticRouterHydrationData = JSON.parse({literal});"
            "</script></body></html>")


def _ids(start: int, n: int = APPLE_PAGE_SIZE) -> list[str]:
    return [f"2006{i:05d}-0836" for i in range(start, start + n)]


def _board(pages: dict[int, list[dict]], total: int, fail: dict[int, int] | None = None):
    asked: list[dict] = []
    fail = dict(fail or {})

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        asked.append(params)
        n = int(params["page"])
        if fail.get(n):
            fail[n] -= 1
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, text=_page(pages.get(n, []), total))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), asked


async def test_paginates_to_the_reported_total():
    pages = {p: [_job(i) for i in _ids((p - 1) * 20)] for p in (1, 2, 3, 4)}
    client, asked = _board(pages, total=60)
    async with client:
        jobs = await apple(client, "Apple", {})
    assert len(jobs) == 60
    assert [int(a["page"]) for a in asked] == [1, 2, 3]
    assert all(a["sort"] == "newest" for a in asked)
    assert all(a["location"] == "united-states-USA" for a in asked)


async def test_a_short_page_ends_the_board():
    pages = {1: [_job(i) for i in _ids(0)], 2: [_job(i) for i in _ids(20, 7)]}
    client, asked = _board(pages, total=9999)
    async with client:
        jobs = await apple(client, "Apple", {})
    assert len(jobs) == 27
    assert len(asked) == 2


async def test_rows_pushed_down_a_page_are_deduped_and_do_not_end_the_scan():
    page2 = [_job(i) for i in [_ids(0)[-1]] + _ids(20, 19)]  # one repeat
    pages = {1: [_job(i) for i in _ids(0)], 2: page2,
             3: [_job(i) for i in _ids(39, 1)]}
    client, asked = _board(pages, total=40)
    async with client:
        jobs = await apple(client, "Apple", {})
    keys = [j.key for j in jobs]
    assert len(keys) == len(set(keys)) == 40
    assert len(asked) == 3


async def test_maps_id_url_date_location_and_team():
    client, _ = _board({1: [_job("200685128-0157")]}, total=1)
    async with client:
        [job] = await apple(client, "Apple", {})
    assert job.raw_id == "200685128-0157"
    assert job.ats == "apple"
    assert job.url == ("https://jobs.apple.com/en-us/details/200685128-0157/"
                       "software-engineer-200685128-0157?team=SFTWR")
    assert job.posted_at == "2026-09-30T14:54:15+00:00"
    assert job.posted_source == "postDateInGMT"
    assert job.location == "Cupertino, United States of America"
    assert is_us_location(job.location)
    assert job.department == "Software and Services"


async def test_shared_position_id_across_sites_keeps_distinct_keys():
    a = _job("200685141-0836")
    b = _job("200685141-0157", locations=[{"name": "Austin",
                                           "countryName": "United States of America"}])
    client, _ = _board({1: [a, b]}, total=2)
    async with client:
        jobs = await apple(client, "Apple", {})
    assert len({j.key for j in jobs}) == 2


async def test_falls_back_to_posting_date_and_distrusts_evergreen_pipe_ids():
    plain = _job("200600001-0836", postDateInGMT=None, postingDate="Jul 04, 2025")
    evergreen = _job("PIPE-114438158", postDateInGMT="2026-10-01T17:38:06.110142504Z",
                     locations=[{"name": "United States",
                                 "countryName": "United States of America", "level": 1}])
    client, _ = _board({1: [plain, evergreen]}, total=2)
    async with client:
        a, b = await apple(client, "Apple", {})
    assert a.posted_at == "2025-07-04T00:00:00+00:00"
    assert a.posted_source == "postingDate"
    assert b.posted_at is None
    assert b.location == "United States"


async def test_retries_a_transient_502_on_one_page():
    pages = {1: [_job(i) for i in _ids(0)], 2: [_job(i) for i in _ids(20, 5)]}
    client, asked = _board(pages, total=25, fail={2: 1})
    async with client:
        jobs = await apple(client, "Apple", {})
    assert len(jobs) == 25
    assert [int(a["page"]) for a in asked] == [1, 2, 2]


async def test_persistent_5xx_raises_rather_than_returning_a_partial_board():
    pages = {1: [_job(i) for i in _ids(0)], 2: [_job(i) for i in _ids(20, 5)]}
    client, _ = _board(pages, total=25, fail={2: 99})
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await apple(client, "Apple", {})


async def test_missing_hydration_data_fails_loudly():
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text="<html>redesigned</html>")))
    async with client:
        with pytest.raises(ValueError):
            await apple(client, "Apple", {})


async def test_site_overrides_the_location_slug():
    client, asked = _board({1: [_job("200600001-0836")]}, total=1)
    async with client:
        await apple(client, "Apple", {"site": "canada-CANC"})
    assert asked[0]["location"] == "canada-CANC"
