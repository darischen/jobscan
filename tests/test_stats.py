"""Store.stats and the get_stats tool: counts over what is stored. Offline."""
from __future__ import annotations

from pathlib import Path

import pytest

from jobscan.store import Store
from tests.test_mcp_server import call


def test_stats_counts_open_postings_by_bucket_and_company(seeded_db: Path):
    st = Store(seeded_db)
    st.mark(["https://co0.example/0"], applied=True)
    st.mark(["https://co1.example/0"], starred=True)
    s = st.stats()
    st.close()
    assert s["open"] == 7 and s["closed"] == 2
    assert s["by_bucket"] == {"explicit_early": 4, "senior": 3}
    assert s["applied"] == 1 and s["starred"] == 1
    co = {c["company"]: c for c in s["companies"]}
    assert co["Co0"]["open"] == 4 and co["Co0"]["explicit_early"] == 4
    assert co["Co0"]["applied"] == 1
    assert co["Co1"]["shown"] == 3 and co["Co1"]["explicit_early"] == 0
    assert "Co2" not in co                     # every posting closed
    # Most early-career postings first: that is what the list is for.
    assert [c["company"] for c in s["companies"]] == ["Co0", "Co1"]


def test_stats_new_since_counts_first_seen(seeded_db: Path):
    st = Store(seeded_db)
    st.db.execute("UPDATE jobs SET first_seen='2020-01-01T00:00:00+00:00' "
                  "WHERE company='Co1'")
    st.db.commit()
    s = st.stats(since="2026-01-01T00:00:00+00:00")
    st.close()
    co = {c["company"]: c for c in s["companies"]}
    assert co["Co0"]["new"] == 4 and co["Co1"]["new"] == 0
    assert s["new"] == 4


@pytest.mark.asyncio
async def test_get_stats_tool(mcp_env):
    d = await call(mcp_env, "get_stats", days=7)
    assert d["ok"] and d["open"] == 7
    assert d["companies"][0]["company"] == "Co0"
    assert "runs" in d and "last_run" in d


@pytest.mark.asyncio
async def test_get_stats_for_one_company(mcp_env):
    d = await call(mcp_env, "get_stats", company="Co1")
    assert [c["company"] for c in d["companies"]] == ["Co1"]
    assert d["open"] == 3
