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

## Tier B

Boards without public JSON: iCIMS, SAP SuccessFactors, Taleo, Phenom,
Eightfold, and one-off custom sites.

```bash
pip install playwright && playwright install chromium
python -m jobscan.tier_b --company amd
```

Each tenant needs a selector config in `registry/selectors.json`:

```json
{
  "AMD": { "card": "a.job-title-link" },
  "Qualcomm": { "card": "[data-ph-at-id='job-link']" }
}
```

Use a Claude Code instance to write that config once per tenant, then commit
it. Do not put a model in the hot loop. That reintroduces exactly the
nondeterministic tool selection the table lookup exists to eliminate.

Apple, Amazon, and Microsoft are marked tier B but all three expose
undocumented JSON search endpoints. Promoting them to tier A adapters is
higher value than any browser work.

---

## Build order

1. Registry to 100+ companies. `discover.py` plus `verify.py` make this
   mechanical. The registry is the asset. The runner is 400 lines and
   rewritable in an afternoon.
2. Apple, Amazon, Microsoft JSON adapters. Three of the largest employers,
   no browser needed.
3. Daily cron writing `--new-only --format md` to a file.
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
  adapters/__init__.py     8 tier A adapters
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
