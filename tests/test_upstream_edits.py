"""Regressions for the four changes the MCP server needed from existing code.

Each of these was a real defect found while wiring the server up, so each keeps
its own test rather than being folded into the server suite.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

import pytest

from jobscan import cli, runner


# ------------------------------------------------- runner progress callback
@pytest.mark.asyncio
async def test_on_board_done_fires_once_per_board(monkeypatch):
    rows = [{"company": f"Co{i}", "ats": "greenhouse", "token": f"co{i}"}
            for i in range(4)]

    async def ok(client, company, row):
        return []
    monkeypatch.setitem(runner.TIER_A, "greenhouse", ok)

    seen: list[tuple[str, int]] = []
    await runner.scan(rows, on_board_done=lambda c, n: seen.append((c, n)))
    assert sorted(c for c, _ in seen) == ["Co0", "Co1", "Co2", "Co3"]


@pytest.mark.asyncio
async def test_on_board_done_fires_even_when_a_board_fails(monkeypatch):
    rows = [{"company": "Good", "ats": "greenhouse", "token": "g"},
            {"company": "Bad", "ats": "greenhouse", "token": "b"}]

    async def maybe(client, company, row):
        if company == "Bad":
            raise RuntimeError("down")
        return []
    monkeypatch.setitem(runner.TIER_A, "greenhouse", maybe)

    seen: list[str] = []
    jobs, errors = await runner.scan(rows, retries=0,
                                    on_board_done=lambda c, n: seen.append(c))
    assert sorted(seen) == ["Bad", "Good"], "a failed board must still report"
    assert len(errors) == 1 and errors[0][0] == "Bad"


@pytest.mark.asyncio
async def test_scan_without_callback_is_unchanged(monkeypatch):
    rows = [{"company": "Co", "ats": "greenhouse", "token": "c"}]

    async def ok(client, company, row):
        return []
    monkeypatch.setitem(runner.TIER_A, "greenhouse", ok)
    jobs, errors = await runner.scan(rows)
    assert jobs == [] and errors == []


# ------------------------------------------------------------- US locations
@pytest.mark.parametrize("loc,want", [
    (None, True), ("", True),
    ("Remote", True), ("Fully Remote", True), ("Remote - Anywhere", True),
    ("Remote, US", True), ("US-CA-Santa Clara", True), ("New York, NY", True),
    ("Austin, TX; Dublin, Ireland", True), ("Washington, DC", True),
    ("Remote - India", False), ("Remote, Canada", False),
    ("Bangalore, India", False), ("Warsaw, Poland", False),
    # Two-letter codes must not match inside a longer foreign word.
    ("Portugal", False), ("Dublin", False), ("Canada", False),
])
def test_is_us_location(loc, want):
    assert runner.is_us_location(loc) is want


@pytest.mark.parametrize("loc", [
    # Every one of these read as US because a two-letter state code matched a
    # foreign word: de/Delaware, La/Louisiana, Al/Alabama.
    "Rio de Janeiro, Brazil", "Rio de Janeiro",
    "Belen La Ribera, Costa Rica", "Abu Dhabi, Al Sila Tower",
    "Ciudad de Mexico", "La Paz, Bolivia", "Al Khobar, Saudi Arabia",
    "Santiago de Chile",
    # An alphanumeric site code must not fake a state either.
    "Cairo, CO55 Uvenues",
])
def test_foreign_names_with_state_code_particles_are_rejected(loc):
    assert runner.is_us_location(loc) is False


@pytest.mark.parametrize("loc", [
    # A genuine state abbreviation is still enough on its own, including the
    # ones whose codes double as foreign words.
    "Wilmington, DE", "New Orleans, LA", "Birmingham, AL",
    "Indianapolis, IN", "Portland, OR", "Philadelphia, PA", "Boise, ID",
    "Chicago, IL", "AF Batavia IL", "Broomfield, Colorado, USA",
])
def test_real_state_codes_still_pass(loc):
    assert runner.is_us_location(loc) is True


@pytest.mark.parametrize("loc", [
    "Location Negotiable", "Multiple Locations", "Various", "TBD",
    "Unspecified", "Remote - Anywhere", "Fully Remote",
])
def test_placeless_values_pass_like_a_blank_field(loc):
    """A non-answer is not evidence of a foreign location. Accenture alone
    sends 292 'Location Negotiable' postings."""
    assert runner.is_us_location(loc) is True


@pytest.mark.parametrize("loc", ["Bengaluru", "Pune", "Riga", "London",
                                 "Krakow, High 5Ive Development",
                                 "Bengaluru, Bdc9A"])
def test_bare_foreign_city_names_are_rejected(loc):
    assert runner.is_us_location(loc) is False


# --------------------------------------------------- workday location recovery
@pytest.mark.parametrize("locations_text,path,bullets,want_us,why", [
    ("US, CA, Santa Clara", "/job/US-CA-Santa-Clara/x_JR1", ["JR1"], True, "normal"),
    ("2 Locations", "/job/US-CA-Santa-Clara/x_JR1", ["JR1"], True, "count falls back to path"),
    (None, "/job/London/Sec_R1", ["R00311", "London"], False, "blank falls back to bulletFields"),
    (None, "/job/IL---Chicago/x_R1", ["R1", "Chicago, IL"], True, "blank, US via bulletFields"),
    (None, "/job/US-TX-Austin/x_R1", ["R1"], True, "no usable bullet falls back to path"),
    (None, "/job/Kronberg-Campus-Kronberg-1/AI_R1",
     ["R00345665", "Kronberg, Campus Kronberg"], False, "Accenture Germany"),
])
def test_workday_location_fallback_chain(locations_text, path, bullets, want_us, why):
    """Accenture sends locationsText=None on all 2,000 postings. A blank
    location passes the US filter by design, so every one of them was entering
    results regardless of country."""
    from jobscan.adapters import _workday_location
    loc = _workday_location(locations_text, path, bullets)
    assert runner.is_us_location(loc) is want_us, f"{why}: got {loc!r}"


def test_workday_location_ignores_the_requisition_number():
    from jobscan.adapters import _workday_location
    # bulletFields[0] is the req number on most tenants; it is not a location.
    loc = _workday_location(None, "/job/London/x_R1", ["R00282385", "London"])
    assert loc == "London"


# ------------------------------------------------------------- provenance
def _rows(n, co="Co"):
    return [{"company": co, "ticker": "", "title": f"Engineer {i}",
             "url": f"http://x/{i}", "location": "US", "posted_at": None,
             "first_seen": "2026-07-30T00:00:00+00:00"} for i in range(n)]


def test_command_override_is_recorded(tmp_path: Path):
    started = datetime(2026, 7, 30, 4, 0, 0).astimezone()
    dest = cli.write_results(_rows(2), started, directory=str(tmp_path),
                             command="mcp:scan(company='X')")
    cli.write_sidecar(dest, started, {"boards": 1}, command="mcp:scan(company='X')")
    meta = json.loads((dest.parent / (dest.stem + ".meta.json")).read_text(encoding="utf-8"))
    assert meta["command"] == "mcp:scan(company='X')"
    assert meta["argv"] == []


def test_command_defaults_to_argv(tmp_path: Path):
    started = datetime(2026, 7, 30, 4, 0, 0).astimezone()
    dest = cli.write_results(_rows(1), started, directory=str(tmp_path))
    cli.write_sidecar(dest, started, {})
    meta = json.loads((dest.parent / (dest.stem + ".meta.json")).read_text(encoding="utf-8"))
    assert meta["command"].startswith("python ")
    assert meta["argv"], "the CLI path still records argv"


def test_same_second_runs_do_not_overwrite(tmp_path: Path):
    """Two scans starting inside one second used to share a folder, and the
    second silently destroyed the first. Found by running two MCP tool calls
    back to back."""
    started = datetime(2026, 7, 30, 4, 0, 0).astimezone()
    made = []
    for n, co in ((3, "Alpha"), (5, "Beta"), (7, "Gamma")):
        d = cli.write_results(_rows(n, co), started, directory=str(tmp_path))
        cli.write_sidecar(d, started, {"companies": [co]})
        made.append((co, n, d))

    assert len({d.parent for _, _, d in made}) == 3
    for co, n, d in made:
        assert d.parent.name == d.stem, "folder name must match its CSV stem"
        rows = list(csv.DictReader(d.open(newline="", encoding="utf-8")))
        assert len(rows) == n and rows[0]["company"] == co
        side = d.parent / (d.stem + ".meta.json")
        assert json.loads(side.read_text(encoding="utf-8"))["companies"] == [co]


def test_manifest_stays_parseable(tmp_path: Path):
    """The header used to be written without a trailing newline, fusing it to
    the first data row."""
    started = datetime(2026, 7, 30, 4, 0, 0).astimezone()
    for i in range(3):
        d = cli.write_results(_rows(1), started, directory=str(tmp_path))
        cli.append_manifest(d, started, {"boards": 1, "shown": 1},
                            directory=str(tmp_path))
    mf = tmp_path / "runs.csv"
    rows = list(csv.DictReader(mf.open(newline="", encoding="utf-8")))
    assert len(rows) == 3
    assert all(r["file"] and r["file"].endswith(".csv") for r in rows)


def test_manifest_repairs_a_headerless_newline(tmp_path: Path):
    """An existing runs.csv already damaged by the old bug must not corrupt
    every future row."""
    mf = tmp_path / "runs.csv"
    mf.write_text(",".join(cli.MANIFEST_COLUMNS), encoding="utf-8")  # no newline
    started = datetime(2026, 7, 30, 4, 0, 0).astimezone()
    d = cli.write_results(_rows(1), started, directory=str(tmp_path))
    cli.append_manifest(d, started, {"boards": 1}, directory=str(tmp_path))
    rows = list(csv.DictReader(mf.open(newline="", encoding="utf-8")))
    assert len(rows) == 1 and rows[0]["file"].endswith(".csv")


# ------------------------------------------------- workday identity collision
def test_workday_ids_falls_back_only_on_collision():
    """bulletFields is a tenant display field. Intel emits 'Spotlight Job' for
    many postings, which collapsed them to one key and lost 30 of 640."""
    from jobscan.adapters import _workday_ids
    from jobscan.core import Job

    def j(rid):
        return Job(company="X", title="t", url="u", raw_id=rid)

    jobs = [j("JR1"), j("JR2"), j("Spotlight Job"), j("Spotlight Job")]
    paths = ["/job/a", "/job/b", "/job/c", "/job/d"]
    out = _workday_ids(jobs, paths)
    assert out[0].raw_id == "JR1", "unique ids must be left alone"
    assert out[1].raw_id == "JR2"
    assert out[2].raw_id == "/job/c", "colliding ids fall back to externalPath"
    assert out[3].raw_id == "/job/d"
    assert len({x.key for x in out}) == 4, "all four must be distinct requisitions"


def test_workday_ids_handles_blank():
    from jobscan.adapters import _workday_ids
    from jobscan.core import Job
    out = _workday_ids([Job(company="X", title="t", url="u", raw_id="")], ["/job/z"])
    assert out[0].raw_id == "/job/z"
