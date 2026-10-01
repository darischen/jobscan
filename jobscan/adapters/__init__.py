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
        r = await _apple_get(c, base, {"location": where, "sort": "newest",
                                       "page": page,
                                       **({"search": query} if query else {})})
        search = _apple_search(r.text)
        if total is None:
            total = int(search.get("totalRecords") or 0)
        results = search.get("searchResults") or []
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


TIER_A = {
    "amazon": amazon,
    "apple": apple,
    "meta": meta,
    "eightfold": eightfold,
    "google": google,
    "icims": icims,
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workday": workday,
    "oracle": oracle,
    "workable": workable,
    "recruitee": recruitee,
    "radancy": radancy,
}

TIER_B = {"successfactors", "taleo", "phenom", "avature", "custom"}

KNOWN = set(TIER_A) | TIER_B
