"""Application tracking: applied_at and starred. Offline.

Both columns were in the schema from the first commit and nothing wrote
them. A posting is named by its url (what the results CSV and every tool
print) or by its key.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jobscan import cli
from jobscan.store import Store
from tests.conftest import make_jobs
from tests.test_mcp_server import call

URL0 = "https://co0.example/0"
URL1 = "https://co0.example/1"


# ---------------------------------------------------------------------- store
def test_mark_applied_stamps_a_time_and_unmark_clears_it(seeded_db: Path):
    st = Store(seeded_db)
    done = st.mark([URL0], applied=True)
    assert [r["url"] for r in done["jobs"]] == [URL0]
    assert done["jobs"][0]["applied_at"]
    assert done["unknown"] == []
    assert st.mark([URL0], applied=False)["jobs"][0]["applied_at"] is None
    st.close()


def test_marking_applied_twice_keeps_the_first_time(seeded_db: Path):
    st = Store(seeded_db)
    st.db.execute("UPDATE jobs SET applied_at='2026-09-01T00:00:00+00:00' "
                  "WHERE url=?", (URL0,))
    assert st.mark([URL0], applied=True)["jobs"][0]["applied_at"] \
        == "2026-09-01T00:00:00+00:00"
    st.close()


def test_star_and_applied_are_independent(seeded_db: Path):
    st = Store(seeded_db)
    st.mark([URL0], starred=True)
    job = st.mark([URL0], applied=True)["jobs"][0]
    assert job["starred"] == 1 and job["applied_at"]
    job = st.mark([URL0], starred=False)["jobs"][0]
    assert job["starred"] == 0 and job["applied_at"]
    st.close()


def test_a_job_is_found_by_key_or_by_url_with_a_stray_slash(seeded_db: Path):
    st = Store(seeded_db)
    key = st.db.execute("SELECT key FROM jobs WHERE url=?", (URL1,)).fetchone()["key"]
    done = st.mark([key, URL0 + "/"], starred=True)
    assert {r["url"] for r in done["jobs"]} == {URL0, URL1}
    st.close()


def test_unknown_references_are_reported_not_ignored(seeded_db: Path):
    st = Store(seeded_db)
    done = st.mark([URL0, "https://nowhere.example/9"], applied=True)
    assert len(done["jobs"]) == 1
    assert done["unknown"] == ["https://nowhere.example/9"]
    st.close()


def test_a_rescan_keeps_the_marks(seeded_db: Path):
    st = Store(seeded_db)
    st.mark([URL0], applied=True, starred=True)
    st.upsert(make_jobs(4, "Co0"), {"Co0"})
    row = st.db.execute("SELECT * FROM jobs WHERE url=?", (URL0,)).fetchone()
    assert row["applied_at"] and row["starred"] == 1
    st.close()


def test_tracked_lists_closed_postings_too(seeded_db: Path):
    """An application outlives its posting: the req closing is news, not a
    reason to drop it from the list."""
    st = Store(seeded_db)
    st.mark(["https://co2.example/0"], applied=True)     # Co2 is closed
    st.mark([URL0], starred=True)
    assert {r["url"] for r in st.tracked()} == {URL0, "https://co2.example/0"}
    assert [r["url"] for r in st.tracked(applied=True)] == ["https://co2.example/0"]
    assert [r["url"] for r in st.tracked(starred=True)] == [URL0]
    assert [r["url"] for r in st.tracked(include_closed=False)] == [URL0]
    st.close()


def test_open_jobs_can_leave_out_what_was_applied_to(seeded_db: Path):
    st = Store(seeded_db)
    st.mark([URL0], applied=True)
    urls = {r["url"] for r in st.open_jobs(applied=False)}
    assert URL0 not in urls and URL1 in urls
    assert [r["url"] for r in st.open_jobs(applied=True)] == [URL0]
    st.close()


# ------------------------------------------------------------------------ cli
def test_cli_apply_star_and_list_without_scanning(seeded_db: Path, capsys, monkeypatch):
    def no_scan(*a, **kw):
        raise AssertionError("tracking must not scan")
    monkeypatch.setattr(cli.runner, "scan", no_scan)

    assert cli.main(["--db", str(seeded_db), "--apply", URL0, URL1]) == 0
    assert cli.main(["--db", str(seeded_db), "--star", URL1]) == 0
    capsys.readouterr()
    assert cli.main(["--db", str(seeded_db), "--tracked"]) == 0
    out = capsys.readouterr().out
    assert URL0 in out and URL1 in out
    assert "applied" in out and "starred" in out

    assert cli.main(["--db", str(seeded_db), "--unapply", URL0]) == 0
    capsys.readouterr()
    cli.main(["--db", str(seeded_db), "--tracked"])
    assert URL0 not in capsys.readouterr().out


def test_cli_reports_an_unknown_posting_and_fails(seeded_db: Path, capsys):
    assert cli.main(["--db", str(seeded_db), "--apply",
                     "https://nowhere.example/9"]) == 1
    assert "nowhere.example" in capsys.readouterr().err


# ------------------------------------------------------------------------ mcp
@pytest.mark.asyncio
async def test_mark_jobs_tool(mcp_env):
    d = await call(mcp_env, "mark_jobs", jobs=[URL0, "https://nowhere.example/9"],
                   applied=True, starred=True)
    assert d["ok"] and d["updated"] == 1
    assert d["unknown"] == ["https://nowhere.example/9"]
    assert d["jobs"][0]["applied_at"] and d["jobs"][0]["starred"] is True


@pytest.mark.asyncio
async def test_mark_jobs_needs_something_to_set(mcp_env):
    d = await call(mcp_env, "mark_jobs", jobs=[URL0])
    assert not d["ok"]


@pytest.mark.asyncio
async def test_tracked_jobs_tool_says_which_are_closed(mcp_env):
    await call(mcp_env, "mark_jobs", jobs=["https://co2.example/0", URL0],
               applied=True)
    d = await call(mcp_env, "tracked_jobs")
    assert d["ok"] and d["count"] == 2
    by_url = {j["url"]: j for j in d["jobs"]}
    assert by_url["https://co2.example/0"]["closed_at"]
    assert not by_url[URL0]["closed_at"]


@pytest.mark.asyncio
async def test_query_jobs_shows_marks_and_filters_on_them(mcp_env):
    await call(mcp_env, "mark_jobs", jobs=[URL0], applied=True)
    d = await call(mcp_env, "query_jobs", buckets=["all"])
    assert next(j for j in d["jobs"] if j["url"] == URL0)["applied_at"]
    assert all("key" in j for j in d["jobs"])
    d = await call(mcp_env, "query_jobs", buckets=["all"], applied=False)
    assert URL0 not in {j["url"] for j in d["jobs"]} and d["count"] == 6
