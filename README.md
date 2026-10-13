# jobscan

Scan company job boards for early-career engineering roles. Deterministic
routing, no search-engine scraping, no cache.

```bash
pip install -r requirements.txt

python jobscan.py                              # every company in the registry
python jobscan.py --company nvidia             # one company
python jobscan.py --file registry/playlists/ai-labs.csv
python jobscan.py --new-only                   # only what appeared since last run
python tools/verify.py                         # check every registry row still resolves
```

Verified working on 20 boards covering 12,344 live postings, 100 percent of
them carrying a real posted date from the ATS.

---

## Where to store companies and links

**CSV. Not Sheets, not Markdown, not JSON.**

The registry is one file, `registry/companies.csv`, one row per board. The
reasoning:

- You edit it by hand, and a CSV opens in Excel, Sheets, VS Code, and vim.
  Sheets locks the source of truth behind an API and an auth token.
- It diffs in git line by line. A one-character token fix shows as a
  one-line diff. JSON reindents and Markdown tables reflow.
- The parser is in the standard library and never breaks on a trailing comma.
- A playlist is the same file with fewer rows, so rows paste back and forth
  with zero conversion.

If you prefer editing in Sheets, do it, then File > Download > CSV into
`registry/`. Keep the CSV as the artifact the code reads.

### Columns

| Column | Used by | Meaning |
|---|---|---|
| `company` | all | display name, `--company` substring-matches it |
| `ticker` | all | stock ticker, `--company` exact-matches it |
| `ats` | all | routing key, decides the adapter |
| `token` | greenhouse, lever, ashby, smartrecruiters, workable, recruitee | board slug |
| `tenant` | workday | subdomain before `.wdN.` |
| `site` | workday, oracle | Workday site id or Oracle `siteNumber` |
| `wd` | workday | cluster: `wd1`, `wd5`, `wd12` |
| `host` | oracle, tier B | Oracle FA host or careers host |
| `careers_url` | tier B, humans | fill this in always, it is your fallback |
| `query` | workday | optional server-side keyword filter |
| `notes` | humans | anything |

`--company` tries the ticker first as an exact match, then falls back to a
substring match on the name. Exact on ticker so a short symbol like `F`
cannot swallow every company with an F in its name.

```
python jobscan.py --company NVDA      # ticker
python jobscan.py --company nvidia    # name
```

Leave `ticker` blank for private companies. Blank cells stay blank. The loader validates every row and refuses to run
on a malformed registry rather than silently skipping it.

### Adding companies

Do not look up tokens by hand. Paste the careers URL:

```bash
python tools/discover.py "Snowflake" --ticker SNOW https://careers.snowflake.com/us/en
python tools/discover.py --batch pending.txt >> registry/companies.csv
```

`pending.txt` is `Company<TAB>TICKER<TAB>URL` per line, ticker optional, which is what you get pasting
two columns out of a spreadsheet. The tool follows redirects, reads the
final host and page body, and prints a finished CSV row. It correctly
identified Workday tenants, an Oracle host, and pushed iCIMS, Eightfold,
and Phenom sites to tier B on the seed set.

Then always run `python tools/verify.py`. Tokens rot. On the seed registry
it caught four wrong rows in one pass: DoorDash's Greenhouse token is
`doordashusa`, Netflix left Lever for Eightfold, and AMD and Qualcomm are
no longer on Workday.

---

## Telling when a posting was added

Two independent signals. Both stored, both shown.

**1. `posted_at`, what the board says.** Every Tier A adapter pulls the
board's own timestamp and normalizes it to ISO 8601 UTC in `dates.py`:

| ATS | Field | Quality |
|---|---|---|
| Greenhouse | `first_published`, falls back to `updated_at` | exact |
| Lever | `createdAt` epoch ms | exact |
| Ashby | `publishedAt` | exact |
| SmartRecruiters | `releasedDate` | exact |
| Oracle | `PostedDate` | date only |
| Workable | `published_on` | exact |
| Recruitee | `published_at` | exact |
| Workday | `startDate`, falls back to `postedOn` | see below |

Workday is the weak one. It reports "Posted 3 Days Ago" as a string, so
resolution is one day at best and "30+ Days Ago" is a ceiling rather than a
date. `dates.from_workday_relative` parses it and the `posted_source`
column records which field the value came from, so you know when a date is
approximate.

**2. `first_seen`, when your scanner first saw it.** Every requisition gets
a `first_seen` on insert. This is the honest signal for any board that hides
dates, and it is the one that matters for applying early. Sort by it and
apply inside 48 hours.

The `--new-only` flag prints only postings that appeared for the first time
in this run. That is the daily-driver flag.

**3. `closed_at`, the disappearance signal.** After each run, any
requisition that was on a scanned company's board last time and is gone now
gets `closed_at` stamped. Requisition removed means role closed. This
replaces per-URL liveness checking entirely and costs one UPDATE. Only
companies actually scanned this run are eligible, so `--company nvidia`
never marks the rest of the registry dead.

---

## CLI

```
source:
  --file, -f PATH      registry CSV to read instead of registry/companies.csv
  --company, -c NAME   only companies whose name contains NAME (case insensitive)
  --ats TYPE           only rows using this ATS
  --list               print selected registry rows and exit, run nothing

filter:
  --bucket, -b LIST    explicit_early,unleveled (default) | senior | excluded
                       | role_miss | all
  --new-only           only postings this run saw for the first time
  --since DAYS         only postings first seen within DAYS
  --title REGEX        extra regex the title must match

output:
  --out, -o PATH       exact results path instead of the timestamped default
  --results-dir DIR    results folder, default results/
  --no-file            skip the results CSV, print only
  --format FMT         what goes to stdout: table (default) | csv | json | md
  --quiet, -q          write the results CSV, print only the summary
  --db PATH            sqlite path, default data/jobs.db
  --no-store           skip sqlite, print what the boards return right now

network:
  --concurrency N      default 20
  --timeout SEC        default 30
  --retries N          default 1
```

No arguments runs the full role regex against every company in the registry.

### Buckets

`core.py` sorts every title into one of five buckets:

- `explicit_early`: says new grad, university grad, entry level, junior,
  associate, Engineer I, L3, campus, rotational
- `unleveled`: matches a target role, carries no level marker. A bare
  "Software Engineer" at a large cap is frequently the new grad req, so
  these are kept by default rather than thrown away
- `senior`: senior, staff, principal, lead, manager, architect, II through
  V, L4+, "5+ years"
- `excluded`: intern, co-op, contractor, postdoc, part-time, non-engineering
- `role_miss`: does not match the role regex

Default output is `explicit_early,unleveled`. Use `--bucket all` to audit
what the filter is discarding.

Role regex covers software engineer, SWE, SDE, AI engineer, applied AI,
machine learning engineer, MLE, ML infra, deep learning, data engineer, data
scientist, analytics engineer, platform, infrastructure, backend, full
stack, frontend, research engineer, forward deployed, solutions, systems,
and product engineer.

---

## Results files

Every run writes `results/YYYY_MM_DD_HH_MM_XM.csv` stamped with the run
start time in local time. `2026_07_27_10_09_AM.csv` is 10:09 AM on 27 July
2026. Year first means the folder sorts chronologically by filename.
Five columns:

```
company,ticker,title,link,posted
Dell,DELL,Software Engineer Student - Haifa,https://...,2026-07-26
Figma,FIG,Forward Deployed Engineer,https://...,2026-07-15 08:42 AM
```

`posted` carries the board's own timestamp when it gave one, and falls back
to `first_seen` otherwise, so the column is never blank. Boards that report
a day with no clock time (Workday, Oracle) print a bare date rather than a
fabricated time. Excel parses both forms as dates.

`--out` overrides the path, `--results-dir` moves the folder, `--no-file`
skips the write. `results/` is gitignored.

### Command provenance

Every run records the exact command in three places, so any result file
traces back to what produced it.

**1. A sidecar next to the CSV**, `2026_07_27_10_18_AM.meta.json`:

```json
{
  "file": "2026_07_27_10_18_AM.csv",
  "command": "python jobscan.py --company NVDA --quiet",
  "argv": ["jobscan.py", "--company", "NVDA", "--quiet"],
  "cwd": "/home/you/jobscan",
  "started": "2026-07-27T10:18:06-07:00",
  "finished": "2026-07-27T10:19:31-07:00",
  "registry": "registry/companies.csv",
  "boards": 1, "raw": 2000, "shown": 94, "new": 94,
  "companies": ["NVIDIA"]
}
```

**2. `results/runs.csv`**, one row per run, append only:

```
file,started,finished,command,registry,boards,raw,shown,new,closed,errors
2026_07_27_10_18_AM.csv,2026-07-27 10:18:06 AM,...,python jobscan.py --company NVDA --quiet,...
```

**3. The `runs` table in sqlite**, with `command` and `result_file` columns.

The command stays out of the results CSV by default, because a run_command
column repeated across 900 identical rows is noise. Use `--embed-command`
if you want it inline anyway.

The command string is `shlex`-quoted, so it pastes straight back into a
shell and reproduces the run.

---

## Tracking applications

Name a posting by its link (or its key) to record that you applied, or to
star it for later. None of these flags scan; they read and write sqlite only.

```bash
jobscan --apply https://boards.greenhouse.io/acme/jobs/123
jobscan --star URL1 URL2
jobscan --unapply URL            # and --unstar
jobscan --tracked                # applied to or starred, with open / closed
jobscan --hide-applied           # a normal scan, minus what you applied to
```

The marks survive every later scan. They also stay on a posting after its
requisition leaves the board, so `--tracked` shows `closed 2026-10-20` next
to an application whose req has gone. Applying twice keeps the first date.
The MCP tools `mark_jobs` and `tracked_jobs` do the same from a session, and
`query_jobs` takes `applied` / `starred` filters.

---

## Architecture

```
registry/companies.csv          company -> (ats, token, tenant, site, ...)
        |
        v
registry.py                     validate, --company / --ats / --file select
        |
        +--> tier A (adapters/)  pure HTTP JSON, asyncio, concurrency 20
        |
        +--> tier B (tier_b.py)  Playwright, separate process, max 2 workers
        |
        v
core.classify                   title -> bucket
        |
        v
store.py                        sqlite: first_seen, last_seen, closed_at
        |
        v
cli.emit                        table | csv | json | md
```

Three rules the design enforces structurally rather than by convention:

**Routing is a table lookup, never inference.** The `ats` column picks the
adapter. Nothing decides at runtime whether to use the browser or the API,
so a Greenhouse URL can never accidentally take the expensive browser path.

**Tier A and Tier B never share an event loop.** Tier B lives in its own
module with its own entry point and a hard `MAX_BROWSER_WORKERS = 2`. Each
worker owns its own `BrowserContext` and `Page`. Concurrent navigation on a
shared browser corrupts tab state and returns reads from the wrong page.

**No search-engine scraping and no HTTP cache.** Every request carries
`Cache-Control: no-cache`. The ATS API is the source of truth. Search
engines cache job pages for weeks, which is how dead postings enter a
pipeline.

Failure is soft everywhere. A dead board logs an error, returns zero jobs,
and the batch continues. One retry with backoff, then it gives up.

---

## MCP server

Exposes scanning, querying and registry maintenance to Claude Code and any
other MCP client, over stdio.

```bash
python -m jobscan.mcp_server
```

`.mcp.json` in the repo root registers it, so Claude Code starts it per
session with no further setup. Paths come from the environment:

| Variable | Default |
|---|---|
| `JOBSCAN_REGISTRY` | `registry/companies.csv` |
| `JOBSCAN_DB` | `data/jobs.db` |
| `JOBSCAN_RESULTS` | `results` |
| `JOBSCAN_SELECTORS` | `registry/selectors.json` |

Fifteen tools:

| Tool | Network | What it does |
|---|---|---|
| `scan` | yes | Fetch boards, classify, store, write a results CSV |
| `scan_status` | no | Progress and result of a background scan |
| `scan_browser` | yes | The same for tier B boards, through a browser |
| `query_jobs` | no | Search stored open postings |
| `mark_jobs` | no | Record an application, or star a posting |
| `tracked_jobs` | no | Everything applied to or starred, closed ones too |
| `list_companies` | no | The registry, with tier and whether a row can be read |
| `registry_health` | no | Baseline coverage and freshness |
| `verify_boards` | yes | The seven health checks per board |
| `discover_company` | yes | Careers URL to a registry row |
| `add_company` | yes | Confirm a board live, then add its row |
| `update_company` | maybe | Change a row; rechecks the board if it now points elsewhere |
| `remove_company` | no | Delete a row |
| `get_run_history` | no | The `runs` table |
| `get_scan_results` | no | A past results folder and its sidecar |

The server adds no scraping, classification or date logic. It calls the same
`registry.load`, `runner.scan`, `core.classify` and `Store` that `cli.py`
does, so behaviour cannot drift between the CLI and the tools.

Scans of fewer than 10 boards block and return rows. Larger selections return
a `scan_id` immediately, because the full registry takes minutes and one slow
board would otherwise push the call past its timeout and lose the whole run.
`scan_status` then shows how many boards are done, the last few finished with
their counts, and the elapsed time. `scan(wait=true)` blocks on any selection
instead.

**Progress.** A blocking `scan`, and `scan_browser`, send MCP progress
notifications as boards finish ("37/118 boards; last NVIDIA: 2701
postings"), to any client that asked for them with a progress token. Clients
that did not ask see nothing different.

**Registry writes.** `add_company` takes either a full row (`ats` plus its
fields) or a careers `url` to discover the board from, and writes nothing
until the board's own adapter reads at least one posting from it live. A
discovery that matched only a partial name, a second row for the same
company and ats, and a tier B row (unless `allow_tier_b`) are refused.
`update_company` changes named fields of one row and reconfirms the board
when `ats`, `token`, `tenant`, `site`, `wd`, `host` or `query` changes.
`remove_company` deletes a row. All three edit `registry/companies.csv` line
by line, so every row they do not touch stays byte for byte as it was, and
validate the result before it replaces the file. `dry_run` checks without
writing. A new row has no health baseline until
`python tools/verify.py --company NAME --baseline`.

`scan`'s `limit` caps the returned array only. It never limits what is
scanned, stored, or written to the CSV, and `shown` always reports the true
total.

Two guarantees worth knowing. Every tool reports failure as
`{"ok": false, "error": ...}` rather than raising, because an exception
crossing the MCP boundary reaches the client as an opaque protocol error.
And provenance records the tool call, so a results file traces back to
`mcp:scan(company='nvidia')` rather than to the server process.

```bash
pytest                 # offline; two tests drive Chromium on a local page
pytest -m network      # the handful that hit a live board
```

---

## Tier B

Boards that refuse plain HTTP and only answer a real browser. A row is tier B
when its `ats` is `custom`, `taleo` or `phenom`. One row is today: Tesla,
whose edge answers 403 to every non-browser request.

Try everything else first. Of the 39 rows that started here, 38 turned out
not to need a browser: wrong CSV cells, an undocumented JSON endpoint, or a
different board behind the careers page. `docs/tier-b-triage.md` has the
history.

```bash
pip install playwright && python -m playwright install chromium
python -m jobscan.tier_b                   # every tier B row
python -m jobscan.tier_b --company tesla
```

It classifies, filters, stores and writes a results CSV exactly as the main
scan does, into the same database. From an MCP session, `scan_browser` runs
it as a child process.

Each board needs an entry in `registry/selectors.json`, keyed by the
registry company name, in one of two modes:

```json
{
  "Tesla": {
    "headed": true,
    "capture": "cua-api/apps/careers/state",
    "items": "listings",
    "fields": {"id": "id", "title": "t", "location": "l", "department": "dp"},
    "lookups": {"location": "lookup.locations", "department": "lookup.departments"},
    "job_url": "https://www.tesla.com/careers/search/job/{id}"
  },
  "Acme": {
    "card": "li.job", "title": "a.title", "location": ".location",
    "next": "button.load-more"
  }
}
```

**capture** waits for a JSON response the page itself fetches (its url
contains `capture`) and reads `items` out of it. `fields` maps id, title,
location and department to keys of each item; `lookups` names the table for
a field that holds an id rather than a name. **card** reads rendered
elements: `card` matches each posting, `title` and `location` are selectors
inside it, and `next` is a next-page or load-more button followed until it
is gone. Prefer capture when the page has such a response: it is one
request and does not break when the markup changes.

Both take `url` (default: the row's `careers_url`), `timeout_ms`, and
`headed`, which opens a visible browser window. Tesla needs it: the same
edge that refuses plain HTTP refuses headless Chromium, and serves a visible
one. A window appears for a few seconds per scan.

A tier B row with no entry is skipped and reported; `list_companies` shows
it as `implemented: false`. A board whose entry stops matching is an error,
never an empty board.

Work out an entry once per board, then commit it. Do not put a model in the
hot loop. That reintroduces exactly the nondeterministic tool selection the
table lookup exists to eliminate.

---

## Build order

All four steps are done; `BACKLOG.md` is the live list.

1. Registry to 100+ companies. `discover.py` plus `verify.py` make this
   mechanical. The registry is the asset. The runner is 400 lines and
   rewritable in an afternoon.
2. Apple, Amazon, Microsoft JSON adapters. Three of the largest employers,
   no browser needed.
3. A daily scan. Run from an MCP session (`scan`, then `scan_browser`)
   rather than a cron job, by decision.
4. Tier B, last. It is the most work per company and the least reliable.

## Layout

```
jobscan.py                 entry point
jobscan/
  cli.py                   argument parsing, output formatting
  core.py                  Job model, role and level regexes, classify()
  registry.py              CSV load, validate, select, tier split
  runner.py                tier A async fan-out
  store.py                 sqlite, first_seen / closed_at
  dates.py                 posted-date normalization per ATS
  tier_b.py                Playwright, separate process
  adapters/__init__.py     the tier A adapters, one function per board family
registry/
  companies.csv            the registry
  selectors.json           tier B per-tenant selectors
  playlists/               same columns, fewer rows
tools/
  discover.py              careers URL -> registry row
  verify.py                health check every tier A row
results/                   timestamped run output, gitignored
data/jobs.db               sqlite, gitignored
```
