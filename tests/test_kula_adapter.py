"""Kula boards (careers.kula.ai/{account}), offline against a fake API.

10x Genomics reported meta.count 34 on 2026-10-01, one page of 99.
"""
from __future__ import annotations

import httpx
import pytest

from jobscan.adapters import KULA_PAGE, _kula_location, kula
from jobscan.runner import is_us_location

ROW = {"token": "10xgenomics", "host": "careers.kula.ai"}


def _office(location: str | None, city=None, state=None, country=None) -> dict:
    return {"id": 1, "name": "x", "location": location, "city": city,
            "state": state, "country": country, "remote": False}


def _post(n: int, **over) -> dict:
    p = {"id": n, "title": f"Job {n}", "listed": True,
         "launch_at": "2025-12-24T11:41:41.000Z",
         "ats_job": {"ats_department": {"id": 1, "name": "R & D"},
                     "offices": [_office("Pleasanton, California, United States")]}}
    p.update(over)
    return p


def _board(posts: list[dict], per_page: int = KULA_PAGE):
    asked: list[httpx.QueryParams] = []
    pages = max(1, -(-len(posts) // per_page))

    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params
        asked.append(q)
        n = int(q["page"])
        chunk = posts[(n - 1) * per_page:n * per_page]
        return httpx.Response(200, json={
            "data": chunk, "errors": [],
            "meta": {"count": len(posts), "page": n, "items": len(chunk), "pages": pages}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), asked


async def test_pages_to_meta_pages():
    posts = [_post(i) for i in range(1, 251)]
    client, asked = _board(posts)
    async with client:
        jobs = await kula(client, "10x Genomics", ROW)
    assert len(jobs) == 250
    assert len({j.raw_id for j in jobs}) == 250
    assert [q["page"] for q in asked] == ["1", "2", "3"]
    q = asked[0]
    assert q["accountName"] == "10xgenomics"
    assert q["type"] == "ats_job_post.index"
    assert q["items"] == str(KULA_PAGE)


async def test_single_page_board_makes_one_request():
    client, asked = _board([_post(i) for i in range(1, 35)])
    async with client:
        jobs = await kula(client, "10x Genomics", ROW)
    assert len(jobs) == 34
    assert len(asked) == 1


async def test_maps_id_url_date_location_and_department():
    client, _ = _board([_post(51437, title="Senior Scientist- Process Chemistry ")])
    async with client:
        (j,) = await kula(client, "10x Genomics", ROW)
    assert j.raw_id == "51437"
    assert j.url == "https://careers.kula.ai/10xgenomics/51437"
    assert j.title == "Senior Scientist- Process Chemistry"
    assert j.posted_at == "2025-12-24T11:41:41+00:00"
    assert j.posted_source == "launch_at"
    assert j.location == "Pleasanton, California, United States"
    assert j.department == "R & D"
    assert j.ats == "kula"


async def test_api_errors_raise():
    def handler(request):
        return httpx.Response(200, json={"data": [], "errors": ["account not found"],
                                         "meta": {"count": 0, "pages": 0}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await kula(client, "10x Genomics", ROW)


async def test_requires_a_token():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
        with pytest.raises(ValueError):
            await kula(client, "10x Genomics", {})


@pytest.mark.parametrize("offices,want,us", [
    ([_office("Japan", country="Japan")], "Japan", False),
    ([_office("Wyoming, United States", state="Wyoming", country="United States")],
     "Wyoming, United States", True),
    ([_office(None, city="Pleasanton", state="California", country="United States")],
     "Pleasanton, California, United States", True),
    ([_office("United Kingdom"), _office("Massachusetts, United States")],
     "United Kingdom; Massachusetts, United States", True),
    ([], "", True),
])
def test_location(offices, want, us):
    loc = _kula_location(offices)
    assert loc == want
    assert is_us_location(loc) is us
