"""A board that fails a scan keeps its stored postings open. Offline.

upsert closes whatever a scanned company no longer lists. A board that
errored lists nothing, so every scan it failed stamped all of its postings
closed, and the next good scan reopened them. Harmless while nothing read
closed_at; with applications tracked it would report a req as gone because
the board timed out.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jobscan import cli
from jobscan.store import Store
from tests.conftest import make_jobs
from tests.test_mcp_server import call


def _age(db: Path) -> None:
    st = Store(db)
    st.db.execute("UPDATE jobs SET last_seen='2026-01-01T00:00:00+00:00' "
                  "WHERE closed_at IS NULL")
    st.db.commit()
    st.close()


def _open(db: Path, company: str) -> int:
    st = Store(db)
    n = st.db.execute("SELECT COUNT(*) c FROM jobs WHERE company=? AND "
                      "closed_at IS NULL", (company,)).fetchone()["c"]
    st.close()
    return n


async def _co0_fails(rows, **kw):
    """Co1 answers with one of its three postings; Co0 errors out."""
    return make_jobs(1, "Co1", "senior"), [("Co0", "greenhouse", "ReadTimeout: ")]


def test_cli_scan_does_not_close_a_failed_boards_postings(
        registry_csv: Path, seeded_db: Path, tmp_path: Path, monkeypatch):
    _age(seeded_db)
    monkeypatch.setattr(cli.runner, "scan", _co0_fails)
    assert cli.main(["--file", str(registry_csv), "--db", str(seeded_db),
                     "--no-file", "--quiet"]) == 0
    assert _open(seeded_db, "Co0") == 4      # failed: untouched
    assert _open(seeded_db, "Co1") == 1      # answered: two really closed


@pytest.mark.asyncio
async def test_mcp_scan_does_not_close_a_failed_boards_postings(mcp_env, monkeypatch):
    _age(Path(mcp_env.DB))
    monkeypatch.setattr(mcp_env.runner, "scan", _co0_fails)
    monkeypatch.setattr(mcp_env, "SYNC_BOARD_LIMIT", 100)
    d = await call(mcp_env, "scan")
    assert d["ok"]
    assert _open(Path(mcp_env.DB), "Co0") == 4
    assert _open(Path(mcp_env.DB), "Co1") == 1
