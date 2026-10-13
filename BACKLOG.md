# Backlog

Worked top to bottom. Tick an item in the commit that completes it, with the
measured result next to it, so this file stays a record rather than a wish list.

Snapshot 2026-10-13: 119 registry rows, **118 tier A, 1 tier B** (Tesla,
read through a browser by `tier_b.py`); every row is now scanned. On 2026-10-01 it was 117 / 105 / 12, and 116 / 87 / 29 that morning.

2026-10-12 also found three of our own paging limits silently truncating
boards: Workday's 2,000 cap (NVIDIA 2,000 -> ~2,700), and `offset > 5000`
stops in the workday and oracle adapters (Lowe's 5,020 -> 12,603,
JPMorgan 5,100 -> 7,301). A full `verify.py` on an unfiltered network:
0 fail, no dead or unreachable links.

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
- [x] **Meta**: new `meta` adapter. Reads the rotating `doc_id` from the
      page's bundles each run; 1,061 postings on 2026-10-01, matching the job
      sitemap exactly. `robots.txt` says automated collection is prohibited
      without written permission; merged disabled, then enabled by owner
      decision on 2026-10-12: 1,138 postings, 166 early-career hits.
- [x] **Tesla**: Akamai answers 403 to plain HTTP on every tesla.com
      path, homepage included. Read by the browser tier since 2026-10-13
      (section 7).
- [x] **IBM**: new `ibm` adapter on `www-api.ibm.com/search/api/v2`. 2,044 =
      board total. Maps "PUNE, IN" / "Toronto, CA" country codes to country
      names, which would otherwise pass the US filter as Indiana / California.
- [x] **TikTok**: new `tiktok` adapter. 4,278 = board count, overlapping
      windows against live churn.
- [x] **10x Genomics**: new `kula` adapter. 34 = board count.

## 4. Remaining tier B (12 rows on 2026-10-01; only Tesla left)

- [x] **eightfold PCSX mode: Microsoft, PayPal, Qualcomm.** Highest value
      left. `/api/pcsx/search?domain=<d>&start=N` answers for all three once
      the `/careers` page has set session cookies (429 without them);
      Microsoft reports 2,352. The July "PCSX disabled" diagnosis came from
      the older `/api/apply/v2/jobs` path only.
      Done 2026-10-12: `site=pcsx` rows, tenant domain in `query`, all three
      in tier A. verify.py: Microsoft 2,236 against a count of 2,237 read
      just after (the board grew 2,235 to 2,237 during the hour), 37
      early-career hits; PayPal 312 of 312, 9 hits; Qualcomm 2,081 of 2,081,
      140 hits. All unique ids, 100% dated (`postedTs`), 3/3 links live.
      The order reshuffles like the old path (step 10 lost 5, 41 and 7
      rows), so the same half-page overlap. Qualcomm's 71 "Tijuana, Baja
      California, Mexico" rows read as US until `runner` masked that name.
- [x] **Atlassian**: unpaged JSON at `atlassian.com/endpoint/careers/listings`
      (351 entries, 333 unique ids, no total field). Small dedicated adapter.
      Done 2026-10-12: `atlassian` adapter, dedupes on the iCIMS id. 347
      entries, 329 unique, equal to the "329 RESULTS" the all-jobs page
      renders. verify.py ok: 329 jobs, 11 early-career hits, 100% dated
      (updatedDate, a last-updated date), 3/3 links live.
- [x] **Shopify, DigitalOcean**: one manual look before assuming a browser;
      `discover.py` misses board tokens unrelated to the company name.
      DigitalOcean done 2026-10-12: greenhouse `digitalocean98`, 189 postings
      = board meta.total = unique gh_jid links on the page, 2 early-career
      hits, 100% dated, verify.py ok.
      Shopify done 2026-10-12: Ashby ATS with no public Ashby board, so a
      `shopify` adapter reads the /careers React Router loader
      (`careers.data`, turbo-stream): 114 published listed postings, no
      total field; discipline pages are subsets of it. 7 early-career hits,
      100% dated, verify.py ok. The loader also ships each requisition's
      hiring team (names, emails); the adapter ignores it.
- [x] **Broadcom, Electronic Arts, Fidelity, Morgan Stanley**: identified
      2026-10-12; all four promoted (Morgan Stanley on eightfold PCSX).
  - [x] Broadcom: Workday `broadcom/External_Career` (links exist only in
        the rendered DOM). 386 of total 387, the other a titleless stub;
        15 early-career hits, 100% dated.
  - [x] Electronic Arts: Avature portal jobs.ea.com, new `avature` adapter
        (SearchJobs HTML, 20 a page). 362 of 362, 42 hits, undated (the RSS
        feed ignores paging).
  - [x] Fidelity: Workday `fmr/FidelityCareers` on wd1.myworkdaysite.com,
        behind jobs.fidelity.com. 795 of 795, 6 hits, 100% dated. The row's
        ticker FIS is a different company (fis.wd5 `SearchJobs`, 545).
  - [x] Morgan Stanley: eightfold PCSX at morganstanley.eightfold.ai
        (`site=pcsx`, query `morganstanley.com`; the v2 API 403s). 1,286 of
        1,286, 22 early-career hits, 100% dated, verify ok.
- [x] **Tesla**: browser only, done 2026-10-13 (see section 7).
- [x] **Meta**: enabled 2026-10-12 by owner decision (see section 3).

## 5. Data quality

- [x] **NVIDIA at Workday's 2,000 cap**: the workday adapter now slices a
      capped board by facet (`WORKDAY_CAP`). 2026-10-01: 2,655 postings, equal
      to the partition facets' sum (was 2,000). Paging past the cap only
      repeats: 4,019 rows held 1,999 unique.
- [x] **Accenture at Workday's 2,000 cap**: the real board is ~42,100
      postings (India 35,633, US 750) and no facet slices it under the cap
      cheaply (~2,100 requests). Workday rows can now filter server side via
      `token` = `facet=value name`; Accenture sets
      `locationCountry=United States of America`. 2026-10-12: 771 of the
      filtered 771, 22 early-career hits, verify ok, rebaselined.
- [x] **Salesforce**: 14 duplicate ids at 1,510. The old loop kept every row
      it was served, so a posting repeated across pages (the board changing
      mid-sweep) counted twice. Since 185c8d6 rows merge on externalPath.
      2026-10-12, two runs: 1,525 of 1,525, total unchanged before and after.
- [x] **Workday (the company)**: 2 duplicate ids at 373. Same cause as
      Salesforce. 2026-10-12, two runs: 373 of 373.
- [x] **JPMorgan Chase**: 5,100 was our own cap, not Oracle's. The adapter
      stopped past offset 5,000; the board reports 7,302 and pages cleanly.
      2026-10-12: 7,301 (one withdrawn mid-sweep), rebaselined. Lowe's had
      the same bug in the workday adapter (5,020 of 12,603). Every other
      adapter's page bound is at least 2x its largest board.
- [x] **SAP cross-board overlap**: measured 2026-10-12, kept both rows.
      17 of the SmartRecruiters board's 163 titles also appear on
      careers.sap.com, but only 5 share title *and* city, and title alone
      proves little (careers.sap.com itself has three separate reqs titled
      "IT Technology Services Senior Specialist" in Newtown Square). Against
      a boilerplate baseline from unrelated SAP ads, ~3 of the 5 look like
      the same posting on both boards, 1 of them US (SAP NS2, Herndon VA).
      Not worth cross-board fuzzy dedupe; the other ~160 are unique.
- [x] **Verily Life Sciences**: has its own Workday board after all,
      `verily.wd1` / `Verily_Careers`, linked from verily.com/careers.
      `discover.py` missed it (tenant not derived from the name). New row:
      78 of 78, 4 early-career hits, verify ok (2026-10-12).
- [x] **TikTok dates are inferred**: decoded from the id's creation time
      (`posted_source=id_epoch`), so evergreen postings show 2023. Owner
      decision 2026-10-12: keep them.

## 6. Tooling

- [x] **`verify.py` paginated check**: 17 adapters now record the board's
      own total (`_board_total`) and verify warns beyond max(2, 1%) of it.
      First full run, 2026-10-12: no paginated warning on 118 boards.
- [x] **`verify.py` dated check**: baseline-driven. The baseline stores each
      board's dated share; the warning is a 20-point fall against it, so the
      7 boards that never publish dates stop warning once baselined.
- [x] **`verify.py --baseline` prunes**: a full run on the default registry
      drops keys with no row (5 on the first run) and keeps the old entry
      of a row that failed that day.
- [x] **`verify.py` link check headers** (found 2026-10-12): sends browser
      navigation headers. Meta answered 400 without them; now 3/3 live.
- [x] **Amazon reads exactly 10,000**: the paginated check was silent because
      amazon.jobs' own API reports `hits: 10000` and refuses offsets past it.
      The adapter now splits a search at the ceiling by country
      (`normalized_country_code[]`), and a country still at the ceiling by
      `category[]`; both facets partition the board. 2026-10-13: 22,424 of
      22,424 (USA 13,323, so one split was not enough), 1,362 early-career
      hits (572 in July at 10,000), 100% dated, 3/3 links live.
- [x] **Workday facet slicing tops out around 10x the cap**: won't fix,
      owner decision 2026-10-13. Only Accenture is that large and its row
      filters to the US server side; a new board of that size gets the same
      `token` filter, and the paginated check says when one needs it. Measured on
      Accenture unfiltered, 2026-10-12: 20,847 of ~42,100 postings in 3,980
      requests and 99 minutes, because slices still over 2,000 after two
      levels are read only to the cap. Fine for NVIDIA (~2,700, 144
      requests). A board that large needs a `token` facet filter, as
      Accenture has; `verify.py`'s paginated check now flags the shortfall.
- [ ] **Apple serves empty search pages** ~15% of the time (2026-10-12). The
      adapter retries 8 times a page; if the rate climbs, a sweep will start
      failing loudly rather than returning a partial board.
- [x] **`discover.py` markers**: given a careers URL it now detects and
      confirms, through the real adapter, Radancy TalentBrew (`tbcdn`,
      `talentbrew`), careers-home iCIMS (`careers-home`, `jibecdn`),
      SuccessFactors classic and `site=unify`, Avature (`avacdn.net`,
      `avature.portal` metas, portal path into `site`), Kula, and eightfold
      plain or `site=pcsx` with the page's own `domain` in `query`. Front
      ends are followed one hop (at most 3 pages) to a Workday or other
      confirmable board, including `myworkdaysite.com` links; taleo / phenom
      stay tier B only when nothing confirms. Unconfirmed icims /
      successfactors / avature / eightfold markers, classic `*.icims.com`
      portals among them, are written as `custom`, not under a tier A name.
      2026-10-12: 17 of 17 registry boards rediscovered with the registry's
      own ats and config (Intuit, UHG, Schwab, AMD, Panasonic, GitHub, SAP,
      Supermicro, Altria, EA, 10x Genomics, Microsoft, PayPal, Qualcomm,
      Morgan Stanley, Netflix, Cisco). Confirmation is a full adapter sweep,
      so the large PCSX tenants take 3 to 5 minutes each.

## 7. Unbuilt features

Owner decisions, 2026-10-13, are recorded on each item.

- [x] **`tier_b.py`**: documented in the README, never written. Playwright
      runner for boards that need a browser. Decision: build it. Tesla is the
      only row today, but other board families will not answer plain HTTP
      either, and this is where they go.
      Done 2026-10-13: `python -m jobscan.tier_b`, its own process, at most
      2 pages at once, same classify / US filter / store / results CSV as
      tier A. `registry/selectors.json` holds one entry per board in one of
      two modes: `capture` (read a JSON response the page fetches) or `card`
      (read rendered elements, following a next / load-more button). MCP
      `scan_browser` runs it as a child process; `list_companies` marks a
      configured tier B row implemented.
      Tesla, capture mode: 8,329 listings in one payload, 5,383 on the US
      site by Tesla's own count; 5,401 kept (the difference is 13 with no
      location plus the board moving), 249 in the default buckets, about 5
      seconds. No posted dates. Headless Chromium gets the same 403 as
      plain HTTP, so the entry sets `headed` and a window opens per scan.
      Card mode is tested against a local page only: no registry row uses
      it yet.
- [x] **US filter let non-Latin locations through** (found by Tesla,
      2026-10-13): a location with no A-Z letters read as a non-answer like
      "Remote" and passed, 201 Japanese and Taiwanese Tesla postings among
      them. Any letter now counts as naming a place.
- [x] **A failed board had its postings closed** (found 2026-10-13): both
      scans passed errored companies to `upsert`, which stamped everything
      they held as closed until the next good scan. Errored boards are now
      left untouched.
- [x] **Application tracking**: `applied_at` and `starred` existed in the
      `jobs` schema unused. Decision: both a CLI command and MCP tools.
      Done 2026-10-13: `Store.mark` / `Store.tracked`; CLI `--apply`,
      `--unapply`, `--star`, `--unstar`, `--tracked`, `--hide-applied`; MCP
      `mark_jobs` and `tracked_jobs`, and `query_jobs` returns each row's
      key and marks and filters on them. A posting is named by url or key.
      Marks survive rescans and stay on a posting after it closes.
- [x] **MCP extensions**: HTTP transport, Docker, progress streaming, registry
      writes. Owner decision 2026-10-13: build progress streaming and registry
      writes; **won't do** HTTP transport or Docker (scans run only from a
      local session, and Tesla's visible browser window cannot run in a
      container).
      Done 2026-10-13. Progress: a blocking `scan` (any size with
      `wait=true`) and `scan_browser` send MCP progress notifications as
      boards finish; `scan_status` shows the last boards finished and the
      elapsed time. Registry writes: `add_company` (a full row, or discovery
      from a careers url), `update_company`, `remove_company`. Nothing is
      written until the board's adapter reads at least one posting live;
      edits touch one line of the CSV and are validated before they land.
      Checked end to end through a stdio server: Anduril added (greenhouse
      `andurilindustries`, 2,491 postings), a made-up token refused on its
      404, rows updated and removed, the copy byte-identical afterwards.
- [x] **Card mode checked live** (2026-10-13): Lever (Palantir, 312 of 312)
      and Greenhouse (Anthropic, 644 of 644 across 13 pages), link for link
      against their HTTP adapters. It found two bugs, both fixed: pagination
      stopped at page one (the list re-renders after load, detaching the
      button) and locations came out uppercased (innerText applies CSS).
- [x] **Full checks after the US filter change** (2026-10-13): a full tier A
      scan, 118 boards, 0 errors, 116,422 postings; the new filter drops none
      the old one kept, so the change only affected Tesla. `verify.py` on all
      118: 116 ok, Wing 29 -> 45 (real growth), Oracle a read timeout that
      passed on retry (2,363).
- [x] **Daily scan**: won't build a cron job or a hosted schedule. Decision:
      the daily scan is run from an interactive session connected to the MCP
      server (`scan`, then `get_scan_results`), nothing outside of that.
