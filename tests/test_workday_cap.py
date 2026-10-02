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
