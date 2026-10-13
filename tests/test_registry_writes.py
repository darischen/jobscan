"""Writing the registry from code: add, update, remove rows. Offline.

The file is edited line by line, not regenerated, so every row a change does
not touch stays byte for byte as it was, and the result is validated with
registry.load before it replaces the original.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from jobscan import registry
from tests.conftest import write_registry
from tests.test_mcp_server import call


@pytest.fixture
def reg(tmp_path: Path) -> Path:
    """LF line endings, like the committed registry."""
    p = write_registry(tmp_path / "companies.csv", [
        {"company": "Alpha", "ats": "greenhouse", "token": "alpha"},
        {"company": "Gamma", "ats": "lever", "token": "gamma",
         "notes": "two boards, see Gamma Labs"},
        {"company": "Gamma", "ats": "ashby", "token": "gammalabs"},
        {"company": "Zeta", "ats": "custom", "host": "zeta.example"},
    ])
    p.write_bytes(p.read_bytes().replace(b"\r\n", b"\n"))
    return p


def _lines(p: Path) -> list[str]:
    return p.read_text(encoding="utf-8").splitlines()


# --------------------------------------------------------------------- add
def test_add_inserts_in_name_order_and_leaves_other_lines_alone(reg: Path):
    before = _lines(reg)
    row = registry.add_row(reg, {"company": "Beta", "ats": "greenhouse",
                                 "token": "beta", "notes": "found, by hand"})
    after = _lines(reg)
    assert row["company"] == "Beta"
    assert after[:2] == before[:2] and after[3:] == before[2:]
    assert after[2] == 'Beta,,greenhouse,beta,,,,,,,"found, by hand"'
    assert [r["company"] for r in registry.load(reg)] == ["Alpha", "Beta", "Gamma",
                                                          "Gamma", "Zeta"]


def test_add_refuses_a_second_row_for_the_same_company_and_ats(reg: Path):
    with pytest.raises(registry.RegistryError, match="already"):
        registry.add_row(reg, {"company": "alpha", "ats": "greenhouse", "token": "x"})


def test_a_row_that_fails_validation_leaves_the_file_unchanged(reg: Path):
    before = reg.read_bytes()
    with pytest.raises(registry.RegistryError, match="token"):
        registry.add_row(reg, {"company": "Beta", "ats": "greenhouse"})
    with pytest.raises(registry.RegistryError, match="unknown"):
        registry.add_row(reg, {"company": "Beta", "ats": "greenhouse",
                               "token": "b", "colour": "red"})
    assert reg.read_bytes() == before


@pytest.mark.parametrize("bad", ["two\nlines", "#comment"])
def test_a_value_that_could_break_the_one_record_per_line_rule_is_refused(reg: Path, bad):
    with pytest.raises(registry.RegistryError):
        registry.add_row(reg, {"company": bad, "ats": "greenhouse", "token": "b"})


def test_crlf_files_stay_crlf(tmp_path: Path, reg: Path):
    crlf = tmp_path / "crlf.csv"
    crlf.write_bytes(reg.read_bytes().replace(b"\n", b"\r\n"))
    registry.add_row(crlf, {"company": "Beta", "ats": "greenhouse", "token": "b"})
    raw = crlf.read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n") == 6
    registry.add_row(reg, {"company": "Beta", "ats": "greenhouse", "token": "b"})
    assert b"\r" not in reg.read_bytes()


# ------------------------------------------------------------------ update
def test_update_changes_only_the_named_fields(reg: Path):
    row = registry.update_row(reg, "alpha", {"token": "alpha2", "ticker": "ALP"})
    assert (row["token"], row["ticker"], row["ats"]) == ("alpha2", "ALP", "greenhouse")
    assert _lines(reg)[1] == "Alpha,ALP,greenhouse,alpha2,,,,,,,"


def test_update_of_a_company_with_two_rows_needs_the_ats(reg: Path):
    with pytest.raises(registry.RegistryError, match="2 rows"):
        registry.update_row(reg, "Gamma", {"notes": "x"})
    row = registry.update_row(reg, "Gamma", {"notes": "x"}, ats="ashby")
    assert row["token"] == "gammalabs" and row["notes"] == "x"


def test_update_can_move_a_row_to_another_ats(reg: Path):
    row = registry.update_row(reg, "Zeta", {"ats": "greenhouse", "token": "zeta",
                                            "host": ""})
    assert row["ats"] == "greenhouse"
    assert registry.select(registry.load(reg), company="Zeta")[0]["token"] == "zeta"


def test_update_of_an_unknown_company_is_an_error(reg: Path):
    with pytest.raises(registry.RegistryError, match="no row"):
        registry.update_row(reg, "Nope", {"notes": "x"})


# ------------------------------------------------------------------ remove
def test_remove_deletes_exactly_one_line(reg: Path):
    removed = registry.remove_row(reg, "Gamma", ats="lever")
    assert removed["token"] == "gamma"
    assert [r["ats"] for r in registry.load(reg) if r["company"] == "Gamma"] == ["ashby"]
    assert len(_lines(reg)) == 4


# --------------------------------------------------------------------- mcp
@pytest.fixture
def probe(mcp_env, monkeypatch):
    """Stand-in for the live board check: postings per token, 0 by default."""
    counts: dict[str, int] = {}
    asked: list[dict] = []

    async def fake(row, timeout=45.0):
        asked.append(dict(row))
        n = counts.get(row.get("token") or row.get("host") or "", 0)
        return (n, None) if n >= 0 else (0, "HTTPStatusError: 404")

    monkeypatch.setattr(mcp_env.runner, "probe", fake)
    return counts, asked


@pytest.mark.asyncio
async def test_add_company_confirms_the_board_live_before_writing(mcp_env, probe):
    counts, _ = probe
    counts["newco"] = 42
    d = await call(mcp_env, "add_company", name="NewCo", ats="greenhouse",
                   token="newco", ticker="nco")
    assert d["ok"] and d["written"] and d["postings"] == 42
    row = registry.select(registry.load(mcp_env.REGISTRY), company="NewCo")[0]
    assert (row["token"], row["ticker"]) == ("newco", "NCO")


@pytest.mark.asyncio
async def test_add_company_refuses_a_board_that_lists_nothing_or_errors(mcp_env, probe):
    counts, _ = probe
    before = Path(mcp_env.REGISTRY).read_bytes()
    d = await call(mcp_env, "add_company", name="Empty", ats="greenhouse", token="empty")
    assert not d["ok"] and "0 postings" in d["error"]
    counts["gone"] = -1
    d = await call(mcp_env, "add_company", name="Gone", ats="greenhouse", token="gone")
    assert not d["ok"] and "404" in d["error"]
    assert Path(mcp_env.REGISTRY).read_bytes() == before


@pytest.mark.asyncio
async def test_add_company_dry_run_checks_but_does_not_write(mcp_env, probe):
    counts, _ = probe
    counts["newco"] = 3
    before = Path(mcp_env.REGISTRY).read_bytes()
    d = await call(mcp_env, "add_company", name="NewCo", ats="greenhouse",
                   token="newco", dry_run=True)
    assert d["ok"] and not d["written"] and d["postings"] == 3 and d["csv_line"]
    assert Path(mcp_env.REGISTRY).read_bytes() == before


@pytest.mark.asyncio
async def test_add_company_without_an_ats_discovers_the_board(mcp_env, probe, monkeypatch):
    import tools.discover as discover
    counts, _ = probe
    counts["found"] = 9

    async def resolve(client, name, ticker, url, ats_hint="", auto=False):
        return {c: "" for c in registry.COLUMNS} | {
            "company": name, "ats": "greenhouse", "token": "found", "careers_url": url}
    monkeypatch.setattr(discover, "resolve", resolve)
    d = await call(mcp_env, "add_company", name="Found",
                   url="https://found.example/careers")
    assert d["ok"] and d["written"] and d["row"]["token"] == "found"


@pytest.mark.asyncio
async def test_an_unconfirmed_tier_b_row_needs_explicit_consent(mcp_env, probe):
    d = await call(mcp_env, "add_company", name="Browsery", ats="custom",
                   host="browsery.example")
    assert not d["ok"] and "tier B" in d["error"]
    d = await call(mcp_env, "add_company", name="Browsery", ats="custom",
                   host="browsery.example", allow_tier_b=True)
    assert d["ok"] and d["written"] and d["postings"] is None


@pytest.mark.asyncio
async def test_update_company_rechecks_the_board_when_its_identity_changes(mcp_env, probe):
    counts, asked = probe
    d = await call(mcp_env, "update_company", company="Co3", token="co3new")
    assert not d["ok"] and "0 postings" in d["error"]
    counts["co3new"] = 5
    d = await call(mcp_env, "update_company", company="Co3", token="co3new")
    assert d["ok"] and d["row"]["token"] == "co3new" and d["postings"] == 5
    asked.clear()
    d = await call(mcp_env, "update_company", company="Co3", notes="just a note")
    assert d["ok"] and d["postings"] is None and asked == []


@pytest.mark.asyncio
async def test_remove_company(mcp_env, probe):
    d = await call(mcp_env, "remove_company", company="Co4")
    assert d["ok"] and d["removed"]["company"] == "Co4"
    assert not registry.select(registry.load(mcp_env.REGISTRY), company="Co4")
