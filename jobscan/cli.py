from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shlex
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import registry, runner
from .core import BUCKETS, DEFAULT_KEEP
from .store import Store, now

DEFAULT_REGISTRY = "registry/companies.csv"
DEFAULT_DB = "data/jobs.db"
RESULTS_DIR = "results"

RESULT_COLUMNS = ["company", "ticker", "title", "location", "link", "posted"]

# Boards that report a day with no clock time. Printing a fake time for
# these would imply precision the board never gave us.
DATE_ONLY_SOURCES = {"postedOn", "PostedDate", "RelevantDate"}

MANIFEST = "runs.csv"
MANIFEST_COLUMNS = ["file", "started", "finished", "command", "registry",
                    "boards", "raw", "shown", "new", "closed", "errors"]


def invocation() -> str:
    """The exact command as typed, quoted so it pastes back into a shell."""
    return "python " + " ".join(shlex.quote(a) for a in sys.argv)


def result_timestamp(started: datetime) -> str:
    """YYYY_MM_DD_HH_MM_SS_XM in local time, e.g. 2026_07_27_03_47_23_PM

    Year first means the folder sorts chronologically by filename, which
    the previous day-first and month-first layouts did not.
    """
    return started.strftime("%Y_%m_%d_%I_%M_%S_%p")


def result_filename(started: datetime) -> str:
    """YYYY_MM_DD_HH_MM_XM.csv in local time, e.g. 2026_07_27_03_47_PM.csv"""
    return result_timestamp(started) + ".csv"


def to_result_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The five columns the results CSV carries.

    `posted` is the board's own timestamp when it gave one. Boards that
    hide dates fall back to first_seen, so the column is never blank.
    """
    out = []
    for r in records:
        raw = r.get("posted_at") or r.get("first_seen")
        src = r.get("posted_source") or ""
        stamp = ""
        if raw:
            try:
                dt = datetime.fromisoformat(raw).astimezone()
                stamp = dt.strftime("%Y-%m-%d" if src in DATE_ONLY_SOURCES
                                    else "%Y-%m-%d %I:%M %p")
            except ValueError:
                stamp = raw
        out.append({
            "company": r.get("company", ""),
            "ticker": r.get("ticker", "") or "",
            "title": r.get("title", ""),
            "location": r.get("location", ""),
            "link": r.get("url", ""),
            "posted": stamp,
        })
    return out


def write_results(records: list[dict[str, Any]], started: datetime,
                  directory: str = RESULTS_DIR, path: str | None = None,
                  embed_command: bool = False,
                  command: str | None = None) -> Path:
    """`command` overrides the recorded invocation.

    The provenance guarantee is that any result file traces back to what
    produced it. sys.argv is right for the CLI and wrong for every other
    caller: an MCP-triggered scan would otherwise record
    `python -m jobscan.mcp_server` for every run and lose which tool call
    actually asked for it.
    """
    if path:
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
    else:
        # The stamp is second-resolution, so two runs starting inside the same
        # second would land in one folder and the later one would overwrite the
        # earlier with no warning. A human at a shell never hit this; two MCP
        # tool calls in a row hit it immediately. Claim the first free name.
        base = Path(directory)
        ts = result_timestamp(started)
        stamp, n = ts, 1
        while (base / stamp).exists():
            n += 1
            stamp = f"{ts}_{n}"
        dest = base / stamp / (stamp + ".csv")
        dest.parent.mkdir(parents=True, exist_ok=True)
    cols = RESULT_COLUMNS + (["run_command"] if embed_command else [])
    cmd = command or invocation()
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for row in to_result_rows(records):
            if embed_command:
                row["run_command"] = cmd
            w.writerow(row)
    return dest


def write_sidecar(dest: Path, started: datetime, meta: dict[str, Any],
                  command: str | None = None) -> Path:
    """Full provenance next to the CSV, without polluting the CSV itself."""
    # Derived from dest rather than recomputed from `started`, so a folder that
    # had to be disambiguated keeps its sidecar named to match its CSV.
    side = dest.parent / (dest.stem + ".meta.json")
    side.write_text(json.dumps({
        "file": dest.name,
        "command": command or invocation(),
        "argv": sys.argv if command is None else [],
        "cwd": str(Path.cwd()),
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now().astimezone().isoformat(timespec="seconds"),
        **meta,
    }, indent=2), encoding="utf-8")
    return side


def append_manifest(dest: Path, started: datetime, meta: dict[str, Any],
                    directory: str = RESULTS_DIR,
                    command: str | None = None) -> Path:
    """One row per run, so every result file is traceable from one place."""
    path = Path(directory) / MANIFEST
    path.parent.mkdir(parents=True, exist_ok=True)
    # A file that exists but holds only a header without its newline (the old
    # bug) would otherwise get the first data row fused onto the header line.
    fresh = not path.exists() or path.stat().st_size == 0
    if not fresh:
        with path.open("rb") as f:
            f.seek(-1, 2)
            needs_newline = f.read(1) not in (b"\n", b"\r")
        if needs_newline:
            with path.open("a", encoding="utf-8") as f:
                f.write("\n")
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, extrasaction="ignore")
        if fresh:
            w.writeheader()
        w.writerow({
            "file": dest.name,
            "started": started.strftime("%Y-%m-%d %I:%M:%S %p"),
            "finished": datetime.now().astimezone().strftime("%Y-%m-%d %I:%M:%S %p"),
            "command": command or invocation(),
            **meta,
        })
    return path


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jobscan",
        description="Scan company job boards for early-career engineering roles.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  jobscan                              scan every company in the registry
  jobscan --company nvidia             scan one company (substring match)
  jobscan --file playlists/faang.csv   scan a different list, same CSV columns
  jobscan --ats greenhouse             scan only greenhouse boards
  jobscan --new-only                   print only postings never seen before
  jobscan --since 7                    print only postings first seen in 7 days
  jobscan --bucket all -o dump.csv     no level filter, write to a file
  jobscan --list                       show the registry, run nothing
""",
    )
    src = p.add_argument_group("source")
    src.add_argument("--file", "-f", metavar="PATH",
                     help="registry CSV to read instead of the default")
    src.add_argument("--company", "-c", metavar="NAME",
                     help="only companies whose name contains NAME")
    src.add_argument("--ats", metavar="TYPE",
                     help="only rows using this ATS")
    src.add_argument("--list", action="store_true",
                     help="print the selected registry rows and exit")

    filt = p.add_argument_group("filter")
    filt.add_argument("--bucket", "-b", default=",".join(DEFAULT_KEEP),
                      help=f"comma list of {'|'.join(BUCKETS)}, or 'all' "
                           f"(default: {','.join(DEFAULT_KEEP)})")
    filt.add_argument("--new-only", action="store_true",
                      help="only postings this run saw for the first time")
    filt.add_argument("--since", type=int, metavar="DAYS",
                      help="only postings first seen within DAYS")
    filt.add_argument("--title", metavar="REGEX",
                      help="extra regex the title must match")

    out = p.add_argument_group("output")
    out.add_argument("--out", "-o", metavar="PATH",
                     help="exact results file path instead of "
                          "results/YYYY_MM_DD_HH_MM_XM.csv")
    out.add_argument("--results-dir", default=RESULTS_DIR,
                     help=f"results folder (default {RESULTS_DIR})")
    out.add_argument("--no-file", action="store_true",
                     help="skip the results CSV, print only")
    out.add_argument("--embed-command", action="store_true",
                     help="add a run_command column to every result row")
    out.add_argument("--format", choices=("table", "csv", "json", "md"),
                     default="table", help="what goes to stdout")
    out.add_argument("--quiet", "-q", action="store_true",
                     help="write the results CSV, print nothing but the summary")
    out.add_argument("--db", default=DEFAULT_DB, help=f"sqlite path (default {DEFAULT_DB})")
    out.add_argument("--no-store", action="store_true",
                     help="skip sqlite entirely, print what the boards return now")

    net = p.add_argument_group("network")
    net.add_argument("--concurrency", type=int, default=20)
    net.add_argument("--timeout", type=float, default=30.0)
    net.add_argument("--retries", type=int, default=1)
    return p


def _fmt_age(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        d = datetime.fromisoformat(iso)
    except ValueError:
        return "?"
    days = (datetime.now(timezone.utc) - d).days
    return "today" if days <= 0 else f"{days}d"


def emit(records: list[dict[str, Any]], fmt: str, dest: Any) -> None:
    cols = ["company", "ticker", "title", "location", "bucket", "posted_at",
            "first_seen", "ats", "url"]
    if fmt == "json":
        json.dump(records, dest, indent=2)
        dest.write("\n")
    elif fmt == "csv":
        w = csv.DictWriter(dest, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    elif fmt == "md":
        dest.write("| Company | Ticker | Title | Location | Age | Link |\n")
        dest.write("|---|---|---|---|---|---|\n")
        for r in records:
            age = _fmt_age(r.get("posted_at") or r.get("first_seen"))
            dest.write(f"| {r['company']} | {r.get('ticker','')} | {r['title']} "
                       f"| {r.get('location','')} | {age} | [apply]({r['url']}) |\n")
    else:
        if not records:
            dest.write("no matches\n")
            return
        wco = max(len(r["company"]) for r in records)
        wtk = max(1, max(len(r.get("ticker") or "") for r in records))
        wti = min(58, max(len(r["title"]) for r in records))
        for r in records:
            age = _fmt_age(r.get("posted_at") or r.get("first_seen"))
            src = "posted" if r.get("posted_at") else "seen"
            dest.write(f"{r['company']:<{wco}}  {(r.get('ticker') or ''):<{wtk}}  "
                       f"{r['title'][:wti]:<{wti}}  "
                       f"{age:>6} {src:<6}  {(r.get('location') or '')[:28]:<28}  "
                       f"{r['url']}\n")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    reg_path = args.file or DEFAULT_REGISTRY

    try:
        rows = registry.load(reg_path)
    except registry.RegistryError as e:
        print(str(e), file=sys.stderr)
        return 2

    rows = registry.select(rows, company=args.company, ats=args.ats)
    if not rows:
        print(f"no registry rows matched (registry={reg_path})", file=sys.stderr)
        return 1

    tier_a, tier_b = registry.split_tiers(rows)

    if args.list:
        for r in rows:
            tier = "A" if r in tier_a else "B"
            ident = r.get("token") or f"{r.get('tenant','')}/{r.get('site','')}"
            print(f"[{tier}] {r['company']:<26} {r.get('ticker',''):<6} "
                  f"{r['ats']:<16} {ident}")
        print(f"\n{len(tier_a)} tier A, {len(tier_b)} tier B", file=sys.stderr)
        return 0

    started_dt = datetime.now().astimezone()
    started = now()
    planned_file = ("" if args.no_file else
                    (args.out or str(Path(args.results_dir)
                                     / result_filename(started_dt))))
    jobs, errors = asyncio.run(runner.scan(
        tier_a, concurrency=args.concurrency,
        timeout=args.timeout, retries=args.retries,
    ))

    buckets = None if args.bucket == "all" else \
        tuple(b.strip() for b in args.bucket.split(",") if b.strip())

    if args.no_store:
        records = [{
            "company": j.company, "ticker": j.ticker, "title": j.title,
            "url": j.url, "location": j.location, "bucket": j.bucket,
            "ats": j.ats, "posted_at": j.posted_at,
            "posted_source": j.posted_source, "first_seen": started,
        } for j in jobs if buckets is None or j.bucket in buckets]
        records.sort(key=lambda r: (r["posted_at"] or "", r["company"]), reverse=True)
        stats = {"new": "n/a", "closed": "n/a"}
    else:
        store = Store(args.db)
        # Everything the board returned goes in, not just the buckets asked
        # for. upsert closes any row it does not see, so passing a filtered
        # list would mark every senior and role_miss req dead on a
        # --bucket explicit_early run. Bucket selection is a display
        # concern, handled by open_jobs below.
        stats = store.upsert(jobs, {r["company"] for r in tier_a})
        since = None
        if args.since:
            since = (datetime.now(timezone.utc)
                     - timedelta(days=args.since)).isoformat(timespec="seconds")
        records = store.open_jobs(
            buckets=buckets, since=since,
            only_new_keys=stats["new_keys"] if args.new_only else None,
            companies=sorted({r["company"] for r in tier_a}),
        )
        store.record_run(started, len(tier_a), len(jobs),
                         stats["new"], stats["closed"], len(errors),
                         command=invocation(), result_file=planned_file)
        store.close()

    if args.title:
        import re
        pat = re.compile(args.title, re.I)
        records = [r for r in records if pat.search(r["title"])]

    meta = {
        "registry": reg_path,
        "boards": len(tier_a),
        "raw": len(jobs),
        "shown": len(records),
        "new": stats["new"],
        "closed": stats["closed"],
        "errors": len(errors),
        "tier_b_deferred": len(tier_b),
        "buckets": list(buckets) if buckets else "all",
        "companies": sorted({r["company"] for r in tier_a}),
    }

    written = None
    if not args.no_file:
        written = write_results(records, started_dt,
                                directory=args.results_dir, path=args.out,
                                embed_command=args.embed_command)
        write_sidecar(written, started_dt, meta)
        append_manifest(written, started_dt, meta, directory=args.results_dir)

    if not args.quiet:
        emit(records, args.format, sys.stdout)

    if written:
        print(f"\nwrote {len(records)} rows to {written}", file=sys.stderr)
        print(f"      provenance in {written.with_suffix('.meta.json').name} "
              f"and {args.results_dir}/{MANIFEST}", file=sys.stderr)

    print(f"boards={len(tier_a)} raw={len(jobs)} shown={len(records)} "
          f"new={stats['new']} closed={stats['closed']} errors={len(errors)}"
          + (f" tier_b_deferred={len(tier_b)}" if tier_b else ""),
          file=sys.stderr)
    for co, ats, e in errors:
        print(f"  ERR {co} ({ats}): {e}", file=sys.stderr)
    return 0
