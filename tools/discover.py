#!/usr/bin/env python3
"""Find a company's real job board API and prove it works.

Three ways in, from most to least information:

  1. Name plus careers URL. Fetches the page, reads redirects and DOM for
     board markers, then confirms the candidate against the live API. A
     branded front end (Phenom, Taleo, Avature skins) is followed one hop
     to its listing or apply links, so the board behind it is what gets
     confirmed.

  2. Name plus an ATS hint, no URL. Generates slug candidates from the
     name and probes the ATS directly.

        python tools/discover.py "Snowflake" --ats greenhouse

  3. Name only. Probes every token-based ATS with every slug candidate.

        python tools/discover.py "Instacart" --auto

Nothing is ever emitted on a pattern match alone. Every row printed here
was confirmed by calling the board's own API and counting real jobs. A
regex that finds "greenhouse.io/acme" on a page proves nothing, because
companies leave dead embeds in their footers for years.

    python tools/discover.py "Snowflake" -t SNOW https://careers.snowflake.com/us/en
    python tools/discover.py --batch pending.txt >> registry/companies.csv
    python tools/discover.py --batch pending.txt --auto
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from jobscan.adapters import HEADERS, TIER_A  # noqa: E402
from jobscan.registry import COLUMNS  # noqa: E402

# ---------------------------------------------------------------- patterns
EMBED = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_(?:board|app)\?(?:for=)?|v1/boards/)([a-z0-9_-]{2,})", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/v1/boards/([a-z0-9_-]{2,})", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([a-z0-9_-]{2,})", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_.-]{2,})", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]{2,})", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([a-z0-9_-]{2,})", re.I)),
    ("recruitee", re.compile(r"([a-z0-9-]{2,})\.recruitee\.com", re.I)),
]
WORKDAY = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:wday/cxs/[a-z0-9-]+/)?"
    r"(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)
ORACLE = re.compile(
    r"https?://([a-z0-9.-]*oraclecloud\.com)/hcmUI/CandidateExperience/"
    r"[^/]+/sites/([A-Za-z0-9_-]+)", re.I)
# Shared-cluster Workday tenants: the tenant sits in the path, not the
# subdomain (Wells Fargo, Fidelity). The adapter selects this shape by `host`.
WORKDAY_SITE = re.compile(
    r"https?://((wd\d+)\.myworkdaysite\.com)/(?:[a-z]{2}-[A-Z]{2}/)?"
    r"recruiting/([a-z0-9_-]+)/([A-Za-z0-9_-]+)", re.I)

# ---- site-hosted families ------------------------------------------------
# These boards are served from the company's own careers host, so the row is
# `host` (plus `site` / `query` for some) rather than a token. A marker only
# nominates a candidate; site_candidates() never decides anything.
KULA = re.compile(r"careers\.kula\.ai/([A-Za-z0-9][A-Za-z0-9_-]+)", re.I)
KULA_SKIP = {"api", "_next", "assets", "static", "images", "fonts"}
RADANCY_MARKS = ("tbcdn.talentbrew.com", "talentbrew", "tbcdn")
RADANCY_WEAK = re.compile(r"""["'/]search-jobs(?:["'/?]|$)""", re.I | re.M)
# careers-home is the Jibe front end iCIMS bought; its assets sit on jibecdn.
ICIMS_MARKS = ("careers-home", "jibecdn", "_jibe", "jibeapply")
EIGHTFOLD_MARKS = ("eightfold", "pcsx-", "/api/pcsx/", "vscdn.net")
EIGHTFOLD_SKIP = {"www.eightfold.ai", "app.eightfold.ai", "static.eightfold.ai"}
# The careers page embeds its config as entity-escaped JSON; "domain" is the
# value /api/pcsx/search wants. customDomain and friends are other keys, so
# the opening quote is part of the match and the key is case sensitive.
_Q = r"""(?:&#34;|&quot;|\\?["'])"""
EIGHTFOLD_DOMAIN = re.compile(
    _Q + r"domain" + _Q + r"\s*:\s*" + _Q + r"([a-z0-9-]+(?:\.[a-z0-9-]+)+)" + _Q)
SF_MARKS = ("rmkcdn.successfactors", "jobs2web.com", "successfactors")
# jobs.ea.com never says avature.net: assets come from avacdn.net and the
# portal announces itself in <meta name="avature.portal.*">.
AVATURE_MARKS = ("avature.net", "avacdn.net", "avature.portal")
AVATURE_SEARCH = re.compile(
    r"""(?:https?://([a-z0-9.-]+))?/([A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)?)/SearchJobs\b""")
AVATURE_META = re.compile(
    r'avature\.portal\.(urlPath|lang)"\s+content="([^"]+)"')

# Labels for what the page looks like. Not a verdict: Cisco's Phenom site
# applies through Workday, UnitedHealth's Taleo and Intuit's Avature are both
# Radancy, Schwab's iCIMS is Radancy. A label reaches the row only when
# nothing confirms: taleo and phenom as the tier B `ats`, the rest (which
# have tier A adapters now) as a note on a `custom` row.
FRONT_HINTS = {
    "icims.com": "icims", "successfactors": "successfactors",
    "taleo.net": "taleo", "phenompeople": "phenom", "eightfold.ai": "eightfold",
    "avature.net": "avature", "avacdn.net": "avature",
    "avature.portal": "avature",
}
TIER_B_HINTS = FRONT_HINTS      # old name, kept for callers
FRONTS = {"taleo", "phenom", "avature", "icims"}

# One hop from the careers page to where its jobs or apply links live.
FOLLOW_MAX = 3
FOLLOW_DELAY = 1.0      # seconds before each followed page
FOLLOW_BACKOFF = 8.0    # seconds, once, after a 429 or 503
_HREF = re.compile(r"""href=["']([^"'#]+)["']""", re.I)
_LISTING = re.compile(
    r"(search-results|search-jobs|searchjobs|job-search|jobsearch"
    r"|/search/?$|/jobs/?$|/careers/?$|/openings|/positions|/opportunities)", re.I)
_JOB = re.compile(r"/(?:job|jobs|jobdetail|position)/[^/]+", re.I)
_BOARD_HOSTS = (".eightfold.ai", ".avature.net")
_ASSET = re.compile(r"\.(?:css|js|png|jpe?g|gif|svg|ico|woff2?|pdf|xml|json)(?:\?|$)", re.I)
TOKEN_ATS = ("greenhouse", "lever", "ashby", "smartrecruiters",
             "workable", "recruitee")
STOPWORDS = {"inc", "corp", "corporation", "co", "ltd", "llc", "the",
             "technologies", "technology", "labs", "group", "holdings", "plc"}


def slugs(name: str) -> list[tuple[str, bool]]:
    """Candidate board tokens, most likely first.

    Returns (token, exact) pairs. `exact` is False for truncations like
    "applied" from "Applied Materials", which are the ones that collide
    with unrelated companies holding the same short slug.
    """
    base = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    core = [w for w in base if w not in STOPWORDS] or base
    joined = "".join(core)
    out = [(joined, True), ("-".join(core), True),
           (joined + "usa", True), (joined + "inc", True),
           (joined + "careers", True), (joined + "hq", True)]
    if len(core) > 1:
        out.append(("".join(core[:2]), False))
        out.append((core[0], False))
    seen: set[str] = set()
    uniq: list[tuple[str, bool]] = []
    for tok, exact in out:
        if tok and tok not in seen:
            seen.add(tok)
            uniq.append((tok, exact))
    return uniq


def _uniq(items: list[Any]) -> list[Any]:
    seen: set[str] = set()
    out: list[Any] = []
    for it in items:
        key = repr(it)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


def eightfold_domains(text: str, host: str) -> list[str]:
    """Tenant domains to try with /api/pcsx/search, likeliest first.

    The page's own "domain" value wins, most frequent first (Microsoft's
    page says microsoft.com twice and careers.microsoft.com once). The
    guess after it costs one request and is confirmed like any other
    candidate.
    """
    found = [m.group(1).lower() for m in EIGHTFOLD_DOMAIN.finditer(text)]
    ranked = sorted(_uniq(found), key=lambda d: -found.count(d))
    labels = host.lower().split(".")
    if host.lower().endswith(".eightfold.ai"):
        ranked.append(f"{labels[0]}.com")
    elif len(labels) >= 2:
        ranked.append(".".join(labels[-2:]))
    return _uniq(ranked)


def avature_sites(text: str, url: str) -> list[str]:
    """Portal paths (en_US/careers) an Avature page offers, likeliest first."""
    u = httpx.URL(url)
    out: list[str] = []
    for m in AVATURE_SEARCH.finditer(url + "\n" + text):
        if m.group(1) in (None, u.host):
            out.append(m.group(2))
    meta = dict(AVATURE_META.findall(text))
    if meta.get("urlPath"):
        out.append("/".join(x for x in (meta.get("lang"), meta["urlPath"]) if x))
    parts = [p for p in u.path.split("/") if p]
    if parts:
        out.append("/".join(parts[:2]))
    return _uniq(out)


def site_candidates(haystack: str, url: str) -> list[tuple[str, dict[str, str]]]:
    """Rows worth confirming for boards served from the careers host itself.

    Pure: reads markers, returns (ats, config) pairs in the order to try
    them. Radancy leads because it is the family that fronts the others
    (Taleo at UnitedHealth, Avature at Intuit, iCIMS at Schwab), so a page
    carrying both sets of markers is a TalentBrew site first.
    """
    low = haystack.lower()
    host = httpx.URL(url).host.lower() if url else ""
    out: list[tuple[str, dict[str, str]]] = []

    accounts = _uniq([m.group(1) for m in KULA.finditer(haystack)
                      if m.group(1).lower() not in KULA_SKIP])
    for account in accounts[:3]:
        out.append(("kula", {"token": account, "host": "careers.kula.ai"}))
    if not host:
        return out

    if any(k in low for k in RADANCY_MARKS):
        out.append(("radancy", {"host": host}))

    # Only the careers-home site has the open /api/jobs. A classic
    # *.icims.com portal answers 405 with an AWS WAF captcha, so it is never
    # a candidate, whatever the page says about iCIMS.
    if any(k in low for k in ICIMS_MARKS) and not host.endswith(".icims.com"):
        out.append(("icims", {"host": host}))

    if any(k in low for k in EIGHTFOLD_MARKS) or host.endswith(".eightfold.ai"):
        # Plain row first: Netflix still answers /api/apply/v2/jobs. A
        # PCSX-only tenant refuses that with a 403 on the first request.
        out.append(("eightfold", {"host": host}))
        for domain in eightfold_domains(haystack, host)[:3]:
            out.append(("eightfold", {"host": host, "site": "pcsx", "query": domain}))

    if any(k in low for k in SF_MARKS):
        # Classic listing first. On a unify site the adapter raises (or
        # finds no rows), and site=unify is then the row that confirms.
        out.append(("successfactors", {"host": host}))
        out.append(("successfactors", {"host": host, "site": "unify"}))

    if any(k in low for k in AVATURE_MARKS):
        for site in avature_sites(haystack, url)[:3]:
            out.append(("avature", {"host": host, "site": site}))

    # Weak: plenty of non-Radancy sites have a /search-jobs path (AMD's
    # careers-home does), so this is tried last and only costs one request.
    if RADANCY_WEAK.search(haystack):
        out.append(("radancy", {"host": host}))
    return _uniq(out)


def follow_links(text: str, url: str) -> list[str]:
    """Pages one hop away that may show the board behind a front end.

    Listing pages on the same host first (Phenom's search-results embeds
    every job's Workday applyUrl), then one job page, then links out to a
    hosted board (morganstanley.com links to morganstanley.eightfold.ai).
    """
    base = httpx.URL(url)
    # A relative href on a page served without a trailing slash is meant
    # against that page as a directory: careers.cisco.com/global/en links
    # "search-results" and means /global/en/search-results.
    last = base.path.rsplit("/", 1)[-1]
    if base.path and not base.path.endswith("/") and "." not in last:
        base = base.copy_with(path=base.path + "/")
    listing: list[str] = []
    job: list[str] = []
    boards: list[str] = []
    for href in _HREF.findall(text):
        href = href.replace("&amp;", "&").strip()
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        try:
            u = base.join(href)
        except Exception:  # noqa: BLE001
            continue
        if u.scheme not in ("http", "https") or _ASSET.search(u.path):
            continue
        full = str(u)
        if full.rstrip("/") == url.rstrip("/"):
            continue
        if u.host == base.host:
            if _LISTING.search(u.path):
                listing.append(full)
            elif _JOB.search(u.path):
                job.append(full)
        elif u.host.endswith(_BOARD_HOSTS) and u.host not in EIGHTFOLD_SKIP:
            boards.append(full)
    listing, job, boards = _uniq(listing), _uniq(job), _uniq(boards)
    return _uniq(listing[:2] + job[:1] + boards[:2] + listing[2:])[:FOLLOW_MAX]


def front_label(haystack: str) -> str:
    """What the page is dressed as, for the note and the tier B fallback."""
    low = haystack.lower()
    for hint, name in FRONT_HINTS.items():
        if hint in low:
            return name
    return ""


# ------------------------------------------------------------- confirmation
async def confirm(client: httpx.AsyncClient, ats: str, row: dict[str, Any]) -> int:
    """Call the real adapter. Return the job count, or -1 on failure."""
    fn = TIER_A.get(ats)
    if not fn:
        return -1
    try:
        jobs = await fn(client, row.get("company", "probe"), row)
    except Exception:  # noqa: BLE001
        return -1
    return len(jobs)


MIN_PLAUSIBLE = 5


def confidence(token: str, exact: bool, n: int) -> str:
    """Flag matches a human should eyeball before committing."""
    flags: list[str] = []
    if not exact:
        flags.append(f"partial-name slug '{token}'")
    if n < MIN_PLAUSIBLE:
        flags.append(f"only {n} jobs")
    return ("VERIFY: " + ", ".join(flags)) if flags else ""


async def probe_tokens(client: httpx.AsyncClient, name: str, only: str | None = None) -> tuple[str | None, str | None, int, bool]:
    """Brute-force slug candidates against token-based ATS APIs.

    Exact-name candidates are tried first across every ATS before any
    truncation is attempted, so "applifyinc" beats "appl" every time.
    """
    targets = [only] if only in TOKEN_ATS else list(TOKEN_ATS)
    cands = slugs(name)
    for want_exact in (True, False):
        for token, exact in cands:
            if exact != want_exact:
                continue
            for ats in targets:
                n = await confirm(client, ats, cast(dict[str, Any], {"token": token, "company": name}))
                if n > 0:
                    return ats, token, n, exact
    return None, None, 0, True


async def probe_workday(client: httpx.AsyncClient, name: str) -> dict[str, Any]:
    """Find a Workday tenant, cluster, and site with no URL to start from.

    The tenant root redirects to its default site, so one GET per
    (slug, cluster) pair yields the site id for free.
    """
    for slug, _exact in slugs(name)[:3]:
        for wd in ("wd1", "wd5", "wd3", "wd12", "wd2", "wd10", "wd101", "wd103"):
            url = f"https://{slug}.{wd}.myworkdayjobs.com/"
            try:
                r = await client.get(url, headers=HEADERS, timeout=12)
            except Exception:  # noqa: BLE001
                continue
            if r.status_code >= 400:
                continue
            m = WORKDAY.search(str(r.url)) or WORKDAY.search(r.text[:200_000])
            if not m:
                continue
            row: dict[str, Any] = {"tenant": m.group(1).lower(), "wd": m.group(2).lower(),
                   "site": m.group(3), "company": name}
            if await confirm(client, "workday", row) > 0:
                return row
    return {}


# ---------------------------------------------------------------- one page
def page_text(final_url: str, body: str) -> str:
    """The text markers are read from: where we landed, then the page.

    JSON embedded in a page may escape its slashes, and an apply link
    written as https:\\/\\/cisco.wd5.myworkdayjobs.com\\/Cisco_Careers would
    slip past every URL pattern unless they are put back.
    """
    return final_url + "\n" + body[:500_000].replace("\\/", "/")


async def fetch_page(client: httpx.AsyncClient, url: str) -> tuple[str, str] | None:
    """A followed page as (final url, text), or None. One retry on 429/503."""
    for attempt in range(2):
        await asyncio.sleep(FOLLOW_BACKOFF if attempt else FOLLOW_DELAY)
        try:
            r = await client.get(url, headers=HEADERS)
        except Exception:  # noqa: BLE001
            return None
        if r.status_code in (429, 503):
            continue
        if r.status_code >= 400:
            return None
        return str(r.url), page_text(str(r.url), r.text)
    return None


def _note(n: int, ats: str, front: str, via: str, extra: str = "") -> str:
    note = f"confirmed {n} jobs" + (f" ({extra})" if extra else "")
    # Only the labels seen fronting another board. Microsoft's eightfold
    # page mentions successfactors twice and is not a skin over anything.
    if front in FRONTS and front != ats:
        note += f"; {front} front end over {ats}"
    if via:
        note += f"; found via {via}"
    return note


async def from_page(client: httpx.AsyncClient, row: dict[str, Any], haystack: str,
                    url: str, front: str = "", via: str = "") -> dict[str, Any]:
    """Confirm the first board this page's markers point at, or return {}.

    `front` is the label of the page that led here, when this page was
    reached by following a link; otherwise the page labels itself.
    """
    company = row["company"]
    front = front or front_label(haystack)

    m = WORKDAY.search(haystack)
    if m:
        cand: dict[str, Any] = {**row, "ats": "workday", "tenant": m.group(1).lower(),
                "wd": m.group(2).lower(), "site": m.group(3)}
        n = await confirm(client, "workday", cand)
        if n > 0:
            cand["notes"] = _note(n, "workday", front, via)
            return cand

    m = WORKDAY_SITE.search(haystack)
    if m:
        cand = {**row, "ats": "workday", "host": m.group(1).lower(),
                "wd": m.group(2).lower(), "tenant": m.group(3), "site": m.group(4)}
        n = await confirm(client, "workday", cand)
        if n > 0:
            cand["notes"] = _note(n, "workday", front, via)
            return cand

    m = ORACLE.search(haystack)
    if m:
        host, site = m.group(1).lower(), m.group(2)
        cand = {**row, "ats": "oracle", "host": host, "site": site}
        n = await confirm(client, "oracle", cand)
        if n > 0:
            cand["notes"] = _note(n, "oracle", front, via)
            return cand
        # Oracle sites are numbered, so walk the neighbours
        for alt in (f"CX_{i}" for i in range(1, 8)):
            cand = {**row, "ats": "oracle", "host": host, "site": alt}
            n = await confirm(client, "oracle", cand)
            if n > 0:
                cand["notes"] = _note(n, "oracle", front, via, f"site {alt}")
                return cand

    # Every embed marker on the page, confirmed one at a time. Dead
    # footer embeds fail confirmation and get skipped.
    for ats, pat in EMBED:
        for m in pat.finditer(haystack):
            token = m.group(1)
            n = await confirm(client, ats, cast(dict[str, Any], {"token": token, "company": company}))
            if n > 0:
                return {**row, "ats": ats, "token": token,
                        "notes": _note(n, ats, front, via)}

    # Boards served from the careers host. Same rule: the adapter has to
    # return jobs for the exact row that would be written.
    for ats, cfg in site_candidates(haystack, url):
        cand = {**row, "ats": ats, **cfg}
        n = await confirm(client, ats, cand)
        if n > 0:
            note = _note(n, ats, front, via)
            if n < MIN_PLAUSIBLE:
                note = f"VERIFY: only {n} jobs | {note}"
            cand["notes"] = note
            return cand
    return {}


# ------------------------------------------------------------------ resolve
async def resolve(client: httpx.AsyncClient, company: str, ticker: str, url: str,
                  ats_hint: str = "", auto: bool = False) -> dict[str, Any]:
    row = {c: "" for c in COLUMNS}
    row.update(company=company, ticker=ticker.upper(), careers_url=url)

    # ---- path 1: a URL to read -------------------------------------------
    haystack, final_url = "", url
    if url:
        try:
            r = await client.get(url, headers=HEADERS)
            final_url = str(r.url)
            haystack = page_text(final_url, r.text)
        except Exception as e:  # noqa: BLE001
            row["notes"] = f"fetch failed: {type(e).__name__}"

    if haystack:
        hit = await from_page(client, row, haystack, final_url)
        if hit:
            return hit
        # Nothing on the page itself confirmed. Before calling it tier B,
        # look one hop in: a front end's listing or job page is where the
        # real board's links are.
        for link in follow_links(haystack, final_url):
            page = await fetch_page(client, link)
            if not page:
                continue
            hit = await from_page(client, row, page[1], page[0],
                                  front=front_label(haystack), via=link)
            if hit:
                return hit

    # ---- path 2: an ATS hint, no usable URL ------------------------------
    if ats_hint in TOKEN_ATS:
        ats, token, n, exact = await probe_tokens(client, company, only=ats_hint)
        if token:
            note = f"probed, confirmed {n} jobs"
            flag = confidence(token, exact, n)
            return {**row, "ats": ats, "token": token,
                    "notes": f"{flag} | {note}" if flag else note}
    if ats_hint == "workday":
        wd = await probe_workday(client, company)
        if wd:
            return {**row, "ats": "workday", "tenant": wd["tenant"],
                    "wd": wd["wd"], "site": wd["site"], "notes": "probed"}

    # ---- path 3: nothing but a name --------------------------------------
    if auto or not url:
        ats, token, n, exact = await probe_tokens(client, company)
        if token:
            note = f"auto-probed, confirmed {n} jobs"
            flag = confidence(token, exact, n)
            return {**row, "ats": ats, "token": token,
                    "notes": f"{flag} | {note}" if flag else note}
        wd = await probe_workday(client, company)
        if wd:
            return {**row, "ats": "workday", "tenant": wd["tenant"],
                    "wd": wd["wd"], "site": wd["site"], "notes": "auto-probed"}

    # ---- tier B fallback --------------------------------------------------
    # Reached only when nothing confirmed. A family with a tier A adapter
    # must not keep its name here: `ats=icims` on an unconfirmed row would
    # read as a working board to the registry and to the summary below.
    # The classic *.icims.com portals (405, AWS WAF captcha) land here.
    label = front_label(haystack)
    if label:
        host = httpx.URL(str(url)).host if url else ""
        if label in TIER_A:
            return {**row, "ats": "custom", "host": host,
                    "notes": f"tier B; {label} markers but its API did not confirm"}
        return {**row, "ats": label, "host": host, "notes": "tier B"}
    row.update(ats="custom",
               host=httpx.URL(str(url)).host if url else "",
               notes=row.get("notes") or "tier B, unidentified")
    return row


def row_to_csv(d: dict[str, Any]) -> str:
    return ",".join((d.get(c, "") or "").replace(",", " ") for c in COLUMNS)


def parse_batch(path: str) -> list[tuple[str, str, str]]:
    pairs: list[tuple[str, str, str]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"\t|\s{2,}|,", line) if p.strip()]
        if not parts:
            continue
        name = parts[0]
        ticker = parts[1] if len(parts) > 1 and not parts[1].startswith("http") else ""
        url = next((p for p in parts if p.startswith("http")), "")
        pairs.append((name, ticker, url))
    return pairs


async def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("company", nargs="?")
    ap.add_argument("url", nargs="?", default="",
                    help="careers page URL, optional")
    ap.add_argument("--ticker", "-t", default="")
    ap.add_argument("--ats", default="", help="hint: greenhouse, lever, ashby, "
                                              "smartrecruiters, workable, "
                                              "recruitee, workday")
    ap.add_argument("--auto", action="store_true",
                    help="probe every ATS even when a URL was given")
    ap.add_argument("--batch", "-b", help="file of 'Name[<TAB>TICKER][<TAB>url]'")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="keep low, probing sends many requests per company")
    ap.add_argument("--resume", metavar="PATH",
                    help="append rows to PATH as they finish and skip "
                         "companies already in it. Survives interruption.")
    # parse_intermixed_args so `"Name" -t TICK https://url` works. Plain
    # parse_args refuses two optional positionals split by a flag.
    a = ap.parse_intermixed_args()

    if a.batch:
        pairs = parse_batch(a.batch)
    elif a.company:
        pairs = [(a.company, a.ticker, a.url)]
    else:
        ap.error("give a company name, or --batch FILE")

    ckpt = Path(a.resume) if a.resume else None
    done: set[str] = set()
    rows: list[dict[str, Any]] = []
    if ckpt and ckpt.exists():
        for line in ckpt.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("company,"):
                done.add(line.split(",", 1)[0].strip().lower())
        pairs = [p for p in pairs if p[0].strip().lower() not in done]
        print(f"# resuming, {len(done)} already done, {len(pairs)} to go",
              file=sys.stderr)
    if ckpt and not ckpt.exists():
        ckpt.write_text(",".join(COLUMNS) + "\n", encoding="utf-8")

    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
        sem = asyncio.Semaphore(a.concurrency)
        lock = asyncio.Lock()
        n_done = [0]

        async def go(name: str, tk: str, u: str) -> dict[str, Any]:
            async with sem:
                r = await resolve(c, name, tk, u, a.ats, a.auto)
            async with lock:
                n_done[0] += 1
                if ckpt:
                    with ckpt.open("a", encoding="utf-8") as f:
                        f.write(row_to_csv(r) + "\n")
                tag = ("?? " if r["notes"].startswith("VERIFY")
                       else "OK " if r["ats"] in TIER_A else "-- ")
                print(f"# [{n_done[0]}/{len(pairs)}] {tag}{r['company']} "
                      f"{r['ats']}", file=sys.stderr, flush=True)
            return r

        rows = await asyncio.gather(*(go(*p) for p in pairs))

    if not ckpt:
        print(",".join(COLUMNS))
        for r in rows:
            print(row_to_csv(r))
    else:
        print(f"# rows in {ckpt}", file=sys.stderr)

    ok = [r for r in rows if r["ats"] in TIER_A]
    tb = [r for r in rows if r["ats"] not in TIER_A]
    print(f"\n# {len(ok)}/{len(rows)} confirmed against a live API", file=sys.stderr)
    if tb:
        print(f"# needs tier B or manual work: "
              f"{', '.join(r['company'] for r in tb)}", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
