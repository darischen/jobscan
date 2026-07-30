"""Fixtures. Every test here runs offline against temporary files.

Tests that hit a real board are marked `network` and deselected by default,
because a board being down should not read as jobscan being broken.
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from jobscan.core import Job
from jobscan.registry import COLUMNS
from jobscan.store import Store

pytest_plugins = ()


def pytest_configure(config):
    config.addinivalue_line("markers", "network: hits a live job board")


def pytest_collection_modifyitems(config, items):
    if config.getoption("-m"):
        return
    skip = pytest.mark.skip(reason="needs -m network")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip)


def write_registry(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})
    return path


@pytest.fixture
def registry_csv(tmp_path: Path) -> Path:
    """Nine tier A rows plus two tier B, so the sync threshold can be crossed."""
    rows = [{"company": f"Co{i}", "ticker": f"C{i}", "ats": "greenhouse",
             "token": f"co{i}", "careers_url": f"https://co{i}.example"}
            for i in range(9)]
    # Distinct names on purpose: --company does a substring match, so
    # "BrowserCo" would also select an "OtherBrowserCo".
    rows += [{"company": "BrowserCo", "ats": "custom", "host": "browserco.example"},
             {"company": "PhenomCo", "ats": "icims", "host": "phenomco.example"}]
    return write_registry(tmp_path / "companies.csv", rows)


@pytest.fixture
def bad_registry_csv(tmp_path: Path) -> Path:
    """Row 2 names an ATS that does not exist, row 3 omits a required token."""
    return write_registry(tmp_path / "bad.csv", [
        {"company": "Fine", "ats": "greenhouse", "token": "fine"},
        {"company": "BadAts", "ats": "notarealats", "token": "x"},
        {"company": "NoToken", "ats": "greenhouse"},
    ])


def make_jobs(n: int = 5, company: str = "Co0", bucket: str = "explicit_early") -> list[Job]:
    out = []
    for i in range(n):
        j = Job(company=company, title=f"Software Engineer I #{i}",
                url=f"https://{company.lower()}.example/{i}",
                ticker="C0", location="Austin, TX", ats="greenhouse",
                posted_at="2026-07-20T00:00:00+00:00", posted_source="first_published",
                raw_id=f"{company}-{i}")
        j.bucket = bucket
        out.append(j)
    return out


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """A store holding a mix of buckets and one closed requisition."""
    path = tmp_path / "jobs.db"
    st = Store(path)
    jobs = (make_jobs(4, "Co0", "explicit_early")
            + make_jobs(3, "Co1", "senior")
            + make_jobs(2, "Co2", "role_miss"))
    st.upsert(jobs, {"Co0", "Co1", "Co2"})
    # Close Co2 directly. Going through a second upsert would not work here:
    # it closes rows whose last_seen < now(), and now() is second-resolution,
    # so two upserts inside one second close nothing. That is harmless in
    # production (runs are minutes apart, the next one closes them) but it
    # makes for a fixture that silently does not do what it says.
    st.db.execute("UPDATE jobs SET closed_at = ? WHERE company = 'Co2'",
                  ("2026-07-29T00:00:00+00:00",))
    st.db.commit()
    st.close()
    return path


@pytest.fixture
def mcp_env(monkeypatch, registry_csv: Path, seeded_db: Path, tmp_path: Path):
    """Point the server's module-level paths at the temp fixtures."""
    from jobscan import mcp_server as m
    results = tmp_path / "results"
    monkeypatch.setattr(m, "REGISTRY", str(registry_csv))
    monkeypatch.setattr(m, "DB", str(seeded_db))
    monkeypatch.setattr(m, "RESULTS", str(results))
    m._SCANS.clear()
    yield m
    m._SCANS.clear()
