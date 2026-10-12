#!/usr/bin/env python3
"""Prove a job board is actually healthy, not merely reachable.

"It returned 200" is a weak check. A board can return 200 with an empty
list, a truncated page, duplicated ids, or dead links. This runs seven
checks per row and stores a baseline so the next run detects drift.

  reachable    the adapter completed without raising
  populated    job count > 0
  paginated    count matches the board's own total, so pagination finished
  unique       no duplicate requisition ids
  titled       no blank titles
  dated        share of postings carrying a real board timestamp; warns when
               it falls against the baseline, not on boards that never date
  live         a random sample of job URLs returns 2xx; a 404 is dead, a
               5xx, rate limit or timeout is unreachable, not dead

    python tools/verify.py
    python tools/verify.py --company nvidia --sample 10
    python tools/verify.py --strict          exit 1 on any warning
    python tools/verify.py --baseline        rewrite registry/health.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from jobscan import registry  # noqa: E402
from jobscan.adapters import HEADERS, TIER_A  # noqa: E402
from jobscan.core import classify  # noqa: E402

HEALTH = Path(__file__).resolve().parents[1] / "registry" / "health.json"
DEFAULT_REGISTRY = "registry/companies.csv"
DRIFT = 0.40  # a 40% swing in job count against baseline is worth a look
# Boards change while they are read: JPMorgan came back 7,301 of 7,302 and
# Microsoft 2,236 of 2,237. A gap is truncation only beyond this slack.
DATED_DROP = 20          # percentage points below the baseline's dated share
PAGINATED_SLACK = 2      # postings
PAGINATED_PCT = 0.01     # of the board total, whichever is larger


class CheckResult(TypedDict):
    company: str
    key: str
    ticker: str
    ats: str
    status: str
    warnings: list[str]
    count: int
    dated_pct: int
    live: str
    matches: int


def load_health() -> dict[str, Any]:
    return json.loads(HEALTH.read_text(encoding="utf-8")) if HEALTH.exists() else {}


def merge_baseline(old: dict[str, Any], snap: dict[str, Any],
                   live_keys: set[str], full_run: bool) -> dict[str, Any]:
    """Today's snapshot over the old baseline, minus keys with no row.

    The file only ever grew: a row removed from the registry, or rekeyed
    when its company gained a second row ("SAP" -> "SAP [careers.sap.com]"),
    left its old key behind for good. A full run knows every live key, so it
    drops the rest. A row that failed today keeps its old baseline, since it
    is still in the registry and the next run needs something to compare to.
    A --company run sees only part of the registry and prunes nothing.
    """
    merged = {**old, **snap}
    if full_run:
        merged = {k: v for k, v in merged.items() if k in live_keys}
    return merged


def baseline_keys(rows: list[dict[str, Any]]) -> dict[int, str]:
    """Map each row's id() to its health.json key.

    Keying on company name alone breaks any company holding several rows.
    Google's four query slices return 145 to 1180 jobs each and all wrote to
    the key "Google", so every run compared one slice against whichever slice
    saved last and reported drift that was not there.

    Only companies with more than one row get a discriminator, so the 70-odd
    single-row baselines already on disk keep working.
    """
    counts = Counter(r["company"] for r in rows)
    out: dict[int, str] = {}
    for r in rows:
        key = r["company"]
        if counts[key] > 1:
            disc = (r.get("query") or r.get("token") or r.get("site")
                    or r.get("tenant") or r.get("host") or "")
            if disc:
                key = f"{key} [{disc}]"
        out[id(r)] = key
    return out


class LiveSample(NamedTuple):
    ok: int
    dead: int
    unreachable: int
    tried: int


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

# The server declined to answer, which says nothing about the posting. 403 is
# here because bot shields (Cloudflare on coinbase.com) send it to clients
# they dislike while the page loads fine in a browser.
_TRANSIENT = {403, 408, 425, 429}


async def sample_live(client: httpx.AsyncClient, jobs: list[Any], n: int,
                      retry_delay: float = 3.0) -> LiveSample:
    """Spot-check that job URLs resolve. Catches boards serving dead links.

    Dead means the page is gone: a 4xx such as 404 or 410. A 5xx, rate limit,
    bot block or network error is unreachable instead, after one retry. On
    2026-10-01 a 503 burst from a throttled IP read as every link being dead.
    """
    urls = [j.url for j in jobs if j.url.startswith("http")]
    if not urls or n <= 0:
        return LiveSample(0, 0, 0, 0)
    picks = random.sample(urls, min(n, len(urls)))
    ok = dead = unreachable = 0
    for u in picks:
        for attempt in range(2):
            try:
                r = await client.get(u, headers=NAVIGATE, timeout=20)
                code = r.status_code
            except httpx.HTTPError:
                code = None
            if code is not None and code < 400:
                ok += 1
                break
            if code is not None and code < 500 and code not in _TRANSIENT:
                dead += 1
                break
            if attempt == 0:
                await asyncio.sleep(retry_delay)
            else:
                unreachable += 1
    return LiveSample(ok, dead, unreachable, len(picks))


async def check(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict[str, Any], sample: int, baseline: dict[str, Any], key: str | None = None) -> CheckResult:
    name, ats = row["company"], row["ats"]
    res: CheckResult = {"company": name, "key": key or name,
           "ticker": row.get("ticker", ""), "ats": ats,
           "status": "ok", "warnings": [], "count": 0, "dated_pct": 0,
           "live": "", "matches": 0}
    async with sem:
        try:
            row.pop("_board_total", None)
            jobs = await TIER_A[ats](client, name, row)
        except httpx.HTTPStatusError as e:
            res.update(status="FAIL", warnings=[f"HTTP {e.response.status_code}"])
            return res
        except Exception as e:  # noqa: BLE001
            res.update(status="FAIL",
                       warnings=[f"{type(e).__name__}: {str(e)[:60]}"])
            return res

        res["count"] = len(jobs)
        if not jobs:
            res.update(status="FAIL", warnings=["board returned zero jobs"])
            return res

        # Adapters record the board's own count on the row as they read it.
        total = row.get("_board_total")
        if total and abs(len(jobs) - total) > max(PAGINATED_SLACK,
                                                  total * PAGINATED_PCT):
            res["warnings"].append(f"{len(jobs)} of board total {total} "
                                   f"(pagination incomplete?)")

        ids = [j.raw_id or j.url for j in jobs]
        dupes = [k for k, v in Counter(ids).items() if v > 1]
        if dupes:
            res["warnings"].append(f"{len(dupes)} duplicate ids "
                                   f"(pagination overlap?)")
        blanks = sum(1 for j in jobs if not j.title.strip())
        if blanks:
            res["warnings"].append(f"{blanks} blank titles (parser drift?)")

        dated = sum(1 for j in jobs if j.posted_at)
        res["dated_pct"] = dated * 100 // len(jobs)
        # Some boards never publish a date (radancy, meta, avature, two
        # successfactors tenants). Once the baseline records that, staying
        # undated is not news; losing dates the board used to carry is.
        was = baseline.get(res["key"], {}).get("dated")
        if was is None:
            if res["dated_pct"] < 50:
                res["warnings"].append(f"only {res['dated_pct']}% carry a date")
        elif res["dated_pct"] < was - DATED_DROP:
            res["warnings"].append(
                f"dated share fell {was}% -> {res['dated_pct']}%")

        res["matches"] = sum(1 for j in jobs
                             if classify(j.title)[0] in ("explicit_early", "unleveled"))

        if sample:
            s = await sample_live(client, jobs, sample)
            res["live"] = f"{s.ok}/{s.tried}"
            if s.dead:
                res["warnings"].append(f"{s.dead}/{s.tried} sampled URLs dead")
            if s.unreachable:
                res["warnings"].append(
                    f"{s.unreachable}/{s.tried} sampled URLs unreachable "
                    f"(5xx, rate limit, bot block or network; not counted dead)")

        prev = baseline.get(res["key"], {}).get("count")
        if prev:
            swing = abs(len(jobs) - prev) / prev
            if swing > DRIFT:
                res["warnings"].append(
                    f"count moved {prev} -> {len(jobs)} ({swing * 100:.0f}%)")

    if res["warnings"] and res["status"] == "ok":
        res["status"] = "WARN"
    return res


async def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", "-f", default=DEFAULT_REGISTRY)
    ap.add_argument("--company", "-c")
    ap.add_argument("--sample", type=int, default=3,
                    help="job URLs to spot-check per board, 0 to skip")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--strict", action="store_true", help="exit 1 on any warning")
    ap.add_argument("--baseline", action="store_true",
                    help="write this run to registry/health.json as the new baseline")
    a = ap.parse_args()

    rows = registry.select(registry.load(a.file), company=a.company)
    tier_a, tier_b = registry.split_tiers(rows)
    if not tier_a:
        print("no tier A rows selected", file=sys.stderr)
        return 1

    baseline: dict[str, Any] = load_health()
    keys = baseline_keys(tier_a)
    sem = asyncio.Semaphore(a.concurrency)
    mark = {"ok": "  ", "WARN": "??", "FAIL": "XX"}
    print(f"{'':2} {'company':<22} {'ats':<15} {'jobs':>6} {'hits':>5} "
          f"{'dated':>6} {'live':>6}")

    results: list[CheckResult] = []
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as c:
        tasks = [check(c, sem, r, a.sample, baseline, keys[id(r)]) for r in tier_a]
        for coro in asyncio.as_completed(tasks):
            r = await coro
            results.append(r)
            print(f"{mark[r['status']]} {r['key']:<22} {r['ats']:<15} "
                  f"{r['count']:>6} {r['matches']:>5} "
                  f"{(str(r['dated_pct']) + '%') if r['count'] else '-':>6} "
                  f"{r['live'] or '-':>6}")
            for w in r["warnings"]:
                print(f"     {w}")

    fails: list[CheckResult] = [r for r in results if r["status"] == "FAIL"]
    warns: list[CheckResult] = [r for r in results if r["status"] == "WARN"]
    print(f"\n{len(results) - len(fails) - len(warns)} ok, {len(warns)} warn, "
          f"{len(fails)} fail, {len(tier_b)} tier B skipped")

    if a.baseline:
        snap: dict[str, Any] = {r["key"]: {"count": r["count"], "ats": r["ats"],
                               "company": r["company"],
                               "dated": r["dated_pct"],
                               "checked": datetime.now(timezone.utc)
                               .isoformat(timespec="seconds")}
                for r in results if r["status"] != "FAIL"}
        merged = merge_baseline(baseline, snap, set(keys.values()),
                                # a custom --file is not the registry this
                                # health.json describes, so it prunes nothing
                                full_run=not a.company and a.file == DEFAULT_REGISTRY)
        HEALTH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        pruned = len({**baseline, **snap}) - len(merged)
        print(f"baseline written to {HEALTH}"
              + (f" ({pruned} orphaned keys pruned)" if pruned else ""))

    return 1 if fails or (a.strict and warns) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
