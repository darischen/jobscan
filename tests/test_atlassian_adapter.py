"""Atlassian's own careers endpoint, offline against a fake API.

Measured 2026-10-12: one unpaged list of 347 entries holding 329 unique
iCIMS ids, the same 329 the all-jobs page reports as "329 RESULTS".
"""
from __future__ import annotations

import httpx
import pytest

from jobscan.adapters import TIER_A, _atlassian_date, _atlassian_location, atlassian

ROW = {"host": "www.atlassian.com"}


def _entry(n: int, portal: int = 111, **over) -> dict:
    e = {
        "portalJobPost": {
            "portalId": portal,
            "portalUrl": f"https://careers-americas.icims.com/jobs/{n}/job-{n}/job",
            "id": n,
            "updatedDate": "2026-09-24 03:33 PM",
        },
        "id": n, "portalId": portal, "title": f" Software Engineer {n} ",
        "locations": ["Bengaluru - India -   Bengaluru,  560071 India",
                      "Remote - Remote"],
        "category": "Engineering", "overview": "<p>x</p>",
        "applyUrl": f"https://careers-americas.icims.com/jobs/{n}/job/job?mode=apply",
    }
    e.update(over)
    return e


def _board(payload, status: int = 200):
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(status, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), asked


async def test_dedupes_verbatim_repeats_on_id():
    entries = [_entry(i) for i in range(1, 6)] + [_entry(2), _entry(4), _entry(4)]
    client, asked = _board(entries)
    async with client:
        jobs = await atlassian(client, "Atlassian", ROW)
    assert len(asked) == 1
    assert str(asked[0].url) == "https://www.atlassian.com/endpoint/careers/listings"
    assert [j.raw_id for j in jobs] == ["1", "2", "3", "4", "5"]


async def test_maps_fields():
    client, _ = _board([_entry(25480)])
    async with client:
        (j,) = await atlassian(client, "Atlassian", ROW)
    assert j.title == "Software Engineer 25480"
    assert j.url == "https://www.atlassian.com/company/careers/details/25480"
    assert j.location == "Bengaluru - India - Bengaluru, 560071 India; Remote - Remote"
    assert j.ats == "atlassian"
    assert j.department == "Engineering"
    assert j.posted_at == "2026-09-24T15:33:00+00:00"
    assert j.posted_source == "updatedDate"


async def test_missing_date_and_locations():
    e = _entry(7, locations=[])
    e["portalJobPost"].pop("updatedDate")
    client, _ = _board([e])
    async with client:
        (j,) = await atlassian(client, "Atlassian", ROW)
    assert j.posted_at is None and j.posted_source == ""
    assert j.location == ""


async def test_non_list_payload_raises():
    client, _ = _board({"error": "nope"})
    async with client:
        with pytest.raises(ValueError):
            await atlassian(client, "Atlassian", ROW)


async def test_http_error_raises():
    client, _ = _board([], status=503)
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await atlassian(client, "Atlassian", ROW)


def test_helpers_and_registration():
    assert _atlassian_date("2026-07-01 09:05 AM") == "2026-07-01T09:05:00+00:00"
    assert _atlassian_date("garbage") is None
    assert _atlassian_date(None) is None
    assert _atlassian_location(["A  -  B", "A - B", None, ""]) == "A - B"
    assert TIER_A["atlassian"] is atlassian
