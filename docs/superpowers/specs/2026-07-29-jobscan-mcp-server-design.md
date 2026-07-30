# Jobscan MCP Server Design

Date: 2026-07-29
Status: approved, ready for implementation planning
Supersedes: `MCP_PLAN.md` (earlier draft, wider scope, written before the code inventory)

## Goal

Expose jobscan's scanning, querying, and registry-maintenance capabilities to Claude
Code and any other MCP client, as a fourth caller of the existing module seams. The
server adds no scraping, classification, or date-parsing logic of its own.

## Scope decisions

Four decisions bound this work. Each was chosen against a specific alternative.

| Decision | Chosen | Rejected alternative |
|---|---|---|
| Write authority | Read registry, run scans, write the jobs DB. Registry edits return a CSV line to paste. | Direct writes to `companies.csv`. Rejected: the registry is the project's asset and stays a hand-owned, git-diffable artifact. |
| Tier B | Out of scope. Tier B rows surface in `list_companies` tagged `implemented: false`. | Building `tier_b.py`, or writing Apple/Microsoft adapters first. Both deferred as separate projects. |
| Scan latency | Synchronous under 10 boards, background with a `scan_id` at 10 or more. | Always sync (risks losing a 3-minute run to a tool timeout). Always background (costs a round trip on the common single-company call). |
| Tool count | 9 tools. | A wider draft surface. Cut: `mark_applied`, `star_job`, `classify_title`, `seed_candidates`, `scan_tier_b`, `search_jobs`, `add_company`. |

## Capability inventory

Established by reading every module, not from `README.md`. The README documents
behavior the code does not have.

### `jobscan/registry.py`
- `load(path)` validates 11 columns with per-ATS required-field rules and raises
  `RegistryError` with a full problem list rather than skipping bad rows.
- `select(rows, company, ats)` matches ticker exactly, then falls back to a name
  substring, so a short ticker like `F` cannot swallow every company with an F.
- `split_tiers(rows)` partitions on `ats in TIER_A`.
- `write_template(path)`, `COLUMNS`.

### `jobscan/core.py`
- `Job` dataclass with a stable sha1 `key` derived from
  `company|ats|raw_id or url`, preferring the board's own id.
- 16 role families in `ROLE_FAMILIES`, 9 enabled via `DEFAULT_ROLES`.
- `role_of()`, `level_of()` (4 tiers, most restrictive first), `EXCLUDE` pre-gate.
- `classify()` returns one of 6 buckets. `DEFAULT_KEEP` is
  `explicit_early, unleveled, senior`.

### `jobscan/adapters/__init__.py`
Ten tier A adapters: `amazon`, `google`, `greenhouse`, `lever`, `ashby`,
`smartrecruiters`, `workday`, `oracle`, `workable`, `recruitee`. All share the
contract `async def fn(client, company, row) -> list[Job]` and raise on hard
failure. `TIER_B` names 7 types with no implementation behind them.

### `jobscan/runner.py`
- `scan(rows, concurrency, timeout, retries)` fans out under a semaphore with
  per-row retry and backoff, collecting failures into an error list.
- `is_us_location()` runs unconditionally, using letter-boundary lookarounds so
  `GA` does not match inside `PortuGAl`. A blank location passes.

### `jobscan/dates.py`
`from_iso`, `from_epoch_ms`, `from_workday_relative`, `pick`. Returns ISO 8601 UTC
or `None`, and records which field supplied the value.

### `jobscan/store.py`
- `upsert(jobs, scanned_companies)` stamps `closed_at` on requisitions that
  vanished, scoped to companies scanned this run.
- `record_run()`, `open_jobs(buckets, since, only_new_keys, companies)`.
- Schema carries `applied_at` and `starred`, both unused by any code path.

### `jobscan/cli.py`
20 flags, 4 output formats, and triple provenance: a `.meta.json` sidecar,
`results/runs.csv`, and the `runs` table. Helpers the MCP server reuses:
`to_result_rows`, `write_results`, `write_sidecar`, `append_manifest`,
`result_timestamp`.

### `tools/`
- `discover.py`: three resolution paths (URL scrape, ATS hint probe, blind probe).
  Emits nothing on a pattern match alone; every row is confirmed against the live
  API. Batch mode, resume checkpoint, confidence flagging. `resolve()` is
  module-level and importable.
- `verify.py`: 7 checks per board (reachable, populated, paginated, unique, titled,
  dated, live) plus baseline drift against `registry/health.json`. `check()` and
  `load_health()` are module-level and importable.
- `seed.py`: Wikipedia index constituents to `pending.txt`. Not exposed.

## Defects found during inventory

Recorded here for traceability. Only #3 is fixed by this work.

1. **`tier_b.py` does not exist.** The README documents it, `registry.py` splits for
   it, `cli.py` reports `tier_b_deferred`, and `selectors.json` holds an AMD entry.
   39 of 118 registry rows (33%) are silently deferred on every run.
2. **Google is duplicated 4x** in `companies.csv` as identical `GOOGL` rows. Every
   full scan crawls the entire Google board four times. Data bug, tracked separately.
3. **`results/runs.csv` is missing a newline** after the header, fusing the header
   and first data row. Fixed by this work.
4. **`applied_at` and `starred` are unused** in the `jobs` schema. Left alone.
5. **Amazon returned 0 jobs** on the 2026-07-28 12:52 PM run while reporting success.
   Adapter bug, tracked separately.

## Architecture

One new module, `jobscan/mcp_server.py`.

```
                    registry.load / select / split_tiers
                                  |
   cli.py ---------+              |              +--------- tools/verify.py
                   |              v              |
   mcp_server.py --+--------> runner.scan <------+--------- tools/discover.py
                   |              |
                   |              v
                   +---> core.classify -> store.upsert -> store.open_jobs
```

Transport is stdio, which is what Claude Code uses. Framework is the official `mcp`
Python SDK.

### Configuration

Paths resolve from environment variables defaulting to current behavior, so cloud
portability costs nothing later.

```python
REGISTRY = os.getenv("JOBSCAN_REGISTRY", "registry/companies.csv")
DB       = os.getenv("JOBSCAN_DB",       "data/jobs.db")
RESULTS  = os.getenv("JOBSCAN_RESULTS",  "results")
```

Registration in `.mcp.json`:

```json
{
  "mcpServers": {
    "jobscan": {
      "command": "python",
      "args": ["-m", "jobscan.mcp_server"],
      "cwd": "."
    }
  }
}
```

## Upstream edits

Four changes to existing files. Each stands on its own merit.

### `jobscan/runner.py`: progress callback

```python
async def scan(rows, concurrency=20, timeout=30.0, retries=1,
               on_board_done=None) -> tuple[list[Job], list[tuple[str, str, str]]]:
```

`_one()` invokes `on_board_done(company, n_jobs)` in a `finally` block before
returning. `asyncio.gather` reports nothing until every coroutine finishes, so
per-board progress needs this hook. When `on_board_done` is `None` the behavior is
byte-identical to today.

### `jobscan/cli.py`: caller-supplied provenance

`write_results`, `write_sidecar`, and `append_manifest` gain
`command: str | None = None`, falling back to `invocation()` when omitted. The MCP
server passes a string identifying the tool call, for example
`mcp:scan(company="nvidia")`.

This preserves the provenance guarantee that any result file traces back to what
produced it. Without it, every MCP-triggered run would record the generic server
command and the guarantee would break for a third of runs.

### `jobscan/cli.py`: manifest newline fix

`append_manifest` writes the header without a trailing newline before the first data
row. Defect #3 above.

### `tools/__init__.py`: new, empty

Makes `from tools.verify import check, load_health` and
`from tools.discover import resolve` work without `sys.path` manipulation. Both
target functions are already module-level, so no extraction is needed.

## Tool surface

Nine tools. Every return value is a dict carrying `ok: bool`.

### `scan`

```python
scan(company: str | None = None,
     ats: str | None = None,
     buckets: list[str] | None = None,   # default: core.DEFAULT_KEEP
     since: int | None = None,           # days
     new_only: bool = False,
     title: str | None = None,           # regex
     store: bool = True,
     limit: int = 200) -> dict           # caps the returned jobs list only
```

`limit` bounds the `jobs` array in the response, matching `query_jobs`. It never
bounds what is scanned, stored, or written to the results CSV. A full NVIDIA scan
stores 1,139 rows and writes every matching row to disk while returning the first
200, because a 269-row array inlined into a tool result is mostly wasted context.
The `shown` count always reports the true total.

Loads the registry, applies `select`, splits tiers. Branches on tier A board count
against `SYNC_BOARD_LIMIT = 10`.

Synchronous path (under 10 boards) returns:

```python
{"ok": True, "mode": "sync", "boards": int, "raw": int, "shown": int,
 "new": int, "closed": int, "errors": [[company, ats, message]],
 "tier_b_deferred": int, "result_file": str,
 "jobs": [ {...} ], "truncated": bool}
```

Background path (10 or more) returns immediately:

```python
{"ok": True, "mode": "background", "scan_id": str, "boards": int,
 "tier_b_deferred": int, "message": "poll scan_status"}
```

Both paths write the results CSV, sidecar, and manifest row through the `cli.py`
helpers with an explicit `command=` string. The `store=False` path skips sqlite and
returns what the boards report right now, matching `--no-store`.

Bucket filtering is a display concern. `store.upsert` always receives every job the
boards returned, because passing a filtered list would mark unmatched requisitions
dead. This mirrors the comment in `cli.py:294`.

### `scan_status`

```python
scan_status(scan_id: str | None = None) -> dict   # omit for most recent
```

```python
{"ok": True, "scan_id": str, "status": "running" | "done" | "error",
 "boards": int, "done": int, "started": str, "finished": str | None,
 "summary": dict | None, "errors": list, "result_file": str | None}
```

### `query_jobs`

```python
query_jobs(company: str | None = None,
           buckets: list[str] | None = None,
           since: int | None = None,
           limit: int = 200) -> dict
```

Wraps `Store.open_jobs`. No network. Returns `{"ok": True, "count": int,
"truncated": bool, "jobs": [...]}`.

### `list_companies`

```python
list_companies(company: str | None = None, ats: str | None = None) -> dict
```

Every row gains `tier: "A" | "B"` and `implemented: bool`. Tier B rows carry
`implemented: false`, making the 39 dead rows visible instead of silent. Returns
counts by tier and by ATS.

### `registry_health`

```python
registry_health(stale_days: int = 7) -> dict
```

Reads `registry/health.json` and the `runs` table. Reports total rows, tier split,
ATS distribution, which companies have no baseline entry, which baselines are older
than `stale_days`, and the last run's summary. No network.

### `verify_boards`

```python
verify_boards(company: str | None = None, sample: int = 3) -> dict
```

Calls `tools.verify.check()` per tier A row with the loaded baseline. Never writes a
baseline; that stays a deliberate CLI act. Returns per-board results plus ok/warn/fail
counts.

### `discover_company`

```python
discover_company(name: str, url: str = "", ticker: str = "",
                 ats_hint: str = "", auto: bool = False) -> dict
```

Calls `tools.discover.resolve()`. Returns the resolved row, the formatted CSV line,
and whether the result landed in tier A. Never writes to the registry.

### `get_run_history`

```python
get_run_history(limit: int = 20) -> dict
```

Reads the `runs` table, newest first.

### `get_scan_results`

```python
get_scan_results(run: str | None = None) -> dict   # omit for latest
```

Reads a `results/` subfolder. Returns the CSV rows and the parsed sidecar. The `run`
argument is resolved against the set of existing subfolder names rather than joined
as a path, so directory traversal is structurally impossible.

## Background scan state

```python
@dataclass
class ScanState:
    id: str
    status: str                  # running | done | error
    boards: int
    done: int
    started: str
    finished: str | None = None
    summary: dict | None = None
    errors: list = field(default_factory=list)
    result_file: str | None = None
```

Held in a module-level `_SCANS: dict[str, ScanState]`, capped at
`MAX_TRACKED_SCANS = 10` with the oldest evicted on insert. A background scan runs as
an `asyncio.create_task` on the loop the MCP framework already owns, with
`on_board_done` incrementing `state.done`.

State lives as long as the process, matching the one-server-per-session lifecycle.
Nothing persists beyond the results files the CLI helpers already write, so there is
no new on-disk format.

If the framework does not own a persistent event loop across tool calls,
implementation falls back to running the scan in a thread with its own loop. This is
verified against current SDK docs during implementation rather than assumed.

## Error handling

Every tool body sits inside a decorator that catches `Exception` and returns
`{"ok": False, "error": "<type>: <message>"}`. An uncaught exception crossing the MCP
boundary reaches the client as an opaque protocol error with no diagnostic value.

Three cases get specific treatment:

- `RegistryError` returns its full validation problem list unchanged. `registry.load`
  already composes a precise message naming every bad line.
- Per-board scrape failures pass through as the existing `(company, ats, message)`
  tuples rather than failing the call, preserving the soft-failure property the
  runner already guarantees.
- A background task raising sets `ScanState.status = "error"` and records the
  traceback summary, so `scan_status` reports the failure instead of reporting
  `running` forever.

## Testing

The repo has no test infrastructure. This work adds `tests/` with pytest.

Unit tests, no network:

- `query_jobs` against a temp sqlite seeded with fixture `Job` objects, covering
  bucket, since, and limit filtering plus the `truncated` flag.
- `list_companies` against a temp CSV including a deliberately malformed row,
  asserting `RegistryError` surfaces as `ok: false` with the problem list.
- The sync-vs-background threshold with `runner.scan` monkeypatched, asserting 9
  boards blocks and 10 boards returns a `scan_id`.
- `scan` with `limit` below the match count, asserting `shown` reports the true total,
  `truncated` is `true`, the `jobs` array holds exactly `limit` entries, and the
  results CSV on disk holds every row.
- `ScanState` eviction at `MAX_TRACKED_SCANS`.
- `get_scan_results` rejecting a `run` value not in the existing subfolder set.
- `runner.scan` with `on_board_done` recording one call per row, and with
  `on_board_done=None` producing unchanged output.
- `append_manifest` producing a parseable CSV, which is the regression test for
  defect #3.

Integration test: launch the server over stdio with the SDK's own client, assert all
9 tools enumerate with schemas, call `query_jobs` end to end against a temp DB.

Network tests: `verify_boards` and `discover_company` against one live greenhouse
board, marked `@pytest.mark.network` and excluded by default.

## File manifest

```
jobscan/mcp_server.py                         NEW
tools/__init__.py                             NEW  (empty)
tests/__init__.py                             NEW  (empty)
tests/conftest.py                             NEW  (temp DB and registry fixtures)
tests/test_mcp_server.py                      NEW
tests/test_runner_progress.py                 NEW
tests/test_cli_provenance.py                  NEW
.mcp.json                                     NEW
jobscan/runner.py                             EDIT (on_board_done)
jobscan/cli.py                                EDIT (command= param, newline fix)
requirements.txt                              EDIT (+mcp, +pytest, +pytest-asyncio)
README.md                                     EDIT (MCP section)
```

## Out of scope

- Tier B implementation and the missing `tier_b.py`.
- Registry writes from any tool.
- Application tracking via `applied_at` and `starred`.
- Exposing `tools/seed.py`.
- HTTP transport, Docker, authentication, multi-user.
- Defects #1, #2, #4, and #5 above, tracked separately.

## Implementation note

The `mcp` SDK's import path and decorator API have shifted across versions. Current
documentation is pulled during implementation rather than written from memory.
