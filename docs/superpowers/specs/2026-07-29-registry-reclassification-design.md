# Registry Reclassification Pass

Date: 2026-07-29
Status: implemented
Related: `docs/tier-b-triage.md` (the measured output),
`2026-07-29-jobscan-mcp-server-design.md` (consumes the corrected registry)

## Goal

Recover every registry row that an existing tier A adapter already handles, fix the
environment-level bugs that made healthy boards look dead, and produce a measured
difficulty classification of whatever genuinely remains.

Explicitly **not** a tier B implementation. No Playwright, no new ATS adapters.

## Why this had to run before any tier B work

The original plan was to build `tier_b.py` with Playwright for 39 rows. Probing the
rows first showed that premise was wrong. Two environment bugs and a set of wrong
CSV cells accounted for most of the "needs a browser" population.

Browser scraping is the most expensive tier per company and the least reliable. Any
row misfiled into it is a large, permanent waste. Measuring after repair rather than
before is the whole point of the sequencing.

## Scope

| In | Out |
|---|---|
| Environment fixes that cause false failures | Building any B1 family adapter |
| Registry cells for rows an existing adapter handles | Bespoke B2 endpoints (Apple, Meta, Tesla, IBM, TikTok) |
| One adapter branch for a second Workday host shape | `tier_b.py` / Playwright |
| Google subsidiary and baseline-key correctness | Google's 1,180-row pagination ceiling |
| A committed difficulty triage of the residue | |

## Changes

### 1. Decompression failure, `jobscan/adapters/__init__.py`

`HEADERS` gains `Accept-Encoding: gzip, deflate`.

httpx's default advertises zstd and brotli. On `httpx 0.28.1` / CPython `3.14.3` a
zstd response raises `DecodingError: cannot use a decompressobj multiple times`.
`amazon.jobs` serves zstd. Naming the two encodings every board supports avoids the
decoder bug without giving up compression, and applies to all ten adapters at once.

### 2. Silent failure in the Amazon adapter, same file

Removed `except Exception: break` from the pagination loop.

The bare catch converted a hard error into a successful scan of zero jobs, so the
runner recorded no error and `verify.py` saw a live board with no openings. Every
other adapter lets failures reach `runner._one`, which retries once and records the
error. Amazon was the only one that swallowed. The trailing unreachable `return out`
went with it.

### 3. TLS trust store, `jobscan/__init__.py`

`truststore.inject_into_ssl()` at package import, guarded by `try/except ImportError`.

`apply.ford.com` and `careers.microsoft.com` failed certifi verification with
`CERTIFICATE_VERIFY_FAILED`, because the intercepting root lives in the OS store and
never in certifi. Verification stays **on**; this changes which trust store is
consulted, not whether trust is checked. `verify.py` and `discover.py` benefit
identically since both import the package.

It lives in `__init__` because an SSL context created before injection keeps the old
behavior, and every entry point imports the package. A missing `truststore` is not
fatal.

### 4. Second Workday host shape, same file

The adapter hardcoded `{tenant}.wdN.myworkdayjobs.com`. Shared-cluster tenants
instead serve from `wd1.myworkdaysite.com` with the tenant only in the path, and
their public job links sit under `/recruiting/{tenant}/{site}` rather than `/{site}`.

An explicit `host` cell now selects the second shape for both the API URL and the
public job URL. Blank `host` keeps today's behavior, so the 24 existing Workday rows
are untouched. Verified by re-running them and comparing counts.

### 5. Alphabet subsidiary filter, same file

The `google` adapter read only `query`. The DeepMind, Waymo, Wing, and YouTube rows
have an empty `query`, so each returned Google's entire 1,180-row board with every
posting relabelled as the subsidiary. Roughly 4,700 wrongly attributed postings were
entering the database per full scan.

`token` now carries the org name the board's `company=` parameter expects. Confirmed:
DeepMind moved from 1,180 mislabelled Google rows to 87 real DeepMind roles.

### 6. Baseline key collision, `tools/verify.py`

`health.json` keyed on company name alone. Google's four query slices return 145 to
1,180 jobs each and all wrote to the key `Google`, so every run compared one slice
against whichever slice saved last and reported drift that did not exist. Three of
the four baseline warnings were this artifact.

Added `baseline_keys(rows)`, which appends a discriminator only for companies holding
more than one row, so the ~70 single-row baselines already on disk keep working.
`CheckResult` gains a `key` field, used for the baseline read, the baseline write, and
the display column. The snapshot also records `company` now, since the key is no
longer the company name.

### 7. Registry cells, `registry/companies.csv`

Eleven rows reclassified to tier A, each confirmed against a live API before
committing. Four subsidiary rows given their `token`. Full table with counts is in
`docs/tier-b-triage.md`.

Two candidates were **rejected**, both flagged by `discover.py`'s confidence
heuristic: `greenhouse/charles` for Charles Schwab (5 jobs, partial-name slug, an
unrelated company) and `recruitee/meta` for Meta (1 job). Neither was committed.

## Results

| | Before | After |
|---|---|---|
| Tier A rows | 79 | **89** |
| Tier B rows | 39 | **29** |
| `verify.py` ok | 71 | **82** |
| `verify.py` fail | 4 | **0** |
| Postings visible | 35,664 | **55,694** |
| Early-career hits | 2,171 | **2,807** |
| Amazon | 0 postings, FAIL | **10,000 postings, 572 hits** |

## Verification performed

- `tools/verify.py --sample 0` captured before and after and diffed. Failures went
  from 4 to 0. No row that previously passed regressed.
- All 24 pre-existing Workday rows re-run after the `host` change, counts unchanged,
  confirming the branch is backward compatible.
- NVIDIA specifically retried, since it returned 503 in the baseline. It returns
  2,000 postings, so the baseline failure was transient rather than caused by the
  adapter change.
- Amazon confirmed at 10,000 postings through the real adapter, and confirmed that a
  failing request now raises instead of returning `[]`.
- DeepMind confirmed at 87 filtered postings against 1,180 unfiltered, with
  `?company=DeepMind` returning `org=DeepMind` records.
- `health.json` rebaselined and inspected: 91 entries, 4 disambiguated keys.
- `python jobscan.py --list` reports 89 tier A / 29 tier B.

## Known-unfixed items

Recorded in `docs/tier-b-triage.md` under "Open items". The notable one found during
verification: the Google adapter truncates at 1,180 postings because its loop is
`range(1, 60)` at 20 records per page, and two of the four query slices report exactly
that ceiling, so their true totals are unknown.

## Next

`docs/tier-b-triage.md` classifies the remaining 29 rows into B1 (18 rows, 6 ATS
families, one adapter serves several), B2 (5 rows, bespoke, one board each), and C
(6 rows, browser-justified). Eightfold is the recommended B1 starting point because
Netflix already returns 476 jobs through it, narrowing the open question to why two
sibling tenants return 403.
