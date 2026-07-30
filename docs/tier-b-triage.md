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
| Registry rows | 118 | 118 |
| Tier A | 79 | **89** |
| Tier B | 39 | **29** |
| `verify.py` ok | 71 | **81** |
| `verify.py` fail | 4 | **0** |
| Postings visible | 35,664 | **55,694** |
| Early-career hits | 2,171 | **2,807** |

Eleven rows moved to tier A. Ten needed only correct CSV cells. One needed a
one-branch adapter change.

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
| eightfold | 3 | Netflix, PayPal, Qualcomm | **Netflix confirmed working: 476 jobs** via `/api/apply/v2/jobs?domain=netflix.com`. PayPal and Qualcomm return 403 on the same shape, so tenant configuration varies. |
| phenom | 1 | Cisco | Phenom markers present in the page served from `careers.cisco.com`. Registry still says `custom`. |
| avature | 1 | Intuit | Avature markers present at `jobs.intuit.com`. |
| taleo | 1 | UnitedHealth Group | Untested. |

Start with eightfold. One tenant already works, so the remaining question is narrow
(what differs about the 403 tenants) rather than open-ended.

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

6 rows. No board marker in server-rendered HTML and no slug match across any
token ATS. These are the only rows that justify Playwright.

Broadcom, DigitalOcean, Electronic Arts, Fidelity, Morgan Stanley, Shopify.

Shopify and DigitalOcean are worth one more manual look before committing to a
browser. Companies that size usually sit on a standard board, and `discover.py`
only probes name-derived slugs, which misses a board token unrelated to the
company name.

## Open items found during the pass, not fixed

1. **Waymo and Wing return 1 posting each.** The `company=` filter works (both were
   returning Google's full 1,180-row board before), but a single result suggests the
   filter value the board expects differs from the plain subsidiary name. Low
   priority, correctness already improved.
2. **Intel reports 1 duplicate requisition id**, Moderna 11. Pagination overlap in
   the workday adapter. Pre-existing for Moderna.
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
