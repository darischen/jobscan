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
  dated        share of postings carrying a real board timestamp
  live         a random sample of job URLs returns 2xx

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
from typing import TypedDict, Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from jobscan import registry  # noqa: E402
from jobscan.adapters import HEADERS, TIER_A  # noqa: E402
from jobscan.core import classify  # noqa: E402

HEALTH = Path(__file__).resolve().parents[1] / "registry" / "health.json"
DRIFT = 0.40  # a 40% swing in job count against baseline is worth a look


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


async def sample_live(client: httpx.AsyncClient, jobs: list[Any], n: int) -> tuple[int, int]:
    """Spot-check that job URLs resolve. Catches boards serving dead links."""
    urls = [j.url for j in jobs if j.url.startswith("http")]
    if not urls or n <= 0:
        return 0, 0
    picks = random.sample(urls, min(n, len(urls)))
    ok = 0
    for u in picks:
        try:
            r = await client.get(u, headers=HEADERS, timeout=20)
            if r.status_code < 400:
                ok += 1
        except Exception:  # noqa: BLE001
            pass
    return ok, len(picks)


async def check(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict[str, Any], sample: int, baseline: dict[str, Any], key: str | None = None) -> CheckResult:
    name, ats = row["company"], row["ats"]
    res: CheckResult = {"company": name, "key": key or name,
           "ticker": row.get("ticker", ""), "ats": ats,
           "status": "ok", "warnings": [], "count": 0, "dated_pct": 0,
           "live": "", "matches": 0}
    async with sem:
        try:
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
        if res["dated_pct"] < 50:
            res["warnings"].append(f"only {res['dated_pct']}% carry a date")

        res["matches"] = sum(1 for j in jobs
                             if classify(j.title)[0] in ("explicit_early", "unleveled"))

        if sample:
            ok, tried = await sample_live(client, jobs, sample)
            res["live"] = f"{ok}/{tried}"
            if tried and ok < tried:
                res["warnings"].append(f"{tried - ok}/{tried} sampled URLs dead")

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
    ap.add_argument("--file", "-f", default="registry/companies.csv")
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
                               "checked": datetime.now(timezone.utc)
                               .isoformat(timespec="seconds")}
                for r in results if r["status"] != "FAIL"}
        HEALTH.write_text(json.dumps({**baseline, **snap}, indent=2),
                          encoding="utf-8")
        print(f"baseline written to {HEALTH}")

    return 1 if fails or (a.strict and warns) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
