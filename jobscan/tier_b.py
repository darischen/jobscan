"""Tier B. Boards that only answer a real browser.

    python -m jobscan.tier_b                    every tier B row
    python -m jobscan.tier_b --company tesla    one company

A separate entry point and a separate process from tier A on purpose: tier A
fans out twenty plain HTTP requests wide, and a browser cannot. Each worker
here owns its own BrowserContext and Page, and there are never more than
MAX_BROWSER_WORKERS of them, because concurrent navigation on a shared page
returns reads from the wrong tab.

Nothing here decides how to read a board. `registry/selectors.json` says so,
one entry per company, keyed by the registry company name, in one of two
modes:

  capture   the page fetches its listings as JSON; wait for that response
            and read it. Tesla: one 1.6 MB payload, every listing in it.

      "Tesla": {
        "headed": true,
        "capture": "cua-api/apps/careers/state",
        "items": "listings",
        "fields": {"id": "id", "title": "t", "location": "l"},
        "lookups": {"location": "lookup.locations"},
        "job_url": "https://www.tesla.com/careers/search/job/{id}"
      }

  card      the listings exist only as rendered elements; read each one.

      "Acme": {
        "card": "li.job",            each posting's element
        "title": "a.title",          inside the card; default is its text
        "location": ".location",     inside the card; optional
        "next": "button.more",       "next page" or "load more"; optional
        "max_pages": 50
      }

Both take "url" (default: the row's careers_url), "headed", "timeout_ms" and
"wait_ms". Write an entry once per board and commit it. A board whose entry
stops matching fails loudly; it never reads as an empty board.

"headed" opens a visible window. tesla.com's edge refuses headless Chromium
with a 403 and serves a visible one, so its entry sets it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import cli, registry, runner
from .core import DEFAULT_KEEP, Job
from .store import Store, now

MAX_BROWSER_WORKERS = 2
SELECTORS = "registry/selectors.json"
DEFAULT_TIMEOUT_MS = 60_000
DEFAULT_WAIT_MS = 1_500
DEFAULT_MAX_PAGES = 50

# Reads every card on the page in one round trip. `title` and `location` are
# selectors inside the card; the link is the card itself when it is an <a>,
# else the first <a> inside it. `el.href` is already absolute.
_CARDS_JS = """(cards, cfg) => cards.map(card => {
    const text = el => el ? (el.innerText || el.textContent || "").trim() : "";
    const link = card.matches("a[href]") ? card : card.querySelector("a[href]");
    return {
        title: text(cfg.title ? card.querySelector(cfg.title) : card),
        href: link ? link.href : "",
        location: cfg.location ? text(card.querySelector(cfg.location)) : "",
    };
})"""


# -------------------------------------------------------------------- config
def load_selectors(path: str | Path = SELECTORS) -> dict[str, dict[str, Any]]:
    """Per-company browser configs. Keys starting with "_" are comments."""
    path = Path(path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out, problems = {}, []
    for company, cfg in raw.items():
        if company.startswith("_"):
            continue
        if not isinstance(cfg, dict) or not (cfg.get("capture") or cfg.get("card")):
            problems.append(f"{company}: needs 'capture' or 'card'")
        elif cfg.get("capture") and not (
                cfg.get("items") and cfg.get("job_url")
                and {"id", "title"} <= set(cfg.get("fields") or {})):
            problems.append(f"{company}: capture needs 'items', 'job_url' and "
                            f"'fields' with at least id and title")
        else:
            out[company] = cfg
    if problems:
        raise ValueError(f"{path}:\n  " + "\n  ".join(problems))
    return out


# ---------------------------------------------------------------- extraction
def _dig(obj: Any, path: str) -> Any:
    """Follow a dotted path through nested dicts; None where it runs out."""
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def jobs_from_json(row: dict[str, Any], cfg: dict[str, Any], payload: Any) -> list[Job]:
    """Jobs from a captured JSON payload, per the entry's field map.

    `lookups` names a table for a field whose value is an id into it. An id
    the table does not hold reads as blank, never as the raw id: a location
    of "777" would otherwise sail through the US filter as a non-answer.
    """
    items = _dig(payload, cfg["items"])
    if not isinstance(items, list):
        raise ValueError(f"no list at '{cfg['items']}' in the captured payload")
    tables = {name: _dig(payload, path) or {}
              for name, path in (cfg.get("lookups") or {}).items()}

    def field(item: dict[str, Any], name: str) -> str:
        key = cfg["fields"].get(name)
        value = item.get(key) if key else None
        if value is None:
            return ""
        if name in tables:
            value = tables[name].get(str(value), "")
        return str(value).strip()

    out, seen = [], set()
    for item in items:
        rid = field(item, "id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        out.append(Job(
            company=row["company"], ticker=row.get("ticker", ""),
            title=field(item, "title"),
            url=cfg["job_url"].format(id=rid),
            location=field(item, "location"),
            ats=row["ats"], raw_id=rid,
            department=field(item, "department"),
        ))
    return out


def jobs_from_cards(row: dict[str, Any], cards: list[dict[str, str]]) -> list[Job]:
    """Jobs from scraped cards. A card with no title or no link is dropped,
    and a link seen twice (a "load more" page re-reads the earlier cards)
    counts once. The link is the identity: a card carries no requisition id."""
    out, seen = [], set()
    for card in cards:
        title = (card.get("title") or "").strip()
        href = (card.get("href") or "").strip()
        if not title or not href or href in seen:
            continue
        seen.add(href)
        out.append(Job(
            company=row["company"], ticker=row.get("ticker", ""),
            title=title, url=href,
            location=(card.get("location") or "").strip(),
            ats=row["ats"],
        ))
    return out


# ------------------------------------------------------------------- browser
class _launch:
    """`async with _launch(headed) as browser`. Imported lazily so the rest
    of the module, and its tests, work without Playwright installed."""

    def __init__(self, headed: bool):
        self.headed = headed

    async def __aenter__(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "tier B needs Playwright: pip install playwright && "
                "python -m playwright install chromium") from e
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=not self.headed)
        return self._browser

    async def __aexit__(self, *exc: Any) -> bool:
        await self._browser.close()
        await self._pw.stop()
        return False


async def _capture(page: Any, url: str, cfg: dict[str, Any], timeout: int) -> Any:
    """Load the page and return the JSON response whose url contains
    cfg["capture"]. A refused navigation is reported as such, since waiting
    out the timeout for a response that will never come hides the cause."""
    async with page.expect_response(
            lambda r: cfg["capture"] in r.url, timeout=timeout) as waiting:
        nav = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        if nav is not None and nav.status >= 400:
            raise RuntimeError(f"HTTP {nav.status} loading {url}")
    response = await waiting.value
    if response.status >= 400:
        raise RuntimeError(f"HTTP {response.status} from {response.url}")
    return await response.json()


async def _cards(page: Any, url: str, cfg: dict[str, Any], timeout: int) -> list[dict[str, str]]:
    """Every card on the page, following cfg["next"] until it is gone,
    disabled, or stops producing new cards."""
    nav = await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    if nav is not None and nav.status >= 400:
        raise RuntimeError(f"HTTP {nav.status} loading {url}")
    await page.wait_for_selector(cfg["card"], timeout=timeout)
    wait = cfg.get("wait_ms", DEFAULT_WAIT_MS)
    spec = {"title": cfg.get("title"), "location": cfg.get("location")}
    cards: list[dict[str, str]] = []
    links: set[str] = set()
    for _ in range(cfg.get("max_pages", DEFAULT_MAX_PAGES)):
        batch = await page.eval_on_selector_all(cfg["card"], _CARDS_JS, spec)
        fresh = [c for c in batch if c["href"] not in links]
        cards += fresh
        links.update(c["href"] for c in fresh)
        if not fresh or not cfg.get("next"):
            break
        button = await page.query_selector(cfg["next"])
        if button is None or not await button.is_enabled():
            break
        await button.click()
        await page.wait_for_timeout(wait)
    return cards


async def fetch(browser: Any, row: dict[str, Any], cfg: dict[str, Any]) -> list[Job]:
    """One board, in a context and page of its own."""
    url = cfg.get("url") or row.get("careers_url")
    if not url:
        raise ValueError("no url in selectors.json and no careers_url in the registry")
    timeout = cfg.get("timeout_ms", DEFAULT_TIMEOUT_MS)
    context = await browser.new_context(locale="en-US")
    try:
        page = await context.new_page()
        if cfg.get("capture"):
            return jobs_from_json(row, cfg, await _capture(page, url, cfg, timeout))
        return jobs_from_cards(row, await _cards(page, url, cfg, timeout))
    finally:
        await context.close()


# -------------------------------------------------------------------- runner
async def scan(rows: list[dict[str, Any]], selectors: dict[str, dict[str, Any]],
               headed: bool = False) -> tuple[list[Job], list[tuple[str, str, str]]]:
    """Fetch every row that has a config; same return shape as runner.scan.

    One browser for the run, visible if any selected board asks for it.
    """
    errors: list[tuple[str, str, str]] = []
    todo = []
    for r in rows:
        if r["company"] in selectors:
            todo.append(r)
        else:
            errors.append((r["company"], r["ats"], "no entry in selectors.json"))
    if not todo:
        return [], errors

    headed = headed or any(selectors[r["company"]].get("headed") for r in todo)
    sem = asyncio.Semaphore(MAX_BROWSER_WORKERS)
    async with _launch(headed) as browser:
        async def one(row: dict[str, Any]) -> list[Job]:
            async with sem:
                try:
                    found = await fetch(browser, row, selectors[row["company"]])
                    if not found:
                        raise RuntimeError("read zero postings")
                    return found
                except Exception as e:  # noqa: BLE001
                    errors.append((row["company"], row["ats"],
                                   f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"))
                    return []
        batches = await asyncio.gather(*(one(r) for r in todo))
    return runner.finalize([j for b in batches for j in b]), errors


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jobscan.tier_b",
        description="Scan the boards that need a browser (tier B).")
    p.add_argument("--file", "-f", metavar="PATH", default=cli.DEFAULT_REGISTRY,
                   help="registry CSV (default %(default)s)")
    p.add_argument("--selectors", metavar="PATH", default=SELECTORS,
                   help="per-company browser configs (default %(default)s)")
    p.add_argument("--company", "-c", metavar="NAME",
                   help="only companies whose name contains NAME")
    p.add_argument("--bucket", "-b", default=",".join(DEFAULT_KEEP),
                   help="comma list of buckets to show, or 'all'")
    p.add_argument("--new-only", action="store_true",
                   help="only postings this run saw for the first time")
    p.add_argument("--since", type=int, metavar="DAYS",
                   help="only postings first seen within DAYS")
    p.add_argument("--headed", action="store_true",
                   help="show the browser window even if no board asks for it")
    p.add_argument("--db", default=cli.DEFAULT_DB)
    p.add_argument("--no-store", action="store_true",
                   help="skip sqlite, show what the boards return now")
    p.add_argument("--results-dir", default=cli.RESULTS_DIR)
    p.add_argument("--no-file", action="store_true", help="skip the results CSV")
    p.add_argument("--format", choices=("table", "csv", "json", "md"),
                   default="table", help="what goes to stdout")
    p.add_argument("--quiet", "-q", action="store_true")
    p.add_argument("--json", action="store_true",
                   help="print one JSON summary to stdout and nothing else")
    p.add_argument("--command", help=argparse.SUPPRESS)   # provenance override
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        rows = registry.select(registry.load(args.file), company=args.company)
        selectors = load_selectors(args.selectors)
    except (registry.RegistryError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 2
    _, tier_b = registry.split_tiers(rows)
    if not tier_b:
        print(f"no tier B rows matched (registry={args.file})", file=sys.stderr)
        return 1

    command = args.command or cli.invocation()
    started_dt = datetime.now().astimezone()
    started = now()
    jobs, errors = asyncio.run(scan(tier_b, selectors, headed=args.headed))

    buckets = None if args.bucket == "all" else \
        tuple(b.strip() for b in args.bucket.split(",") if b.strip())
    # Only boards that answered. upsert closes whatever a scanned company
    # no longer lists, and a board that failed today lists nothing.
    answered = {r["company"] for r in tier_b} - {e[0] for e in errors}

    if args.no_store:
        records = [{
            "company": j.company, "ticker": j.ticker, "title": j.title,
            "url": j.url, "location": j.location, "bucket": j.bucket,
            "ats": j.ats, "posted_at": j.posted_at,
            "posted_source": j.posted_source, "first_seen": started,
        } for j in jobs if buckets is None or j.bucket in buckets]
        records.sort(key=lambda r: (r["company"], r["title"]))
        stats: dict[str, Any] = {"new": "n/a", "closed": "n/a"}
    else:
        store = Store(args.db)
        try:
            stats = store.upsert(jobs, answered)
            since = None
            if args.since:
                since = (datetime.now(timezone.utc)
                         - timedelta(days=args.since)).isoformat(timespec="seconds")
            records = store.open_jobs(
                buckets=buckets, since=since,
                only_new_keys=stats["new_keys"] if args.new_only else None,
                companies=sorted(answered))
        finally:
            store.close()

    meta = {
        "registry": args.file, "tier": "B", "boards": len(tier_b),
        "raw": len(jobs), "shown": len(records), "new": stats["new"],
        "closed": stats["closed"], "errors": len(errors),
        "buckets": list(buckets) if buckets else "all",
        "companies": sorted(r["company"] for r in tier_b),
    }
    written = None
    if not args.no_file:
        written = cli.write_results(records, started_dt,
                                    directory=args.results_dir, command=command)
        cli.write_sidecar(written, started_dt, meta, command=command)
        cli.append_manifest(written, started_dt, meta,
                            directory=args.results_dir, command=command)
    if not args.no_store:
        store = Store(args.db)
        try:
            store.record_run(started, len(tier_b), len(jobs), stats["new"],
                             stats["closed"], len(errors), command=command,
                             result_file=str(written or ""))
        finally:
            store.close()

    if args.json:
        cols = ("company", "ticker", "title", "location", "bucket", "ats",
                "posted_at", "first_seen", "url", "key", "applied_at", "starred")
        json.dump({
            "boards": len(tier_b), "raw": len(jobs), "shown": len(records),
            "new": stats["new"], "closed": stats["closed"],
            "errors": [list(e) for e in errors],
            "result_file": str(written) if written else None,
            "jobs": [{k: r[k] for k in cols if k in r} for r in records],
        }, sys.stdout)
        sys.stdout.write("\n")
        return 0

    if not args.quiet:
        cli.emit(records, args.format, sys.stdout)
    if written:
        print(f"\nwrote {len(records)} rows to {written}", file=sys.stderr)
    print(f"boards={len(tier_b)} raw={len(jobs)} shown={len(records)} "
          f"new={stats['new']} closed={stats['closed']} errors={len(errors)}",
          file=sys.stderr)
    for co, ats, e in errors:
        print(f"  ERR {co} ({ats}): {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
