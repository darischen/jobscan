"""verify.py's link spot-check, offline against a fake server.

On 2026-10-01 job-boards.greenhouse.io throttled the IP after a burst and
answered 503 to every company. The check counted each one as a dead link, so
a rate limit read as broken postings. Dead now means the page is gone (404,
410); a server that would not answer is reported as unreachable instead.
"""
from __future__ import annotations

import httpx

from jobscan.core import Job
from tools.verify import sample_live


def _jobs(*paths: str) -> list[Job]:
    return [Job(company="Acme", title="Engineer", url=f"https://jobs.test/{p}",
                location="", ats="greenhouse", raw_id=p) for p in paths]


def _client(statuses: dict[str, list[int | type[Exception]]]) -> httpx.AsyncClient:
    """Each path answers with its listed statuses in order, last one repeating."""
    def handler(request: httpx.Request) -> httpx.Response:
        queue = statuses[request.url.path.strip("/")]
        nxt = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(nxt, type):
            raise nxt("simulated", request=request)
        return httpx.Response(nxt)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_live_and_dead_are_counted_apart():
    async with _client({"a": [200], "b": [404], "c": [410]}) as c:
        r = await sample_live(c, _jobs("a", "b", "c"), 3, retry_delay=0)
    assert (r.ok, r.dead, r.unreachable, r.tried) == (1, 2, 0, 3)


async def test_throttling_is_unreachable_not_dead():
    async with _client({"a": [503], "b": [429], "c": [403]}) as c:
        r = await sample_live(c, _jobs("a", "b", "c"), 3, retry_delay=0)
    assert (r.ok, r.dead, r.unreachable) == (0, 0, 3)


async def test_network_errors_are_unreachable():
    async with _client({"a": [httpx.ReadTimeout], "b": [httpx.ConnectError]}) as c:
        r = await sample_live(c, _jobs("a", "b"), 2, retry_delay=0)
    assert (r.ok, r.dead, r.unreachable) == (0, 0, 2)


async def test_a_transient_failure_gets_one_retry():
    async with _client({"a": [503, 200], "b": [httpx.ReadTimeout, 200]}) as c:
        r = await sample_live(c, _jobs("a", "b"), 2, retry_delay=0)
    assert (r.ok, r.dead, r.unreachable) == (2, 0, 0)


async def test_a_dead_link_is_not_retried():
    async with _client({"a": [404, 200]}) as c:
        r = await sample_live(c, _jobs("a"), 1, retry_delay=0)
    assert (r.ok, r.dead) == (0, 1)
