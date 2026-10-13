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
    JOBSCAN_SELECTORS  default registry/selectors.json
"""
from __future__ import annotations

import asyncio
import csv
import functools
import json
import os
import re
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
from mcp.server.fastmcp import Context, FastMCP

from . import cli, registry, runner, tier_b
from .adapters import HEADERS, TIER_A
from .core import BUCKETS, DEFAULT_KEEP
from .store import Store, now

REGISTRY = os.getenv("JOBSCAN_REGISTRY", "registry/companies.csv")
DB = os.getenv("JOBSCAN_DB", "data/jobs.db")
RESULTS = os.getenv("JOBSCAN_RESULTS", "results")
SELECTORS = os.getenv("JOBSCAN_SELECTORS", tier_b.SELECTORS)

# Under this many tier A boards a scan blocks and returns rows. At or above it
# the call returns a scan_id immediately. One company finishes in seconds; the
# full registry took 2m46s across 77 boards, which is long enough that a slow
# board could push the tool call past its timeout and lose the whole run.
SYNC_BOARD_LIMIT = 10
MAX_TRACKED_SCANS = 10
TIER_B_TIMEOUT = 900       # seconds for one scan_browser child process
PROGRESS_EVERY = 1.0       # seconds between progress notifications
RECENT_BOARDS = 5          # finished boards scan_status shows
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
    last_board: str = ""
    recent: list[str] = field(default_factory=list)
    clock: float = field(default_factory=time.monotonic)

    def board_done(self, company: str, n: int) -> None:
        self.done += 1
        self.last_board = company
        self.recent = (self.recent + [f"{company}: {n}"])[-RECENT_BOARDS:]

    def progress_text(self) -> str:
        if not self.recent:
            return f"scanning {self.boards} boards"
        return f"{self.done}/{self.boards} boards; last {self.recent[-1]} postings"


_SCANS: dict[str, ScanState] = {}
# create_task returns a task the event loop only weakly references, so without
# holding it here a background scan can be garbage collected mid-flight.
_TASKS: set[asyncio.Task] = set()


def _track(state: ScanState) -> None:
    _SCANS[state.id] = state
    while len(_SCANS) > MAX_TRACKED_SCANS:
        _SCANS.pop(next(iter(_SCANS)))


# ------------------------------------------------------------------ progress
async def _report(ctx: Context | None, done: float, total: float | None,
                  message: str) -> None:
    """One progress notification. Silently nothing when the caller sent no
    progress token, when there is no request (a direct call in a test), or
    when the client has gone: progress is a courtesy, never a failure."""
    if ctx is None:
        return
    try:
        await ctx.report_progress(done, total, message)
    except Exception:  # noqa: BLE001
        pass


async def _pump(state: ScanState, ctx: Context) -> None:
    """Report a running scan's progress until cancelled, only on change."""
    sent = -1
    while True:
        if state.done != sent:
            sent = state.done
            await _report(ctx, sent, state.boards, state.progress_text())
        await asyncio.sleep(PROGRESS_EVERY)


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
    out = [{k: r.get(k, "") for k in cols} | _marks(r) for r in records[:limit]]
    return out, len(records) > limit


def _marks(r: dict[str, Any]) -> dict[str, Any]:
    """The tracking fields of a stored row. Empty for a store=False scan,
    whose rows never touched sqlite and have no key."""
    if "key" not in r:
        return {}
    return {"key": r["key"], "applied_at": r.get("applied_at"),
            "starred": bool(r.get("starred"))}


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

    jobs, errors = await runner.scan(tier_a, on_board_done=state.board_done)

    if store:
        st = Store(DB)
        try:
            # Every job the boards returned goes in, not just the requested
            # buckets: upsert closes whatever it does not see, so a filtered
            # list would mark every senior req dead on an early-career scan.
            # Nor a board that errored: it returned nothing, which is not
            # the same as listing nothing.
            stats = st.upsert(jobs, {r["company"] for r in tier_a}
                              - {e[0] for e in errors})
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
               store: bool = True, limit: int = DEFAULT_LIMIT,
               wait: bool = False, ctx: Context = None) -> dict[str, Any]:
    """Scan job boards for early-career engineering roles and store the results.

    Selects registry rows, fetches every tier A board concurrently, classifies
    each title into a bucket, records what is new and what disappeared, then
    writes a timestamped results CSV with full provenance.

    Selections under 10 boards run to completion and return rows. Larger ones
    return a scan_id immediately; poll scan_status with it. wait=True runs
    any selection to completion instead, sending a progress notification as
    boards finish; the full registry takes several minutes that way.

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
    wait: block until done, streaming progress, whatever the selection's size.
    """
    rows = _load(company, ats)
    if not rows:
        return {"ok": False, "error": f"no registry rows matched "
                                      f"(company={company!r}, ats={ats!r})"}
    tier_a, tier_b = registry.split_tiers(rows)
    if not tier_a:
        return {"ok": False,
                "error": f"{len(tier_b)} row(s) matched but all are tier B, "
                         f"which needs a browser. Use scan_browser",
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

    if wait or len(tier_a) < SYNC_BOARD_LIMIT:
        pump = asyncio.create_task(_pump(state, ctx))
        try:
            out = await _run_scan(state, tier_a, bk, since_iso, new_only, title,
                                  store, limit, command, len(tier_b))
        except Exception as e:  # noqa: BLE001
            state.status = "error"
            state.finished = now()
            state.errors = [f"{type(e).__name__}: {e}"]
            raise
        finally:
            pump.cancel()
            await asyncio.gather(pump, return_exceptions=True)
        await _report(ctx, state.done, state.boards,
                      f"done: {out['raw']} postings, {len(out['errors'])} errors")
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
            "last_board": state.last_board, "recent": state.recent,
            "elapsed_s": round(time.monotonic() - state.clock),
            "companies": state.companies,
            "started": state.started, "finished": state.finished,
            "summary": state.summary, "errors": state.errors,
            "result_file": state.result_file}


# -------------------------------------------------------------------- tier B
async def _tier_b_process(
        args: list[str],
        on_progress: Callable[[str], Any] | None = None) -> tuple[int, str, str]:
    """Run `python -m jobscan.tier_b` and return (exit code, stdout, stderr).

    A child process, not an import: the browser must never share this
    server's event loop. stdin is closed because this server's own stdin is
    the MCP transport, and a child that inherited it could eat a message.
    stderr is read as it arrives so "progress ..." lines reach on_progress
    while the scan runs; the rest of stderr is returned.
    """
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "jobscan.tier_b", *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    err_lines: list[str] = []

    async def read_err() -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if line.startswith("progress ") and on_progress is not None:
                await on_progress(line)
            else:
                err_lines.append(line)

    async def run() -> bytes:
        assert proc.stdout is not None
        out, _ = await asyncio.gather(proc.stdout.read(), read_err())
        await proc.wait()
        return out

    try:
        out = await asyncio.wait_for(run(), TIER_B_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, "", f"timed out after {TIER_B_TIMEOUT}s"
    return (proc.returncode or 0, out.decode("utf-8", "replace"),
            "\n".join(err_lines))


@tool
async def scan_browser(company: str | None = None,
                       buckets: list[str] | None = None,
                       new_only: bool = False, store: bool = True,
                       limit: int = DEFAULT_LIMIT,
                       ctx: Context = None) -> dict[str, Any]:
    """Scan the tier B boards, the ones that only answer a real browser.

    Runs `python -m jobscan.tier_b` as a separate process and waits for it:
    one browser, at most two boards at a time, so allow a minute or two.
    A board whose selectors.json entry sets "headed" opens a visible
    browser window on this machine for the length of the scan. Results are
    classified, stored and written to a results CSV exactly as scan does.
    Sends a progress notification as each board finishes.

    Only tier B rows with an entry in registry/selectors.json can be read;
    the rest are listed in `not_configured`.

    company: name substring, or an exact ticker match. Omit for every tier B row.
    buckets, new_only, store, limit: as in scan.
    """
    rows = _load(company)
    _, rows_b = registry.split_tiers(rows)
    if not rows_b:
        return {"ok": False,
                "error": f"no tier B rows matched (company={company!r}); "
                         f"tier A boards are read with scan"}
    configured = tier_b.load_selectors(SELECTORS)
    missing = sorted({r["company"] for r in rows_b} - set(configured))
    if len(missing) == len({r["company"] for r in rows_b}):
        return {"ok": False,
                "error": f"none of the {len(missing)} tier B row(s) matched has "
                         f"an entry in {SELECTORS}",
                "not_configured": missing}

    bk = _buckets(buckets)
    command = _describe("scan_browser", company=company, buckets=buckets,
                        new_only=new_only, store=store)
    args = ["--file", REGISTRY, "--selectors", SELECTORS, "--db", DB,
            "--results-dir", RESULTS, "--json", "--command", command,
            "--bucket", ",".join(bk) if bk else "all"]
    if company:
        args += ["--company", company]
    if new_only:
        args.append("--new-only")
    if not store:
        args.append("--no-store")

    async def relay(line: str) -> None:
        m = re.match(r"progress (\d+)/(\d+) (.+) (\d+)$", line)
        if m:
            await _report(ctx, int(m[1]), int(m[2]), f"{m[3]}: read {m[4]} postings")

    code, out, err = await _tier_b_process(args, on_progress=relay)
    if code != 0:
        return {"ok": False, "kind": "tier_b",
                "error": (err.strip() or out.strip() or f"exit code {code}")[-600:]}
    summary = json.loads(out)
    jobs = summary.pop("jobs", [])
    # The row with no entry is reported once, here, not also as an error.
    summary["errors"] = [e for e in summary.get("errors", [])
                         if e[0] not in missing]
    return {"ok": True, "tier": "B", **summary, "not_configured": missing,
            "jobs": jobs[:limit], "truncated": len(jobs) > limit}


# ---------------------------------------------------------------------- query
@tool
def query_jobs(company: str | None = None, buckets: list[str] | None = None,
               since: int | None = None, limit: int = DEFAULT_LIMIT,
               applied: bool | None = None,
               starred: bool | None = None) -> dict[str, Any]:
    """Search already-stored open postings. Reads sqlite, touches no network.

    Returns postings that were open as of the last scan that covered them.
    Anything whose requisition disappeared from its board is excluded.

    company: exact company name as stored. Omit for all.
    buckets: as in scan. Defaults to explicit_early, unleveled, senior.
    since: only postings first seen within this many days.
    applied: False leaves out postings already applied to, True keeps only
        those. Omit for both.
    starred: the same for the star.
    """
    st = Store(DB)
    try:
        records = st.open_jobs(buckets=_buckets(buckets),
                              since=_since_iso(since),
                              companies=[company] if company else None,
                              applied=applied, starred=starred)
    finally:
        st.close()
    rows, truncated = _job_rows(records, limit)
    return {"ok": True, "count": len(records), "returned": len(rows),
            "truncated": truncated, "jobs": rows}


# ------------------------------------------------------------------- tracking
def _tracked_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cols = ("company", "ticker", "title", "location", "bucket", "url",
            "first_seen", "closed_at")
    return [{k: r.get(k, "") for k in cols} | _marks(r) for r in records]


@tool
def mark_jobs(jobs: list[str], applied: bool | None = None,
              starred: bool | None = None) -> dict[str, Any]:
    """Record that postings were applied to, or star them to come back to.

    Writes sqlite only. The marks survive every later scan, and stay on a
    posting after its requisition closes.

    jobs: each posting's url, or its key, as returned by scan, query_jobs
        and tracked_jobs. Anything that matches no stored posting is
        returned in `unknown` and changes nothing.
    applied: True stamps the current time (a posting already marked keeps
        its first date), False clears it. Omit to leave it alone.
    starred: True or False. Omit to leave it alone.
    """
    if applied is None and starred is None:
        return {"ok": False, "error": "nothing to change: pass applied "
                                      "and/or starred"}
    st = Store(DB)
    try:
        done = st.mark(jobs, applied=applied, starred=starred)
    finally:
        st.close()
    return {"ok": True, "updated": len(done["jobs"]),
            "unknown": done["unknown"], "jobs": _tracked_rows(done["jobs"])}


@tool
def tracked_jobs(applied: bool | None = None, starred: bool | None = None,
                 include_closed: bool = True) -> dict[str, Any]:
    """Postings that were applied to or starred. Reads sqlite, no network.

    Closed postings are included by default, with closed_at set: a
    requisition disappearing from its board is worth knowing about for
    something you applied to.

    applied: True for only those applied to, False for only those not
        (so starred but not yet applied). Omit for either mark.
    starred: the same for the star.
    include_closed: False drops postings no longer on their board.
    """
    st = Store(DB)
    try:
        records = st.tracked(applied=applied, starred=starred,
                             include_closed=include_closed)
    finally:
        st.close()
    return {"ok": True, "count": len(records), "jobs": _tracked_rows(records)}


@tool
def list_companies(company: str | None = None,
                   ats: str | None = None) -> dict[str, Any]:
    """The registry, with each row's tier and whether it can be read.

    Tier A rows have a working HTTP adapter and are read by scan. Tier B
    rows need a browser: one with an entry in registry/selectors.json is
    read by scan_browser and carries implemented: true; one without is
    skipped by everything and carries implemented: false.
    """
    rows = _load(company, ats)
    tier_a, rows_b = registry.split_tiers(rows)
    ids = {id(r) for r in tier_a}
    configured = tier_b.load_selectors(SELECTORS)
    out = []
    for r in rows:
        a = id(r) in ids
        out.append({
            "company": r["company"], "ticker": r.get("ticker", ""),
            "ats": r["ats"], "tier": "A" if a else "B",
            "implemented": a or r["company"] in configured,
            "identity": r.get("token") or (f"{r.get('tenant','')}/{r.get('site','')}"
                                           if r.get("tenant") else r.get("host", "")),
            "careers_url": r.get("careers_url", ""),
            "query": r.get("query", ""), "notes": r.get("notes", ""),
        })
    by_ats: dict[str, int] = {}
    for r in out:
        by_ats[r["ats"]] = by_ats.get(r["ats"], 0) + 1
    unread = [r for r in rows_b if r["company"] not in configured]
    return {"ok": True, "total": len(out), "tier_a": len(tier_a),
            "tier_b": len(rows_b), "by_ats": by_ats,
            "unimplemented_note": (f"{len(unread)} tier B row(s) have no "
                                   f"selectors.json entry and are never "
                                   f"scanned") if unread else "",
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


# ------------------------------------------------------------ registry writes
# Board-identity fields: changing one points the row at a different board,
# so the row is checked against the live board again before it is saved.
_IDENTITY = ("ats", "token", "tenant", "site", "wd", "host", "query")


async def _check_board(row: dict[str, Any], allow_tier_b: bool,
                       known: int | None = None) -> tuple[int | None, str | None]:
    """(postings, None) when the row may be written, (None, why) when not."""
    if row["ats"] not in TIER_A:
        if allow_tier_b:
            return None, None
        return None, (f"ats {row['ats']!r} is tier B: no adapter can confirm "
                      f"it. Pass allow_tier_b=true to record it anyway; it is "
                      f"read only once it has a {SELECTORS} entry")
    n, err = (known, None) if known else await runner.probe(row)
    if err:
        return None, f"board check failed, nothing written: {err}"
    if not n:
        return None, (f"the {row['ats']} board for {row['company']!r} lists 0 "
                      f"postings, nothing written; check its fields")
    return n, None


def _baseline_hint(company: str) -> str:
    return (f"record its baseline with: python tools/verify.py "
            f"--company {json.dumps(company)} --baseline")


@tool
async def add_company(name: str, ats: str = "", token: str = "", tenant: str = "",
                      site: str = "", wd: str = "", host: str = "", query: str = "",
                      careers_url: str = "", ticker: str = "", notes: str = "",
                      url: str = "", ats_hint: str = "", auto: bool = False,
                      allow_tier_b: bool = False,
                      dry_run: bool = False) -> dict[str, Any]:
    """Add a company to the registry, after confirming its board is real.

    Two ways in. With `ats` and its fields (token, tenant/site/wd, host...)
    the row is used as given. Without `ats`, the board is discovered from
    `url` (a careers page) the way discover_company does. Either way a tier A
    row is written only after its adapter reads at least one posting from the
    live board, and a discovery that matched only a partial name is refused
    for a human to check. A second row with the same company and ats is
    refused: that is update_company.

    The row is inserted in name order; every other line of the file stays as
    it was, and the result is validated before it replaces the original.

    allow_tier_b: record a row no adapter can confirm (ats custom, taleo,
        phenom). It is skipped by scan, and read by scan_browser once it has
        a selectors.json entry.
    dry_run: check everything, return the line, write nothing.
    """
    from tools import discover

    discovered = not ats
    if discovered:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
            row = await discover.resolve(c, name, ticker, url, ats_hint, auto)
        if notes:
            row["notes"] = notes
        if (row.get("notes") or "").startswith("VERIFY"):
            return {"ok": False, "row": row,
                    "error": "discovery matched a board by a partial name only; "
                             "check it by hand, then call add_company again "
                             "with ats and its fields"}
    else:
        row = {c: "" for c in registry.COLUMNS} | dict(
            company=name, ticker=ticker, ats=ats.lower(), token=token,
            tenant=tenant, site=site, wd=wd, host=host, query=query,
            careers_url=careers_url or url, notes=notes)
    row["ticker"] = (row.get("ticker") or "").upper()

    clash = [r for r in registry.load(REGISTRY)
             if r["company"].lower() == name.strip().lower() and r["ats"] == row["ats"]]
    if clash:
        return {"ok": False, "error": f"{clash[0]['company']} already has a "
                                      f"{row['ats']} row; use update_company"}

    m = re.search(r"confirmed (\d+) jobs", row.get("notes") or "") if discovered else None
    postings, why = await _check_board(row, allow_tier_b, int(m[1]) if m else None)
    if why:
        return {"ok": False, "error": why, "row": row}
    line = registry.format_row(row)
    if dry_run:
        return {"ok": True, "written": False, "postings": postings, "row": row,
                "csv_line": line}
    written = registry.add_row(REGISTRY, row)
    return {"ok": True, "written": True, "postings": postings, "row": written,
            "csv_line": line, "registry": REGISTRY,
            "next": _baseline_hint(written["company"])}


@tool
async def update_company(company: str, row_ats: str | None = None,
                         new_name: str | None = None, ticker: str | None = None,
                         ats: str | None = None, token: str | None = None,
                         tenant: str | None = None, site: str | None = None,
                         wd: str | None = None, host: str | None = None,
                         query: str | None = None, careers_url: str | None = None,
                         notes: str | None = None,
                         dry_run: bool = False) -> dict[str, Any]:
    """Change fields of one registry row. Fields left out stay as they are;
    pass "" to clear one.

    company: the row's exact name (any case). A company with two boards
        needs row_ats to say which row.
    new_name: rename the company.
    A change to ats, token, tenant, site, wd, host or query points the row at
    a different board, so it is confirmed live first, as add_company does;
    notes, ticker, careers_url and a rename are written without a network
    call.
    dry_run: check everything, write nothing.
    """
    changes = {k: v for k, v in dict(
        company=new_name, ticker=ticker, ats=ats, token=token, tenant=tenant,
        site=site, wd=wd, host=host, query=query, careers_url=careers_url,
        notes=notes).items() if v is not None}
    if not changes:
        return {"ok": False, "error": "nothing to change"}
    current = registry.find_row(REGISTRY, company, row_ats)
    new = current | {k: (v.lower() if k == "ats" else v) for k, v in changes.items()}
    postings = None
    if any(new[k] != current[k] for k in _IDENTITY):
        postings, why = await _check_board(new, allow_tier_b=True)
        if why:
            return {"ok": False, "error": why, "row": new}
    if dry_run:
        return {"ok": True, "written": False, "postings": postings, "row": new,
                "was": current}
    row = registry.update_row(REGISTRY, company, changes, ats=row_ats)
    return {"ok": True, "written": True, "postings": postings, "row": row,
            "was": current,
            **({"next": _baseline_hint(row["company"])} if postings else {})}


@tool
def remove_company(company: str, ats: str | None = None) -> dict[str, Any]:
    """Delete one registry row. Stored postings stay in the database; the
    company is simply no longer scanned.

    company: the row's exact name (any case).
    ats: which row, for a company with two boards.
    """
    removed = registry.remove_row(REGISTRY, company, ats)
    return {"ok": True, "removed": removed, "registry": REGISTRY,
            "note": "its health.json baseline is pruned by the next full "
                    "python tools/verify.py --baseline"}


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
