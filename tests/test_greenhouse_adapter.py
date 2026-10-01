"""Greenhouse job links, offline against a fake board.

`absolute_url` is whatever the company configured, often a page on its own
site. Wing points it at wing.com/careers/{id}, which returns 404 as of
2026-10-01, and Greenhouse's hosted job URL redirects to the same dead page.
The embed URL ignores that redirect, so `site=embed` opts a row into it.
"""
from __future__ import annotations

import httpx

from jobscan.adapters import greenhouse

JOB = {"id": 8809451002, "title": "Assistant Chief Pilot",
       "absolute_url": "https://wing.com/careers/8809451002?gh_jid=8809451002",
       "location": {"name": "Dallas, TX"}, "first_published": "2026-09-01T00:00:00Z",
       "departments": []}


def _client() -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobs": [JOB]})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_uses_the_company_configured_url_by_default():
    async with _client() as c:
        [job] = await greenhouse(c, "Wing", {"token": "wing"})
    assert job.url == JOB["absolute_url"]


async def test_site_embed_uses_the_greenhouse_embed_url():
    async with _client() as c:
        [job] = await greenhouse(c, "Wing", {"token": "wing", "site": "embed"})
    assert job.url == ("https://job-boards.greenhouse.io/embed/job_app"
                       "?for=wing&token=8809451002")
