# Backlog

Worked top to bottom. Tick an item in the commit that completes it, with the
measured result next to it, so this file stays a record rather than a wish list.

Snapshot 2026-10-01: 116 registry rows, 87 tier A, 29 tier B. Last full
`verify.py`: 73 ok, 14 warn, 0 fail.

## 1. Follow-ups from the 2026-10-01 health pass

- [x] **Confirm Wing embed links.** Done 2026-10-01: 6/6 live. `site=embed` shipped in `4e6d6ef` but the
      live check was cut short when job-boards.greenhouse.io throttled the IP.
      Re-run `verify.py --company wing`.
- [x] **`verify.py`: tell throttling apart from dead links.** Done: 404/410 is dead; 5xx, 403, 429, timeouts are unreachable after one retry. `sample_live`
      counts any 5xx or timeout as dead, so a 503 burst reads as broken links.
- [x] **Refresh `docs/tier-b-triage.md`.** Done: open items restated with status, 2026-10-01 update section added. Open items #3 and #4 (Amazon zero
      jobs, Google 1,180 cap) were fixed in July; add the page-27 Google fix.
- [ ] **Rebaseline `health.json`** with `verify.py --baseline` on an
      unfiltered network, so ~12 boards stop warning on drift (Anthropic
      402 -> 637, Elastic 223 -> 393, ...).
- [x] **Investigate Airtable 41 -> 4.** Done: real. Greenhouse holds 4 sales roles; `discover.py --auto` finds no other board. Real, or a board move like 10x Genomics.

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
- [x] **icims** (5): AMD, Atlassian, Charles Schwab, GitHub, Panasonic. The
      `?format=json` path is dead; needs the newer `careers-home` API.
      Done 2026-10-01: `icims` adapter on `{host}/api/jobs`. 3 rows promoted,
      1,776 postings, each equal to the board's totalCount, 0 duplicate ids,
      100% dated: AMD 1,256 (55 hits), Panasonic 447 (5), GitHub 73 (0).
      Classic `*.icims.com` portals all return a 405 AWS WAF captcha.
      Atlassian and Charles Schwab have no careers-home site and moved to
      `custom`: Atlassian has an unpaged JSON list at
      `atlassian.com/endpoint/careers/listings` (333 unique ids), Schwab is
      Radancy TalentBrew at schwabjobs.com, since promoted on the `radancy`
      adapter (346 of 346). Atlassian details in its `notes`.
- [x] **phenom** (1): Cisco. No adapter needed: the Phenom site's own
      `applyUrl`s point at Workday, so the row now reads `workday`
      `cisco` / `wd5` / `Cisco_Careers` directly. 2026-10-01: 1,339 postings
      (Phenom mirror reported 1,307), 170 early-career hits, 100% dated,
      `verify.py` ok.
- [x] **avature** (1): Intuit. jobs.intuit.com is a Radancy TalentBrew
      front-end over Avature + "EH"; Avature's own search 404s. New
      `radancy` adapter. 2026-10-01: 567 of 567, 10 early-career hits,
      0% dated (the listing carries no date), `verify.py` warn on date only.
- [x] **taleo** (1): UnitedHealth Group. Also TalentBrew, over Taleo, whose
      REST search answers `careerSectionUnAvailable`. Same `radancy`
      adapter. 2026-10-01: 5,547 of 5,547, 72 early-career hits, 0% dated,
      `verify.py` warn on date only.

## 3. Tier B2: bespoke JSON, one company each

- [x] **Apple**: promoted to tier A (`apple` adapter). No token needed: the
      search pages are server rendered with hydration JSON and `totalRecords`;
      `/api/role/search` and `/api/csrfToken` now 404. 4,523 US postings,
      653 early-career hits, 99% dated, verify ok.
- [x] **Meta**: promoted to tier A (`meta` adapter). `doc_id` and LSD are
      read from the job search page and its direct script bundles on every
      run. 1,061 postings (= the 1,061 URLs in `/jobsearch/sitemap.xml`),
      145 early-career hits. verify warns: 0% dated (the query has no date;
      `datePosted` exists only on each 500 KB job page) and 0/5 live,
      because Meta answers 400 to any request without browser
      `Sec-Fetch-*` headers; the same URLs return 200 with them.
      `robots.txt` carries a notice prohibiting automated collection
      without written permission, so the row is merged DISABLED (`custom`);
      set `ats=meta` to enable. Owner decision pending.
- [ ] **Tesla**: blocked over plain HTTP (2026-10-01). Akamai answers 403
      Access Denied on every tesla.com path, the homepage included, so the
      `/cua-api/` endpoints cannot be reached either. Browser tier only.
- [x] **IBM**: done. New `ibm` adapter on `www-api.ibm.com/search/api/v2`
      (appId `careers`, scope `careers2`, from the search page's inline
      config). 2,044 postings = board total, 0 duplicate ids, 100% dated,
      208 early-career hits. Sorted on `_id`: the page's own score sort
      ties at 0 for every doc and is not a stable order.
- [x] **TikTok**: done. New `tiktok` adapter on
      `api.lifeattiktok.com/api/v1/public/supplier/search/job/posts` (needs
      `website-path: tiktok` and `origin` headers, else 400). 4,278 = board
      count, 0 duplicate ids, 519 early-career hits. No date field: dates are
      decoded from the snowflake id (creation time, `posted_source=id_epoch`).
      Overlapping windows: one plain sweep lost 2 of 4,281 to live churn.
- [x] **10x Genomics**: done. New `kula` adapter on Kula's unauthenticated
      `/api/internal/ats_job_posts` (token = account name). 34 = meta.count =
      job links on the page, 0 duplicates, 100% dated, 0 early-career hits.

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
