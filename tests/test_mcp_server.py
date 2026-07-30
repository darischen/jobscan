"""MCP server tool behaviour. Offline."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.conftest import make_jobs


async def call(mod, _tool: str, **kw):
    """Invoke a tool the way a client does, unwrapping the structured result."""
    res = await mod.mcp.call_tool(_tool, kw)
    d = res[1] if isinstance(res, tuple) and len(res) > 1 else res
    if isinstance(d, dict) and set(d) == {"result"}:
        d = d["result"]
    return d


# ------------------------------------------------------------------ discovery
@pytest.mark.asyncio
async def test_all_nine_tools_register(mcp_env):
    names = {t.name for t in await mcp_env.mcp.list_tools()}
    assert names == {"scan", "scan_status", "query_jobs", "list_companies",
                     "registry_health", "verify_boards", "discover_company",
                     "get_run_history", "get_scan_results"}


@pytest.mark.asyncio
async def test_every_tool_documents_itself(mcp_env):
    for t in await mcp_env.mcp.list_tools():
        assert t.description and len(t.description) > 40, t.name


# ----------------------------------------------------------------- query_jobs
@pytest.mark.asyncio
async def test_query_jobs_excludes_closed(mcp_env):
    d = await call(mcp_env, "query_jobs", buckets=["all"])
    assert d["ok"]
    # Co2's two rows were closed by the second upsert and must not appear.
    assert {j["company"] for j in d["jobs"]} == {"Co0", "Co1"}
    assert d["count"] == 7


@pytest.mark.asyncio
async def test_query_jobs_bucket_filter(mcp_env):
    d = await call(mcp_env, "query_jobs", buckets=["explicit_early"])
    assert [j["bucket"] for j in d["jobs"]] == ["explicit_early"] * 4


@pytest.mark.asyncio
async def test_query_jobs_default_buckets_drop_role_miss(mcp_env):
    d = await call(mcp_env, "query_jobs")
    assert "role_miss" not in {j["bucket"] for j in d["jobs"]}


@pytest.mark.asyncio
async def test_query_jobs_limit_reports_true_total(mcp_env):
    d = await call(mcp_env, "query_jobs", buckets=["all"], limit=2)
    assert d["count"] == 7 and d["returned"] == 2 and d["truncated"] is True


@pytest.mark.asyncio
async def test_unknown_bucket_is_rejected_not_silently_empty(mcp_env):
    d = await call(mcp_env, "query_jobs", buckets=["typo"])
    assert d["ok"] is False and "typo" in d["error"]


# ------------------------------------------------------------- list_companies
@pytest.mark.asyncio
async def test_list_companies_flags_unimplemented_tier_b(mcp_env):
    d = await call(mcp_env, "list_companies")
    assert d["tier_a"] == 9 and d["tier_b"] == 2
    b = [c for c in d["companies"] if not c["implemented"]]
    assert {c["company"] for c in b} == {"BrowserCo", "PhenomCo"}
    assert all(c["tier"] == "B" for c in b)


@pytest.mark.asyncio
async def test_registry_error_surfaces_every_bad_line(monkeypatch, mcp_env,
                                                      bad_registry_csv: Path):
    monkeypatch.setattr(mcp_env, "REGISTRY", str(bad_registry_csv))
    d = await call(mcp_env, "list_companies")
    assert d["ok"] is False and d["kind"] == "registry"
    # Both problems named, not just the first one encountered.
    assert "BadAts" in d["error"] and "NoToken" in d["error"]


# --------------------------------------------------------------------- scan
def _fake_scan(jobs, errors=()):
    async def inner(rows, *a, on_board_done=None, **kw):
        for r in rows:
            if on_board_done:
                on_board_done(r["company"], len(jobs))
        return list(jobs), list(errors)
    return inner


@pytest.mark.asyncio
async def test_scan_under_threshold_runs_synchronously(monkeypatch, mcp_env):
    monkeypatch.setattr(mcp_env.runner, "scan", _fake_scan(make_jobs(3, "Co0")))
    d = await call(mcp_env, "scan", company="Co0", buckets=["all"])
    assert d["ok"] and d["mode"] == "sync" and d["boards"] == 1
    assert Path(d["result_file"]).exists()


@pytest.mark.asyncio
async def test_scan_at_threshold_goes_background_then_completes(monkeypatch, mcp_env):
    monkeypatch.setattr(mcp_env.runner, "scan", _fake_scan(make_jobs(2, "Co0")))
    monkeypatch.setattr(mcp_env, "SYNC_BOARD_LIMIT", 9)
    d = await call(mcp_env, "scan", buckets=["all"])   # 9 tier A rows
    assert d["mode"] == "background" and d["boards"] == 9
    sid = d["scan_id"]
    for _ in range(50):
        s = await call(mcp_env, "scan_status", scan_id=sid)
        if s["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert s["status"] == "done", s
    assert s["done"] == 9, "on_board_done must fire once per board"
    assert s["summary"]["boards"] == 9


@pytest.mark.asyncio
async def test_scan_limit_caps_payload_not_the_csv(monkeypatch, mcp_env):
    monkeypatch.setattr(mcp_env.runner, "scan", _fake_scan(make_jobs(9, "Co0")))
    d = await call(mcp_env, "scan", company="Co0", buckets=["all"], limit=3)
    assert d["shown"] == 9, "shown must be the true total"
    assert len(d["jobs"]) == 3 and d["truncated"] is True
    import csv as c
    rows = list(c.DictReader(Path(d["result_file"]).open(newline="", encoding="utf-8")))
    assert len(rows) == 9, "the CSV keeps every row regardless of limit"


@pytest.mark.asyncio
async def test_scan_records_the_tool_call_as_provenance(monkeypatch, mcp_env):
    monkeypatch.setattr(mcp_env.runner, "scan", _fake_scan(make_jobs(1, "Co0")))
    d = await call(mcp_env, "scan", company="Co0", buckets=["all"])
    p = Path(d["result_file"])
    meta = json.loads((p.parent / (p.stem + ".meta.json")).read_text(encoding="utf-8"))
    assert meta["command"].startswith("mcp:scan(")
    assert "company='Co0'" in meta["command"]
    assert meta["argv"] == [], "argv belongs to the CLI, not an MCP call"


@pytest.mark.asyncio
async def test_scan_rejects_a_tier_b_only_selection(mcp_env):
    d = await call(mcp_env, "scan", company="BrowserCo")
    assert d["ok"] is False and "tier B" in d["error"]
    assert d["tier_b_companies"] == ["BrowserCo"]


@pytest.mark.asyncio
async def test_scan_reports_no_match(mcp_env):
    d = await call(mcp_env, "scan", company="nope")
    assert d["ok"] is False and "no registry rows matched" in d["error"]


@pytest.mark.asyncio
async def test_board_failure_does_not_fail_the_scan(monkeypatch, mcp_env):
    errs = [("Co0", "greenhouse", "HTTPStatusError: 503")]
    monkeypatch.setattr(mcp_env.runner, "scan", _fake_scan(make_jobs(1, "Co0"), errs))
    d = await call(mcp_env, "scan", company="Co0", buckets=["all"])
    assert d["ok"] is True and d["errors"] == [list(errs[0])]


@pytest.mark.asyncio
async def test_background_crash_is_reported_not_left_running(monkeypatch, mcp_env):
    async def boom(rows, *a, on_board_done=None, **kw):
        raise RuntimeError("board exploded")
    monkeypatch.setattr(mcp_env.runner, "scan", boom)
    monkeypatch.setattr(mcp_env, "SYNC_BOARD_LIMIT", 9)
    d = await call(mcp_env, "scan", buckets=["all"])
    sid = d["scan_id"]
    for _ in range(50):
        s = await call(mcp_env, "scan_status", scan_id=sid)
        if s["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert s["status"] == "error"
    assert any("board exploded" in str(e) for e in s["errors"])
    assert s["finished"] is not None


# -------------------------------------------------------------- scan_status
@pytest.mark.asyncio
async def test_scan_status_unknown_id(mcp_env):
    """A typo'd id must say so, even before any scan has run, rather than
    reporting that no scans exist."""
    d = await call(mcp_env, "scan_status", scan_id="nope")
    assert d["ok"] is False and "unknown scan_id" in d["error"]


@pytest.mark.asyncio
async def test_scan_status_no_scans_yet(mcp_env):
    d = await call(mcp_env, "scan_status")
    assert d["ok"] is False and "no scans" in d["error"]


@pytest.mark.asyncio
async def test_scan_state_evicts_oldest(mcp_env):
    from jobscan.mcp_server import ScanState
    mcp_env._SCANS.clear()
    for i in range(mcp_env.MAX_TRACKED_SCANS + 5):
        mcp_env._track(ScanState(id=f"s{i}", status="done", boards=1))
    assert len(mcp_env._SCANS) == mcp_env.MAX_TRACKED_SCANS
    assert "s0" not in mcp_env._SCANS
    assert f"s{mcp_env.MAX_TRACKED_SCANS + 4}" in mcp_env._SCANS


# ---------------------------------------------------------- get_scan_results
@pytest.mark.asyncio
async def test_get_scan_results_rejects_traversal(monkeypatch, mcp_env):
    monkeypatch.setattr(mcp_env.runner, "scan", _fake_scan(make_jobs(1, "Co0")))
    await call(mcp_env, "scan", company="Co0", buckets=["all"])
    for evil in ("../../etc", "..", "/etc/passwd", r"..\..\windows"):
        d = await call(mcp_env, "get_scan_results", run=evil)
        assert d["ok"] is False, evil
        assert "unknown run" in d["error"]


@pytest.mark.asyncio
async def test_get_scan_results_returns_rows_and_sidecar(monkeypatch, mcp_env):
    monkeypatch.setattr(mcp_env.runner, "scan", _fake_scan(make_jobs(4, "Co0")))
    s = await call(mcp_env, "scan", company="Co0", buckets=["all"])
    run = Path(s["result_file"]).parent.name
    d = await call(mcp_env, "get_scan_results", run=run)
    assert d["ok"] and d["run"] == run and d["row_count"] == 4
    assert d["metadata"]["command"].startswith("mcp:scan(")


@pytest.mark.asyncio
async def test_get_scan_results_empty_dir(mcp_env):
    d = await call(mcp_env, "get_scan_results")
    assert d["ok"] is False and "no results directory" in d["error"]


# ------------------------------------------------------------------- health
@pytest.mark.asyncio
async def test_registry_health_counts_rows(mcp_env):
    d = await call(mcp_env, "registry_health")
    assert d["ok"] and d["rows"] == 11 and d["tier_a"] == 9 and d["tier_b"] == 2
    assert d["open_jobs_stored"] == 7
