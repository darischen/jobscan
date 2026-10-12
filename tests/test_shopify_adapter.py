"""Shopify's careers loader (React Router turbo-stream), offline.

Measured 2026-10-12: /careers.data carried 114 published, listed postings.
"""
from __future__ import annotations

import json

import httpx
import pytest

from jobscan.adapters import TIER_A, _shopify_places, _turbo_decode, shopify
from jobscan.runner import is_us_location

ROW = {"host": "www.shopify.com"}
NULL = -5


def _encode(value) -> str:
    """Minimal turbo-stream encoder: strings deduped, objects as key refs."""
    values: list = []
    index: dict = {}

    def put(v) -> int:
        if v is None:
            return NULL
        if isinstance(v, str):
            if ("s", v) in index:
                return index[("s", v)]
            values.append(v)
            index[("s", v)] = len(values) - 1
            return len(values) - 1
        if isinstance(v, (bool, int, float)):
            values.append(v)
            return len(values) - 1
        slot = len(values)
        values.append(None)
        if isinstance(v, list):
            values[slot] = [put(x) for x in v]
        else:
            values[slot] = {f"_{put(k)}": put(x) for k, x in v.items()}
        return slot

    put(value)
    return json.dumps(values) + "\nP1:[]\n"


LOCS = [
    {"id": "h-am", "name": "Americas", "type": "LocationHierarchy", "address": None},
    {"id": "loc-am", "name": "Americas", "externalName": None, "type": "Location",
     "parentLocationId": "h-am",
     "address": {"postalAddress": {"addressCountry": "Americas"}}},
    {"id": "loc-ny", "name": "New York City", "type": "Location", "parentLocationId": "h-am",
     "address": {"postalAddress": {"addressRegion": "New York",
                                   "addressCountry": "United States",
                                   "addressLocality": "New York City"}}},
    {"id": "loc-ca", "name": "Canada ", "externalName": None, "type": "Location",
     "parentLocationId": "h-am",
     "address": {"postalAddress": {"addressRegion": "Ontario", "addressCountry": "Canada",
                                   "addressLocality": "Ottawa "}}},
    {"id": "loc-ie", "name": "Ireland", "externalName": None, "type": "Location",
     "address": {"postalAddress": {"addressCountry": "Ireland"}}},
    {"id": "loc-latam", "name": "LATAM", "type": "Location",
     "address": {"postalAddress": {}}},
]


def _item(n: int, **over) -> dict:
    pid = f"0000000{n}-aaaa-bbbb-cccc-dddddddddddd"
    p = {"id": pid, "title": f" Software Engineer {n} ", "status": "Published",
         "isListed": True, "teamName": "Engineering", "locationName": "Americas",
         "workplaceType": "Remote", "publishedDate": "2026-08-17",
         "updatedAt": "2026-08-26T08:45:55.810Z",
         "externalLink": f"https://www.shopify.com/careers?ashby_jid={pid}",
         "locationIds": {"primaryLocationId": "loc-am", "secondaryLocationIds": ["loc-ca"]}}
    p.update(over)
    job = {"id": f"job-{n}", "customFields": [
        {"title": "Discipline", "isPrivate": False, "value": "Engineering",
         "valueLabel": "Engineering"},
        {"title": "Secret", "isPrivate": True, "value": "x"}],
        "hiringTeam": [{"email": "someone@example.com"}]}
    return {"jobPosting": p, "job": job}


def _payload(items: list[dict]) -> str:
    return _encode({
        "root": {"data": {"site": "x"}},
        "($locale)/careers": {"data": {"url": "https://www.shopify.com/careers",
                                       "jobPostingsWithJobs": items,
                                       "atsLocations": LOCS}},
    })


def _board(text: str, status: int = 200):
    asked: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request)
        return httpx.Response(status, text=text,
                              headers={"content-type": "text/x-script"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), asked


def test_turbo_decode_round_trip_and_specials():
    src = {"a": [1, "x", {"b": None}], "c": "x", "d": True}
    assert _turbo_decode(json.loads(_encode(src).split("\n")[0])) == src
    # -7 (undefined) and -5 (null) both read as None
    assert _turbo_decode([{"_1": -7, "_2": -5}, "u", "n"]) == {"u": None, "n": None}


async def test_maps_fields_and_reads_one_endpoint():
    client, asked = _board(_payload([_item(1)]))
    async with client:
        (j,) = await shopify(client, "Shopify", ROW)
    assert str(asked[0].url) == "https://www.shopify.com/careers.data"
    assert j.title == "Software Engineer 1"
    assert j.raw_id == "00000001-aaaa-bbbb-cccc-dddddddddddd"
    assert j.url.endswith("ashby_jid=00000001-aaaa-bbbb-cccc-dddddddddddd")
    assert j.location == ("Remote - Americas (United States, Canada); "
                          "Remote - Ottawa, Ontario, Canada")
    assert is_us_location(j.location)
    assert j.department == "Engineering"
    assert j.ats == "shopify"
    assert j.posted_at == "2026-08-17T00:00:00+00:00"
    assert j.posted_source == "publishedDate"


async def test_dedupes_and_skips_unlisted():
    items = [_item(1), _item(2), _item(1), _item(3, isListed=False),
             _item(4, status="Archived")]
    client, _ = _board(_payload(items))
    async with client:
        jobs = await shopify(client, "Shopify", ROW)
    assert [j.raw_id[:8] for j in jobs] == ["00000001", "00000002"]


async def test_onsite_location_and_date_fallback():
    it = _item(5, workplaceType="OnSite", publishedDate=None,
               locationIds={"primaryLocationId": "missing"}, locationName="Ireland")
    it["job"]["customFields"] = []
    client, _ = _board(_payload([it]))
    async with client:
        (j,) = await shopify(client, "Shopify", ROW)
    assert j.location == "Ireland"
    assert j.posted_source == "updatedAt"
    assert j.department == "Engineering"   # teamName fallback


async def test_missing_postings_raises():
    client, _ = _board(_encode({"root": {"data": {}}}))
    async with client:
        with pytest.raises(ValueError):
            await shopify(client, "Shopify", ROW)


async def test_http_error_raises():
    client, _ = _board("", status=503)
    async with client:
        with pytest.raises(httpx.HTTPStatusError):
            await shopify(client, "Shopify", ROW)


def test_registered():
    assert TIER_A["shopify"] is shopify


def test_region_without_children_is_left_alone():
    places = _shopify_places(LOCS)
    assert places["loc-latam"] == "LATAM"
    assert places["loc-ie"] == "Ireland"
    assert "h-am" not in places
