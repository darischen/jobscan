"""Progress streaming from the scan tools. Offline.

A client that passes a progress token on a tool call receives
notifications/progress while the call runs. Driven through a real client
session in memory, since progress travels as protocol notifications that a
direct mcp.call_tool never sends.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from tests.test_mcp_browser import CARD, SUMMARY
from tests.test_mcp_server import call


@pytest.fixture
def slow_scan(mcp_env, monkeypatch):
    """runner.scan that finishes one board every 20 ms."""
    async def fake(rows, on_board_done=None, **kw):
        for r in rows:
            await asyncio.sleep(0.02)
            if on_board_done:
                on_board_done(r["company"], 3)
        return [], []
    monkeypatch.setattr(mcp_env.runner, "scan", fake)
    monkeypatch.setattr(mcp_env, "PROGRESS_EVERY", 0.005)
    # The fixture registry has 9 tier A boards; make that a background scan.
    monkeypatch.setattr(mcp_env, "SYNC_BOARD_LIMIT", 5)
    return mcp_env


async def _call_with_progress(m, tool: str, args: dict):
    seen: list[tuple[float, float | None, str | None]] = []

    async def cb(progress, total, message):
        seen.append((progress, total, message))

    async with create_connected_server_and_client_session(m.mcp) as s:
        res = await s.call_tool(tool, args, progress_callback=cb)
    assert not res.isError
    return json.loads(res.content[0].text), seen


@pytest.mark.asyncio
async def test_a_waited_scan_streams_progress_board_by_board(slow_scan):
    out, seen = await _call_with_progress(slow_scan, "scan", {"wait": True})
    assert out["ok"] and out["mode"] == "sync" and out["boards"] == 9
    assert len(seen) >= 3
    assert [p for p, _, _ in seen] == sorted(p for p, _, _ in seen)
    assert seen[-1][:2] == (9, 9)
    assert "Co" in (seen[-2][2] or "")


@pytest.mark.asyncio
async def test_without_wait_a_large_scan_still_returns_at_once(slow_scan):
    out, seen = await _call_with_progress(slow_scan, "scan", {})
    assert out["mode"] == "background"
    await asyncio.sleep(0.4)
    d = await call(slow_scan, "scan_status", scan_id=out["scan_id"])
    assert d["status"] == "done" and d["done"] == 9
    assert d["last_board"] and len(d["recent"]) == 5 and d["elapsed_s"] >= 0


@pytest.mark.asyncio
async def test_scan_status_mid_scan_shows_where_it_is(slow_scan, monkeypatch):
    out = await call(slow_scan, "scan")
    await asyncio.sleep(0.07)
    d = await call(slow_scan, "scan_status", scan_id=out["scan_id"])
    assert d["status"] == "running" and 0 < d["done"] < 9
    assert d["recent"][-1].startswith(d["last_board"])
    await asyncio.sleep(0.3)


@pytest.mark.asyncio
async def test_scan_browser_relays_the_child_process_progress(mcp_env, monkeypatch):
    from pathlib import Path
    Path(mcp_env.SELECTORS).write_text(json.dumps({"BrowserCo": CARD}), encoding="utf-8")

    async def fake(args, on_progress=None):
        for line in ("progress 1/2 BrowserCo 40", "progress 2/2 PhenomCo 0"):
            await on_progress(line)
        return 0, json.dumps(SUMMARY), ""

    monkeypatch.setattr(mcp_env, "_tier_b_process", fake)
    out, seen = await _call_with_progress(mcp_env, "scan_browser", {})
    assert out["ok"]
    assert [(p, t) for p, t, _ in seen] == [(1, 2), (2, 2)]
    assert seen[0][2] == "BrowserCo: read 40 postings"
