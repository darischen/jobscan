"""Meta board, offline against a fake metacareers.com.

Measured live on 2026-10-01: the listing is one persisted Relay query,
CareersJobSearchResultsV2DataQuery, returning every posting in a single
response. Its doc_id changes whenever Meta ships the bundle, so the adapter
reads it from the page's direct <script src> bundles on every run, and
reads the LSD token from the page itself. 1,061 postings, matching the
1,061 URLs in /jobsearch/sitemap.xml.
"""
from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from jobscan.adapters import meta
from jobscan.runner import is_us_location

LSD = "AdSua4Of1owZyRAQbqqL_ENNLKY"
BUNDLE_A = "https://static.xx.fbcdn.net/rsrc.php/v4/yl/r/aaa.js"
BUNDLE_B = "https://static.xx.fbcdn.net/rsrc.php/v4iw6G4/yo/l/en_US-j/bbb.js"


def _page(lsd: str | None = LSD) -> str:
    token = f'["LSD",[],{{"token":"{lsd}"}},323],' if lsd else ""
    return (f'<html><head><script src="{BUNDLE_A}" async></script>'
            f'<script src="{BUNDLE_B}" nonce="x"></script></head><body>'
            f'<script type="application/json">{{"define":[{token}'
            '["SprinkleConfig",[],{},2111]]}</script></body></html>')


def _op(name: str, doc: str) -> str:
    return (f'__d("{name}_candidate_portalRelayOperation",[],'
            f'(function(t,n,r,o,a,i){{a.exports="{doc}"}}),null);')


def _bundle(v2: str | None = "27129360303422352") -> str:
    out = _op("CareersJobSearchResultsDataQuery", "27506805582236862")
    if v2:
        out += _op("CareersJobSearchResultsV2DataQuery", v2)
    return out + _op("CareersJobSearchFiltersV3Query", "25103492705924273")


def _job(jid: str, **over) -> dict:
    j = {"id": jid, "title": f"Software Engineer {jid}",
         "locations": ["Menlo Park, CA", "Remote, US"],
         "teams": ["Software Engineering", "Infrastructure"],
         "sub_teams": ["Engineering"]}
    j.update(over)
    return j


def _board(all_jobs: list[dict], featured: list[dict] | None = None, *,
           page: str | None = None, bundles: dict[str, str] | None = None,
           gql: dict | None = None):
    calls: list[tuple[str, str]] = []
    posted: list[dict] = []
    bundles = {BUNDLE_A: "/* no relay ops here */", BUNDLE_B: _bundle()} \
        if bundles is None else bundles

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        calls.append((request.method, url))
        if url.endswith("/jobsearch/"):
            return httpx.Response(200, text=page if page is not None else _page())
        if url in bundles:
            return httpx.Response(200, text=bundles[url])
        if url.endswith("/api/graphql/"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            posted.append({**form, "_lsd_header": request.headers.get("x-fb-lsd")})
            body = gql if gql is not None else {"data": {
                "job_search_with_featured_jobs_v2": {
                    "all_jobs": all_jobs, "featured_jobs": featured or [],
                    "all_jobs_title": "All Jobs"}},
                "extensions": {"is_final": True}}
            return httpx.Response(200, text=json.dumps(body))
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), calls, posted


async def test_discovers_doc_id_and_lsd_then_reads_every_posting():
    client, calls, posted = _board([_job(str(1000 + i)) for i in range(50)])
    async with client:
        jobs = await meta(client, "Meta", {})
    assert len(jobs) == 50
    [form] = posted
    assert form["doc_id"] == "27129360303422352"       # V2 preferred over V1
    assert form["fb_api_req_friendly_name"] == "CareersJobSearchResultsV2DataQuery"
    assert form["lsd"] == LSD and form["_lsd_header"] == LSD
    variables = json.loads(form["variables"])
    assert variables["isLoggedIn"] is False
    assert variables["search_input"]["teams"] == []


async def test_follows_a_rotated_doc_id():
    bundles = {BUNDLE_A: "", BUNDLE_B: _bundle(v2="31415926535897932")}
    client, _, posted = _board([_job("1")], bundles=bundles)
    async with client:
        await meta(client, "Meta", {})
    assert posted[0]["doc_id"] == "31415926535897932"


async def test_stops_fetching_bundles_once_the_doc_id_is_found():
    bundles = {BUNDLE_A: _bundle(), BUNDLE_B: "should not be fetched"}
    client, calls, posted = _board([_job("1")], bundles=bundles)
    async with client:
        await meta(client, "Meta", {})
    assert ("GET", BUNDLE_B) not in calls
    assert posted[0]["doc_id"] == "27129360303422352"


async def test_falls_back_to_the_v1_query_when_v2_is_gone():
    bundles = {BUNDLE_A: "", BUNDLE_B: _bundle(v2=None)}
    gql = {"data": {"job_search_with_featured_jobs": {
        "all_jobs": [_job("7")], "featured_jobs": []}}}
    client, _, posted = _board([], bundles=bundles, gql=gql)
    async with client:
        jobs = await meta(client, "Meta", {})
    assert posted[0]["doc_id"] == "27506805582236862"
    assert posted[0]["fb_api_req_friendly_name"] == "CareersJobSearchResultsDataQuery"
    assert [j.raw_id for j in jobs] == ["7"]


async def test_maps_id_url_location_and_team_and_leaves_date_blank():
    client, _, _ = _board([_job("1050587157626836")])
    async with client:
        [job] = await meta(client, "Meta", {})
    assert job.raw_id == "1050587157626836"
    assert job.ats == "meta"
    assert job.url == "https://www.metacareers.com/profile/job_details/1050587157626836/"
    assert job.location == "Menlo Park, CA; Remote, US"
    assert is_us_location(job.location)
    assert job.department == "Software Engineering, Infrastructure"
    assert job.posted_at is None


async def test_featured_jobs_are_deduped_against_all_jobs():
    all_jobs = [_job("1"), _job("2")]
    client, _, _ = _board(all_jobs, featured=[_job("2"), _job("3")])
    async with client:
        jobs = await meta(client, "Meta", {})
    assert sorted(j.raw_id for j in jobs) == ["1", "2", "3"]
    assert len({j.key for j in jobs}) == 3


async def test_missing_lsd_fails_loudly():
    client, _, _ = _board([_job("1")], page=_page(lsd=None))
    async with client:
        with pytest.raises(ValueError, match="LSD"):
            await meta(client, "Meta", {})


async def test_missing_doc_id_fails_loudly():
    client, _, _ = _board([_job("1")], bundles={BUNDLE_A: "", BUNDLE_B: ""})
    async with client:
        with pytest.raises(ValueError, match="doc_id"):
            await meta(client, "Meta", {})


async def test_graphql_error_fails_rather_than_returning_zero_jobs():
    gql = {"errors": [{"message": "PersistedQueryNotFound"}]}
    client, _, _ = _board([], gql=gql)
    async with client:
        with pytest.raises(ValueError, match="PersistedQueryNotFound"):
            await meta(client, "Meta", {})
