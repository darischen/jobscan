"""IBM careers search, offline against a fake Elasticsearch front end.

The live board (www-api.ibm.com/search/api/v2, appId=careers) reported 2,044
postings on 2026-10-01 and a full _id-sorted sweep returned exactly that many
unique jobIds. These tests pin the paging contract and the location rewrite
that keeps 'PUNE, IN' from reading as Indiana.
"""
from __future__ import annotations

import json

import httpx
import pytest

from jobscan import adapters
from jobscan.adapters import IBM_PAGE, _ibm_location, ibm
from jobscan.runner import is_us_location


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch):
    monkeypatch.setattr(adapters, "IBM_PAUSE", 0)


def _doc(n: int, **over) -> dict:
    src = {"title": f"Job {n}", "url": f"https://careers.ibm.com/careers/JobDetail?jobId={n}",
           "dcdate": "2026-08-18", "field_keyword_05": "United States",
           "field_keyword_08": "Software Engineering", "field_keyword_19": "Austin, US",
           "field_text_01": n}
    src.update(over)
    return {"_id": f"hash{n}", "_score": 0, "_source": src}


def _board(docs: list[dict], total: int | None = None):
    """Serve `docs` by from/size, recording each request body."""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        b = json.loads(request.content)
        bodies.append(b)
        page = docs[b["from"]:b["from"] + b["size"]]
        t = len(docs) if total is None else total
        return httpx.Response(200, json={"hits": {"total": {"value": t, "relation": "eq"},
                                                  "hits": page}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), bodies


async def test_pages_to_the_reported_total():
    docs = [_doc(100000 + i) for i in range(250)]
    client, bodies = _board(docs)
    async with client:
        jobs = await ibm(client, "IBM", {})
    assert len(jobs) == 250
    assert len({j.raw_id for j in jobs}) == 250
    assert [b["from"] for b in bodies] == [0, 100, 200]
    assert all(b["size"] == IBM_PAGE for b in bodies)


async def test_requests_the_careers_scope_with_a_total_order():
    client, bodies = _board([_doc(1)])
    async with client:
        await ibm(client, "IBM", {})
    b = bodies[0]
    assert b["appId"] == "careers" and b["scopes"] == ["careers2"]
    # an empty query scores every doc 0, so only a unique key gives stable pages
    assert b["sort"] == [{"_id": "asc"}]


async def test_stops_when_a_full_last_page_meets_the_total():
    docs = [_doc(i) for i in range(1, 201)]
    client, bodies = _board(docs)
    async with client:
        jobs = await ibm(client, "IBM", {})
    assert len(jobs) == 200
    assert len(bodies) == 2


async def test_stops_on_an_empty_page_if_the_total_overstates():
    docs = [_doc(i) for i in range(1, 51)]
    client, bodies = _board(docs, total=500)
    async with client:
        jobs = await ibm(client, "IBM", {})
    assert len(jobs) == 50
    assert len(bodies) == 2


async def test_maps_id_url_date_location_and_department():
    client, _ = _board([_doc(129622, title="Client Engineering", dcdate="2026-08-18",
                             field_keyword_05="Switzerland", field_keyword_19="Zurich, CH",
                             field_keyword_08="Sales")])
    async with client:
        (j,) = await ibm(client, "IBM", {})
    assert j.raw_id == "129622"
    assert j.url == "https://careers.ibm.com/careers/JobDetail?jobId=129622"
    assert j.title == "Client Engineering"
    assert j.posted_at == "2026-08-18T00:00:00+00:00"
    assert j.posted_source == "dcdate"
    assert j.location == "Zurich, Switzerland"
    assert j.department == "Sales"
    assert j.ats == "ibm"


@pytest.mark.parametrize("city,country,want,us", [
    ("PUNE, IN", "India", "PUNE, India", False),          # IN is not Indiana
    ("Toronto, CA", "Canada", "Toronto, Canada", False),  # CA is not California
    ("Austin, US", "United States", "Austin, United States", True),
    ("Multiple Cities", "India", "Multiple Cities, India", False),
    ("Multiple Cities", "United States", "Multiple Cities, United States", True),
    ("No City, US", "United States", "United States", True),
])
def test_location_rewrite_survives_the_us_filter(city, country, want, us):
    loc = _ibm_location(city, country)
    assert loc == want
    assert is_us_location(loc) is us
