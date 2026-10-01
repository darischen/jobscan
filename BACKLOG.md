# Backlog

Worked top to bottom. Tick an item in the commit that completes it, with the
measured result next to it, so this file stays a record rather than a wish list.

Snapshot 2026-10-01: 116 registry rows, 87 tier A, 29 tier B. Last full
`verify.py`: 73 ok, 14 warn, 0 fail.

## 1. Follow-ups from the 2026-10-01 health pass

- [ ] **Confirm Wing embed links.** `site=embed` shipped in `4e6d6ef` but the
      live check was cut short when job-boards.greenhouse.io throttled the IP.
      Re-run `verify.py --company wing`.
- [ ] **`verify.py`: tell throttling apart from dead links.** `sample_live`
      counts any 5xx or timeout as dead, so a 503 burst reads as broken links.
- [ ] **Refresh `docs/tier-b-triage.md`.** Open items #3 and #4 (Amazon zero
      jobs, Google 1,180 cap) were fixed in July; add the page-27 Google fix.
- [ ] **Rebaseline `health.json`** with `verify.py --baseline` on an
      unfiltered network, so ~12 boards stop warning on drift (Anthropic
      402 -> 637, Elastic 223 -> 393, ...).
- [ ] **Investigate Airtable 41 -> 4.** Real, or a board move like 10x Genomics.

## 2. Tier B1: new adapter, one family serves several rows

Highest value per hour. SuccessFactors and iCIMS are untested, so their row
counts are potential, not confirmed.

- [x] **successfactors** (7): Microsoft, SAP, TSMC, Hyundai, Paramount Global,
      Supermicro, Altria. Done 2026-10-01: 6 of 7 promoted to tier A, plus a
      second SAP row, 2,865 postings and 94 early-career hits, every row's
      count equal to the board's own total with zero duplicate ids. Classic
      CSB listing: SAP 788 (careers.sap.com, 34 hits), Supermicro 1,037 (32),
      TSMC 325 (15), Hyundai 288 (5), Paramount 279 (8). Unify JSON
      (`site=unify`): Altria 105 (0). SAP's new SmartRecruiters board
      `SAPITBusinessSysteme` added as a second row, 43. SAP and Supermicro
      show no date on their listings, so they verify as warn on `dated` 0%.
      **Microsoft is not SuccessFactors**: it is an eightfold PCSX site, moved
      to `custom`. `/api/pcsx/search?domain=microsoft.com` serves count 2,352
      with session cookies, and the same endpoint also answers for PayPal and
      Qualcomm, whose "PCSX disabled" diagnosis came from the older
      `/api/apply/v2/jobs` path. A pcsx mode in the eightfold adapter would
      cover all three.
- [ ] **icims** (5): AMD, Atlassian, Charles Schwab, GitHub, Panasonic. The
      `?format=json` path is dead; needs the newer `careers-home` API.
- [ ] **phenom** (1): Cisco. Registry still says `custom`.
- [ ] **avature** (1): Intuit
- [ ] **taleo** (1): UnitedHealth Group

## 3. Tier B2: bespoke JSON, one company each

- [ ] **Apple**: `POST /api/role/search` exists; CSRF token acquisition unsolved.
- [ ] **Meta**: GraphQL `doc_id` rotates.
- [ ] **Tesla**: listings load client-side, API unidentified.
- [ ] **IBM**: no board marker in server HTML.
- [ ] **TikTok**: custom portal, API unconfirmed.
- [ ] **10x Genomics**: moved to Kula (`careers.kula.ai`). Low value at 26 jobs.

## 4. Tier C: browser required, or board not identified

Broadcom, DigitalOcean, Electronic Arts, Fidelity, Morgan Stanley, Shopify,
PayPal, Qualcomm.

- Shopify and DigitalOcean deserve one manual look first: `discover.py` only
  probes name-derived slugs and misses a board token unrelated to the name.
- PayPal and Qualcomm are eightfold tenants with the public PCSX API disabled
  (`403 Not authorized for PCSX`). No request shape fixes that.

## 5. Data quality

- **Accenture** sits at Workday's 2,000-result cap with ~32 duplicate ids.
  Slice the board with query rows, as Google once was.
- **JPMorgan Chase**: 1 duplicate id at 5,100 postings. Check for a cap.
- **Verily Life Sciences**: ~1 posting via the Google org facet, no
  standalone board found.

## 6. Unbuilt features

- **`tier_b.py`**: documented in the README, never written. Playwright runner
  for boards that need a browser.
- **Application tracking**: `applied_at` and `starred` exist in the `jobs`
  schema and are unused.
- **MCP extensions**: HTTP transport, Docker, progress streaming, registry writes.
- **Daily cron** writing `--new-only --format md` (README roadmap #3).
