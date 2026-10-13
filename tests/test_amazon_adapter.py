"""amazon.jobs, offline against a fake search API.

The search API never reports more than 10,000 hits and refuses offsets past
that, so Amazon read as exactly 10,000 on every scan. On 2026-10-13 the
country facet summed to 22,424, and the USA alone held 13,323: one split by
country is not enough, so a country still at the ceiling splits by category.
"""
from __future__ import annotations

import httpx
import pytest

from jobscan import adapters
from jobscan.adapters import amazon

CEILING = 300


def _job(i: int, country: str, category: str) -> dict:
    return {"id_icims": str(1000000 + i), "title": f"Engineer {i}",
            "job_path": f"/en/jobs/{1000000 + i}/engineer-{i}",
            "normalized_location": f"City, {country}",
            "posted_date": "October 1, 2026", "job_category": category,
            "country_code": country}


def _board(sizes: dict[tuple[str, str], int]):
    """A board holding sizes[(country, category)] postings in each cell.

    Like the real one it caps `hits` at the ceiling, refuses offsets at or
    past it, and returns facet counts only for the facets asked for.
    """
    jobs, asked = [], []
    for (country, category), n in sizes.items():
        jobs += [_job(len(jobs) + k, country, category) for k in range(n)]

    def handler(request: httpx.Request) -> httpx.Response:
        p = request.url.params
        asked.append(dict(p.multi_items()))
        offset, limit = int(p["offset"]), int(p["result_limit"])
        if offset >= CEILING:
            return httpx.Response(200, json={
                "error": "Cannot return more than 10000 results at once",
                "hits": 0, "facets": {}, "jobs": None})
        match = [j for j in jobs
                 if p.get("normalized_country_code[]") in (None, j["country_code"])
                 and p.get("category[]") in (None, j["job_category"])]
        facets = {}
        for name, field in (("normalized_country_code", "country_code"),
                            ("category", "job_category")):
            if name in p.get_list("facets[]"):
                counts: dict[str, int] = {}
                for j in match:
                    counts[j[field]] = counts.get(j[field], 0) + 1
                facets[f"{name}_facet"] = [{k: v} for k, v in counts.items()]
        return httpx.Response(200, json={
            "error": None, "hits": min(len(match), CEILING), "facets": facets,
            "jobs": match[offset:offset + limit]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), asked


@pytest.fixture(autouse=True)
def _small_ceiling(monkeypatch):
    monkeypatch.setattr(adapters, "AMAZON_CEILING", CEILING)


async def test_a_board_under_the_ceiling_is_read_in_one_sweep():
    client, asked = _board({("USA", "Software"): 250})
    row: dict = {}
    async with client:
        jobs = await amazon(client, "Amazon", row)
    assert len(jobs) == 250
    assert row["_board_total"] == 250
    assert not any("normalized_country_code[]" in a for a in asked)


async def test_a_board_at_the_ceiling_is_split_by_country():
    client, _ = _board({("USA", "Software"): 280, ("IND", "Software"): 200,
                        ("JPN", "Software"): 90})
    row: dict = {}
    async with client:
        jobs = await amazon(client, "Amazon", row)
    assert len(jobs) == 570
    assert len({j.raw_id for j in jobs}) == 570
    assert row["_board_total"] == 570


async def test_a_country_still_at_the_ceiling_is_split_by_category():
    client, asked = _board({("USA", "Software"): 280, ("USA", "Operations"): 250,
                            ("USA", "Finance"): 40, ("IND", "Software"): 120})
    row: dict = {}
    async with client:
        jobs = await amazon(client, "Amazon", row)
    assert len(jobs) == 690
    assert len({j.raw_id for j in jobs}) == 690
    assert row["_board_total"] == 690
    # India fits under the ceiling, so it is never split further.
    assert not any(a.get("normalized_country_code[]") == "IND" and "category[]" in a
                   for a in asked)


async def test_a_cell_no_facet_can_split_reports_the_shortfall():
    client, _ = _board({("USA", "Software"): 400, ("IND", "Software"): 50})
    row: dict = {}
    async with client:
        jobs = await amazon(client, "Amazon", row)
    assert len(jobs) == 350
    assert row["_board_total"] == 450


async def test_the_row_query_is_kept_on_every_slice():
    client, asked = _board({("USA", "Software"): 280, ("IND", "Software"): 200})
    async with client:
        await amazon(client, "Amazon", {"query": "intern"})
    assert {a["base_query"] for a in asked} == {"intern"}
