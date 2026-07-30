"""Tier A adapters. Pure HTTP JSON, safe to run in parallel.

Contract: every adapter is
    async def fn(client, company: str, row: dict) -> list[Job]
and raises on hard failure. The runner catches and records.
"""
from __future__ import annotations

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

# -------------------------------------------------------------------- amazon
# www.amazon.jobs exposes an open JSON search layer over their iCIMS
# backend. No auth. account.amazon.jobs is the application portal and does
# require a login, but nothing here touches it.
AMAZON_PAGE = 100          # server rejects result_limit > 100
AMAZON_CEILING = 10_000    # hits saturates here, so slice with `query`

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
    url = "https://www.amazon.jobs/en/search.json"
    query = row.get("query", "")
    out, offset, hits = [], 0, None
    while True:
        # No try/except here. Every other adapter lets failures reach
        # runner._one, which retries once and then records the error. Catching
        # and breaking turned a hard failure into a successful scan of zero
        # jobs, so a decoder bug read as "Amazon has no openings" for weeks.
        r = await c.get(url, headers=HEADERS, params={
            "base_query": query, "offset": offset,
            "result_limit": AMAZON_PAGE, "sort": "recent",
        })
        r.raise_for_status()
        d = r.json()
        if d.get("error"):
            raise ValueError(str(d["error"])[:120])
        if hits is None:
            hits = d.get("hits") or 0
        jobs = d.get("jobs") or []
        for j in jobs:
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
        offset += AMAZON_PAGE
        if not jobs or offset >= min(hits, AMAZON_CEILING):
            return out

# -------------------------------------------------------------------- google
# Google self-hosts. There is no REST endpoint. The results page is server
# rendered and embeds its job records in an AF_initDataCallback blob, which
# is Google's standard server-to-client data channel. That is still plain
# HTTP, so this stays Tier A: no browser, no model, deterministic parse.
_GOOG_BLOB = re.compile(r"AF_initDataCallback\((\{.*?\})\);", re.S)
_GOOG_DATA = re.compile(r"data:\s*(\[.*?\])\s*,\s*sideChannel", re.S)
_GOOG_SLUG = re.compile(r"[^a-z0-9]+")
_GOOG_TOTAL = re.compile(r"([\d,]+)\s+jobs?\s+matched", re.I)
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
                total = int(m.group(1).replace(",", ""))
        fresh = 0
        for j in _goog_records(r.text):
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
        if fresh == 0 or fresh < GOOGLE_PAGE_SIZE:
            break
        if total is not None and len(seen) >= total:
            break
    return out

# ---------------------------------------------------------------- greenhouse
async def greenhouse(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    token = row["token"]
    r = await c.get(
        f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
        params={"content": "true"}, headers=HEADERS,
    )
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        posted, src = dates.pick(
            ("first_published", dates.from_iso(j.get("first_published"))),
            ("updated_at", dates.from_iso(j.get("updated_at"))),
        )
        out.append(Job(
            company=company,
            title=j.get("title", ""),
            url=j.get("absolute_url", ""),
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
    out, offset, total = [], 0, None
    paths: list[str] = []   # parallel to `out`, for the raw_id repair below
    while True:  # Workday reports `total` only on the first page
        r = await c.post(
            url,
            headers={**HEADERS, "Content-Type": "application/json"},
            json={"appliedFacets": {}, "limit": 20, "offset": offset,
                  "searchText": row.get("query", "")},
        )
        r.raise_for_status()
        d = r.json()
        posts = d.get("jobPostings", [])
        if total is None:
            total = d.get("total") or 0
        for j in posts:
            path = j.get("externalPath", "")
            if not (j.get("title") or "").strip():
                continue  # Workday occasionally emits a titleless stub
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
        offset += 20
        if len(posts) < 20 or offset >= total or offset > 5000:
            return _workday_ids(out, paths)


# -------------------------------------------------------------------- oracle
async def oracle(c: httpx.AsyncClient, company: str, row: dict[str, Any]) -> list[Job]:
    host, site = row.get("host", ""), row.get("site", "")
    if not host or not site:
        raise ValueError("oracle rows need host and site columns")
    out, offset, total = [], 0, None
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
        reqs = items[0].get("requisitionList") or []
        for j in reqs:
            rid = j.get("Id", "")
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
        # Oracle sometimes returns 99 on a full page, so trust TotalJobsCount
        if not reqs or offset >= total or offset > 5000:
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


TIER_A = {
    "amazon": amazon,
    "eightfold": eightfold,
    "google": google,
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workday": workday,
    "oracle": oracle,
    "workable": workable,
    "recruitee": recruitee,
}

TIER_B = {"icims", "successfactors", "taleo", "phenom", "avature", "custom"}

KNOWN = set(TIER_A) | TIER_B
