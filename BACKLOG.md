# Backlog

Worked top to bottom. Tick an item in the commit that completes it, with the
measured result next to it, so this file stays a record rather than a wish list.

Snapshot 2026-10-01, end of day: 117 registry rows, **105 tier A, 12 tier B**
(was 116 / 87 / 29 that morning). Last full `verify.py`: 84 ok, 21 warn,
0 fail, baselines rewritten. The 18 newly promoted rows carry 23,369
postings and 1,813 early-career hits; the Google fix recovered ~2,700 more.

## 1. Follow-ups from the 2026-10-01 health pass

- [x] **Confirm Wing embed links.** `site=embed` (`4e6d6ef`): 6/6 live.
- [x] **`verify.py`: tell throttling apart from dead links.** 404/410 is dead;
      5xx, 403, 429 and timeouts are "unreachable" after one retry (`dd276da`).
      Paid off the same day: on the filtered network Roblox, Epic Games and
      Coinbase now read unreachable, not dead.
- [x] **Refresh `docs/tier-b-triage.md`.** Open items restated with status;
      dated section for 2026-09-25 to 2026-10-01 (`1c3aeed`).
- [x] **Rebaseline `health.json`.** 105 boards, 2026-10-01. Counts come from
      the board APIs, which the filtered network does not block, so the
      baselines are valid even though link sampling ran on that network.
- [x] **Investigate Airtable 41 -> 4.** Real: greenhouse holds 4 sales roles
      and `discover.py --auto` finds no other board.

## 2. Tier B1: new adapter, one family serves several rows

- [x] **successfactors** (7): new `successfactors` adapter, classic listing
      plus `site=unify` JSON. 6 promoted, every count equal to the board's
      total: SAP 788, Supermicro 1,037, TSMC 325, Hyundai 288, Paramount 279,
      Altria 105 (Altria reorders, so it re-sweeps to the total). SAP gained a
      second row for its new SmartRecruiters board (43). **Microsoft is not
      SuccessFactors**: it is an eightfold PCSX site, see section 4.
- [x] **icims** (5): new `icims` adapter on the careers-home `/api/jobs`.
      AMD 1,256, Panasonic 447, GitHub 73, each equal to `totalCount`.
      Classic `*.icims.com` portals all return a 405 AWS WAF captcha. Charles
      Schwab turned out to be Radancy TalentBrew and went tier A on that
      adapter (346 of 346). Atlassian stays tier B, see section 4.
- [x] **phenom** (1): Cisco needed no adapter. The Phenom site mirrors the
      Workday board it applies through, so the row reads Workday directly:
      1,339 postings (the mirror showed 1,307).
- [x] **avature** (1): Intuit lists publicly only on Radancy TalentBrew. New
      `radancy` adapter: 567 of 567.
- [x] **taleo** (1): UnitedHealth Group, also TalentBrew (Taleo's search
      answers `careerSectionUnAvailable`): 5,547 of 5,547.

## 3. Tier B2: bespoke JSON, one company each

- [x] **Apple**: new `apple` adapter. No token needed: search pages are
      server rendered with the results as JSON plus `totalRecords`. 4,523 US
      postings, 653 early-career hits.
- [x] **Meta**: new `meta` adapter, merged **disabled**. Reads the rotating
      `doc_id` from the page's bundles each run; 1,061 postings, matching the
      job sitemap exactly. `robots.txt` says automated collection is
      prohibited without written permission, so the row is `custom` until
      the owner decides. Set `ats=meta` to enable.
- [ ] **Tesla**: blocked. Akamai answers 403 to plain HTTP on every
      tesla.com path, homepage included. Browser tier only (section 6).
- [x] **IBM**: new `ibm` adapter on `www-api.ibm.com/search/api/v2`. 2,044 =
      board total. Maps "PUNE, IN" / "Toronto, CA" country codes to country
      names, which would otherwise pass the US filter as Indiana / California.
- [x] **TikTok**: new `tiktok` adapter. 4,278 = board count, overlapping
      windows against live churn.
- [x] **10x Genomics**: new `kula` adapter. 34 = board count.

## 4. Remaining tier B (12 rows)

- [ ] **eightfold PCSX mode: Microsoft, PayPal, Qualcomm.** Highest value
      left. `/api/pcsx/search?domain=<d>&start=N` answers for all three once
      the `/careers` page has set session cookies (429 without them);
      Microsoft reports 2,352. The July "PCSX disabled" diagnosis came from
      the older `/api/apply/v2/jobs` path only.
- [ ] **Atlassian**: unpaged JSON at `atlassian.com/endpoint/careers/listings`
      (351 entries, 333 unique ids, no total field). Small dedicated adapter.
- [ ] **Shopify, DigitalOcean**: one manual look before assuming a browser;
      `discover.py` misses board tokens unrelated to the company name.
- [ ] **Broadcom, Electronic Arts, Fidelity, Morgan Stanley**: unidentified.
- [ ] **Tesla**: browser only (see section 3).
- [ ] **Meta**: owner decision (see section 3).

## 5. Data quality

- [ ] **NVIDIA at Workday's 2,000 cap**: the board reports `total=2000`, so
      the real count is unknown. Slice with query rows, as for Accenture.
- [ ] **Accenture at Workday's 2,000 cap**: 1,998 with 19 duplicate ids.
- [ ] **Salesforce**: 14 duplicate ids at 1,510 (new on 2026-10-01). Check
      whether Workday reorders between pages here, as eightfold does.
- [ ] **Workday (the company)**: 2 duplicate ids at 373.
- [ ] **JPMorgan Chase**: 5,100 postings, suspiciously round. Check for a cap.
- [ ] **SAP cross-board overlap**: one title appears on both careers.sap.com
      and the SmartRecruiters board. Confirm whether it is one posting twice.
- [ ] **Verily Life Sciences**: ~1 posting via the Google org facet, no
      standalone board found.
- [ ] **TikTok dates are inferred**: decoded from the id's creation time
      (`posted_source=id_epoch`), so evergreen postings show 2023. Owner
      sign-off, or drop the date.

## 6. Tooling

- [ ] **`verify.py` paginated check**: the docstring promises "count matches
      the board's own total" but nothing implements it. It would have caught
      Google's page-27 stop automatically. Needs each adapter to report the
      board's total.
- [ ] **`verify.py` dated check**: Radancy, SAP, Supermicro and Meta boards
      publish no dates, so they warn on every run. Allow a per-adapter or
      per-row "undated" expectation.
- [ ] **`verify.py --baseline` never prunes**: keys for removed or rekeyed rows
      (e.g. the old single-row `SAP`) stay forever. Drop keys with no row on
      a full, unfiltered run.
- [ ] **`discover.py` markers**: recognise Radancy TalentBrew (`tbcdn`,
      `talentbrew`), careers-home iCIMS and Kula; it still sends taleo /
      avature / phenom sites to tier B though all three proved to be
      fronts for other boards.

## 7. Unbuilt features

- **`tier_b.py`**: documented in the README, never written. Playwright runner
  for boards that need a browser (Tesla, and whatever section 4 leaves).
- **Application tracking**: `applied_at` and `starred` exist in the `jobs`
  schema and are unused.
- **MCP extensions**: HTTP transport, Docker, progress streaming, registry writes.
- **Daily cron** writing `--new-only --format md` (README roadmap #3).
