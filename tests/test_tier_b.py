"""Tier B: boards read through a browser. Offline.

Turning a captured JSON payload or a list of scraped cards into jobs is pure
and tested directly. The browser half runs against a page on disk, so it
needs Chromium but no network, and is skipped where Playwright is absent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobscan import tier_b
from jobscan.store import Store

# The shape of tesla.com/cua-api/apps/careers/state, 2026-10-13.
STATE = {
    "lookup": {
        "locations": {"10001": "Akron, Ohio", "401022": "Palo Alto, California",
                      "1013": "Luxembourg, Luxembourg"},
        "departments": {"3": "Tesla AI", "8": "Engineering & Information Technology"},
    },
    "listings": [
        {"id": "224501", "t": "Software Engineer, Optimus", "dp": "3", "l": "401022"},
        {"id": "224502", "t": "Service Technician", "dp": "8", "l": "10001"},
        {"id": "224503", "t": "Software Engineer", "dp": "8", "l": "1013"},
        {"id": "224504", "t": "Software Engineer, Unknown Site", "dp": "99", "l": "777"},
    ],
}
JSON_CFG = {
    "capture": "cua-api/apps/careers/state",
    "items": "listings",
    "fields": {"id": "id", "title": "t", "location": "l", "department": "dp"},
    "lookups": {"location": "lookup.locations", "department": "lookup.departments"},
    "job_url": "https://www.tesla.com/careers/search/job/{id}",
}
ROW = {"company": "Tesla", "ticker": "TSLA", "ats": "custom",
       "careers_url": "https://www.tesla.com/careers/search/?site=US"}


# ---------------------------------------------------------------- extraction
def test_json_payload_becomes_jobs_through_the_lookup_tables():
    jobs = tier_b.jobs_from_json(ROW, JSON_CFG, STATE)
    assert [j.raw_id for j in jobs] == ["224501", "224502", "224503", "224504"]
    first = jobs[0]
    assert first.title == "Software Engineer, Optimus"
    assert first.location == "Palo Alto, California"
    assert first.department == "Tesla AI"
    assert first.url == "https://www.tesla.com/careers/search/job/224501"
    assert (first.company, first.ticker, first.ats) == ("Tesla", "TSLA", "custom")


def test_an_id_missing_from_a_lookup_reads_blank_not_as_the_raw_id():
    """A location of "777" would otherwise pass the US filter as a non-answer
    and print a number where a city belongs."""
    job = tier_b.jobs_from_json(ROW, JSON_CFG, STATE)[3]
    assert job.location == "" and job.department == ""


def test_a_payload_without_the_items_path_is_an_error_not_an_empty_board():
    with pytest.raises(ValueError, match="listings"):
        tier_b.jobs_from_json(ROW, JSON_CFG, {"lookup": {}})


def test_cards_become_jobs_deduped_on_their_link():
    cards = [
        {"title": " Software Engineer I ", "href": "https://x.example/jobs/1",
         "location": "Austin, TX"},
        {"title": "Software Engineer I", "href": "https://x.example/jobs/1",
         "location": "Austin, TX"},
        {"title": "Data Engineer", "href": "https://x.example/jobs/2", "location": ""},
        {"title": "", "href": "https://x.example/jobs/3", "location": ""},
        {"title": "No link", "href": "", "location": ""},
    ]
    jobs = tier_b.jobs_from_cards(ROW, cards)
    assert [(j.title, j.url) for j in jobs] == [
        ("Software Engineer I", "https://x.example/jobs/1"),
        ("Data Engineer", "https://x.example/jobs/2")]
    assert jobs[0].raw_id == "" and jobs[0].location == "Austin, TX"


def test_capture_matches_only_the_pages_own_host_and_path():
    page = "https://www.tesla.com/careers/search/?site=US"
    api = "https://www.tesla.com/cua-api/apps/careers/state"
    assert tier_b.is_capture(JSON_CFG, page, api)
    assert not tier_b.is_capture(
        JSON_CFG, page, "https://tracker.example/e?u=https://www.tesla.com/cua-api/apps/careers/state")
    assert not tier_b.is_capture(JSON_CFG, page, "https://www.tesla.com.evil.example/cua-api/apps/careers/state")
    assert not tier_b.is_capture(JSON_CFG, page, "https://www.tesla.com/x?next=cua-api/apps/careers/state")
    other = {**JSON_CFG, "capture_host": "api.tesla.com"}
    assert tier_b.is_capture(other, page, "https://api.tesla.com/cua-api/apps/careers/state")
    assert not tier_b.is_capture(other, page, api)


# -------------------------------------------------------------------- config
def test_selectors_skip_comment_keys_and_reject_a_config_with_no_mode(tmp_path: Path):
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps({"_comment": "x", "Tesla": JSON_CFG,
                                "CardCo": {"card": "a.job"}}), encoding="utf-8")
    assert set(tier_b.load_selectors(path)) == {"Tesla", "CardCo"}
    path.write_text(json.dumps({"Bad": {"url": "https://x.example"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Bad"):
        tier_b.load_selectors(path)


def test_a_capture_config_must_say_where_the_items_and_links_are(tmp_path: Path):
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps({"Half": {"capture": "api/jobs"}}), encoding="utf-8")
    with pytest.raises(ValueError, match="Half"):
        tier_b.load_selectors(path)


def test_missing_selectors_file_is_no_configs_not_a_crash(tmp_path: Path):
    assert tier_b.load_selectors(tmp_path / "none.json") == {}


# ------------------------------------------------------------------ pipeline
async def test_rows_without_a_config_are_errors_and_never_open_a_browser(monkeypatch):
    def no_browser(*a, **kw):
        raise AssertionError("no row can be fetched, so nothing should launch")
    monkeypatch.setattr(tier_b, "_launch", no_browser)
    jobs, errors = await tier_b.scan([ROW], {})
    assert jobs == []
    assert errors == [("Tesla", "custom", "no entry in selectors.json")]


async def test_scan_classifies_filters_to_the_us_and_survives_one_bad_board(monkeypatch):
    async def fake_fetch(browser, row, cfg):
        if row["company"] == "Broken":
            raise RuntimeError("selector matched nothing")
        return tier_b.jobs_from_json(row, cfg, STATE)

    class FakeBrowser:
        async def close(self):
            pass

    class FakeLaunch:
        async def __aenter__(self):
            return FakeBrowser()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(tier_b, "_launch", lambda headed: FakeLaunch())
    monkeypatch.setattr(tier_b, "fetch", fake_fetch)
    rows = [ROW, {"company": "Broken", "ats": "custom"}]
    jobs, errors = await tier_b.scan(rows, {"Tesla": JSON_CFG, "Broken": JSON_CFG})
    assert {j.raw_id for j in jobs} == {"224501", "224502", "224504"}   # Luxembourg dropped
    assert next(j for j in jobs if j.raw_id == "224501").bucket
    assert errors == [("Broken", "custom", "RuntimeError: selector matched nothing")]


def test_main_stores_only_boards_that_answered(monkeypatch, tmp_path: Path, capsys):
    """A board that failed today must not have its stored postings closed."""
    from tests.conftest import write_registry
    reg = write_registry(tmp_path / "companies.csv", [
        {"company": "Tesla", "ticker": "TSLA", "ats": "custom"},
        {"company": "Broken", "ats": "custom"}])
    sel = tmp_path / "selectors.json"
    sel.write_text(json.dumps({"Tesla": JSON_CFG, "Broken": JSON_CFG}), encoding="utf-8")
    db = tmp_path / "jobs.db"
    st = Store(db)
    old = tier_b.jobs_from_json({"company": "Broken", "ats": "custom"}, JSON_CFG, STATE)
    st.upsert(old, {"Broken"})
    st.db.execute("UPDATE jobs SET last_seen='2026-01-01T00:00:00+00:00'")
    st.db.commit()
    st.close()

    async def fake_scan(rows, selectors, headed=False, on_board_done=None):
        jobs = tier_b.jobs_from_json(ROW, JSON_CFG, STATE)
        return jobs, [("Broken", "custom", "RuntimeError: boom")]
    monkeypatch.setattr(tier_b, "scan", fake_scan)

    code = tier_b.main(["--file", str(reg), "--selectors", str(sel), "--db", str(db),
                        "--results-dir", str(tmp_path / "results"), "--json",
                        "--bucket", "all"])
    assert code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["boards"] == 2 and summary["raw"] == 4 and summary["new"] == 4
    assert summary["errors"] == [["Broken", "custom", "RuntimeError: boom"]]
    assert Path(summary["result_file"]).exists()
    st = Store(db)
    assert st.db.execute("SELECT COUNT(*) c FROM jobs WHERE company='Broken' "
                         "AND closed_at IS NULL").fetchone()["c"] == 4
    st.close()


def test_main_with_no_tier_b_rows_says_so(tmp_path: Path, capsys):
    from tests.conftest import write_registry
    reg = write_registry(tmp_path / "companies.csv", [
        {"company": "Fine", "ats": "greenhouse", "token": "fine"}])
    assert tier_b.main(["--file", str(reg), "--db", str(tmp_path / "j.db")]) == 1
    assert "tier B" in capsys.readouterr().err


# ------------------------------------------------------------------- browser
PAGE = """<!doctype html><html><body>
<ul id="list"></ul><button id="more">More</button>
<script>
  const rows = [["Software Engineer I","/jobs/1","Austin, TX"],
                ["Data Engineer","/jobs/2","Seattle, WA"],
                ["Platform Engineer","/jobs/3","Denver, CO"]];
  let shown = 0;
  function add(n) {
    for (const [t, h, l] of rows.slice(shown, shown + n)) {
      const li = document.createElement("li"); li.className = "job";
      li.innerHTML = `<a class="t" href="https://x.example${h}">${t}</a>` +
                     `<span class="loc">${l}</span>`;
      document.getElementById("list").appendChild(li);
    }
    shown += n;
    if (shown >= rows.length) document.getElementById("more").remove();
  }
  add(2);
  document.getElementById("more").onclick = () => add(2);
  window.payload = {jobs: [{id: "7", name: "SRE", city: "Reno, NV"}]};
</script></body></html>"""


@pytest.fixture
def page_url(tmp_path: Path) -> str:
    pytest.importorskip("playwright")
    path = tmp_path / "board.html"
    path.write_text(PAGE, encoding="utf-8")
    return path.as_uri()


@pytest.mark.browser
async def test_card_mode_reads_a_real_page_and_follows_the_more_button(page_url: str):
    cfg = {"url": page_url, "card": "li.job", "title": "a.t", "location": ".loc",
           "next": "#more", "wait_ms": 100}
    row = {"company": "CardCo", "ats": "custom"}
    async with tier_b._launch(headed=False) as browser:
        jobs = await tier_b.fetch(browser, row, cfg)
    assert [(j.title, j.url, j.location) for j in jobs] == [
        ("Software Engineer I", "https://x.example/jobs/1", "Austin, TX"),
        ("Data Engineer", "https://x.example/jobs/2", "Seattle, WA"),
        ("Platform Engineer", "https://x.example/jobs/3", "Denver, CO")]


# A board shaped like job-boards.greenhouse.io, 2026-10-13: the list is
# re-rendered after load (so a button found early is detached by the time it
# is clicked), "Next page" replaces the rows rather than appending, the new
# page arrives after a delay, the last page marks the button aria-disabled,
# and the location is uppercased by CSS only.
PAGED = """<!doctype html><html><head>
<style>.loc { text-transform: uppercase; }</style></head><body>
<table id="t"></table><nav id="nav"></nav>
<script>
  const all = Array.from({length: 7}, (_, i) =>
      [`Engineer ${i}`, `/jobs/${i}`, i % 2 ? "Austin, TX" : "London, United Kingdom"]);
  let page = 0;
  function render() {
    document.getElementById("t").innerHTML = all.slice(page * 3, page * 3 + 3).map(
      ([t, h, l]) => `<tr class="job-post"><td><a href="https://x.example${h}">` +
                     `<p class="title">${t}</p><p class="loc">${l}</p></a></td></tr>`).join("");
    const last = (page + 1) * 3 >= all.length;
    document.getElementById("nav").innerHTML =
      `<button class="next" aria-disabled="${last}">Next page</button>`;
    document.querySelector("button.next").onclick = () => {
      if (last) return;
      setTimeout(() => { page += 1; render(); }, 400);
    };
  }
  render();
  setTimeout(render, 50);     // hydration: every element replaced once
</script></body></html>"""


@pytest.mark.browser
async def test_card_mode_pages_through_a_re_rendering_paginated_list(tmp_path: Path):
    pytest.importorskip("playwright")
    path = tmp_path / "paged.html"
    path.write_text(PAGED, encoding="utf-8")
    cfg = {"url": path.as_uri(), "card": "tr.job-post", "title": "p.title",
           "location": "p.loc", "next": "button.next", "timeout_ms": 5000}
    async with tier_b._launch(headed=False) as browser:
        jobs = await tier_b.fetch(browser, {"company": "PagedCo", "ats": "custom"}, cfg)
    assert [j.url for j in jobs] == [f"https://x.example/jobs/{i}" for i in range(7)]
    assert jobs[0].location == "London, United Kingdom"     # not as CSS shows it


@pytest.mark.browser
async def test_card_mode_fails_loudly_when_the_selector_matches_nothing(page_url: str):
    cfg = {"url": page_url, "card": "div.renamed", "timeout_ms": 1500}
    async with tier_b._launch(headed=False) as browser:
        with pytest.raises(Exception):
            await tier_b.fetch(browser, {"company": "CardCo", "ats": "custom"}, cfg)
