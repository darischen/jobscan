"""Tier A adapters. Pure HTTP JSON, safe to run in parallel.

Contract: every adapter is
    async def fn(client, company: str, row: dict) -> list[Job]
and raises on hard failure. The runner catches and records.
"""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from typing import Any

import httpx

from ..core import Job
from .. import dates

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    # Named explicitly rather than left to httpx's default, which advertises
    # zstd and brotli. On httpx 0.28 / CPython 3.14 a zstd response dies with
    # "cannot use a decompressobj multiple times", and amazon.jobs serves
    # exactly that. Naming the two encodings every board supports sidesteps
    # the decoder bug without giving up compression.
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


# What a browser sends when a person opens a job link. metacareers.com
# answers 400 without the Sec-Fetch set and 200 with it, so every Meta link
# read as dead. HEADERS asks for JSON, which is right for board APIs and
# wrong for a posting's public page.
NAVIGATE = {
    **HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _board_total(row: dict[str, Any], n: Any) -> None:
    """Record the board's own posting count on the row, for tools/verify.py.

    Most adapters already read a total to know when to stop paging, and then
    threw it away. Every truncation found in 2026-10 (Google at page 27,
    Lowe's and JPMorgan at offset 5,000, Apple on an empty page) was a count
    that disagreed with a total the adapter was holding. Missing or zero
    counts are ignored: single-list boards (lever, ashby) have none.
    """
    try:
        n = int(str(n).replace(",", "")) if n is not None else 0
    except ValueError:
        return
    if n > 0:
        row["_board_total"] = n

# -------------------------------------------------------------------- amazon
# www.amazon.jobs exposes an open JSON search layer over their iCIMS
# backend. No auth. account.amazon.jobs is the application portal and does
# require a login, but nothing here touches it.
AMAZON_PAGE = 100          # server rejects result_limit > 100
AMAZON_CEILING = 10_000    # hits saturates here and offsets past it are refused
# Filters that split a search at the ceiling, in order. Both partition the
# board: on 2026-10-13 each facet's counts summed to the same 22,424, of which
# the USA held 13,323, so a country can itself need the second split.
AMAZON_SLICES = ("normalized_country_code", "category")

_AMZ_DATE = re.compile(r"([A-Z][a-z]+)\s+(\d{1,2}),\s+(\d{4})")
_AMZ_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}


def _amazon_date(value: str | None) -> str | None:
    """Amazon sends 'July 27, 2026'. Date only, no clock time."""
    if not isinstance(value, str):
        return None
    m = _AMZ_DATE.search(value)
    if not m:
        return None
    mon = _AMZ_MONTHS.get(m.group(1))
    if not mon:
        return None
    return dates.from_iso(f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}")


async def amazon(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    found = await _amazon_sweep(c, row.get("query", ""), {}, row)
    out = []
    for j in found.values():
        posted, src = dates.pick(
            ("posted_date", dates.from_iso(j.get("posted_date"))),
            ("posted_date", _amazon_date(j.get("posted_date"))),
        )
        path = j.get("job_path") or ""
        out.append(Job(
            company=company,
            title=j.get("title", ""),
            url=f"https://www.amazon.jobs{path}",
            location=(j.get("normalized_location")
                      or j.get("location") or ""),
            ats="amazon",
            posted_at=posted,
            posted_source=src,
            raw_id=str(j.get("id_icims") or j.get("id") or path),
            department=j.get("job_category") or j.get("business_category") or "",
        ))
    return out


async def _amazon_sweep(c: httpx.AsyncClient, query: str,
                        applied: dict[str, str],
                        row: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Every posting matching `applied`, keyed by id.

    A search at the ceiling is re-read one facet value at a time, and a
    value still at the ceiling by the next facet. With no facet left it is
    read to the ceiling; the board total recorded at the top is the facet
    sum, so tools/verify.py reports the shortfall.
    """
    url = "https://www.amazon.jobs/en/search.json"
    facet = (AMAZON_SLICES[len(applied)]
             if len(applied) < len(AMAZON_SLICES) else None)
    found: dict[str, dict[str, Any]] = {}
    offset, hits = 0, None
    while True:
        params = [("base_query", query), ("offset", offset),
                  ("result_limit", AMAZON_PAGE), ("sort", "recent")]
        params += [(f"{k}[]", v) for k, v in applied.items()]
        if facet and offset == 0:
            params.append(("facets[]", facet))
        # No try/except here. Every other adapter lets failures reach
        # runner._one, which retries once and then records the error. Catching
        # and breaking turned a hard failure into a successful scan of zero
        # jobs, so a decoder bug read as "Amazon has no openings" for weeks.
        r = await c.get(url, headers=HEADERS, params=params)
        r.raise_for_status()
        d = r.json()
        if d.get("error"):
            raise ValueError(str(d["error"])[:120])
        if hits is None:
            hits = d.get("hits") or 0
            counts = {name: n
                      for value in (d.get("facets") or {}).get(f"{facet}_facet") or []
                      for name, n in value.items()}
            if hits >= AMAZON_CEILING and counts:
                if row is not None:
                    _board_total(row, sum(counts.values()))
                for name in counts:
                    found.update(await _amazon_sweep(
                        c, query, {**applied, facet: name}))
                return found
            if row is not None:
                _board_total(row, hits)
        jobs = d.get("jobs") or []
        for j in jobs:
            found[str(j.get("id_icims") or j.get("id") or j.get("job_path"))] = j
        offset += AMAZON_PAGE
        if not jobs or offset >= min(hits, AMAZON_CEILING):
            return found

# -------------------------------------------------------------------- google
# Google self-hosts. There is no REST endpoint. The results page is server
# rendered and embeds its job records in an AF_initDataCallback blob, which
# is Google's standard server-to-client data channel. That is still plain
# HTTP, so this stays Tier A: no browser, no model, deterministic parse.
_GOOG_BLOB = re.compile(r"AF_initDataCallback\((\{.*?\})\);", re.S)
_GOOG_DATA = re.compile(r"data:\s*(\[.*?\])\s*,\s*sideChannel", re.S)
_GOOG_SLUG = re.compile(r"[^a-z0-9]+")
# The board has worded its total two ways: "3,617 jobs matched" until mid
# 2026, then "Showing 1 to 20 of 3414 rows". Missing it is silent, because
# the loop still stops on a short page, so both wordings are kept.
_GOOG_TOTAL = re.compile(r"([\d,]+)\s+jobs?\s+matched|of\s+([\d,]+)\s+rows", re.I)
GOOGLE_PAGE_SIZE = 20
# Safety net only. The board reports its own total and the loop stops on an
# empty page, so this bound exists so a markup change cannot spin forever.
# 400 pages is 8,000 records, well past the ~3,600 the board carries.
GOOGLE_MAX_PAGES = 400


def _goog_records(html: str) -> list:
    """Pull the job record arrays out of the embedded blob."""
    out: list = []
    for blob in _GOOG_BLOB.findall(html):
        m = _GOOG_DATA.search(blob)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue

        def walk(x):
            if isinstance(x, list):
                if (len(x) > 7 and isinstance(x[0], str) and x[0].isdigit()
                        and isinstance(x[1], str) and x[1].strip()):
                    out.append(x)
                    return
                for i in x:
                    walk(i)
        walk(data)
    return out


async def google(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    base = "https://www.google.com/about/careers/applications/jobs/results"
    query = row.get("query", "")
    # Alphabet subsidiaries (DeepMind, Waymo, Wing, YouTube) post to the same
    # board behind a `company` filter. Without it a subsidiary row returns the
    # entire Google board with every posting relabelled as the subsidiary, so
    # `token` carries the org name the board expects.
    org = row.get("token", "")
    # Optional server-side location filter. The board accepts "United States"
    # and normalizes "US" to the same 2,034-row set. Left blank the adapter
    # pulls every country and runner.is_us_location decides, which keeps that
    # judgement in one place rather than trusting Google's geo classification.
    where = row.get("site", "")
    out: list[Job] = []
    seen: set[str] = set()
    total: int | None = None
    # The board paginates to the end: at 20 records a page, page 181 returns
    # the final 17 of 3,617 and page 200 returns nothing. An earlier
    # range(1, 60) capped this at 1,180 and silently dropped ~2,400 postings,
    # which the four query-sliced registry rows existed to work around. Trust
    # the board's own total instead of a guessed page count.
    for page in range(1, GOOGLE_MAX_PAGES):
        r = await c.get(base,
                        params={"page": page,
                                **({"q": query} if query else {}),
                                **({"company": org} if org else {}),
                                **({"location": where} if where else {})},
                        headers={**HEADERS, "Accept": "text/html"})
        r.raise_for_status()
        if total is None:
            m = _GOOG_TOTAL.search(r.text)
            if m:
                total = int((m.group(1) or m.group(2)).replace(",", ""))
        records = _goog_records(r.text)
        fresh = 0
        for j in records:
            jid = j[0]
            if jid in seen:
                continue
            seen.add(jid)
            fresh += 1
            title = j[1].strip()
            slug = _GOOG_SLUG.sub("-", title.lower()).strip("-")
            # index 9 holds nested location arrays, index 12 the created
            # timestamp as [seconds, nanos], index 7 the operating company
            loc = ""
            try:
                loc = j[9][0][0]
            except (IndexError, TypeError):
                pass
            posted = None
            try:
                posted = dates.from_epoch_ms(int(j[12][0]) * 1000)
            except (IndexError, TypeError, ValueError):
                pass
            out.append(Job(
                company=company,
                title=title,
                url=f"{base}/{jid}-{slug}",
                location=loc,
                ats="google",
                posted_at=posted,
                posted_source="createdAt" if posted else "",
                raw_id=jid,
                department=j[7] if len(j) > 7 and isinstance(j[7], str) else "",
            ))
        # A short or empty page is the end of the board. The total is a second
        # stop condition for the case where the last page happens to be full.
        # Count records served, not new ids: the board reorders between
        # requests, so a full page can repeat a posting from the page before.
        # Stopping on fresh < 20 ended the full board at 539 of ~3,400.
        # A full page with nothing new means the board is replaying itself.
        if len(records) < GOOGLE_PAGE_SIZE or fresh == 0:
            break
        if total is not None and len(seen) >= total:
            break
    _board_total(row, total)
    return out

# ---------------------------------------------------------------- greenhouse
async def greenhouse(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    token = row["token"]
    r = await c.get(
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        params={"content": "true"}, headers=HEADERS,
    )
    r.raise_for_status()
    # `absolute_url` is whatever the company configured, usually a page on its
    # own site. When that site drops the route (Wing, 2026-10) the hosted job
    # URL redirects to the same dead page, but the embed URL never redirects.
    # `site=embed` opts a row into it.
    embed = row.get("site") == "embed"
    _board_total(row, (r.json().get("meta") or {}).get("total"))
    out = []
    for j in r.json().get("jobs", []):
        posted, src = dates.pick(
            ("first_published", dates.from_iso(j.get("first_published"))),
            ("updated_at", dates.from_iso(j.get("updated_at"))),
        )
        url = j.get("absolute_url", "")
        if embed and j.get("id"):
            url = (f"https://job-boards.greenhouse.io/embed/job_app"
                   f"?for={token}&token={j['id']}")
        out.append(Job(
            company=company,
            title=j.get("title", ""),
            url=url,
            location=(j.get("location") or {}).get("name", ""),
            ats="greenhouse",
            posted_at=posted,
            posted_source=src,
            raw_id=str(j.get("id", "")),
            department=", ".join(d.get("name", "") for d in j.get("departments", [])),
        ))
    return out


# --------------------------------------------------------------------- lever
async def lever(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    token = row["token"]
    r = await c.get(f"https://api.lever.co/v0/postings/{token}",
                    params={"mode": "json"}, headers=HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json():
        cats = j.get("categories") or {}
        posted, src = dates.pick(("createdAt", dates.from_epoch_ms(j.get("createdAt"))))
        out.append(Job(
            company=company,
            title=j.get("text", ""),
            url=j.get("hostedUrl", ""),
            location=cats.get("location", ""),
            ats="lever",
            posted_at=posted,
            posted_source=src,
            raw_id=j.get("id", ""),
            department=cats.get("team", "") or cats.get("department", ""),
        ))
    return out


# --------------------------------------------------------------------- ashby
async def ashby(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    token = row["token"]
    r = await c.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}",
                    headers=HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        posted, src = dates.pick(("publishedAt", dates.from_iso(j.get("publishedAt"))))
        out.append(Job(
            company=company,
            title=j.get("title", ""),
            url=j.get("jobUrl", ""),
            location=j.get("location", ""),
            ats="ashby",
            posted_at=posted,
            posted_source=src,
            raw_id=j.get("id", ""),
            department=j.get("department", "") or j.get("team", ""),
        ))
    return out


# ----------------------------------------------------------- smartrecruiters
async def smartrecruiters(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    token = row["token"]
    out, offset = [], 0
    while True:
        r = await c.get(
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
            params={"limit": 100, "offset": offset}, headers=HEADERS,
        )
        r.raise_for_status()
        d = r.json()
        content = d.get("content", [])
        for j in content:
            loc = j.get("location") or {}
            city = ", ".join(x for x in (loc.get("city"), loc.get("region"),
                                         loc.get("country")) if x)
            posted, src = dates.pick(
                ("releasedDate", dates.from_iso(j.get("releasedDate"))),
                ("createdOn", dates.from_iso(j.get("createdOn"))),
            )
            out.append(Job(
                company=company,
                title=j.get("name", ""),
                url=f"https://jobs.smartrecruiters.com/{token}/{j.get('id','')}",
                location=city,
                ats="smartrecruiters",
                posted_at=posted,
                posted_source=src,
                raw_id=str(j.get("id", "")),
                department=(j.get("department") or {}).get("label", ""),
            ))
        offset += 100
        _board_total(row, d.get("totalFound"))
        if not content or offset >= d.get("totalFound", 0):
            return out


# ------------------------------------------------------------------- workday
def _workday_ids(jobs: list[Job], paths: list[str]) -> list[Job]:
    """Repair raw_id where bulletFields turned out not to identify anything.

    bulletFields is a tenant-configured *display* field, the bullets shown on
    a result card. Most tenants put the requisition number there, which is why
    it worked: 24 of 26 boards surveyed return values like JR2022322 or
    R170608, unique per posting. Two do not. Intel shows the badge
    "Spotlight Job" for 31 postings, and Moderna shows a city.

    Because Job.key hashes raw_id, every colliding posting produced the same
    key, so store.upsert inserted one and updated it with the rest. Intel lost
    30 of 640 requisitions on every scan and Moderna 11 of 186, silently.

    externalPath is unique per posting (verified 640/640 on Intel), so it is
    the fallback. Only ids that actually collide are rewritten, which leaves
    the 24 healthy boards' keys untouched and avoids re-keying their stored
    history for a bug they never had.
    """
    counts = Counter(j.raw_id for j in jobs)
    for j, path in zip(jobs, paths):
        if counts[j.raw_id] > 1 or not j.raw_id:
            j.raw_id = path
    return jobs


def _workday_path_location(external_path: str) -> str:
    """Location recovered from the requisition path: /job/US-CA-Santa-Clara/...

    A last resort. The convention varies by tenant, so the result is a rough
    de-hyphenation rather than structured fields: NVIDIA writes
    country-state-city (US-CA-Santa-Clara), Amcor writes facility-city-state
    (AF-Batavia-IL), Warner Bros writes state-city (NY-New-York), and Accenture
    writes a bare site name (Krakow-High-5ive-Development). Handing the whole
    string to is_us_location, which looks for a US token anywhere, copes with
    all four without needing to know which is which.
    """
    parts = external_path.split("/")
    if len(parts) <= 2 or parts[1] != "job":
        return ""
    seg = parts[2].split("-")
    if len(seg) >= 2:
        return f"{seg[0]}, {seg[1]}, {' '.join(seg[2:])}"
    return parts[2]


def _workday_location(locations_text: str, external_path: str,
                      bullets: list[Any] | None = None) -> str:
    """Best available location for a posting, in descending order of trust.

    `locationsText` is authoritative when it names a place, but it has two
    failure modes. It collapses to the summary "N Locations" for multi-site
    reqs, and some tenants send nothing at all: every one of Accenture's 2,000
    postings arrives with locationsText None. A blank location passes the US
    filter by design (a hidden field is not evidence of a foreign one), so
    those reqs were entering results regardless of country, London and Madrid
    and Krakow included.

    bulletFields is the fallback because it is still the board's own text, and
    tenants that omit locationsText tend to put the location there instead
    (Accenture sends ["R00282385", "Krakow, High 5ive Development"]). The URL
    path is the last resort. bulletFields[0] is skipped: it is the requisition
    number on most tenants, and _workday_ids already deals with it.
    """
    loc = (locations_text or "").strip()
    if loc and not re.match(r"^\d+\s+locations?$", loc, re.IGNORECASE):
        return loc
    if not loc:
        for b in (bullets or [])[1:]:
            text = str(b or "").strip()
            # A requisition number is not a location. Require a letter and
            # reject anything that looks like an id.
            if len(text) > 2 and re.search(r"[A-Za-z]{3}", text) \
                    and not re.fullmatch(r"[A-Z]{0,3}[-_ ]?\d[\w-]*", text):
                return text
    return _workday_path_location(external_path)


async def workday(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    tenant, site = row.get("tenant", ""), row.get("site", "")
    wd = row.get("wd") or "wd1"
    if not tenant or not site:
        raise ValueError("workday rows need tenant and site columns")
    # Two host shapes in the wild. The common one puts the tenant in the
    # subdomain, {tenant}.wd1.myworkdayjobs.com. Shared-cluster tenants
    # instead sit behind wd1.myworkdaysite.com with the tenant only in the
    # path, which is how Wells Fargo serves. An explicit `host` selects the
    # second; leaving it blank keeps the first, so existing rows are unchanged.
    if row.get("host"):
        base = f"https://{row['host']}"
        public = f"{base}/recruiting/{tenant}/{site}"
    else:
        base = f"https://{tenant}.{wd}.myworkdayjobs.com"
        public = f"{base}/{site}"
    url = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    applied = await _workday_filter(c, url, row.get("token", ""))
    found = await _workday_sweep(c, url, row.get("query", ""), applied, depth=0,
                                 row=row)
    out: list[Job] = []
    paths: list[str] = []   # parallel to `out`, for the raw_id repair below
    for path, j in found.items():
        posted, src = dates.pick(
            ("startDate", dates.from_iso(j.get("startDate"))),
            ("postedOn", dates.from_workday_relative(j.get("postedOn"))),
        )
        out.append(Job(
            company=company,
            title=j.get("title", ""),
            url=f"{public}{path}",
            location=_workday_location(j.get("locationsText", ""), path,
                                       j.get("bulletFields")),
            ats="workday",
            posted_at=posted,
            posted_source=src,
            raw_id=j.get("bulletFields", [path])[0] if j.get("bulletFields") else path,
        ))
        paths.append(path)
    return _workday_ids(out, paths)


# Workday stops counting at 2,000. On NVIDIA (2026-10-01) the board reported
# total 2000 while its facets summed to 2,656, and paging past offset 2,000
# kept returning full pages of repeats: 4,019 rows, 1,999 unique. A board at
# the cap is therefore sliced by a facet whose values each fit under it, and
# the slices merged on externalPath. Accenture's registry note asked for query
# rows to do this by hand; facets need no upkeep and cannot miss a posting
# whose title matches no keyword.
#
# The cap is per tenant and shows as a total of exactly 2,000. A total above
# it is a real count: Lowe's reports 12,603 and pages cleanly to the end with
# no repeats. Slicing it instead (by its 1,855 locations) cost ~1,900
# requests where paging costs 631, and the old `offset > 5000` stop had read
# it as 5,020 for months.
WORKDAY_CAP = 2000
WORKDAY_PAGE = 20
WORKDAY_MAX_OFFSET = 100_000   # last-resort bound; the total and a page with
                               # nothing new stop the loop long before this


async def _workday_sweep(c: httpx.AsyncClient, url: str, query: str,
                         applied: dict[str, list[str]],
                         depth: int,
                         row: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Every posting under `applied`, keyed by externalPath.

    Keying on the path merges a posting that appears in several slices, which
    a multi-valued facet such as locations produces. That has to happen here,
    before _workday_ids: left in, the repeats would read as id collisions and
    healthy requisition numbers would be rewritten to paths.
    """
    found: dict[str, dict[str, Any]] = {}
    offset, total, facets = 0, None, []
    while True:  # Workday reports `total` and facets only on the first page
        r = await c.post(
            url,
            headers={**HEADERS, "Content-Type": "application/json"},
            json={"appliedFacets": applied, "limit": WORKDAY_PAGE,
                  "offset": offset, "searchText": query},
        )
        r.raise_for_status()
        d = r.json()
        posts = d.get("jobPostings", [])
        if total is None:
            total = d.get("total") or 0
            facets = d.get("facets") or []
            if row is not None:
                # At the cap the reported total is a lie; the facets are not.
                _board_total(row, _workday_estimate(facets, applied)
                             if total == WORKDAY_CAP else total)
            if total == WORKDAY_CAP and depth < 2:
                plan = _workday_slices(facets, applied)
                if plan:
                    param, values = plan
                    for v in values:
                        found.update(await _workday_sweep(
                            c, url, query, {**applied, param: [v]}, depth + 1))
                    return found
        fresh = 0
        for j in posts:
            path = j.get("externalPath", "")
            if not path or not (j.get("title") or "").strip():
                continue  # Workday occasionally emits a titleless or pathless stub
            if path not in found:
                found[path] = j
                fresh += 1
        offset += WORKDAY_PAGE
        # offset >= total also stops a still-capped slice at the cap rather
        # than letting it page on through repeats. A full page with nothing
        # new means the board is replaying itself.
        if (len(posts) < WORKDAY_PAGE or offset >= total
                or (posts and not fresh) or offset > WORKDAY_MAX_OFFSET):
            return found


async def _workday_filter(c: httpx.AsyncClient, url: str,
                          spec: str) -> dict[str, list[str]]:
    """Resolve a row's optional `token` facet filter to appliedFacets.

    `token` is unused by workday rows, so it carries "<facet>=<value name>",
    e.g. "locationCountry=United States of America". Facet value ids are
    opaque per tenant, so the value is matched on its display name. Accenture
    (2026-10-12) holds ~42,100 postings, 35,633 of them in India and 750 in
    the US; the runner keeps only US postings, so sweeping the rest is ~2,100
    requests spent on rows it throws away. Same idea as the google adapter's
    opt-in location filter. An unmatched name raises: silently dropping the
    filter would return the whole board under a row that promises a slice.
    """
    if not spec:
        return {}
    param, _, name = spec.partition("=")
    if not param or not name:
        raise ValueError(f"workday token filter must be facet=value, got {spec!r}")
    r = await c.post(url, headers={**HEADERS, "Content-Type": "application/json"},
                     json={"appliedFacets": {}, "limit": WORKDAY_PAGE, "offset": 0,
                           "searchText": ""})
    r.raise_for_status()
    for f in _workday_facets(r.json().get("facets") or []):
        if f["facetParameter"] != param:
            continue
        for v in f["values"]:
            if (v.get("descriptor") or "").strip().lower() == name.strip().lower():
                return {param: [v["id"]]}
    raise ValueError(f"workday facet filter {spec!r}: no {param} value named {name!r}")


def _workday_facets(facets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten facet groups: locationMainGroup nests its real facets."""
    flat = []
    for f in facets:
        vals = f.get("values") or []
        if vals and all("facetParameter" in v for v in vals):
            flat.extend(_workday_facets(vals))
        elif vals and f.get("facetParameter"):
            flat.append(f)
    return flat


def _workday_estimate(facets: list[dict[str, Any]],
                      applied: dict[str, list[str]]) -> int:
    """A capped board's real size: the most common facet sum (see below).

    When no two facets agree, the middle sum. Facets that skip postings
    (State) sum low and facets that list a posting once per location sum
    high, so the ends of the range are the wrong ones to trust: Citi's
    `locations` said 4,993 against ~4,210 from country and job family.
    """
    sums = Counter(sum(v.get("count", 0) for v in f["values"])
                   for f in _workday_facets(facets)
                   if f["facetParameter"] not in applied)
    if not sums:
        return 0
    common, n = max(sums.items(), key=lambda kv: (kv[1], kv[0]))
    if n > 1:
        return common
    ordered = sorted(sums)
    return ordered[len(ordered) // 2]


def _workday_slices(facets: list[dict[str, Any]],
                    applied: dict[str, list[str]]) -> tuple[str, list[str]] | None:
    """Pick the facet to slice a capped board by, or None.

    A facet only covers the board if its counts add up to the board's real
    size. Single-valued facets (time type, job category) all sum to exactly
    that size, so the most common sum is the estimate; a facet summing to
    less leaves postings out and is skipped. Among facets whose every value
    fits under the cap, the smallest sum costs the fewest requests. When none
    fits, the facet with the fewest postings in oversized values is used and
    those values are sliced again one level down.
    """
    usable = []
    for f in _workday_facets(facets):
        if f["facetParameter"] in applied:
            continue
        counts = [v.get("count", 0) for v in f["values"]]
        usable.append((f, sum(counts), max(counts)))
    if not usable:
        return None
    sums = Counter(s for _, s, _ in usable)
    estimate = max(sums, key=lambda s: (sums[s], s))
    covering = [(f, s, m) for f, s, m in usable if s >= estimate]
    if not covering:
        return None
    fits = [(f, s, m) for f, s, m in covering if m < WORKDAY_CAP]
    f, _, _ = (min(fits, key=lambda x: (x[1], len(x[0]["values"]))) if fits
               else min(covering, key=lambda x: (x[2], x[1])))
    return f["facetParameter"], [v["id"] for v in f["values"] if v.get("count")]


# -------------------------------------------------------------------- oracle
ORACLE_MAX_OFFSET = 100_000   # last resort; the total ends the loop first


async def oracle(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    host, site = row.get("host", ""), row.get("site", "")
    if not host or not site:
        raise ValueError("oracle rows need host and site columns")
    out, offset, total = [], 0, None
    seen: set[str] = set()
    while True:
        r = await c.get(
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
            params={
                "onlyData": "true",
                # requisitionList is NOT returned unless you expand it
                "expand": "requisitionList.secondaryLocations",
                "finder": (f"findReqs;siteNumber={site},limit=100,"
                           f"offset={offset},sortBy=POSTING_DATES_DESC"),
            },
            headers=HEADERS,
        )
        r.raise_for_status()
        items = r.json().get("items") or [{}]
        if total is None:
            total = items[0].get("TotalJobsCount") or 0
            _board_total(row, total)
        reqs = items[0].get("requisitionList") or []
        fresh = 0
        for j in reqs:
            rid = str(j.get("Id", ""))
            if rid in seen:
                continue
            seen.add(rid)
            fresh += 1
            posted, src = dates.pick(
                ("PostedDate", dates.from_iso(j.get("PostedDate"))),
                ("RelevantDate", dates.from_iso(j.get("RelevantDate"))),
            )
            out.append(Job(
                company=company,
                title=j.get("Title", ""),
                url=(f"https://{host}/hcmUI/CandidateExperience/en/"
                     f"sites/{site}/job/{rid}"),
                location=(j.get("PrimaryLocation")
                          or j.get("Location")
                          or j.get("PrimaryLocationCountry") or ""),
                ats="oracle",
                posted_at=posted,
                posted_source=src,
                raw_id=str(rid),
            ))
        offset += 100
        # Oracle sometimes returns 99 on a full page, so trust TotalJobsCount.
        # There used to be an `offset > 5000` stop as well: JPMorgan reports
        # 7,302 and pages cleanly past it, but read as exactly 5,100 on every
        # scan. A page with nothing new now guards against a replaying board.
        if not reqs or offset >= total or not fresh or offset > ORACLE_MAX_OFFSET:
            return out


# ------------------------------------------------------------------ workable
async def workable(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    token = row["token"]
    r = await c.get(f"https://apply.workable.com/api/v1/widget/accounts/{token}",
                    params={"details": "true"}, headers=HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        posted, src = dates.pick(("published_on", dates.from_iso(j.get("published_on"))))
        out.append(Job(
            company=company,
            title=j.get("title", ""),
            url=j.get("url", "") or j.get("application_url", ""),
            location=", ".join(x for x in (j.get("city"), j.get("country")) if x),
            ats="workable",
            posted_at=posted,
            posted_source=src,
            raw_id=j.get("shortcode", ""),
            department=j.get("department", ""),
        ))
    return out


# ----------------------------------------------------------------- recruitee
async def recruitee(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    token = row["token"]
    r = await c.get(f"https://{token}.recruitee.com/api/offers/", headers=HEADERS)
    r.raise_for_status()
    out = []
    for j in r.json().get("offers", []):
        posted, src = dates.pick(("published_at", dates.from_iso(j.get("published_at"))))
        out.append(Job(
            company=company,
            title=j.get("title", ""),
            url=j.get("careers_url", "") or j.get("careers_apply_url", ""),
            location=j.get("location", ""),
            ats="recruitee",
            posted_at=posted,
            posted_source=src,
            raw_id=str(j.get("id", "")),
            department=j.get("department", ""),
        ))
    return out


# ----------------------------------------------------------------- eightfold
# Eightfold's public career-site API (they call it PCSX). Centralized: any
# tenant is reachable at {host}/api/apply/v2/jobs, and app.eightfold.ai with
# a `domain` param answers identically.
#
# PCSX is enabled per tenant. A tenant with it switched off returns
# 403 {"message": "Not authorized for PCSX"} no matter what parameters you
# send, so there is nothing to retry or guess. That failure reaches
# runner._one and gets recorded, which is the honest outcome: those boards
# need a different route entirely, not a better request.
EIGHTFOLD_PAGE = 10   # server clamps to 10 however large `num` is
# Advance by half a page so consecutive windows overlap.
#
# The board reorders results between requests, under every sort_by value it
# accepts (relevance, timestamp, distance, recent) and with none at all.
# Non-overlapping windows therefore both duplicate and *skip* rows: a full
# sweep of Netflix's 476 returned 471 unique, losing 5. Measured on that
# board, step=10 loses 5, step=8 loses 1, step=5 loses 0. Paging past the
# reported total does not help, because the gaps are scattered rather than
# at the end.
#
# The cost is 96 requests instead of 48. Worth it: a silently dropped
# requisition is the exact failure this scanner exists to prevent, and it
# would be invisible without comparing against the board's own count.
EIGHTFOLD_STEP = 5


async def eightfold(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    host = row.get("host") or (f"{row['token']}.eightfold.ai" if row.get("token") else "")
    if not host:
        raise ValueError("eightfold rows need host or token")
    if row.get("site") == "pcsx":
        return await _eightfold_pcsx(c, company, row, host)
    domain = row.get("query", "")
    out: list[Job] = []
    seen: set[str] = set()
    start, total = 0, None
    while True:
        r = await c.get(
            f"https://{host}/api/apply/v2/jobs",
            params={"start": start, "num": EIGHTFOLD_PAGE, "sort_by": "relevance",
                    **({"domain": domain} if domain else {})},
            headers={**HEADERS, "Referer": f"https://{host}/careers"},
        )
        r.raise_for_status()
        d = r.json()
        if total is None:
            total = d.get("count") or 0
            _board_total(row, total)
        positions = d.get("positions") or []
        for j in positions:
            jid = str(j.get("id") or j.get("ats_job_id") or "")
            if jid in seen:
                continue        # overlapping windows re-serve rows by design
            seen.add(jid)
            # t_create and t_update are epoch seconds. dates.from_epoch_ms
            # takes either, normalizing anything past 1e12 as milliseconds.
            posted, src = dates.pick(
                ("t_create", dates.from_epoch_ms(j.get("t_create"))),
                ("t_update", dates.from_epoch_ms(j.get("t_update"))),
            )
            loc = j.get("location") or ", ".join(j.get("locations") or [])
            out.append(Job(
                company=company,
                title=j.get("name") or j.get("posting_name") or "",
                url=j.get("canonicalPositionUrl", ""),
                location=loc,
                ats="eightfold",
                posted_at=posted,
                posted_source=src,
                raw_id=jid,
                department=j.get("department") or j.get("business_unit") or "",
            ))
        start += EIGHTFOLD_STEP
        if not positions or start >= total:
            return out


# ------------------------------------------------------------ eightfold pcsx
# Tenants that refuse /api/apply/v2/jobs with 403 "Not authorized for PCSX"
# still serve the newer search endpoint the career site itself calls,
# {host}/api/pcsx/search?domain=<d>&start=N. Selected per row with
# site=pcsx; `query` carries the tenant domain (microsoft.com). Rows without
# site=pcsx, Netflix among them, take the path above unchanged.
#
# Session: the endpoint answered 429 without the cookies the /careers page
# sets (_vs, _vscid, _vscidv2) on 2026-10-01, and 200 without them on
# 2026-10-12. One GET of /careers per sweep costs nothing and covers both,
# and a 429 mid-sweep re-establishes it before backing off.
#
# Paging: 10 rows a page whatever `num`, `limit` or `pageSize` say (measured
# 2026-10-12 on all three tenants). The default sort is postedTs descending,
# and the order is no more stable than the old path's. PayPal on 2026-10-12:
# count 310, two step-10 sweeps each returned 305 unique ids (4 duplicates,
# 5 skipped); one step-5 sweep returned 311 unique against counts of 309-311
# reported across its own pages. Qualcomm: step 10 gave 2,042 of 2,083,
# step 5 gave 2,082 (count drifting 2,083 to 2,084 mid-sweep) and on a
# repeat 2,081 of 2,081. Microsoft: step 10 gave 2,228 of 2,235, step 5 all
# 2,235. Qualcomm and PayPal stamp postedTs at day resolution, so hundreds
# of rows tie (563 on one Qualcomm date) and their order is not fixed;
# Microsoft's are to the second and lose least. So the same half-page
# overlap as the path above, and the same dedupe on id.
PCSX_PAGE = 10
PCSX_STEP = 5            # half a page: consecutive windows overlap by 5 rows
PCSX_PAUSE = 0.3         # seconds between pages, one sweep per tenant
PCSX_RETRIES = 4         # per page, for 429 / 5xx / transport errors
PCSX_BACKOFF = 5.0       # seconds, multiplied by the attempt number
PCSX_MAX_PAGES = 2000    # loop bound only; the board's count is the real stop


async def _pcsx_session(c: httpx.AsyncClient, host: str) -> None:
    r = await c.get(f"https://{host}/careers", follow_redirects=True,
                    headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"})
    r.raise_for_status()


async def _pcsx_get(c: httpx.AsyncClient, host: str, params: dict[str, Any]) -> dict:
    for attempt in range(PCSX_RETRIES):
        last = attempt == PCSX_RETRIES - 1
        try:
            r = await c.get(f"https://{host}/api/pcsx/search", params=params,
                            headers={**HEADERS, "Referer": f"https://{host}/careers"})
        except httpx.TransportError:
            if last:
                raise
        else:
            if r.status_code == 429 or r.status_code >= 500:
                if last:
                    r.raise_for_status()
                if r.status_code == 429:
                    await asyncio.sleep(PCSX_BACKOFF * (attempt + 1))
                    await _pcsx_session(c, host)
                    continue
            else:
                r.raise_for_status()
                body = r.json()
                data = body.get("data")
                if not isinstance(data, dict):
                    err = (body.get("error") or {}).get("message") or "no data"
                    raise ValueError(f"pcsx search: {err}")
                return data
        await asyncio.sleep(PCSX_BACKOFF * (attempt + 1))
    raise AssertionError("unreachable")


def _pcsx_job(p: dict[str, Any], company: str, host: str) -> Job:
    jid = str(p.get("id") or "")
    # postedTs is the date the board shows and sorts by; creationTs is when
    # the requisition was created in Eightfold, sometimes months earlier.
    # Both are epoch seconds, present on every row of all three tenants on
    # 2026-10-12.
    posted, src = dates.pick(
        ("postedTs", dates.from_epoch_ms(p.get("postedTs"))),
        ("creationTs", dates.from_epoch_ms(p.get("creationTs"))),
    )
    path = p.get("positionUrl") or (f"/careers/job/{jid}" if jid else "")
    # `locations` is the tenant's own wording ("United States, Washington,
    # Redmond", "Hyderabad, Telangana, India"). standardizedLocations is
    # deliberately not used: it abbreviates to "Hyderabad, TS, IN" and
    # "Dublin, D, IE", where IN reads as Indiana to runner.is_us_location.
    return Job(
        company=company,
        title=p.get("name") or "",
        url=f"https://{host}{path}" if path.startswith("/") else path,
        location="; ".join(p.get("locations") or []),
        ats="eightfold",
        posted_at=posted,
        posted_source=src,
        raw_id=jid,
        department=p.get("department") or "",
    )


async def _eightfold_pcsx(c: httpx.AsyncClient, company: str, row: dict[str, Any],
                          host: str) -> list[Job]:
    domain = row.get("query", "")
    if not domain:
        raise ValueError("eightfold pcsx rows need the tenant domain in query")
    await _pcsx_session(c, host)
    out: list[Job] = []
    seen: set[str] = set()
    start, total = 0, 0
    for page in range(PCSX_MAX_PAGES):
        if page:
            await asyncio.sleep(PCSX_PAUSE)
        d = await _pcsx_get(c, host, {"domain": domain, "start": start})
        # The count drifts by a row or two within one sweep as postings open
        # and close; stop at the largest seen so a dip never cuts it short.
        total = max(total, int(d.get("count") or 0))
        _board_total(row, total)
        positions = d.get("positions") or []
        for p in positions:
            jid = str(p.get("id") or "")
            if not jid or jid in seen:
                continue        # overlapping windows re-serve rows by design
            seen.add(jid)
            out.append(_pcsx_job(p, company, host))
        start += PCSX_STEP
        if not positions or start >= total:
            return out
    raise RuntimeError(f"pcsx sweep of {host} exceeded {PCSX_MAX_PAGES} pages")

# ----------------------------------------------------------------------- ibm
# ibm.com/careers/search is an Adobe page whose results widget posts to IBM's
# site-wide Elasticsearch front end, scoped by the two ids embedded in the
# page as careerSearchAppId / careerSearchScope. No auth, no token.
#
# Measured 2026-10-01: hits.total 2,044, a full sweep returned 2,044 unique
# jobIds, every one carrying dcdate.
IBM_URL = "https://www-api.ibm.com/search/api/v2"
IBM_PAGE = 100       # the API rejects size > 100 with a 400
# The page's own sort is [_score, pageviews]. With an empty query every score
# is 0, so that order is a tie broken per shard and is not guaranteed stable
# across requests, which is the reordering hazard docs/tier-b-triage.md warns
# about. _id is unique per document, so sorting on it alone is a total order
# and plain non-overlapping windows are exact.
IBM_SORT = [{"_id": "asc"}]
IBM_SOURCE = ["title", "url", "dcdate", "field_keyword_05", "field_keyword_08",
              "field_keyword_19", "field_text_01"]
# Elasticsearch refuses from + size past 10,000 by default. The board holds
# ~2,000, so this only bounds the loop against a runaway total.
IBM_MAX_PAGES = 100
IBM_PAUSE = 0.5      # seconds between pages; one reset seen on back-to-back sweeps
_IBM_CODE = re.compile(r"^(.*?),\s*[A-Z]{2}$")


def _ibm_location(city: str | None, country: str | None) -> str:
    """IBM sends 'PUNE, IN' plus the country name separately.

    The trailing code is ISO 3166 alpha-2, which collides with US state codes:
    'PUNE, IN' reads as Indiana and 'Toronto, CA' as California. Swapping the
    code for the country name removes the collision. 'Multiple Cities' carries
    no code at all, so without the country it would read as a non-answer and
    pass the US filter for every country.
    """
    city = (city or "").strip()
    country = (country or "").strip()
    m = _IBM_CODE.match(city)
    base = m.group(1).strip() if m else city
    if base.lower() in ("", "no city"):
        return country or city
    return f"{base}, {country}" if country else city


async def ibm(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    out: list[Job] = []
    seen: set[str] = set()
    total: int | None = None
    for page in range(IBM_MAX_PAGES):
        if page:
            await asyncio.sleep(IBM_PAUSE)
        r = await c.post(IBM_URL, headers={**HEADERS, "Content-Type": "application/json"},
                         json={"appId": "careers", "scopes": ["careers2"],
                               "query": {"bool": {"must": []}},
                               "size": IBM_PAGE, "from": page * IBM_PAGE,
                               "sort": IBM_SORT, "lang": "zz", "localeSelector": {},
                               "p": 1, "sm": {"query": "", "lang": "zz"},
                               "_source": IBM_SOURCE})
        r.raise_for_status()
        d = r.json()
        hits = d.get("hits") or {}
        if total is None:
            t = hits.get("total")
            total = (t.get("value") if isinstance(t, dict) else t) or 0
            _board_total(row, total)
        records = hits.get("hits") or []
        for h in records:
            s = h.get("_source") or {}
            jid = str(s.get("field_text_01") or h.get("_id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            posted, src = dates.pick(("dcdate", dates.from_iso(s.get("dcdate"))))
            out.append(Job(
                company=company,
                title=s.get("title") or "",
                url=s.get("url") or f"https://careers.ibm.com/careers/JobDetail?jobId={jid}",
                location=_ibm_location(s.get("field_keyword_19"), s.get("field_keyword_05")),
                ats="ibm",
                posted_at=posted,
                posted_source=src,
                raw_id=jid,
                department=s.get("field_keyword_08") or "",
            ))
        if not records or (page + 1) * IBM_PAGE >= total:
            break
    return out

# -------------------------------------------------------------------- tiktok
# lifeattiktok.com (careers.tiktok.com redirects there) is a Next.js site
# over ByteDance's ATS. Its client posts to a public "supplier" search API.
# No auth, but the API answers 400 "invalid request" unless the request
# carries the headers the site's fetch wrapper adds: website-path and origin.
#
# Measured 2026-10-01, count ~4,281. The order itself is stable: five
# instrumented non-overlapping sweeps (100 and 500 per page) each served every
# id exactly once, the same set every time. But one adapter run with plain
# windows returned 4,279 unique against a count of 4,281 before and after.
# The board is live (newest first), so a posting withdrawn mid-sweep from a
# page already read shifts every later row up one, and the row that crosses
# the window boundary is never served. Overlapping windows plus dedupe absorb
# shifts up to PAGE - STEP rows, the same remedy eightfold needed for a
# different cause.
#
# The API accepts up to 500 per page. Every record carries its full
# description, so a 400 page is ~1.6 MB; large windows keep the sweep short,
# which also shrinks the churn exposure. ~22 requests for the full board.
TIKTOK_URL = "https://api.lifeattiktok.com/api/v1/public/supplier/search/job/posts"
TIKTOK_HEADERS = {**HEADERS, "Content-Type": "application/json",
                  "accept-language": "en-US", "origin": "https://lifeattiktok.com",
                  "website-path": "tiktok"}
TIKTOK_PAGE = 400
TIKTOK_STEP = 200        # half a page: consecutive windows overlap by 200 rows
TIKTOK_MAX_PAGES = 200   # loop bound only; the board stops at its own count
TIKTOK_PAUSE = 0.5       # seconds between pages


def _tiktok_location(city: dict | None) -> str:
    """city_info nests city -> state -> country through `parent`."""
    names: list[str] = []
    while isinstance(city, dict):
        n = (city.get("en_name") or city.get("i18n_name") or city.get("name") or "").strip()
        if n and n not in names:        # Singapore is its own city and country
            names.append(n)
        city = city.get("parent")
    return ", ".join(names)


def _tiktok_created(jid: str) -> str | None:
    """The board sends no date. Its ids are ByteDance snowflakes whose high 32
    bits are the record's creation time in epoch seconds.

    Checked against the live board: the newest id decodes to the scan day, none
    decode to the future, and the board's own newest-first order matches the
    decoded times. It is the requisition's creation, not its last repost, so
    some evergreen "2026 Start" postings decode to 2023. Kept under its own
    posted_source so that difference stays visible.
    """
    try:
        return dates.from_epoch_ms(int(jid) >> 32)
    except (TypeError, ValueError):
        return None


async def tiktok(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    out: list[Job] = []
    seen: set[str] = set()
    total = 0
    for page in range(TIKTOK_MAX_PAGES):
        offset = page * TIKTOK_STEP
        if page:
            await asyncio.sleep(TIKTOK_PAUSE)
        r = await c.post(TIKTOK_URL, headers=TIKTOK_HEADERS, json={
            "recruitment_id_list": [], "job_category_id_list": [],
            "subject_id_list": [], "location_code_list": [],
            "keyword": row.get("query", ""),
            "limit": TIKTOK_PAGE, "offset": offset,
        })
        r.raise_for_status()
        d = r.json()
        if d.get("code") not in (0, None):
            raise ValueError(f"tiktok code {d.get('code')}: {str(d.get('message'))[:80]}")
        data = d.get("data") or {}
        # The largest count seen, so a board that grows mid-sweep is read
        # to its new end rather than the end it had on the first page.
        total = max(total, data.get("count") or 0)
        _board_total(row, total)
        posts = data.get("job_post_list") or []
        for j in posts:
            jid = str(j.get("id") or "")
            if not jid or jid in seen:
                continue        # overlapping windows re-serve rows by design
            seen.add(jid)
            posted, src = dates.pick(("id_epoch", _tiktok_created(jid)))
            out.append(Job(
                company=company,
                title=(j.get("title") or "").strip(),
                url=f"https://lifeattiktok.com/search/{jid}",
                location=_tiktok_location(j.get("city_info")),
                ats="tiktok",
                posted_at=posted,
                posted_source=src,
                raw_id=jid,
                department=(j.get("job_category") or {}).get("en_name") or "",
            ))
        if not posts or offset + TIKTOK_PAGE >= total:
            break
    return out

# ---------------------------------------------------------------------- kula
# Kula hosts boards at careers.kula.ai/{account}. The page is server rendered,
# and its own client reads the same postings from a JSON endpoint under
# /api/internal/ with no auth. `type` selects the handler server-side; 99 per
# page is the value the site itself sends. meta carries count and pages.
#
# Measured 2026-10-01 on 10x Genomics: meta.count 34, 34 unique ids, every
# one with launch_at, matching the 34 job links in the rendered page.
KULA_PAGE = 99
KULA_MAX_PAGES = 100     # loop bound only; meta.pages is the real stop


def _kula_location(offices: list | None) -> str:
    """Each office carries a ready 'City, State, Country' string."""
    names: list[str] = []
    for o in offices or []:
        if not isinstance(o, dict):
            continue
        n = (o.get("location") or ", ".join(
            x for x in (o.get("city"), o.get("state"), o.get("country")) if x) or "").strip()
        if n and n not in names:
            names.append(n)
    return "; ".join(names)


async def kula(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    account = row.get("token", "")
    if not account:
        raise ValueError("kula rows need token (the careers.kula.ai account name)")
    host = row.get("host") or "careers.kula.ai"
    out: list[Job] = []
    seen: set[str] = set()
    for page in range(1, KULA_MAX_PAGES):
        r = await c.get(f"https://{host}/api/internal/ats_job_posts", headers=HEADERS,
                        params={"accountName": account, "page": page,
                                "type": "ats_job_post.index", "items": KULA_PAGE})
        r.raise_for_status()
        d = r.json()
        if d.get("errors"):
            raise ValueError(f"kula: {str(d['errors'])[:120]}")
        posts = d.get("data") or []
        for j in posts:
            jid = str(j.get("id") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            ats_job = j.get("ats_job") or {}
            posted, src = dates.pick(("launch_at", dates.from_iso(j.get("launch_at"))))
            out.append(Job(
                company=company,
                title=(j.get("title") or "").strip(),
                # the bare id redirects to the canonical id-slug URL
                url=f"https://{host}/{account}/{jid}",
                location=_kula_location(ats_job.get("offices")),
                ats="kula",
                posted_at=posted,
                posted_source=src,
                raw_id=jid,
                department=(ats_job.get("ats_department") or {}).get("name") or "",
            ))
        pages = (d.get("meta") or {}).get("pages") or 0
        _board_total(row, (d.get("meta") or {}).get("count"))
        if not posts or page >= pages:
            break
    return out


# --------------------------------------------------------------------- icims
# iCIMS "careers-home" sites (the Jibe front end iCIMS acquired) expose an
# open JSON search at https://{host}/api/jobs?page=N&limit=100, returning
# {"totalCount": n, "jobs": [{"data": {...}}]}. `host` is that career site,
# NOT the classic *.icims.com portal: every classic portal measured on
# 2026-10-01 (Atlassian, Schwab, Panasonic) answers 405 with an AWS WAF
# captcha page, which is why the old ?format=json path looked dead.
#
# totalCount spans every language the site publishes; `count` is only the
# default language. Panasonic reports totalCount 447, count 430, and the 17
# es-mx postings carry their own req_ids, so they are real postings and the
# sweep targets totalCount. A full non-overlapping sweep returned exactly
# totalCount unique req_ids on AMD (1,256), Panasonic (447) and GitHub (73),
# so unlike eightfold no overlap is needed. The seen-set stays as a cheap
# guard in case a board ever re-serves a row across pages.
ICIMS_PAGE = 100   # honored by every tenant measured; 13 requests for AMD


async def icims(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    host = row.get("host", "")
    if not host:
        raise ValueError("icims rows need host (the careers-home site, not *.icims.com)")
    out: list[Job] = []
    seen: set[str] = set()
    page, total = 1, None
    while True:
        r = await c.get(
            f"https://{host}/api/jobs",
            params={"page": page, "limit": ICIMS_PAGE},
            headers={**HEADERS, "Referer": f"https://{host}/careers-home/jobs"},
        )
        r.raise_for_status()
        d = r.json()
        if total is None:
            total = d.get("totalCount") or d.get("count") or 0
            _board_total(row, total)
        jobs = d.get("jobs") or []
        for item in jobs:
            j = item.get("data") or {}
            rid = str(j.get("req_id") or j.get("slug") or "")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            posted, src = dates.pick(
                ("posted_date", dates.from_iso(j.get("posted_date"))),
                ("create_date", dates.from_iso(j.get("create_date"))),
            )
            meta = j.get("meta_data") or {}
            cats = j.get("categories") or []
            out.append(Job(
                company=company,
                title=j.get("title", ""),
                url=(meta.get("canonical_url")
                     or f"https://{host}/jobs/{j.get('slug') or rid}"),
                # full_location joins every location with "; " when
                # multipleLocations is set; short_location is the first only.
                location=(j.get("full_location") or j.get("short_location")
                          or j.get("location_name") or ""),
                ats="icims",
                posted_at=posted,
                posted_source=src,
                raw_id=rid,
                department=(j.get("department")
                            or (cats[0].get("name", "") if cats else "")),
            ))
        page += 1
        if not jobs or len(seen) >= total or page > 100:
            return out


# ------------------------------------------------------------------- radancy
# Radancy TalentBrew career sites (jobs.intuit.com, careers.unitedhealthgroup
# .com). TalentBrew is a search front-end that sits over the real ATS, and for
# these tenants it is the only public listing: Intuit's Avature portal 404s on
# search and serves an empty feed, and UHG's Taleo REST search answers
# careerSectionUnAvailable. It is also the more complete view, since Intuit's
# listing merges two ATSs (Avature 530 + "EH" 37 on 2026-10-01).
#
# GET /search-jobs/results is the page's own AJAX call. It returns JSON whose
# `results` is an HTML fragment of <li> cards plus a section carrying
# data-total-results and data-total-pages. Blank SearchFiltersModuleName drops
# the facet HTML, which on UHG is ~8.7 MB per request against ~0.4 MB of
# results. RecordsPerPage=500 is honored.
#
# The cards carry no date in any sort order and the sitemap's lastmod is the
# generation time for every job, so posted_at stays None. The detail page's
# JSON-LD has a real datePosted, but that is one request per posting (5,547
# for UHG), which is not worth it while the store's first_seen covers the gap.
from html import unescape as _unescape  # noqa: E402  (kept in this section)

RADANCY_PAGE = 500
_RADANCY_LI = re.compile(r"<li\b[^>]*>.*?</li>", re.S)
_RADANCY_HREF = re.compile(r'href="(/job/[^"]*?/(\d+)/(\d+))"')
_RADANCY_TOTAL = re.compile(r'data-total-results="(\d+)"')
_RADANCY_PAGES = re.compile(r'data-total-pages="(\d+)"')


def _radancy_text(pattern: str, block: str) -> str:
    m = re.search(pattern, block, re.S)
    if not m:
        return ""
    return _unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()


def _radancy_cards(fragment: str) -> list[dict[str, str]]:
    cards = []
    for li in _RADANCY_LI.findall(fragment):
        href = _RADANCY_HREF.search(li)
        if not href:
            continue    # pagination and other non-job list items
        cards.append({
            "path": href.group(1),
            "id": href.group(3),
            "title": _radancy_text(r"<h2[^>]*>(.*?)</h2>", li),
            "location": _radancy_text(r'class="job-location[^"]*"[^>]*>(.*?)</span>', li),
            # UHG names the hiring brand on some cards (LHC Group, ...).
            # Intuit's data-category is not usable: on 2026-10-01 it read
            # "Data" for 500 of 567 cards, software developers included.
            "department": _radancy_text(
                r'class="job-info job-entity"[^>]*>(.*?)</span>', li),
        })
    return cards


async def radancy(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    host = row.get("host", "")
    if not host:
        raise ValueError("radancy rows need host")
    out: list[Job] = []
    seen: set[str] = set()
    page, pages = 1, None
    while True:
        r = await c.get(
            f"https://{host}/search-jobs/results",
            params={
                "ActiveFacetID": "0", "CurrentPage": page,
                "RecordsPerPage": RADANCY_PAGE, "Distance": "50",
                "RadiusUnitType": "0", "Keywords": row.get("query", ""),
                "Location": "", "ShowRadius": "False", "IsPagination": "True",
                "CustomFacetName": "", "FacetTerm": "", "FacetType": "0",
                "SearchResultsModuleName": "Search Results",
                "SearchFiltersModuleName": "",
                "SortCriteria": "0", "SortDirection": "0", "SearchType": "5",
                "PostalCode": "", "ResultsType": "0",
            },
            headers={**HEADERS, "X-Requested-With": "XMLHttpRequest",
                     "Referer": f"https://{host}/search-jobs"},
        )
        r.raise_for_status()
        fragment = r.json().get("results") or ""
        if pages is None:
            m = _RADANCY_PAGES.search(fragment)
            total = _RADANCY_TOTAL.search(fragment)
            if total:
                _board_total(row, total.group(1))
            # Prefer the reported page count; derive it from the total if not.
            pages = (int(m.group(1)) if m else
                     -(-int(total.group(1)) // RADANCY_PAGE) if total else 0)
        cards = _radancy_cards(fragment)
        for card in cards:
            if card["id"] in seen:
                continue
            seen.add(card["id"])
            out.append(Job(
                company=company,
                title=card["title"],
                url=f"https://{host}{card['path']}",
                location=card["location"],
                ats="radancy",
                raw_id=card["id"],
                department=card["department"],
            ))
        page += 1
        if not cards or page > pages or page > 100:
            return out


# --------------------------------------------------------------------- apple
# jobs.apple.com is a server-rendered React Router app. Every search page
# embeds its loader data as window.__staticRouterHydrationData, a JSON string
# holding `searchResults` (20 per page) and the board's own `totalRecords`.
# That is the whole listing over plain GET, so no CSRF token is needed.
#
# The old lead, POST /api/role/search behind an x-apple-csrf-token header,
# is gone: both /api/role/search and /api/csrfToken return Apple's 404 page
# (measured 2026-10-01). Nothing in the client calls them any more.
#
# A bare /en-us/search 301s to ?location=<geo>, so the location filter is
# always applied. `site` overrides it; the default is the US slug. Measured
# 2026-10-01 at sort=newest: 226 pages, 4,516 rows, 4,514 unique against a
# reported 4,514. The two repeats are postings published mid-sweep pushing
# rows down a page, hence the dedupe.
import asyncio as _asyncio  # noqa: E402  kept in this section to avoid merge churn

_APPLE_HYDRATION = re.compile(
    r"window\.__staticRouterHydrationData\s*=\s*JSON\.parse\((\".*?\")\);", re.S)
_APPLE_DAY = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})")
_APPLE_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
APPLE_PAGE_SIZE = 20       # fixed by the board, no size parameter
APPLE_MAX_PAGES = 1000     # safety net; the total ends the loop long before
APPLE_DEFAULT_LOCATION = "united-states-USA"
APPLE_PAGE_DELAY = 0.3     # seconds between pages, to stay polite over ~230 GETs
APPLE_RETRY_DELAY = 3.0    # base backoff for a transient 5xx on one page
APPLE_RETRIES = 3          # per page; one 502 in 226 pages was seen live
# Empty pages are a different failure from a 5xx. On 2026-10-12, 9 of 60
# sequential requests (15%) came back with no search data, scattered at
# random with a longest run of 2, which looks like some backends behind the
# load balancer rather than throttling. Three tries per page fails a
# 220-page sweep about half the time; eight makes it ~1 in 20,000. Random
# failures do not need a long wait, so the retry is quick.
APPLE_EMPTY_RETRIES = 8
APPLE_EMPTY_DELAY = 0.5    # seconds


def _apple_search(html: str) -> dict:
    m = _APPLE_HYDRATION.search(html)
    if not m:
        # A markup change must fail loudly, never read as "Apple has no jobs".
        raise ValueError("apple: hydration data not found in search page")
    data = json.loads(json.loads(m.group(1)))
    search = (data.get("loaderData") or {}).get("search")
    if not isinstance(search, dict):
        raise ValueError("apple: hydration data has no search loader")
    return search


def _apple_day(value: Any) -> str | None:
    """postingDate is 'Oct 01, 2026'. Fallback only; date with no clock time."""
    if not isinstance(value, str):
        return None
    m = _APPLE_DAY.search(value)
    mon = _APPLE_MONTHS.get(m.group(1)) if m else None
    if not mon:
        return None
    return dates.from_iso(f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}")


def _apple_location(locs: Any) -> str:
    parts = []
    for loc in locs or []:
        name = (loc.get("name") or "").strip()
        country = (loc.get("countryName") or "").strip()
        # Store-level rows carry only a city name ("Cupertino"), so the country
        # is appended for runner.is_us_location to have something to read.
        if name and country and name not in country:
            parts.append(f"{name}, {country}")
        elif name or country:
            parts.append(name or country)
    return "; ".join(parts)


async def _apple_get(c: httpx.AsyncClient, url: str, params: dict) -> httpx.Response:
    for attempt in range(APPLE_RETRIES):
        try:
            r = await c.get(url, params=params, follow_redirects=True,
                            headers={**HEADERS, "Accept": "text/html"})
        except httpx.TransportError:
            if attempt == APPLE_RETRIES - 1:
                raise
        else:
            if r.status_code < 500 or attempt == APPLE_RETRIES - 1:
                r.raise_for_status()
                return r
        await _asyncio.sleep(APPLE_RETRY_DELAY * (attempt + 1))
    raise AssertionError("unreachable")


async def apple(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    base = "https://jobs.apple.com/en-us/search"
    where = row.get("site") or APPLE_DEFAULT_LOCATION
    query = row.get("query", "")
    out: list[Job] = []
    seen: set[str] = set()
    total: int | None = None
    for page in range(1, APPLE_MAX_PAGES):
        if page > 1:
            await _asyncio.sleep(APPLE_PAGE_DELAY)
        # Some responses carry no search data at all: totalRecords 0 and no
        # results, then the same URL is fine a moment later. On 2026-10-12
        # one landed on page 2 and the scan read 20 of 4,395 as the whole
        # board. A page cannot legitimately be empty before the total is
        # reached (a real last page is short and ends the loop first), so an
        # empty one is retried, and one that stays empty raises rather than
        # returning a partial board.
        for attempt in range(APPLE_EMPTY_RETRIES):
            r = await _apple_get(c, base, {"location": where, "sort": "newest",
                                           "page": page,
                                           **({"search": query} if query else {})})
            search = _apple_search(r.text)
            results = search.get("searchResults") or []
            if results:
                break
            await _asyncio.sleep(APPLE_EMPTY_DELAY)
        else:
            # Within one page of the total, an empty page is the real end of
            # a board that shrank mid-sweep, not a failure.
            if total and len(seen) >= total - APPLE_PAGE_SIZE:
                break
            raise RuntimeError(f"apple: page {page} came back empty "
                               f"{APPLE_EMPTY_RETRIES} times with {len(seen)} of "
                               f"{total or '?'} postings read")
        if total is None:
            total = int(search.get("totalRecords") or 0)
            _board_total(row, total)
        for j in results:
            jid = str(j.get("id") or j.get("jobPositionId") or "")
            if not jid or jid in seen:
                continue
            seen.add(jid)
            # One evergreen retail req, id "PIPE-114438158", is stamped with
            # the render time on every request, so it would look brand new on
            # every scan. Its ids carry a "PIPE-" prefix; real requisitions
            # are "<positionId>-<site code>". Trust no date for the former.
            if jid.startswith("PIPE-"):
                posted, src = None, ""
            else:
                posted, src = dates.pick(
                    ("postDateInGMT", dates.from_iso(j.get("postDateInGMT"))),
                    ("postingDate", _apple_day(j.get("postingDate"))),
                )
            team = j.get("team") or {}
            slug = j.get("transformedPostingTitle") or ""
            url = f"https://jobs.apple.com/en-us/details/{jid}/{slug}"
            if team.get("teamCode"):
                url += f"?team={team['teamCode']}"
            out.append(Job(
                company=company,
                title=(j.get("postingTitle") or "").strip(),
                url=url,
                location=_apple_location(j.get("locations")),
                ats="apple",
                posted_at=posted,
                posted_source=src,
                # `id` is unique per posting. positionId is not: one req posted
                # to several sites shares it (720 of 4,514 collide).
                raw_id=jid,
                department=team.get("teamName") or "",
            ))
        if len(results) < APPLE_PAGE_SIZE:
            break
        if total and len(seen) >= total:
            break
    return out


# ---------------------------------------------------------------------- meta
# metacareers.com is a Relay app. The listing is one persisted GraphQL query,
# CareersJobSearchResultsV2DataQuery, which returns every posting in a single
# response (no pagination). Persisted queries are addressed by `doc_id`, and
# that id changes whenever Meta ships the bundle, so hardcoding it breaks.
#
# Both moving parts are rediscovered on every run, over plain HTTP:
#   - the LSD token sits in the job search page as ["LSD",[],{"token":...}]
#   - the doc_id sits in one of the ~10 bundles the page loads with a direct
#     <script src>, as __d("CareersJobSearchResultsV2DataQuery_<x>RelayOperation",
#     [],(function(...){a.exports="<doc_id>"}). The other ~525 bundles in the
#     page's resource map are lazy and never needed.
# The page's own preloaded CPJobSearchQuery carries no jobs, so it is no help.
#
# Measured 2026-10-01: 1,061 postings, 1,061 unique, matching the 1,061 job
# URLs in /jobsearch/sitemap.xml exactly. The query selects id, title,
# locations, teams and sub_teams only; there is no posting date to read, so
# posted_at stays None and first_seen is the signal.
_META_LSD = re.compile(r'\["LSD",\[\],\{"token":"([^"]+)"')
_META_SCRIPT = re.compile(r'<script[^>]+src="(https://static\.[^"]+\.js[^"]*)"')
_META_DOC = re.compile(
    r'__d\("CareersJobSearchResults(V\d+)?DataQuery_[A-Za-z_]*RelayOperation",'
    r'\[\],\(function\([^)]*\)\{\w+\.exports="(\d+)"')
META_BASE = "https://www.metacareers.com"
_META_NAV = {
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
    "Sec-Fetch-Dest": "document",
}


def _meta_doc_id(js: str) -> tuple[int, str] | None:
    """Newest results query in a bundle, as (version, doc_id)."""
    best: tuple[int, str] | None = None
    for m in _META_DOC.finditer(js):
        ver = int(m.group(1)[1:]) if m.group(1) else 1
        if best is None or ver > best[0]:
            best = (ver, m.group(2))
    return best


async def meta(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    page = await c.get(f"{META_BASE}/jobsearch/", follow_redirects=True,
                       headers={**HEADERS, **_META_NAV})
    page.raise_for_status()
    m = _META_LSD.search(page.text)
    if not m:
        raise ValueError("meta: LSD token not found in job search page")
    lsd = m.group(1)

    found: tuple[int, str] | None = None
    for src in dict.fromkeys(_META_SCRIPT.findall(page.text)):
        r = await c.get(src.replace("&amp;", "&"), headers=HEADERS)
        if r.status_code != 200:
            continue
        found = _meta_doc_id(r.text)
        if found:
            break
    if not found:
        raise ValueError("meta: CareersJobSearchResults doc_id not found in page bundles")
    version, doc_id = found
    name = f"CareersJobSearchResults{'V%d' % version if version > 1 else ''}DataQuery"

    search_input = {
        "q": row.get("query") or None, "divisions": [], "offices": [],
        "roles": [], "leadership_levels": [], "saved_jobs": [],
        "saved_searches": [], "sub_teams": [], "teams": [],
        "is_leadership": False, "is_remote_only": False,
        "sort_by_new": True, "results_per_page": None,
    }
    r = await c.post(
        f"{META_BASE}/api/graphql/",
        data={"lsd": lsd, "fb_api_caller_class": "RelayModern",
              "fb_api_req_friendly_name": name, "server_timestamps": "true",
              "variables": json.dumps({"search_input": search_input,
                                       "isLoggedIn": False,
                                       "viewasUserID": None}),
              "doc_id": doc_id},
        headers={**HEADERS, "Accept": "*/*", "X-FB-LSD": lsd,
                 "X-FB-Friendly-Name": name, "Origin": META_BASE,
                 "Referer": f"{META_BASE}/jobsearch/",
                 "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin",
                 "Sec-Fetch-Dest": "empty"},
    )
    r.raise_for_status()
    body = r.text.strip()
    if body.startswith("for (;;);"):
        body = body[len("for (;;);"):]
    # Relay may stream several JSON objects, one per line; the first holds data.
    d = json.loads(body.split("\n", 1)[0])
    data = d.get("data") or {}
    result = next((v for k, v in data.items()
                   if k.startswith("job_search_with_featured_jobs") and v), None)
    if result is None:
        err = (d.get("errors") or [{}])[0].get("message") or str(d)[:160]
        raise ValueError(f"meta: no job search result ({err})")

    out: list[Job] = []
    seen: set[str] = set()
    # featured_jobs has always been a subset of all_jobs; read both and dedupe
    # so a featured posting can never be the one that goes missing.
    for j in (result.get("all_jobs") or []) + (result.get("featured_jobs") or []):
        jid = str(j.get("id") or "")
        if not jid or jid in seen:
            continue
        seen.add(jid)
        out.append(Job(
            company=company,
            title=(j.get("title") or "").strip(),
            url=f"{META_BASE}/profile/job_details/{jid}/",
            location="; ".join(j.get("locations") or []),
            ats="meta",
            raw_id=jid,
            department=", ".join(j.get("teams") or []),
        ))
    return out


# ------------------------------------------------------------ successfactors
# SAP SuccessFactors Recruiting Marketing ("Career Site Builder") sites. One
# platform, two search front ends, picked per row with `site`:
#
#   (blank)  the classic server-rendered listing, GET /search/?q=&startrow=N.
#            25 rows a page, rendered either as a table (tr.data-row) or as
#            tiles (li.job-tile) depending on the site's theme, and both carry
#            the board's own total ("Results 1 - 25 of 289", "Showing 1 to 25
#            of 281 Jobs"). Measured 2026-10-01 with the default sort, every
#            board swept to exactly its total with zero duplicates: SAP 790,
#            Supermicro 1,037, TSMC 325, Hyundai 289, Paramount 279.
#   unify    the newer "Unify" results page, which renders client side from
#            POST /services/recruiting/v1/jobs. Needs the page's CSRFToken and
#            its JSESSIONID cookie, so the page is fetched first. 10 a page.
#
# The unify API reorders between requests unless sorted on a unique-enough
# key. On Altria's 106 postings, a full sweep returned 81 unique ids with
# sortBy=recent, 77 with relevance, 81 with location, 92 with none, and 106
# with `date`. Even `date` is not fully stable, presumably because many
# postings share a start date: three later sweeps of 105 returned 105, 103 and
# 103. No request shape gives a stable order, so the adapter dedupes on id and
# sweeps again, under a different sort each time, until it holds the board's
# own total. The extra passes cost ~11 requests each and only run when short.
# Imported here rather than at the top so this section stays self-contained.
import asyncio as _asyncio  # noqa: E402
import html as _html  # noqa: E402
from datetime import datetime as _dt  # noqa: E402

SF_CLASSIC_TOTAL = re.compile(
    r"of\s*<b>\s*([\d,.\s]+?)\s*</b>|of\s+([\d,.]+)\s+jobs", re.I)
SF_ROW_SPLIT = re.compile(r'<tr class="data-row|<li class="job-tile\b')
SF_HREF = re.compile(r'(?:href|data-url)="([^"]*?/job/[^"]*?/(\d+)/?)"')
SF_TITLE = re.compile(r'<a\b[^>]*class="jobTitle-link[^"]*"[^>]*>(.*?)</a>', re.S)
SF_LOCATION = re.compile(r'class="jobLocation">(.*?)</span>'
                         r'|-section-location-value">(.*?)</div>', re.S)
SF_DATE = re.compile(r'class="jobDate">(.*?)<|-section-date-value">(.*?)<', re.S)
SF_DEPT = re.compile(r'class="jobDepartment">(.*?)</span>', re.S)
SF_CSRF = re.compile(r'var CSRFToken\s*=\s*"([^"]+)"')
SF_LOCALE = re.compile(r"currentLocale:\s*'([A-Za-z_]+)'")
SF_TAG = re.compile(r"<[^>]+>")
# Safety net. Supermicro, the largest board seen, is 42 pages of 25.
SF_MAX_PAGES = 400
# Pause between pages. TSMC's board dropped connections ("Server disconnected
# without sending a response", then a read timeout) when its 14 pages were
# fetched back to back on 2026-10-01, and swept cleanly at 0.7s spacing.
SF_PAGE_DELAY = 0.7
# Sort orders for successive unify passes, most stable first.
SF_UNIFY_SORTS = ("date", "date", "", "recent")


# ISO 3166 codes SuccessFactors tenants write as the country. Only the
# country's name has to reach runner.is_us_location: a code that is also a
# US state (DE, IN, CA, PA, GA, AL...) otherwise reads as that state.
_SF_COUNTRIES = {
    "AE": "United Arab Emirates", "AR": "Argentina", "AT": "Austria",
    "AU": "Australia", "BE": "Belgium", "BG": "Bulgaria", "BR": "Brazil",
    "CA": "Canada", "CH": "Switzerland", "CL": "Chile", "CN": "China",
    "CO": "Colombia", "CZ": "Czechia", "DE": "Germany", "DK": "Denmark",
    "EG": "Egypt", "ES": "Spain", "FI": "Finland", "FR": "France",
    "GB": "United Kingdom", "GR": "Greece", "HK": "Hong Kong",
    "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland", "IL": "Israel",
    "IN": "India", "IT": "Italy", "JP": "Japan", "KR": "Korea",
    "MA": "Morocco", "MX": "Mexico", "MY": "Malaysia", "NL": "Netherlands",
    "NO": "Norway", "NZ": "New Zealand", "PE": "Peru", "PH": "Philippines",
    "PL": "Poland", "PT": "Portugal", "RO": "Romania", "RS": "Serbia",
    "SA": "Saudi Arabia", "SE": "Sweden", "SG": "Singapore", "SK": "Slovakia",
    "TH": "Thailand", "TR": "Turkey", "TW": "Taiwan", "UA": "Ukraine",
    "VN": "Vietnam", "ZA": "South Africa",
}
_SF_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
    "WI WY DC PR".split())
# head, then the country code, then an optional postcode (which has a digit).
_SF_PLACE = re.compile(r"^(?P<head>[^,].*?),\s*(?P<cc>[A-Z]{2})(?:,\s*[\w -]*\d[\w -]*)?$")


def _sf_country(location: str) -> str:
    """'Walldorf, DE, 69190' -> 'Walldorf, Germany'.

    SuccessFactors writes City, [State,] COUNTRY, [postcode], and always
    includes the country: 'Austin, TX, US, 78728', 'Phoenix, AZ, US'. A
    country other than US is replaced by its name, or lowercased when the
    table lacks it, which no US check reads as a state. Anything not in
    that shape is returned as it came.
    """
    m = _SF_PLACE.match(location.strip())
    if not m or m["cc"] == "US":
        return location
    # Some tenants leave the country out (Hyundai's "Montgomery, AL"). Then a
    # trailing code that is also a US state is the state, unless a postcode
    # or a region before it shows the shape is City, Region, COUNTRY.
    sure = location.rstrip().endswith(tuple("0123456789")) or "," in m["head"]
    if m["cc"] in _SF_US_STATES and not sure:
        return location
    return f"{m['head']}, {_SF_COUNTRIES.get(m['cc'], m['cc'].lower())}"


def _sf_text(fragment: str | None) -> str:
    return " ".join(_html.unescape(SF_TAG.sub(" ", fragment or "")).split())


def _sf_date(value: str | None, us_numeric: bool = False) -> str | None:
    """'Oct 1, 2026' on classic boards, '5/12/26' (en_US M/D/YY) on unify.

    Numeric dates are only read when the caller knows the locale is en_US.
    Read blind, 5/12/26 is equally May 12 and December 5.
    """
    v = _sf_text(value)
    if not v:
        return None
    fmts = ["%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d"]
    if us_numeric:
        fmts += ["%m/%d/%y", "%m/%d/%Y"]
    for fmt in fmts:
        try:
            return dates.from_iso(_dt.strptime(v, fmt).date().isoformat())
        except ValueError:
            continue
    return None


def _sf_first(rx: re.Pattern[str], block: str) -> str:
    for m in rx.finditer(block):
        text = _sf_text(next((g for g in m.groups() if g is not None), ""))
        if text:
            return text
    return ""


def _sf_classic_rows(page: str, host: str, company: str) -> list[Job]:
    out = []
    for block in SF_ROW_SPLIT.split(page)[1:]:
        m = SF_HREF.search(block)
        if not m:
            continue
        href, rid = _html.unescape(m.group(1)), m.group(2)
        posted = dates.pick(("jobDate", _sf_date(_sf_first(SF_DATE, block))))
        out.append(Job(
            company=company,
            title=_sf_first(SF_TITLE, block),
            url=f"https://{host}{href}" if href.startswith("/") else href,
            location="; ".join(_sf_country(p.strip())
                               for p in _sf_first(SF_LOCATION, block).split(";")),
            ats="successfactors",
            posted_at=posted[0],
            posted_source=posted[1],
            raw_id=rid,
            department=_sf_first(SF_DEPT, block),
        ))
    return out


async def _sf_classic(c: httpx.AsyncClient, company: str, host: str,
                      query: str, row: dict[str, Any]) -> list[Job]:
    out: list[Job] = []
    seen: set[str] = set()
    start, total = 0, None
    for _ in range(SF_MAX_PAGES):
        r = await c.get(f"https://{host}/search/",
                        params={"q": query, "startrow": start},
                        headers={**HEADERS, "Accept": "text/html"})
        r.raise_for_status()
        if total is None:
            m = SF_CLASSIC_TOTAL.search(r.text)
            if m:
                total = int(re.sub(r"\D", "", m.group(1) or m.group(2)))
                _board_total(row, total)
            elif "searchresultsunify" in r.text.lower():
                # A unify site serves this page with no rows in it, which
                # would otherwise read as an empty board.
                raise ValueError(f"{host} renders results client side, set site=unify")
        rows = _sf_classic_rows(r.text, host, company)
        for j in rows:
            if j.raw_id not in seen:
                seen.add(j.raw_id)
                out.append(j)
        # startrow is an offset, so advance by what the page actually held
        # rather than assuming the theme's page size.
        start += len(rows)
        if not rows or total is None or start >= total:
            return out
        await _asyncio.sleep(SF_PAGE_DELAY)
    return out


async def _sf_unify(c: httpx.AsyncClient, company: str, host: str,
                    query: str, row: dict[str, Any]) -> list[Job]:
    r = await c.get(f"https://{host}/search/", params={"q": query},
                    headers={**HEADERS, "Accept": "text/html"})
    r.raise_for_status()
    m = SF_CSRF.search(r.text)
    if not m:
        raise ValueError(f"{host}: no CSRFToken on the search page")
    token = m.group(1)
    lm = SF_LOCALE.search(r.text)
    locale = lm.group(1) if lm else "en_US"
    out: list[Job] = []
    seen: set[str] = set()
    total = None
    for sort in SF_UNIFY_SORTS:
        for page in range(SF_MAX_PAGES):
            r = await c.post(
                f"https://{host}/services/recruiting/v1/jobs",
                headers={**HEADERS, "Content-Type": "application/json",
                         "X-CSRF-Token": token, "Referer": f"https://{host}/search/"},
                json={"keywords": query, "locale": locale, "location": "",
                      "pageNumber": page, "sortBy": sort},
            )
            r.raise_for_status()
            d = r.json()
            if d.get("error"):
                raise ValueError(str(d["error"])[:120])
            if total is None:
                total = d.get("totalJobs") or 0
                _board_total(row, total)
            results = d.get("jobSearchResult") or []
            for item in results:
                j = item.get("response") or {}
                rid = str(j.get("id") or "")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                out.append(_sf_unify_job(j, company, host, locale, rid))
            if len(seen) >= total:
                return out
            if not results:
                break   # end of this pass, still short: sweep again
            await _asyncio.sleep(SF_PAGE_DELAY)
    return out


def _sf_unify_job(j: dict[str, Any], company: str, host: str, locale: str,
                  rid: str) -> Job:
    slug = j.get("urlTitle") or j.get("unifiedUrlTitle") or "job"
    posted, src = dates.pick(
        ("unifiedStandardStart",
         _sf_date(j.get("unifiedStandardStart"), us_numeric=locale == "en_US")),
    )
    return Job(
        company=company,
        title=_sf_text(j.get("unifiedStandardTitle")),
        url=f"https://{host}/job/{slug}/{rid}-{locale}/",
        location="; ".join(_sf_country(_sf_text(x)) for x in j.get("jobLocationShort") or []
                           if _sf_text(x)),
        ats="successfactors",
        posted_at=posted,
        posted_source=src,
        raw_id=rid,
    )


async def successfactors(c: httpx.AsyncClient, company: str,
                         row: dict[str, Any]) -> list[Job]:
    host = row.get("host", "")
    if not host:
        raise ValueError("successfactors rows need host")
    query = row.get("query", "")
    if row.get("site") == "unify":
        return await _sf_unify(c, company, host, query, row)
    return await _sf_classic(c, company, host, query, row)


# ----------------------------------------------------------------- atlassian
# Atlassian runs iCIMS behind six classic portals (careers-americas,
# globalcareers-atlassian, careers-apac-atlassian, two campus portals and
# globalmobility), every one of them 405 behind an AWS WAF captcha. Its own
# careers page instead reads one unpaged JSON list from
# www.atlassian.com/endpoint/careers/listings: no paging, no total field.
#
# The list repeats some postings verbatim (same id, same portal, same
# fields), so the adapter dedupes on the iCIMS id. Measured 2026-10-12:
# 347 entries, 329 unique ids, no id on two portals, and the all-jobs page
# rendered in a browser said "329 RESULTS" with 329 distinct detail links.
# The deduped count is the board's own count.
#
# The only date is portalJobPost.updatedDate, "2026-09-24 03:33 PM" with no
# zone, so it is a last-updated date at day resolution, not a posting date.
# Links go to www.atlassian.com/company/careers/details/{id}, the page the
# site itself links, because the iCIMS portal URLs hit the captcha.
_ATL_SPACES = re.compile(r"\s+")


def _atlassian_date(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    from datetime import datetime as _datetime
    try:
        dt = _datetime.strptime(value.strip(), "%Y-%m-%d %I:%M %p")
    except ValueError:
        return dates.from_iso(value)
    return dates.from_iso(dt.isoformat())


def _atlassian_location(locs: Any) -> str:
    """Entries look like 'Bengaluru - India -   Bengaluru,  560071 India'."""
    out: list[str] = []
    for loc in locs or []:
        if not isinstance(loc, str):
            continue
        s = _ATL_SPACES.sub(" ", loc).strip()
        if s and s not in out:
            out.append(s)
    return "; ".join(out)


async def atlassian(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    host = row.get("host") or "www.atlassian.com"
    r = await c.get(f"https://{host}/endpoint/careers/listings", headers={
        **HEADERS, "Referer": f"https://{host}/company/careers/all-jobs"})
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError(f"atlassian: expected a JSON list, got {type(data).__name__}")
    out: list[Job] = []
    seen: set[str] = set()
    for j in data:
        if not isinstance(j, dict):
            continue
        post = j.get("portalJobPost") or {}
        jid = str(j.get("id") or post.get("id") or "")
        if not jid or jid in seen:
            continue
        seen.add(jid)
        posted, src = dates.pick(("updatedDate", _atlassian_date(post.get("updatedDate"))))
        out.append(Job(
            company=company,
            title=(j.get("title") or "").strip(),
            url=f"https://{host}/company/careers/details/{jid}",
            location=_atlassian_location(j.get("locations")),
            ats="atlassian",
            posted_at=posted,
            posted_source=src,
            raw_id=jid,
            department=j.get("category") or "",
        ))
    return out


# ------------------------------------------------------------------- shopify
# Shopify hires through Ashby but publishes no Ashby job board (the posting
# API and the hosted-board GraphQL both answer null for every slug tried).
# Its own careers page is a React Router app whose /careers route loader
# returns every published, listed posting. The single-fetch endpoint
# https://www.shopify.com/careers.data serves that loader data as a
# turbo-stream: one flat JSON array where objects are {"_<key index>":
# <value index>} and negative numbers encode null / undefined / NaN.
#
# Measured 2026-10-12: 114 postings (72 requisitions), all Published and
# listed, 114 unique ids. The discipline pages are strict subsets of it
# (engineering-data 49 = Engineering 44 + Data Insights 5, commercial 32).
# There is no total field anywhere to compare against.
#
# The loader also embeds the Ashby `job` record next to each posting,
# including the hiring team. Only posting fields and the public Discipline
# custom field are read here.
_TS_SPECIAL = {-1: None, -2: None, -3: None, -4: 0, -5: None, -6: None, -7: None}


def _turbo_decode(values: list) -> Any:
    """Inflate a React Router turbo-stream payload (the first line)."""
    memo: dict[int, Any] = {}

    def h(i: Any) -> Any:
        if not isinstance(i, int) or isinstance(i, bool):
            return i
        if i < 0:
            return _TS_SPECIAL.get(i)
        if i in memo:
            return memo[i]
        v = values[i]
        if isinstance(v, dict):
            obj: dict[str, Any] = {}
            memo[i] = obj
            for k, vi in v.items():
                key = values[int(k[1:])] if k.startswith("_") and k[1:].isdigit() else k
                obj[str(key)] = h(vi)
            return obj
        if isinstance(v, list):
            if v and isinstance(v[0], str):   # typed value (date, promise, ...)
                memo[i] = v
                return v
            arr: list[Any] = []
            memo[i] = arr
            arr.extend(h(x) for x in v)
            return arr
        memo[i] = v
        return v

    return h(0)


def _find_key(obj: Any, key: str, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _find_key(v, key, depth + 1)
            if found is not None:
                return found
    return None


def _shopify_place(loc: dict[str, Any]) -> str:
    addr = (loc.get("address") or {}).get("postalAddress") or {}
    city = (addr.get("addressLocality") or "").strip()
    if city:
        parts = [city, (addr.get("addressRegion") or "").strip(),
                 (addr.get("addressCountry") or "").strip()]
        return ", ".join(p for p in parts if p)
    return _ATL_SPACES.sub(" ", loc.get("externalName") or loc.get("name") or "").strip()


def _shopify_places(locs: list) -> dict[str, str]:
    """Map location id -> display string.

    81 of 114 postings sit on the bare region "Americas", which names no
    country and so fails the US-location gate. The board's own hierarchy
    says what a region covers: its child locations (Provo, Dallas, New York
    City, Toronto, ...) hang off a hierarchy node of the same name. A region
    with children is shown with their countries, "Americas (United States,
    Canada)", taken from the board rather than assumed.
    """
    locs = [x for x in locs if isinstance(x, dict) and x.get("id")]
    node_name = {x["id"]: (x.get("name") or "").strip() for x in locs}
    covers: dict[str, list[str]] = {}
    for x in locs:
        parent = node_name.get(x.get("parentLocationId") or "")
        country = (((x.get("address") or {}).get("postalAddress") or {})
                   .get("addressCountry") or "").strip()
        if parent and country and country != parent:
            covers.setdefault(parent, [])
            if country not in covers[parent]:
                covers[parent].append(country)
    out: dict[str, str] = {}
    for x in locs:
        if x.get("type") == "LocationHierarchy":
            continue
        place = _shopify_place(x)
        addr = (x.get("address") or {}).get("postalAddress") or {}
        if not addr.get("addressLocality") and covers.get(place):
            place = f"{place} ({', '.join(covers[place])})"
        out[x["id"]] = place
    return out


def _shopify_location(post: dict[str, Any], places: dict[str, str]) -> str:
    ids = post.get("locationIds") or {}
    names: list[str] = []
    for lid in [ids.get("primaryLocationId"), *(ids.get("secondaryLocationIds") or [])]:
        n = places.get(lid or "", "")
        if n and n not in names:
            names.append(n)
    if not names and post.get("locationName"):
        names = [_ATL_SPACES.sub(" ", post["locationName"]).strip()]
    if post.get("workplaceType") == "Remote":
        names = [f"Remote - {n}" for n in names] or ["Remote"]
    return "; ".join(names)


async def shopify(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    host = row.get("host") or "www.shopify.com"
    r = await c.get(f"https://{host}/careers.data", headers={
        **HEADERS, "Referer": f"https://{host}/careers"})
    r.raise_for_status()
    first = r.text.split("\n", 1)[0]
    data = _turbo_decode(json.loads(first))
    postings = _find_key(data, "jobPostingsWithJobs")
    if not isinstance(postings, list):
        raise ValueError("shopify: no jobPostingsWithJobs in the careers loader data")
    places = _shopify_places(_find_key(data, "atsLocations") or [])
    out: list[Job] = []
    seen: set[str] = set()
    for item in postings:
        p = (item or {}).get("jobPosting") or {}
        pid = str(p.get("id") or "")
        if not pid or pid in seen:
            continue
        if p.get("isListed") is False or p.get("status") not in (None, "Published"):
            continue
        seen.add(pid)
        discipline = next((f.get("valueLabel") or f.get("value")
                           for f in ((item.get("job") or {}).get("customFields") or [])
                           if isinstance(f, dict) and f.get("title") == "Discipline"
                           and not f.get("isPrivate")), None)
        posted, src = dates.pick(
            ("publishedDate", dates.from_iso(p.get("publishedDate"))),
            ("updatedAt", dates.from_iso(p.get("updatedAt"))),
        )
        out.append(Job(
            company=company,
            title=(p.get("title") or "").strip(),
            # redirects to /careers/{slug}_{id}
            url=p.get("externalLink") or f"https://{host}/careers?ashby_jid={pid}",
            location=_shopify_location(p, places),
            ats="shopify",
            posted_at=posted,
            posted_source=src,
            raw_id=pid,
            department=str(discipline or p.get("teamName") or ""),
        ))
    return out


# ------------------------------------------------------------------- avature
# Avature career portals (jobs.ea.com). The listing is server-rendered HTML
# at https://{host}/{site}/SearchJobs/?jobOffset=N, where `site` is the
# portal path, locale included ("en_US/careers"). Each result is an
# <article class="article--result"> holding the JobDetail link, whose last
# path segment is the posting id, and a subtitle of spans: list-item-location,
# list-item-id, list-item-workerType, list-item-department. The page states
# the board's total as "362 results".
#
# Measured on EA 2026-10-12: 20 cards a page whatever jobRecordsPerPage asks
# for, so the offset advances by the cards actually received; a plain sweep
# returned 362 unique ids for a total of 362, so no overlap is needed. The
# portal's RSS feed (SearchJobs/feed/) carries pubDate but ignores jobOffset
# and jobRecordsPerPage, always serving the same 20 items, so it cannot date
# the board and posted_at stays None. Not every Avature tenant serves this
# page: Intuit's portal 404s on search, which is why Intuit reads Radancy.
AVATURE_PAGE = 100          # requested; EA serves 20 regardless
AVATURE_DELAY = 0.4         # seconds between pages
AVATURE_MAX_PAGES = 500     # loop bound only; the board's total is the stop
_AVATURE_CARD = re.compile(r'<article\b[^>]*\barticle--result\b')
_AVATURE_LINK = re.compile(
    r'<a\b[^>]*href="([^"]*/JobDetail/[^"]*?/(\d+))"[^>]*>(.*?)</a>', re.S)
_AVATURE_TOTAL = re.compile(r"(\d[\d,.]*)\s+results?\b", re.I)


def _avature_span(cls: str, card: str) -> str:
    m = re.search(rf'class="{cls}"[^>]*>(.*?)</span>', card, re.S)
    if not m:
        return ""
    return _unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()


def _avature_cards(page: str) -> list[dict[str, str]]:
    cards = []
    for block in _AVATURE_CARD.split(page)[1:]:
        link = _AVATURE_LINK.search(block)
        if not link:
            continue
        cards.append({
            "url": _unescape(link.group(1)),
            "id": link.group(2),
            "title": _unescape(re.sub(r"<[^>]+>", "", link.group(3))).strip(),
            "location": _avature_span("list-item-location", block),
            "department": _avature_span("list-item-department", block),
        })
    return cards


async def avature(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    host, site = row.get("host", ""), (row.get("site") or "").strip("/")
    if not host or not site:
        raise ValueError("avature rows need host and site (the portal path, e.g. en_US/careers)")
    url = f"https://{host}/{site}/SearchJobs/"
    out: list[Job] = []
    seen: set[str] = set()
    offset, total = 0, None
    for page in range(AVATURE_MAX_PAGES):
        if page:
            await asyncio.sleep(AVATURE_DELAY)
        params: dict[str, Any] = {"jobOffset": offset, "jobRecordsPerPage": AVATURE_PAGE}
        if row.get("query"):
            params["search"] = row["query"]
        r = await c.get(url, params=params,
                        headers={**HEADERS, "Accept": "text/html,application/xhtml+xml"})
        r.raise_for_status()
        if total is None:
            m = _AVATURE_TOTAL.search(r.text)
            total = int(re.sub(r"\D", "", m.group(1))) if m else 0
            _board_total(row, total)
        cards = _avature_cards(r.text)
        fresh = 0
        for card in cards:
            if card["id"] in seen:
                continue
            seen.add(card["id"])
            fresh += 1
            out.append(Job(
                company=company,
                title=card["title"],
                url=card["url"],
                location=card["location"],
                ats="avature",
                raw_id=card["id"],
                department=card["department"],
            ))
        offset += len(cards)
        # A page of nothing new means the portal has run out (or is looping
        # on its last page); a reported total stops the sweep exactly.
        if not cards or not fresh or (total and len(seen) >= total):
            return out
    raise RuntimeError(f"avature sweep of {host} exceeded {AVATURE_MAX_PAGES} pages")


TIER_A = {
    "amazon": amazon,
    "atlassian": atlassian,
    "shopify": shopify,
    "avature": avature,
    "apple": apple,
    "meta": meta,
    "eightfold": eightfold,
    "google": google,
    "icims": icims,
    "ibm": ibm,
    "kula": kula,
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workday": workday,
    "oracle": oracle,
    "workable": workable,
    "recruitee": recruitee,
    "radancy": radancy,
    "tiktok": tiktok,
    "successfactors": successfactors,
}

TIER_B = {"taleo", "phenom", "custom"}

KNOWN = set(TIER_A) | TIER_B
