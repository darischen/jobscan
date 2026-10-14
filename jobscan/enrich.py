"""Fill posted dates from a posting's own page.

Some boards list postings with no date at all: Radancy TalentBrew, Meta and
SuccessFactors. The posting's page still carries one, as schema.org
JobPosting data: `"datePosted"` in a JSON-LD block (Radancy, Meta), or an
`itemprop="datePosted"` attribute (SuccessFactors). Checked on 2026-10-13
against fifteen postings per board; the dates spread from February to that
week, so they are the posting's and not the page's refresh time.

One request per posting is too many for a whole board, 5,300 at
UnitedHealth Group. So only postings in the buckets a scan shows are
fetched, about 630 across the registry that day, and only once: a filled
date is kept by every later scan, and a page that carries none is marked
`detail_none` and not asked again. A failed fetch is left for the next run.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from . import dates
from .adapters import NAVIGATE
from .core import DEFAULT_KEEP
from .store import Store

# Board families whose postings carry a date on the page and none in the
# listing. Not avature (Electronic Arts' pages have none) and not tier B
# (Tesla refuses anything but a browser).
FAMILIES = ("radancy", "meta", "successfactors")
MAX_PER_RUN = 400
CONCURRENCY = 4
MAX_PAGE_BYTES = 4_000_000     # the largest page seen was 0.6 MB (Intuit)
MAX_REDIRECTS = 4
# Second-level labels that are part of a public suffix (acme.co.uk), where
# the site is the last three labels, not two.
_SUFFIX_SECOND = frozenset({"co", "com", "org", "net", "ac", "gov", "edu"})

_POSTED = re.compile(
    r'"datePosted"\s*:\s*"([^"]+)"'
    r'|itemprop="datePosted"\s+content="([^"]+)"'
    r'|content="([^"]+)"\s+itemprop="datePosted"')


def parse_posted(html: str) -> str | None:
    """The page's schema.org datePosted as ISO 8601 UTC, or None."""
    m = _POSTED.search(html)
    if not m:
        return None
    value = next(g for g in m.groups() if g).strip()
    iso = dates.from_iso(value)
    if iso:
        return iso
    try:        # SuccessFactors: "Tue Oct 13 02:00:00 UTC 2026"
        dt = datetime.strptime(value, "%a %b %d %H:%M:%S UTC %Y")
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


def _site(host: str) -> str:
    labels = host.lower().rstrip(".").split(".")
    n = 3 if len(labels) >= 3 and labels[-2] in _SUFFIX_SECOND else 2
    return ".".join(labels[-n:])


def same_site(origin: str, target: str) -> bool:
    """Whether `target` is an https page on the same site as `origin`.

    A posting's link comes from a board, and so does any redirect it
    answers with. Following one anywhere would let a board point this
    fetch at another host, a local address included; within its own site
    (www, a locale path, a trailing slash) is all a posting page needs.
    """
    a, b = httpx.URL(origin), httpx.URL(target)
    return (a.scheme == b.scheme == "https" and bool(a.host) and bool(b.host)
            and "." in b.host and _site(a.host) == _site(b.host))


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    """The page's text: same-site redirects only, at most MAX_PAGE_BYTES."""
    origin = url
    for _ in range(MAX_REDIRECTS + 1):
        if not same_site(origin, url):
            raise ValueError(f"not following {url} from {origin}")
        async with client.stream("GET", url, headers=NAVIGATE,
                                 follow_redirects=False, timeout=30) as r:
            if r.is_redirect:
                url = str(r.url.join(r.headers.get("location", "")))
                continue
            r.raise_for_status()
            body = bytearray()
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) >= MAX_PAGE_BYTES:
                    break
            return bytes(body[:MAX_PAGE_BYTES]).decode(r.encoding or "utf-8",
                                                       "replace")
    raise ValueError(f"too many redirects from {origin}")


async def fill_dates(store: Store, client: httpx.AsyncClient | None = None,
                     buckets: tuple[str, ...] = DEFAULT_KEEP,
                     limit: int = MAX_PER_RUN) -> dict[str, int]:
    """Date the shown, undated postings of FAMILIES from their own pages.

    Returns how many were tried, dated, found to carry no date, and failed.
    Without a client it opens one, and only if there is something to fetch.
    """
    todo = store.undated(FAMILIES, buckets, limit)
    out = {"tried": len(todo), "dated": 0, "no_date": 0, "failed": 0}
    if not todo:
        return out
    if client is None:
        async with httpx.AsyncClient() as own:
            return await fill_dates(store, own, buckets, limit)
    sem = asyncio.Semaphore(CONCURRENCY)

    async def one(job: dict[str, Any]) -> None:
        # Everything about one page stays inside this try: a page that will
        # not load, redirects away, or breaks the parser costs that posting
        # its date and nothing else. It is left undated and tried next run.
        try:
            async with sem:
                html = await _fetch(client, job["url"])
            posted = parse_posted(html)
        except Exception:  # noqa: BLE001
            out["failed"] += 1
            return
        store.set_posted(job["key"], posted,
                         "detail_page" if posted else "detail_none")
        out["dated" if posted else "no_date"] += 1

    await asyncio.gather(*(one(j) for j in todo))
    store.db.commit()
    return out

