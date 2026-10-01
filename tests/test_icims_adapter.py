"""iCIMS careers-home adapter, offline against a fake /api/jobs board.

The fake mirrors what AMD, GitHub and Panasonic served on 2026-10-01:
{"totalCount": n, "count": m, "jobs": [{"data": {...}}]}, 1-based `page`,
and `limit` honored up to 100. Panasonic's totalCount (447) exceeds its
`count` (430) because count covers only the default language, so the fake
reproduces that split to pin the sweep to totalCount.
"""
from __future__ import annotations

import httpx
import pytest

from jobscan import registry
from jobscan.adapters import TIER_A, TIER_B, icims
from tests.conftest import write_registry

HOST = "careers.example.com"


def _job(i: int, lang: str = "en-us", **over) -> dict:
    d = {
        "slug": str(1000 + i), "req_id": str(1000 + i), "language": lang,
        "title": f"Software Engineer {i}",
        "full_location": "Austin, Texas", "short_location": "Austin, Texas",
        "location_name": "US,TX,Austin",
        "posted_date": "2026-09-30T17:20:00+0000",
        "create_date": "2026-10-01T00:00:00+0000",
        "categories": [{"name": "Engineering"}],
        "department": "",
        "meta_data": {"canonical_url": f"https://{HOST}/jobs/{1000 + i}?lang={lang}"},
    }
    d.update(over)
    return {"data": d}


def _board(jobs: list[dict], count: int | None = None, log: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == HOST and request.url.path == "/api/jobs"
        page = int(request.url.params["page"])
        limit = int(request.url.params["limit"])
        if log is not None:
            log.append((page, limit))
        chunk = jobs[(page - 1) * limit: page * limit]
        return httpx.Response(200, json={
            "jobs": chunk, "totalCount": len(jobs),
            "count": len(jobs) if count is None else count,
        })
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_paginates_to_total_count_not_default_language_count():
    # 230 en-us + 17 es-mx, like Panasonic: count says 230, totalCount 247
    jobs = [_job(i) for i in range(230)] + [_job(500 + i, "es-mx") for i in range(17)]
    log: list = []
    async with _board(jobs, count=230, log=log) as c:
        out = await icims(c, "Panasonic", {"host": HOST})
    assert len(out) == 247
    assert len({j.raw_id for j in out}) == 247
    assert [p for p, _ in log] == [1, 2, 3]       # stops at total, no empty probe
    assert all(limit == 100 for _, limit in log)


async def test_stops_on_an_empty_page_if_total_overstates():
    jobs = [_job(i) for i in range(5)]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"jobs": jobs if page == 1 else [],
                                         "totalCount": 9, "count": 9})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await icims(c, "AMD", {"host": HOST})
    assert len(out) == 5


async def test_dedupes_a_row_reserved_across_pages():
    page1 = [_job(i) for i in range(100)]
    page2 = [_job(99), _job(100), _job(101)]       # 1099 served twice

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        body = {1: page1, 2: page2}.get(page, [])
        return httpx.Response(200, json={"jobs": body, "totalCount": 102})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await icims(c, "AMD", {"host": HOST})
    ids = [j.raw_id for j in out]
    assert len(ids) == len(set(ids)) == 102


async def test_maps_id_url_date_location_and_department():
    jobs = [
        _job(1),
        _job(2, full_location="San Jose, California; Santa Clara, California",
             short_location="San Jose, California", multipleLocations=True,
             categories=[], department="Silicon"),
        _job(3, posted_date=None, meta_data={}),
    ]
    async with _board(jobs) as c:
        a, b, d = await icims(c, "AMD", {"host": HOST})
    assert a.raw_id == "1001" and a.ats == "icims" and a.company == "AMD"
    assert a.url == f"https://{HOST}/jobs/1001?lang=en-us"
    assert a.posted_at == "2026-09-30T17:20:00+00:00" and a.posted_source == "posted_date"
    assert a.location == "Austin, Texas" and a.department == "Engineering"
    assert b.location == "San Jose, California; Santa Clara, California"
    assert b.department == "Silicon"
    # no canonical_url: fall back to the careers-home job page
    assert d.url == f"https://{HOST}/jobs/1003"
    assert d.posted_at == "2026-10-01T00:00:00+00:00" and d.posted_source == "create_date"


async def test_needs_a_host():
    async with _board([]) as c:
        with pytest.raises(ValueError):
            await icims(c, "AMD", {})


def test_icims_is_tier_a_and_registry_requires_host(tmp_path):
    assert TIER_A["icims"] is icims and "icims" not in TIER_B
    bad = write_registry(tmp_path / "r.csv", [{"company": "X", "ats": "icims"}])
    with pytest.raises(registry.RegistryError, match="icims needs host"):
        registry.load(bad)
    ok = write_registry(tmp_path / "ok.csv",
                        [{"company": "X", "ats": "icims", "host": HOST}])
    assert registry.load(ok)[0]["host"] == HOST
