"""Workday's 2,000 result cap, offline against a fake board.

Measured on NVIDIA 2026-10-01: the board reports `total: 2000`, and paging
past offset 2,000 keeps returning full pages that repeat earlier postings
(4,019 rows, 1,999 unique). Its facets tell the truth: every posting has
exactly one time type and one job category, and both sum to 2,656. So the
adapter slices a capped board by a facet whose values each fit under the cap.
"""
from __future__ import annotations

import json

import httpx
import pytest

from jobscan.adapters import WORKDAY_CAP, workday

ROW = {"tenant": "acme", "site": "Careers", "wd": "wd1"}


def _posting(i: int) -> dict:
    return {"title": f"Engineer {i}", "externalPath": f"/job/Santa-Clara-CA/Engineer_JR{i}",
            "locationsText": "Santa Clara, CA", "bulletFields": [f"JR{i}"],
            "postedOn": "Posted Today"}


def _board(n: int, family: dict[str, range], sites: dict[str, list[range]] | None = None):
    """A fake capped board of n postings. `family` partitions them by job
    category; `sites` optionally gives a multi-valued location facet."""
    asked: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(body)
        applied = body.get("appliedFacets") or {}
        ids = list(range(n))
        if "jobFamilyGroup" in applied:
            ids = [i for v in applied["jobFamilyGroup"] for i in family[v]]
        if "locations" in applied and sites:
            ids = sorted({i for v in applied["locations"] for r in sites[v] for i in r})
        off, lim = body["offset"], body["limit"]
        # Past the cap Workday wraps around and repeats earlier postings.
        page = [ids[(off + k) % min(len(ids), WORKDAY_CAP)]
                for k in range(lim) if off + k < len(ids) or len(ids) > WORKDAY_CAP]
        facets = [{"facetParameter": "jobFamilyGroup", "descriptor": "Job Category",
                   "values": [{"id": k, "descriptor": k, "count": len(r)}
                              for k, r in family.items()]},
                  {"facetParameter": "timeType", "descriptor": "Time Type",
                   "values": [{"id": "ft", "descriptor": "Full time", "count": n}]}]
        if sites:
            facets.append({"facetParameter": "locations", "descriptor": "Sites",
                           "values": [{"id": k, "descriptor": k,
                                       "count": sum(len(r) for r in rs)}
                                      for k, rs in sites.items()]})
        return httpx.Response(200, json={
            "total": min(len(ids), WORKDAY_CAP) if off == 0 else 0,
            "jobPostings": [_posting(i) for i in page],
            "facets": facets if off == 0 else []})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), asked


async def test_a_capped_board_is_sliced_to_its_real_size():
    family = {"eng": range(0, 1500), "ops": range(1500, 2400), "hr": range(2400, 2656)}
    client, _ = _board(2656, family)
    async with client:
        jobs = await workday(client, "Acme", ROW)
    assert len(jobs) == 2656
    assert len({j.raw_id for j in jobs}) == 2656


async def test_postings_in_several_slices_keep_their_own_ids():
    # A multi-valued facet repeats postings across slices. They must merge on
    # path before the raw_id repair, or the repeats read as id collisions and
    # healthy requisition numbers get rewritten to paths.
    family = {"all": range(0, 2600)}           # one value at the cap: unusable
    sites = {"sc": [range(0, 1400)], "tx": [range(1200, 2600)]}
    client, _ = _board(2600, family, sites)
    async with client:
        jobs = await workday(client, "Acme", ROW)
    assert len(jobs) == 2600
    assert all(j.raw_id.startswith("JR") for j in jobs)


async def test_a_board_under_the_cap_is_not_sliced():
    client, asked = _board(45, {"eng": range(0, 45)})
    async with client:
        jobs = await workday(client, "Acme", ROW)
    assert len(jobs) == 45
    assert all(not (b.get("appliedFacets") or {}) for b in asked)


def _uncapped(n: int):
    """A large board that reports its true total and pages cleanly to the end,
    as Lowe's does (12,603 on 2026-10-12)."""
    asked: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(body)
        off, lim = body["offset"], body["limit"]
        return httpx.Response(200, json={
            "total": n if off == 0 else 0,
            "jobPostings": [_posting(i) for i in range(off, min(off + lim, n))],
            "facets": [{"facetParameter": "locations", "descriptor": "Sites",
                        "values": [{"id": f"s{k}", "descriptor": f"s{k}", "count": n // 50}
                                   for k in range(50)]}] if off == 0 else []})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), asked


async def test_a_large_board_reporting_its_true_total_is_paged_not_sliced():
    # total > 2,000 is a real count; only exactly 2,000 is the cap. Slicing
    # Lowe's by its 1,855 locations cost ~1,900 requests to read a board that
    # pages cleanly in 631.
    client, asked = _uncapped(12603)
    async with client:
        jobs = await workday(client, "Acme", ROW)
    assert len(jobs) == 12603
    assert all(not (b.get("appliedFacets") or {}) for b in asked)


async def test_paging_is_not_cut_off_at_an_arbitrary_offset():
    # The loop used to stop past offset 5,000, so Lowe's read as 5,020.
    client, _ = _uncapped(7000)
    async with client:
        jobs = await workday(client, "Acme", ROW)
    assert len(jobs) == 7000


def _countries():
    """A board whose facets carry locationCountry with opaque ids, as
    Accenture's does: India 35,633, United States 750, ... (2026-10-12)."""
    asked: list[dict] = []
    us = range(0, 75)
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        asked.append(body)
        applied = body.get("appliedFacets") or {}
        ids = list(us) if applied.get("locationCountry") == ["bc33"] else list(range(0, 2000))
        off, lim = body["offset"], body["limit"]
        return httpx.Response(200, json={
            "total": len(ids) if off == 0 else 0,
            "jobPostings": [_posting(i) for i in ids[off:off + lim]],
            "facets": [{"facetParameter": "locationMainGroup", "values": [
                {"facetParameter": "locationCountry", "descriptor": "Country",
                 "values": [{"id": "c4f7", "descriptor": "India", "count": 1925},
                            {"id": "bc33", "descriptor": "United States of America",
                             "count": 75}]}]}] if off == 0 else []})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), asked


async def test_token_restricts_the_board_to_one_facet_value_by_name():
    client, asked = _countries()
    row = {**ROW, "token": "locationCountry=United States of America"}
    async with client:
        jobs = await workday(client, "Acme", row)
    assert len(jobs) == 75
    assert asked[-1]["appliedFacets"] == {"locationCountry": ["bc33"]}


async def test_an_unknown_facet_filter_fails_loudly():
    client, _ = _countries()
    row = {**ROW, "token": "locationCountry=Atlantis"}
    async with client:
        with pytest.raises(ValueError, match="Atlantis"):
            await workday(client, "Acme", row)


def test_estimate_with_no_agreeing_facets_takes_the_middle_sum_not_the_largest():
    """Citi, 2026-10-13: no two facets summed alike, and the tie-break took
    the largest, 4,993 from `locations`, which counts a multi-location
    posting once per location. The partitioning facets (country, job
    family, worker type) said ~4,210, and 4,207 had been read, so verify
    reported a complete board as 790 short."""
    from jobscan.adapters import _workday_estimate

    def facet(name, total):
        return {"facetParameter": name, "values": [{"id": "x", "descriptor": "x",
                                                    "count": total}]}
    facets = [facet("Country", 4219), facet("State", 3600), facet("family", 4179),
              facet("workerSubType", 4209), facet("timeType", 3957),
              facet("locations", 4993)]
    assert _workday_estimate(facets, {}) == 4209
    # Two facets agreeing still wins, as before.
    assert _workday_estimate(facets + [facet("family2", 4179)], {}) == 4179
