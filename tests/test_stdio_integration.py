"""Drive the server as a real subprocess over stdio, the way a client does.

The in-process tests call tool functions directly, which does not prove the
server starts, negotiates the protocol, or serializes its results. This does.
No network: the env vars point the subprocess at temp fixtures.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[1]

EXPECTED = {"scan", "scan_status", "query_jobs", "list_companies",
            "registry_health", "verify_boards", "discover_company",
            "get_run_history", "get_scan_results"}


def _params(registry: Path, db: Path, results: Path) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "jobscan.mcp_server"],
        cwd=str(REPO),
        env={**os.environ,
             "JOBSCAN_REGISTRY": str(registry),
             "JOBSCAN_DB": str(db),
             "JOBSCAN_RESULTS": str(results)},
    )


def _payload(res):
    body = json.loads(res.content[0].text)
    return body["result"] if set(body) == {"result"} else body


@pytest.mark.asyncio
async def test_server_starts_and_serves_all_nine_tools(registry_csv, seeded_db,
                                                       tmp_path):
    async with stdio_client(_params(registry_csv, seeded_db,
                                    tmp_path / "results")) as (r, w):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            assert init.serverInfo.name == "jobscan"
            tools = await s.list_tools()
            assert {t.name for t in tools.tools} == EXPECTED
            # Descriptions reach the client, which is what lets a model pick
            # the right tool without being told.
            assert all(t.description for t in tools.tools)


@pytest.mark.asyncio
async def test_env_vars_redirect_the_server(registry_csv, seeded_db, tmp_path):
    async with stdio_client(_params(registry_csv, seeded_db,
                                    tmp_path / "results")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            d = _payload(await s.call_tool("list_companies", {}))
            # 11 rows is the fixture registry, not the repo's 117.
            assert d["ok"] and d["total"] == 11 and d["tier_a"] == 9


@pytest.mark.asyncio
async def test_query_jobs_round_trips(registry_csv, seeded_db, tmp_path):
    async with stdio_client(_params(registry_csv, seeded_db,
                                    tmp_path / "results")) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            d = _payload(await s.call_tool("query_jobs",
                                           {"buckets": ["all"], "limit": 3}))
            assert d["ok"] and d["count"] == 7 and d["returned"] == 3
            assert d["truncated"] is True


@pytest.mark.asyncio
async def test_tool_errors_arrive_as_data_not_protocol_errors(registry_csv,
                                                              seeded_db, tmp_path):
    """A tool failure must stay readable. Raising across the boundary would
    reach the client as an opaque protocol error with nothing to act on."""
    # A populated results dir, so the traversal branch is the one reached
    # rather than the earlier "no results directory" guard.
    results = tmp_path / "results"
    (results / "2026_07_30_04_00_00_AM").mkdir(parents=True)
    (results / "2026_07_30_04_00_00_AM" / "2026_07_30_04_00_00_AM.csv").write_text(
        "company,ticker,title,location,link,posted\n", encoding="utf-8")

    async with stdio_client(_params(registry_csv, seeded_db, results)) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            for evil in ("../../etc", "..", "/etc/passwd"):
                res = await s.call_tool("get_scan_results", {"run": evil})
                assert res.isError is False, evil
                d = _payload(res)
                assert d["ok"] is False and "unknown run" in d["error"], evil
            # and the legitimate folder still resolves
            d = _payload(await s.call_tool("get_scan_results", {}))
            assert d["ok"] and d["run"] == "2026_07_30_04_00_00_AM"
