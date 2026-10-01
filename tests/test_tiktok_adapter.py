"""TikTok supplier search API, offline against a fake board.

The live board reported count 4,282 on 2026-10-01 and sweeps at two page
sizes each returned that many unique ids, the same set both times.
"""
from __future__ import annotations

import json

import httpx
import pytest

from jobscan import adapters
from jobscan.adapters import TIKTOK_PAGE, TIKTOK_STEP, _tiktok_created, _tiktok_location, tiktok
from jobscan.runner import is_us_location

# A real id from the board. Its high 32 bits are 1790742635 epoch seconds.
REAL_ID = "7691181055635327237"


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(adapters, "TIKTOK_PAUSE", 0)


def _city(city: str, state: str | None, country: str) -> dict:
    node = {"code": "CN_1", "location_type": 1, "en_name": country, "parent": None}
    if state:
        node = {"code": "ST_1", "location_type": 2, "en_name": state, "parent": node}
    return {"code": "CT_1", "location_type": 3, "en_name": city, "parent": node}


def _post(n: int, **over) -> dict:
    p = {"id": str((1790000000 << 32) + n), "code": f"A{n}", "title": f"Job {n}",
         "job_category": {"id": "1", "en_name": "R&D"},
         "city_info": _city("San Jose", "California", "United States of America")}
    p.update(over)
    return p


def _board(posts: list[dict], count: int | None = None, code: int = 0):
    bodies: list[dict] = []
    seen_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        b = json.loads(request.content)
        bodies.append(b)
        seen_headers.append(request.headers)
        page = posts[b["offset"]:b["offset"] + b["limit"]]
        return httpx.Response(200, json={
            "code": code, "message": "ok" if code == 0 else "params is invalid",
            "data": {"job_post_list": page,
                     "count": len(posts) if count is None else count}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), bodies, seen_headers


async def test_overlapping_windows_page_to_the_reported_count():
    posts = [_post(i) for i in range(1000)]
    client, bodies, _ = _board(posts)
    async with client:
        jobs = await tiktok(client, "TikTok", {})
    assert len(jobs) == 1000
    assert len({j.raw_id for j in jobs}) == 1000
    assert [b["offset"] for b in bodies] == [0, 200, 400, 600]
    assert all(b["limit"] == TIKTOK_PAGE for b in bodies)
    assert TIKTOK_STEP < TIKTOK_PAGE


async def test_a_withdrawn_posting_mid_sweep_loses_nothing():
    """Live churn: a posting already read is withdrawn after the first page,
    so every later row shifts up one. Plain windows skip the row that crosses
    the boundary; overlapping windows still serve it."""
    posts = [_post(i) for i in range(1000)]
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        b = json.loads(request.content)
        calls["n"] += 1
        board = posts if calls["n"] == 1 else posts[:10] + posts[11:]
        page = board[b["offset"]:b["offset"] + b["limit"]]
        return httpx.Response(200, json={"code": 0, "data": {
            "job_post_list": page, "count": len(board)}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        jobs = await tiktok(client, "TikTok", {})
    assert {j.raw_id for j in jobs} == {p["id"] for p in posts}


async def test_stops_when_a_window_reaches_the_count():
    client, bodies, _ = _board([_post(i) for i in range(400)])
    async with client:
        jobs = await tiktok(client, "TikTok", {})
    assert len(jobs) == 400
    assert len(bodies) == 1


async def test_stops_on_an_empty_page_if_the_count_overstates():
    client, bodies, _ = _board([_post(i) for i in range(30)], count=9000)
    async with client:
        jobs = await tiktok(client, "TikTok", {})
    assert len(jobs) == 30
    assert len(bodies) == 2


async def test_sends_the_headers_the_api_requires():
    client, _, headers = _board([_post(1)])
    async with client:
        await tiktok(client, "TikTok", {})
    h = headers[0]
    # without these two the live API answers 400 "invalid request"
    assert h["website-path"] == "tiktok"
    assert h["origin"] == "https://lifeattiktok.com"


async def test_an_error_code_raises_rather_than_returning_nothing():
    client, _, _ = _board([], code=-9000002)
    async with client:
        with pytest.raises(ValueError):
            await tiktok(client, "TikTok", {})


async def test_maps_id_url_date_location_and_department():
    client, _, _ = _board([_post(0, id=REAL_ID, title=" Software Engineer ",
                                 city_info=_city("Seattle", "Washington",
                                                 "United States of America"),
                                 job_category={"en_name": "R&D"})])
    async with client:
        (j,) = await tiktok(client, "TikTok", {})
    assert j.raw_id == REAL_ID
    assert j.url == f"https://lifeattiktok.com/search/{REAL_ID}"
    assert j.title == "Software Engineer"
    assert j.location == "Seattle, Washington, United States of America"
    assert j.department == "R&D"
    assert j.posted_at == "2026-09-30T04:30:35+00:00"
    assert j.posted_source == "id_epoch"
    assert j.ats == "tiktok"


def test_created_time_is_none_for_a_non_numeric_id():
    assert _tiktok_created("abc") is None


@pytest.mark.parametrize("city,want,us", [
    (_city("San Jose", "California", "United States of America"),
     "San Jose, California, United States of America", True),
    (_city("New York", None, "United States of America"),
     "New York, United States of America", True),
    (_city("Singapore", "Singapore", "Singapore"), "Singapore", False),
    (_city("London", "England", "United Kingdom"), "London, England, United Kingdom", False),
    (None, "", True),
])
def test_location_chain(city, want, us):
    loc = _tiktok_location(city)
    assert loc == want
    assert is_us_location(loc) is us
