"""The scan_browser tool: tier B from an MCP session. Offline.

The tool runs `python -m jobscan.tier_b` as a child process, so the browser
never shares the server's event loop. Here the child is replaced; what is
tested is which boards it is asked for and how its answer is passed on.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_mcp_server import call

CARD = {"card": "a.job"}
SUMMARY = {"boards": 1, "raw": 40, "shown": 2, "new": 2, "closed": 0, "errors": [],
           "result_file": "results/x/x.csv",
           "jobs": [{"company": "BrowserCo", "title": "Software Engineer I",
                     "url": "https://browserco.example/1", "key": "k1"},
                    {"company": "BrowserCo", "title": "Software Engineer II",
                     "url": "https://browserco.example/2", "key": "k2"}]}


@pytest.fixture
def child(mcp_env, monkeypatch):
    """Stand-in for the tier_b process. Records the arguments it was given."""
    calls: list[list[str]] = []
    state = {"code": 0, "out": json.dumps(SUMMARY), "err": ""}

    async def fake(args: list[str]):
        calls.append(args)
        return state["code"], state["out"], state["err"]

    monkeypatch.setattr(mcp_env, "_tier_b_process", fake)
    Path(mcp_env.SELECTORS).write_text(json.dumps({"BrowserCo": CARD}), encoding="utf-8")
    return calls, state


@pytest.mark.asyncio
async def test_scan_browser_runs_the_configured_boards_and_returns_the_summary(mcp_env, child):
    calls, _ = child
    d = await call(mcp_env, "scan_browser", limit=1)
    assert d["ok"] and d["raw"] == 40 and d["shown"] == 2
    assert len(d["jobs"]) == 1 and d["truncated"] is True
    # PhenomCo is tier B too but has no entry, and is reported, not scanned.
    assert d["not_configured"] == ["PhenomCo"]
    args = calls[0]
    assert args[args.index("--file") + 1] == mcp_env.REGISTRY
    assert args[args.index("--db") + 1] == mcp_env.DB
    assert args[args.index("--results-dir") + 1] == mcp_env.RESULTS
    assert "--json" in args
    assert args[args.index("--command") + 1].startswith("mcp:scan_browser(")


@pytest.mark.asyncio
async def test_scan_browser_passes_the_company_and_buckets_through(mcp_env, child):
    calls, _ = child
    await call(mcp_env, "scan_browser", company="BrowserCo", buckets=["all"],
               new_only=True, store=False)
    args = calls[0]
    assert args[args.index("--company") + 1] == "BrowserCo"
    assert args[args.index("--bucket") + 1] == "all"
    assert "--new-only" in args and "--no-store" in args


@pytest.mark.asyncio
async def test_scan_browser_with_nothing_configured_never_starts_a_browser(mcp_env, child):
    calls, _ = child
    d = await call(mcp_env, "scan_browser", company="PhenomCo")
    assert not d["ok"] and "selectors.json" in d["error"]
    assert calls == []


@pytest.mark.asyncio
async def test_scan_browser_on_a_tier_a_company_says_to_use_scan(mcp_env, child):
    d = await call(mcp_env, "scan_browser", company="Co0")
    assert not d["ok"] and "scan" in d["error"]


@pytest.mark.asyncio
async def test_a_failed_child_reports_its_stderr(mcp_env, child):
    _, state = child
    state.update(code=1, out="", err="tier B needs Playwright: pip install playwright")
    d = await call(mcp_env, "scan_browser")
    assert not d["ok"] and "Playwright" in d["error"]


@pytest.mark.asyncio
async def test_list_companies_marks_a_configured_tier_b_row_implemented(mcp_env, child):
    d = await call(mcp_env, "list_companies")
    by = {c["company"]: c for c in d["companies"]}
    assert by["BrowserCo"]["tier"] == "B" and by["BrowserCo"]["implemented"] is True
    assert by["PhenomCo"]["implemented"] is False
    assert by["Co0"]["implemented"] is True


@pytest.mark.asyncio
async def test_scan_of_only_tier_b_rows_points_at_scan_browser(mcp_env, child):
    d = await call(mcp_env, "scan", company="BrowserCo")
    assert not d["ok"] and "scan_browser" in d["error"]
