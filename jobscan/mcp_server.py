"""MCP server. A fourth caller of the existing seams.

cli.py, tools/verify.py and tools/discover.py already drive registry.load,
runner.scan, core.classify and Store. This module adds a fifth entry point and
no new scraping, classification or date logic of its own. Anything that looks
like domain logic here is a bug: it belongs in the module that owns it.

    python -m jobscan.mcp_server

Paths come from the environment so a container can relocate them without a
code change:

    JOBSCAN_REGISTRY   default registry/companies.csv
    JOBSCAN_DB         default data/jobs.db
    JOBSCAN_RESULTS    default results
"""
from __future__ import annotations

import asyncio
import csv
import functools
import json
import os
import re
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from mcp.server.fastmcp import FastMCP

from . import cli, registry, runner
from .adapters import HEADERS, TIER_A
from .core import BUCKETS, DEFAULT_KEEP
from .store import Store, now

REGISTRY = os.getenv("JOBSCAN_REGISTRY", "registry/companies.csv")
DB = os.getenv("JOBSCAN_DB", "data/jobs.db")
RESULTS = os.getenv("JOBSCAN_RESULTS", "results")

# Under this many tier A boards a scan blocks and returns rows. At or above it
# the call returns a scan_id immediately. One company finishes in seconds; the
# full registry took 2m46s across 77 boards, which is long enough that a slow
# board could push the tool call past its timeout and lose the whole run.
SYNC_BOARD_LIMIT = 10
MAX_TRACKED_SCANS = 10
DEFAULT_LIMIT = 200

mcp = FastMCP("jobscan")


# --------------------------------------------------------------------- errors
def tool(fn: Callable) -> Callable:
    """Register an MCP tool that reports failure as data.

    An exception crossing the MCP boundary reaches the client as an opaque
    protocol error with no diagnostic value. Every tool returns
    {"ok": false, "error": ...} instead, so a caller can read what went wrong
    and decide what to do.
    """
    @functools.wraps(fn)
    async def wrapper(*a: Any, **kw: Any) -> dict[str, Any]:
        try:
            out = fn(*a, **kw)
            if asyncio.iscoroutine(out):
                out = await out
            return out
        except registry.RegistryError as e:
            # load() composes a message naming every bad line. Pass it through
            # unchanged rather than flattening it to one sentence.
            return {"ok": False, "error": str(e), "kind": "registry"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "kind": "internal"}
    return mcp.tool()(wrapper)


# ------------------------------------------------------------------ scan state
@dataclass
class ScanState:
    id: str
    status: str                       # running | done | error
    boards: int
    done: int = 0
    started: str = ""
    finished: str | None = None
    summary: dict[str, Any] | None = None
    errors: list[Any] = field(default_factory=list)
    result_file: str | None = None
    companies: list[str] = field(default_factory=list)


_SCANS: dict[str, ScanState] = {}
# create_task returns a task the event loop only weakly references, so without
# holding it here a background scan can be garbage collected mid-flight.
_TASKS: set[asyncio.Task] = set()


def _track(state: ScanState) -> None:
    _SCANS[state.id] = state
    while len(_SCANS) > MAX_TRACKED_SCANS:
        _SCANS.pop(next(iter(_SCANS)))


# ------------------------------------------------------------------- helpers
def _load(company: str | None = None, ats: str | None = None) -> list[dict[str, Any]]:
    return registry.select(registry.load(REGISTRY), company=company, ats=ats)


def _buckets(buckets: list[str] | None) -> tuple[str, ...] | None:
    """None means every bucket. Unknown names are rejected loudly rather than
    silently matching nothing, which would look like an empty board."""
    if buckets is None:
        return tuple(DEFAULT_KEEP)
    if len(buckets) == 1 and buckets[0] == "all":
        return None
    bad = [b for b in buckets if b not in BUCKETS]
    if bad:
        raise ValueError(f"unknown bucket(s) {bad}; valid: {list(BUCKETS)}")
    return tuple(buckets)


def _since_iso(days: int | None) -> str | None:
    if not days:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _job_rows(records: list[dict[str, Any]], limit: int) -> tuple[list[dict], bool]:
    cols = ("company", "ticker", "title", "location", "bucket", "ats",
            "posted_at", "first_seen", "url")
    out = [{k: r.get(k, "") for k in cols} for r in records[:limit]]
    return out, len(records) > limit


def _describe(name: str, **kw: Any) -> str:
    """Provenance string recorded in the sidecar, runs.csv and the runs table."""
    args = ", ".join(f"{k}={v!r}" for k, v in kw.items() if v not in (None, False, ""))
    return f"mcp:{name}({args})"


# ----------------------------------------------------------------------- scan
async def _run_scan(state: ScanState, tier_a: list[dict[str, Any]],
                    buckets: tuple[str, ...] | None, since: str | None,
                    new_only: bool, title: str | None, store: bool,
                    limit: int, command: str,
                    tier_b: int) -> dict[str, Any]:
    started_dt = datetime.now().astimezone()
    started = now()

    def progress(company: str, n: int) -> None:
        state.done += 1

    jobs, errors = await runner.scan(tier_a, on_board_done=progress)

    if store:
        st = Store(DB)
        try:
            # Every job the boards returned goes in, not just the requested
            # buckets: upsert closes whatever it does not see, so a filtered
            # list would mark every senior req dead on an early-career scan.
            stats = st.upsert(jobs, {r["company"] for r in tier_a})
            records = st.open_jobs(
                buckets=buckets, since=since,
                only_new_keys=stats["new_keys"] if new_only else None,
                companies=sorted({r["company"] for r in tier_a}),
            )
        finally:
            st.close()
    else:
        stats = {"new": "n/a", "closed": "n/a"}
        records = [{
            "company": j.company, "ticker": j.ticker, "title": j.title,
            "url": j.url, "location": j.location, "bucket": j.bucket,
            "ats": j.ats, "posted_at": j.posted_at,
            "posted_source": j.posted_source, "first_seen": started,
        } for j in jobs if buckets is None or j.bucket in buckets]
        records.sort(key=lambda r: (r["posted_at"] or "", r["company"]), reverse=True)

    if title:
        pat = re.compile(title, re.I)
        records = [r for r in records if pat.search(r["title"])]

    meta = {
        "registry": REGISTRY, "boards": len(tier_a), "raw": len(jobs),
        "shown": len(records), "new": stats["new"], "closed": stats["closed"],
        "errors": len(errors), "tier_b_deferred": tier_b,
        "buckets": list(buckets) if buckets else "all",
        "companies": sorted({r["company"] for r in tier_a}),
    }
    dest = cli.write_results(records, started_dt, directory=RESULTS, command=command)
    cli.write_sidecar(dest, started_dt, meta, command=command)
    cli.append_manifest(dest, started_dt, meta, directory=RESULTS, command=command)

    if store:
        st = Store(DB)
        try:
            st.record_run(started, len(tier_a), len(jobs), stats["new"],
                          stats["closed"], len(errors), command=command,
                          result_file=str(dest))
        finally:
            st.close()

    rows, truncated = _job_rows(records, limit)
    state.result_file = str(dest)
    state.errors = [list(e) for e in errors]
    return {"ok": True, "boards": len(tier_a), "raw": len(jobs),
            "shown": len(records), "new": stats["new"], "closed": stats["closed"],
            "errors": [list(e) for e in errors], "tier_b_deferred": tier_b,
            "result_file": str(dest), "jobs": rows, "truncated": truncated}


@tool
async def scan(company: str | None = None, ats: str | None = None,
               buckets: list[str] | None = None, since: int | None = None,
               new_only: bool = False, title: str | None = None,
               store: bool = True, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Scan job boards for early-career engineering roles and store the results.

    Selects registry rows, fetches every tier A board concurrently, classifies
    each title into a bucket, records what is new and what disappeared, then
    writes a timestamped results CSV with full provenance.

    Selections under 10 boards run to completion and return rows. Larger ones
    return a scan_id immediately; poll scan_status with it.

    company: name substring, or an exact ticker match. Omit to scan everything.
    ats: restrict to one platform, e.g. greenhouse, workday, oracle.
    buckets: any of explicit_early, unleveled, senior, above_senior, excluded,
        role_miss, or ["all"]. Defaults to explicit_early, unleveled, senior.
    since: only postings first seen within this many days.
    new_only: only postings this run saw for the first time.
    title: extra case-insensitive regex the title must match.
    store: False skips sqlite and reports what the boards return right now.
    limit: caps the returned jobs array only. Never limits what is scanned,
        stored, or written to the CSV; `shown` always reports the true total.
    """
    rows = _load(company, ats)
    if not rows:
        return {"ok": False, "error": f"no registry rows matched "
                                      f"(company={company!r}, ats={ats!r})"}
    tier_a, tier_b = registry.split_tiers(rows)
    if not tier_a:
        return {"ok": False,
                "error": f"{len(tier_b)} row(s) matched but all are tier B, "
                         f"which has no implementation. See docs/tier-b-triage.md",
                "tier_b_companies": sorted({r["company"] for r in tier_b})}

    bk = _buckets(buckets)
    since_iso = _since_iso(since)
    command = _describe("scan", company=company, ats=ats, buckets=buckets,
                        since=since, new_only=new_only, title=title,
                        store=store)
    state = ScanState(id=uuid.uuid4().hex[:8], status="running",
                      boards=len(tier_a), started=now(),
                      companies=sorted({r["company"] for r in tier_a}))
    _track(state)

    if len(tier_a) < SYNC_BOARD_LIMIT:
        try:
            out = await _run_scan(state, tier_a, bk, since_iso, new_only, title,
                                  store, limit, command, len(tier_b))
        except Exception as e:  # noqa: BLE001
            state.status = "error"
            state.finished = now()
            state.errors = [f"{type(e).__name__}: {e}"]
            raise
        state.status = "done"
        state.finished = now()
        state.summary = {k: out[k] for k in ("boards", "raw", "shown", "new",
                                             "closed")}
        return {**out, "mode": "sync", "scan_id": state.id}

    async def background() -> None:
        try:
            out = await _run_scan(state, tier_a, bk, since_iso, new_only, title,
                                  store, limit, command, len(tier_b))
            state.summary = {k: out[k] for k in ("boards", "raw", "shown",
                                                 "new", "closed")}
            state.status = "done"
        except Exception as e:  # noqa: BLE001
            # Without this the status would sit on "running" forever and
            # scan_status would never report the failure.
            state.status = "error"
            state.errors = [f"{type(e).__name__}: {e}",
                            traceback.format_exc(limit=3)]
        finally:
            state.finished = now()

    task = asyncio.create_task(background())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return {"ok": True, "mode": "background", "scan_id": state.id,
            "boards": len(tier_a), "tier_b_deferred": len(tier_b),
            "companies": state.companies,
            "message": f"scanning {len(tier_a)} boards; poll "
                       f"scan_status(scan_id={state.id!r})"}


@tool
def scan_status(scan_id: str | None = None) -> dict[str, Any]:
    """Progress and result of a scan started by the scan tool.

    scan_id: omit for the most recent scan this session.
    """
    # An explicitly supplied id gets the precise error even when nothing is
    # tracked, so a typo never reads as "the server forgot your scan".
    if scan_id is not None:
        if scan_id not in _SCANS:
            return {"ok": False, "error": f"unknown scan_id {scan_id!r}",
                    "known": list(_SCANS)}
        state = _SCANS[scan_id]
    elif not _SCANS:
        return {"ok": False, "error": "no scans this session"}
    else:
        state = list(_SCANS.values())[-1]
    return {"ok": True, "scan_id": state.id, "status": state.status,
            "boards": state.boards, "done": state.done,
            "companies": state.companies,
            "started": state.started, "finished": state.finished,
            "summary": state.summary, "errors": state.errors,
            "result_file": state.result_file}


# ---------------------------------------------------------------------- query
@tool
def query_jobs(company: str | None = None, buckets: list[str] | None = None,
               since: int | None = None, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Search already-stored open postings. Reads sqlite, touches no network.

    Returns postings that were open as of the last scan that covered them.
    Anything whose requisition disappeared from its board is excluded.

    company: exact company name as stored. Omit for all.
    buckets: as in scan. Defaults to explicit_early, unleveled, senior.
    since: only postings first seen within this many days.
    """
    st = Store(DB)
    try:
        records = st.open_jobs(buckets=_buckets(buckets),
                              since=_since_iso(since),
                              companies=[company] if company else None)
    finally:
        st.close()
    rows, truncated = _job_rows(records, limit)
    return {"ok": True, "count": len(records), "returned": len(rows),
            "truncated": truncated, "jobs": rows}


@tool
def list_companies(company: str | None = None,
                   ats: str | None = None) -> dict[str, Any]:
    """The registry, with each row's tier and whether that tier is implemented.

    Tier A rows have a working adapter. Tier B rows are recorded but have no
    scraper, so they are skipped by every scan; they carry implemented: false.
    """
    rows = _load(company, ats)
    tier_a, tier_b = registry.split_tiers(rows)
    ids = {id(r) for r in tier_a}
    out = []
    for r in rows:
        a = id(r) in ids
        out.append({
            "company": r["company"], "ticker": r.get("ticker", ""),
            "ats": r["ats"], "tier": "A" if a else "B", "implemented": a,
            "identity": r.get("token") or (f"{r.get('tenant','')}/{r.get('site','')}"
                                           if r.get("tenant") else r.get("host", "")),
            "careers_url": r.get("careers_url", ""),
            "query": r.get("query", ""), "notes": r.get("notes", ""),
        })
    by_ats: dict[str, int] = {}
    for r in out:
        by_ats[r["ats"]] = by_ats.get(r["ats"], 0) + 1
    return {"ok": True, "total": len(out), "tier_a": len(tier_a),
            "tier_b": len(tier_b), "by_ats": by_ats,
            "unimplemented_note": ("tier B rows are never scanned; see "
                                   "docs/tier-b-triage.md") if tier_b else "",
            "companies": out}


@tool
def registry_health(stale_days: int = 7) -> dict[str, Any]:
    """Coverage and freshness of the registry, from health.json and the runs table.

    Reports which boards have no recorded baseline and which were last checked
    longer ago than stale_days. Touches no network; run verify_boards for that.
    """
    from tools.verify import load_health, baseline_keys

    rows = _load()
    tier_a, tier_b = registry.split_tiers(rows)
    health = load_health()
    keys = baseline_keys(tier_a)
    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)

    missing, stale, fresh = [], [], 0
    for r in tier_a:
        k = keys[id(r)]
        entry = health.get(k)
        if not entry:
            missing.append(k)
            continue
        try:
            when = datetime.fromisoformat(entry["checked"])
        except (KeyError, ValueError):
            stale.append({"key": k, "checked": entry.get("checked", "?")})
            continue
        if when < cutoff:
            stale.append({"key": k, "checked": entry["checked"],
                          "count": entry.get("count")})
        else:
            fresh += 1

    st = Store(DB)
    try:
        last = [dict(r) for r in st.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1")]
        total_jobs = st.db.execute(
            "SELECT COUNT(*) c FROM jobs WHERE closed_at IS NULL").fetchone()["c"]
    finally:
        st.close()

    return {"ok": True, "registry": REGISTRY,
            "rows": len(rows), "tier_a": len(tier_a), "tier_b": len(tier_b),
            "baseline_fresh": fresh, "baseline_stale": stale,
            "baseline_missing": missing,
            "open_jobs_stored": total_jobs,
            "last_run": last[0] if last else None}


# ---------------------------------------------------------------- maintenance
@tool
async def verify_boards(company: str | None = None,
                        sample: int = 3) -> dict[str, Any]:
    """Health-check tier A boards against their live APIs. Never writes a baseline.

    Runs seven checks per board: reachable, populated, pagination complete,
    unique requisition ids, no blank titles, share carrying a real posted date,
    and a random sample of job URLs still resolving. Also flags job-count drift
    against registry/health.json.

    company: restrict to one company. Omit to check every tier A row, which
        issues a lot of requests.
    sample: job URLs to spot-check per board, 0 to skip.
    """
    from tools.verify import check, load_health, baseline_keys

    rows = _load(company)
    tier_a, tier_b = registry.split_tiers(rows)
    if not tier_a:
        return {"ok": False, "error": "no tier A rows selected"}
    baseline = load_health()
    keys = baseline_keys(tier_a)
    sem = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as c:
        results = await asyncio.gather(*(
            check(c, sem, r, sample, baseline, keys[id(r)]) for r in tier_a))
    counts = {"ok": 0, "WARN": 0, "FAIL": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {"ok": True, "checked": len(results), "counts": counts,
            "tier_b_skipped": len(tier_b),
            "boards": [dict(r) for r in results]}


@tool
async def discover_company(name: str, url: str = "", ticker: str = "",
                           ats_hint: str = "", auto: bool = False) -> dict[str, Any]:
    """Resolve a company to a job-board API and return a registry row to paste.

    Never writes to the registry: it returns the CSV line for you to review.
    Nothing is emitted on a pattern match alone, every candidate is confirmed
    by calling the board's own API and counting real jobs, and partial-name
    slug matches are flagged in `notes` with a VERIFY prefix. Check those by
    hand; short slugs are frequently held by an unrelated company.

    name: company display name.
    url: careers page URL. Strongly preferred, it makes resolution reliable.
    ticker: stock ticker, optional.
    ats_hint: one of greenhouse, lever, ashby, smartrecruiters, workable,
        recruitee, workday, to probe that platform directly.
    auto: probe every platform even when a URL was given. Slower.
    """
    from tools.discover import resolve, row_to_csv

    async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
        row = await resolve(c, name, ticker, url, ats_hint, auto)
    notes = row.get("notes", "")
    return {"ok": True, "row": row, "csv_line": row_to_csv(row),
            "columns": ",".join(registry.COLUMNS),
            "tier": "A" if row["ats"] in TIER_A else "B",
            "confirmed": row["ats"] in TIER_A,
            "needs_human_check": notes.startswith("VERIFY"),
            "note": notes}


# ---------------------------------------------------------------- provenance
@tool
def get_run_history(limit: int = 20) -> dict[str, Any]:
    """Past scan runs, newest first, from the runs table."""
    st = Store(DB)
    try:
        runs = [dict(r) for r in st.db.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        st.close()
    return {"ok": True, "count": len(runs), "runs": runs}


@tool
def get_scan_results(run: str | None = None) -> dict[str, Any]:
    """Read a past results folder: its CSV rows and provenance sidecar.

    run: folder name under the results directory, e.g. 2026_07_29_08_21_49_PM.
        Omit for the most recent.
    """
    base = Path(RESULTS)
    if not base.exists():
        return {"ok": False, "error": f"no results directory at {base}"}
    # Resolved against the set of existing folder names rather than joined as a
    # path, so no value of `run` can escape the results directory.
    folders = sorted(p.name for p in base.iterdir() if p.is_dir())
    if not folders:
        return {"ok": False, "error": f"no result folders in {base}"}
    if run is None:
        run = folders[-1]
    elif run not in folders:
        return {"ok": False, "error": f"unknown run {run!r}",
                "available": folders[-10:]}

    folder = base / run
    csv_path = folder / f"{run}.csv"
    meta_path = folder / f"{run}.meta.json"
    rows: list[dict[str, str]] = []
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    meta = None
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    return {"ok": True, "run": run, "folder": str(folder),
            "csv_file": csv_path.name if csv_path.exists() else None,
            "row_count": len(rows), "rows": rows[:DEFAULT_LIMIT],
            "truncated": len(rows) > DEFAULT_LIMIT, "metadata": meta,
            "available_runs": folders[-10:]}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
