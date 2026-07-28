#!/usr/bin/env python3
"""Find a company's real job board API and prove it works.

Three ways in, from most to least information:

  1. Name plus careers URL. Fetches the page, reads redirects and DOM for
     board markers, then confirms the candidate against the live API.

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
TIER_B_HINTS = {
    "icims.com": "icims", "successfactors": "successfactors",
    "taleo.net": "taleo", "phenompeople": "phenom", "eightfold.ai": "eightfold",
    "avature.net": "avature",
}
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


# ------------------------------------------------------------------ resolve
async def resolve(client: httpx.AsyncClient, company: str, ticker: str, url: str,
                  ats_hint: str = "", auto: bool = False) -> dict[str, Any]:
    row = {c: "" for c in COLUMNS}
    row.update(company=company, ticker=ticker.upper(), careers_url=url)

    # ---- path 1: a URL to read -------------------------------------------
    haystack = ""
    if url:
        try:
            r = await client.get(url, headers=HEADERS)
            haystack = str(r.url) + "\n" + r.text[:500_000]
        except Exception as e:  # noqa: BLE001
            row["notes"] = f"fetch failed: {type(e).__name__}"

    if haystack:
        m = WORKDAY.search(haystack)
        if m:
            cand: dict[str, Any] = {**row, "ats": "workday", "tenant": m.group(1).lower(),
                    "wd": m.group(2).lower(), "site": m.group(3)}
            n = await confirm(client, "workday", cand)
            if n > 0:
                cand["notes"] = f"confirmed {n} jobs"
                return cand

        m = ORACLE.search(haystack)
        if m:
            host, site = m.group(1).lower(), m.group(2)
            cand: dict[str, Any] = {**row, "ats": "oracle", "host": host, "site": site}
            n = await confirm(client, "oracle", cand)
            if n > 0:
                cand["notes"] = f"confirmed {n} jobs"
                return cand
            # Oracle sites are numbered, so walk the neighbours
            for alt in (f"CX_{i}" for i in range(1, 8)):
                cand: dict[str, Any] = {**row, "ats": "oracle", "host": host, "site": alt}
                n = await confirm(client, "oracle", cand)
                if n > 0:
                    cand["notes"] = f"confirmed {n} jobs (site {alt})"
                    return cand

        # Every embed marker on the page, confirmed one at a time. Dead
        # footer embeds fail confirmation and get skipped.
        for ats, pat in EMBED:
            for m in pat.finditer(haystack):
                token = m.group(1)
                n = await confirm(client, ats, cast(dict[str, Any], {"token": token, "company": company}))
                if n > 0:
                    return {**row, "ats": ats, "token": token,
                            "notes": f"confirmed {n} jobs"}

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
    for hint, name in TIER_B_HINTS.items():
        if hint in haystack.lower():
            return {**row, "ats": name,
                    "host": httpx.URL(str(url)).host if url else "",
                    "notes": "tier B"}
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
