# Tier B triage

Measured 2026-07-29, after the reclassification pass. Every claim here came from
calling a live endpoint, not from reading a page for markers.

Read this before starting any tier B work. It exists so the next project is scoped
against evidence instead of estimates.

## Why this was measured after the infrastructure fixes, not before

Two environment-level bugs made healthy boards look dead:

- `httpx 0.28.1` on CPython 3.14.3 raised `DecodingError: cannot use a decompressobj
  multiple times` on any zstd response. Amazon serves zstd by default, so its
  adapter returned zero jobs and reported success.
- Certifi's bundle rejected `apply.ford.com` and `careers.microsoft.com` with
  `CERTIFICATE_VERIFY_FAILED`, because the intercepting root lives in the OS store.

Both present as "the board returns nothing," which is indistinguishable from "the
board needs a browser." Any triage run before fixing them would have filed working
API boards into the browser tier, which is the most expensive tier per company.
Amazon alone would have been misfiled, and it carries 10,000 postings.

## Result of the pass

| | Before | After |
|---|---|---|
| Registry rows | 118 | **117** |
| Tier A | 79 | **89** |
| Tier B | 39 | **28** |
| `verify.py` ok | 71 | **85** |
| `verify.py` fail | 4 | **0** |
| Postings visible | 35,664 | **57,255** |
| Early-career hits | 2,171 | **2,747** |

The hit count understates the gain. Before, Google's four query slices each counted
their own matches (65 + 154 + 148 + 24 = 391) over heavily overlapping result sets,
so the same requisition was counted several times. The single collapsed row reports
192 real matches. The 2,647 figure is deduplicated where 2,171 was not.

Twelve rows moved to tier A: ten needed only correct CSV cells, one needed a
one-branch Workday change, and Netflix needed the new eightfold adapter. Registry
row count *fell* because Google's four query slices collapsed into one once the
pagination ceiling that forced them was removed.

## Difficulty tiers

### Tier A: existing adapter, data fix only

Done. Listed for the record, with confirmed counts.

| Company | Now | Confirmed |
|---|---|---|
| Oracle | oracle `eeho.fa.us2.oraclecloud.com` / `jobsearch` | 2,316 |
| Wells Fargo | workday `wf` / `WellsFargoJobs` via `wd1.myworkdaysite.com` | 1,866 |
| Ford | oracle `efds.fa.em5.oraclecloud.com` / `CX_1` | 840 |
| Intel | workday `intel` / `wd1` / `External` | 639 |
| Okta | greenhouse `okta` | 363 |
| American Express | oracle `egug.fa.us2.oraclecloud.com` / `CX_1` | 349 |
| Epic Games | greenhouse `epicgames` | 137 |
| Vercel | greenhouse `vercel` | 79 |
| Dropbox | greenhouse `dropbox` | 28 |
| 10x Genomics | greenhouse `10xgenomics` | 26 |

Amazon is not in this table because it never left tier A. It was failing on the
decompression bug and now returns 10,000 postings with 572 early-career hits.

### Tier B1: new adapter, known ATS family, one adapter serves several rows

18 rows across 6 families. Highest value per hour of the remaining work, because
each adapter amortizes across its family and follows the existing contract
`async def fn(client, company, row) -> list[Job]` with no new architecture.

| Family | Rows | Companies | Evidence |
|---|---|---|---|
| successfactors | 7 | Microsoft, SAP, TSMC, Hyundai, Paramount Global, Supermicro, Altria | Untested. Largest family, so highest payoff if a shared endpoint exists. |
| icims | 5 | AMD, Atlassian, Charles Schwab, GitHub, Panasonic | `?format=json` returned HTML 404 / 405. Needs the newer `careers-home` API path. |
| avature | 1 | Intuit | Avature markers present at `jobs.intuit.com`. |
| taleo | 1 | UnitedHealth Group | Untested. |
| phenom | 1 | Cisco | Phenom markers in the page served from `careers.cisco.com`. Registry still says `custom`, so it is counted under Tier C below until an adapter exists. |

**eightfold is done.** Adapter written, Netflix in tier A at 476 postings with zero
duplicates and 100 percent carrying a board date. See "Resolved" below for why it
serves one row rather than three.

Do not start with iCIMS or SuccessFactors on row count alone. Both are entirely
untested, so their row counts are potential, not confirmed.

### Tier B2: bespoke JSON, one company each, no reuse

5 rows. Each needs its own investigation and yields exactly one board.

| Company | What is known |
|---|---|
| Apple | `POST /api/role/search` exists. No CSRF token found in the 328 KB search page by either meta tag or cookie, so token acquisition is unsolved. |
| Meta | `metacareers.com` GraphQL requires a `doc_id` that rotates. `discover.py` matched `recruitee/meta` with 1 job, a slug squatter, correctly flagged `VERIFY`. |
| Tesla | No board marker in the initial HTML. Listings load client-side. |
| IBM | No board marker in the initial HTML. |
| TikTok | No board marker in the initial HTML. Custom portal, API unconfirmed. |

### Tier C: browser required, or not yet identified

8 rows. No board marker in server-rendered HTML and no slug match across any
token ATS, or a known board that refuses public API access.

Broadcom, DigitalOcean, Electronic Arts, Fidelity, Morgan Stanley, Shopify, plus
**PayPal** and **Qualcomm** (eightfold tenants with PCSX disabled, see Resolved
below). Cisco sits here too until a phenom adapter exists.

Shopify and DigitalOcean are worth one more manual look before committing to a
browser. Companies that size usually sit on a standard board, and `discover.py`
only probes name-derived slugs, which misses a board token unrelated to the
company name.

## Resolved after the first triage draft

**eightfold serves one row, not three.** PayPal and Qualcomm return
`403 {"message": "Not authorized for PCSX"}` for every parameter combination, on
their own hosts and on `app.eightfold.ai` with a `domain` param. PCSX is Eightfold's
public career-site API, enabled per tenant, and those two have it switched off. No
request shape fixes an authorization setting. Both moved to `custom` with that
recorded in `notes`, so they now belong to Tier B2/C and need a different route.

`app.eightfold.ai?domain=netflix.com` returns the same 476 rows as Netflix's own
host, confirming the API is centralized and the difference is purely tenant config.

**The eightfold board reorders results between requests.** Every `sort_by` value it
accepts (`relevance`, `timestamp`, `distance`, `recent`) and none at all produce
both duplicates and skips. A non-overlapping sweep of Netflix's 476 returned 471
unique, losing 5 silently. Measured: step=10 loses 5, step=8 loses 1, step=5 loses 0.
Paging past the reported total does not help, because gaps are scattered rather than
trailing. The adapter advances by half a page and dedupes on id, costing 96 requests
instead of 48 for complete coverage.

The general lesson: compare against the board's own count. Without `count` there
would have been no signal that 5 rows vanished.

**Waymo and Wing genuinely have ~1 posting each on the Google board.** Not a filter
bug. The facet totals reconcile exactly: Google 3,381 + DeepMind 87 + YouTube 145 +
4 across Waymo/Wing/GFiber/Verily = 3,617. Their real hiring lives elsewhere, so
discovering separate boards for Waymo and Wing is worth a look.

**Google's 1,180 ceiling was ours.** The board paginates to page 181 of 3,617 and
returns nothing at 200. Fixed, and the four query-slice rows collapsed into one.
The board also exposes usable facets: `target_level` (EARLY 417, MID 1,944,
ADVANCED 1,113), `location=United States` (2,034 of 3,617), and `company` for the
seven orgs. `GFiber` and `Verily Life Sciences` appear in that facet and are absent
from the registry, though at ~1 posting each.

**Workday's `bulletFields` is not an identifier, and trusting it lost postings.**
It is a tenant-configured *display* field, the bullets on a result card. 24 of the
26 boards surveyed put the requisition number there (`JR2022322`, `R170608`), which
is why it appeared to work. Intel puts the badge `"Spotlight Job"` on 31 postings and
Moderna puts a city (`"Cambridge, Massachusetts"`).

Since `Job.key` hashes `raw_id`, every colliding posting produced one key, so
`store.upsert` inserted one row and updated it with the rest. **Intel silently lost
30 of 640 requisitions on every scan, Moderna 11 of 186.** `externalPath` is unique
per posting (verified 640/640 on Intel) and is now the fallback, applied only to ids
that actually collide so the 24 healthy boards keep their existing keys and stored
history.

Overlapping windows were the wrong hypothesis here. Intel returns exactly 610 unique
at step 20, 15, and 10, which is what proved the loss was identity collision rather
than pagination jitter.

**Alphabet subsidiaries mostly have their own boards, and they are much larger.**
Found only because the "1 posting" result was treated as a smell rather than as
reconciled. Note the triage method missed these: only tier B rows were re-probed, and
these were already tier A and nominally working.

| Company | Google board | Own board |
|---|---|---|
| Waymo | 1 | greenhouse `waymo`, **398** (54 early-career hits) |
| GFiber | ~1 | greenhouse `googlefiber`, **81** (was absent from the registry) |
| Wing | 1 | greenhouse `wing`, **38** |
| DeepMind | **87** | greenhouse `deepmind`, 10, zero title overlap, kept as a second row |
| YouTube | **145** | none found, stays on the Google board |
| Verily Life Sciences | ~1 | none found, not added |

## Open items found during the pass, not fixed

1. **Accenture reports 40 duplicate ids** across 1,997 postings. Unlike Intel and
   Moderna its `bulletFields` are genuine requisition numbers, so this is true
   pagination overlap against Workday's platform-level 2,000 result cap. The registry
   `notes` for that row already prescribe the remedy: add query rows to slice the
   board, the same technique Google needed before its ceiling was lifted.
2. **Verily Life Sciences and YouTube** have no standalone board, so they depend on
   the Google org facet. YouTube is well covered at 145; Verily is not, at ~1.
3. **Accenture caps at 2,000** postings and reports 95 duplicate requisition ids.
   Pre-existing.
4. **The Google adapter truncates at 1,180 postings.** Its loop is
   `for page in range(1, 60)` at 20 records per page, so 59 x 20 = 1,180 is a hard
   ceiling rather than a real count. Two of the four query slices (`data engineer`
   and `software engineer`) report exactly 1,180, meaning both are cut off and the
   true totals are unknown. The query slicing was added to work around this, and it
   is not slicing finely enough. Raising the ceiling or adding narrower slices would
   recover real postings. Not touched by this pass.
5. **`health.json` baselines were rewritten** by this pass, now holding 91 entries.
   Keys for multi-row companies changed from `Google` to `Google [software engineer]`
   and similar, so the four Google query slices hold separate baselines instead of
   overwriting one another. The Alphabet subsidiaries needed no suffix, since their
   company names are already distinct.

## Method, for repeating this

```bash
# 1. list current tier B rows in discover.py --batch format
python - <<'EOF' > pending_tierb.txt
import csv
from jobscan.adapters import TIER_A
for r in csv.DictReader(open("registry/companies.csv", newline="", encoding="utf-8-sig")):
    if r["company"] and r["ats"] not in TIER_A:
        print(f"{r['company']}\t{r['ticker'] or ''}\t{r['careers_url']}".rstrip())
EOF

# 2. probe every token ATS plus workday, confirming against live APIs
python tools/discover.py --batch pending_tierb.txt --auto --concurrency 4

# 3. never commit a row carrying a VERIFY flag without checking it by hand
```

`discover.py` refuses to emit on a pattern match alone and flags partial-name slugs.
That is what kept `greenhouse/charles` (5 jobs, an unrelated company) and
`recruitee/meta` (1 job) out of the registry during this pass. Trust the flags.
