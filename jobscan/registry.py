"""Load and validate the company registry.

The registry is CSV. Read the README for why. One row per company board.
A company with two boards gets two rows with the same company name.
"""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path
from typing import Any

from .adapters import KNOWN, TIER_A

COLUMNS = [
    "company",      # display name, matched by --company (case insensitive substring)
    "ticker",       # stock ticker, also matched by --company (exact, case insensitive)
    "ats",          # greenhouse|lever|ashby|smartrecruiters|workday|oracle|
                    # workable|recruitee|icims|successfactors|custom
    "token",        # board slug for token-based boards
    "tenant",       # workday tenant
    "site",         # workday site id, oracle siteNumber, google location, greenhouse "embed",
                    # successfactors "unify", eightfold "pcsx"
    "wd",           # workday cluster: wd1, wd3, wd5, wd12 ...
    "host",         # oracle fa host, or full careers host for tier B
    "careers_url",  # human-facing careers page, always fill this in
    "query",        # optional server-side keyword filter
    "notes",
]


class RegistryError(Exception):
    pass


def load(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        raise RegistryError(f"registry not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out, problems = [], []
    for i, r in enumerate(rows, start=2):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if not r.get("company") or r.get("company", "").startswith("#"):
            continue
        ats = r.get("ats", "").lower()
        r["ats"] = ats
        if ats not in KNOWN:
            problems.append(f"line {i} {r['company']}: unknown ats '{ats}'")
            continue
        if ats in ("greenhouse", "lever", "ashby", "smartrecruiters",
                   "workable", "recruitee") and not r.get("token"):
            problems.append(f"line {i} {r['company']}: {ats} needs a token")
            continue
        if ats == "workday" and not (r.get("tenant") and r.get("site")):
            problems.append(f"line {i} {r['company']}: workday needs tenant and site")
            continue
        if ats == "oracle" and not (r.get("host") and r.get("site")):
            problems.append(f"line {i} {r['company']}: oracle needs host and site")
            continue
        if ats == "successfactors" and not r.get("host"):
            problems.append(f"line {i} {r['company']}: successfactors needs host")
            continue
        if ats == "successfactors" and r.get("site") not in ("", "unify"):
            problems.append(f"line {i} {r['company']}: successfactors site must be blank or 'unify'")
            continue
        if ats == "eightfold" and not (r.get("host") or r.get("token")):
            problems.append(f"line {i} {r['company']}: eightfold needs host or token")
            continue
        if ats == "eightfold" and r.get("site") not in ("", "pcsx"):
            problems.append(f"line {i} {r['company']}: eightfold site must be blank or 'pcsx'")
            continue
        if ats == "eightfold" and r.get("site") == "pcsx" and not r.get("query"):
            problems.append(f"line {i} {r['company']}: eightfold pcsx needs the tenant domain in query")
            continue
        if ats == "icims" and not r.get("host"):
            problems.append(f"line {i} {r['company']}: icims needs host")
        if ats == "radancy" and not r.get("host"):
            problems.append(f"line {i} {r['company']}: radancy needs host")
            continue
        out.append(r)
    if problems:
        raise RegistryError("registry validation failed:\n  " + "\n  ".join(problems))
    return out


def select(rows: list[dict[str, Any]], company: str | None = None,
           ats: str | None = None) -> list[dict[str, Any]]:
    """--company matches either the display name or the ticker.

    Ticker match is exact so that a short ticker like "F" cannot swallow
    every company with an F in its name. Name match is substring.
    """
    sel = rows
    if company:
        needle = company.strip().lower()
        by_ticker = [r for r in sel if r.get("ticker", "").lower() == needle]
        sel = by_ticker or [r for r in sel if needle in r["company"].lower()]
    if ats:
        sel = [r for r in sel if r["ats"] == ats.lower()]
    return sel


def split_tiers(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    a = [r for r in rows if r["ats"] in TIER_A]
    b = [r for r in rows if r["ats"] not in TIER_A]
    return a, b


# ------------------------------------------------------------------- writing
# The file is edited line by line rather than regenerated, so a change touches
# exactly one line and every other row stays byte for byte as it was. Each
# edit is written to a temporary file, loaded and validated, and only then
# moved over the original: a write that would break the registry never lands.

def _clean(row: dict[str, Any]) -> dict[str, str]:
    unknown = set(row) - set(COLUMNS)
    if unknown:
        raise RegistryError(f"unknown column(s) {sorted(unknown)}; "
                            f"columns are {COLUMNS}")
    out = {k: str(v if v is not None else "").strip() for k, v in row.items()}
    for k, v in out.items():
        if "\n" in v or "\r" in v:
            # One record per line is what keeps a stray quote from swallowing
            # the rows after it (tests/test_registry_file.py).
            raise RegistryError(f"{k} contains a line break")
    if "ats" in out:
        out["ats"] = out["ats"].lower()
    if "ticker" in out:
        out["ticker"] = out["ticker"].upper()
    return out


def format_row(row: dict[str, Any]) -> str:
    """One registry line, quoted only where csv needs it, no line ending."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(
        [row.get(c, "") or "" for c in COLUMNS])
    return buf.getvalue()


def _read_lines(path: Path) -> tuple[list[str], str, bool]:
    text = path.read_bytes().decode("utf-8")  # read_text would fold CRLF
    bom = text.startswith("\ufeff")
    text = text.removeprefix("\ufeff")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    if lines and lines[-1] == "":
        lines.pop()
    return lines, newline, bom


def _parse(line: str) -> dict[str, str]:
    values = next(csv.reader([line]), [])
    return {c: (values[i] if i < len(values) else "").strip()
            for i, c in enumerate(COLUMNS)}


def _matches(path: Path, lines: list[str], company: str,
             ats: str | None) -> list[int]:
    """Indexes of the data lines for `company` (exact, any case), narrowed
    to one `ats` when given. Exactly one, or an error naming the problem."""
    want = company.strip().lower()
    hits = [i for i, line in enumerate(lines[1:], start=1)
            if line.strip() and _parse(line)["company"].lower() == want]
    if ats:
        hits = [i for i in hits if _parse(lines[i])["ats"].lower() == ats.lower()]
    if not hits:
        raise RegistryError(f"no row for {company!r}"
                            + (f" with ats {ats!r}" if ats else "") + f" in {path}")
    if len(hits) > 1:
        kinds = ", ".join(_parse(lines[i])["ats"] for i in hits)
        raise RegistryError(f"{company!r} has {len(hits)} rows ({kinds}); "
                            f"name the ats to pick one")
    return hits


def _commit(path: Path, lines: list[str], newline: str, bom: bool,
            expect: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(("\ufeff" if bom else "") + newline.join(lines) + newline,
                   encoding="utf-8", newline="")
    try:
        rows = load(tmp)
        if len(rows) != expect:
            raise RegistryError(f"expected {expect} rows after the edit, "
                                f"read {len(rows)}; not written")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def find_row(path: str | Path, company: str,
             ats: str | None = None) -> dict[str, str]:
    """The one row for `company` (exact name, any case), as stored."""
    path = Path(path)
    lines, _, _ = _read_lines(path)
    return _parse(lines[_matches(path, lines, company, ats)[0]])


def add_row(path: str | Path, row: dict[str, Any]) -> dict[str, str]:
    """Insert a row in company-name order. Refuses a second row with the same
    company and ats, which is an update, not an addition."""
    path = Path(path)
    new = {c: "" for c in COLUMNS} | _clean(row)
    if not new["company"] or new["company"].startswith("#"):
        raise RegistryError("company must be set and must not start with '#'")
    lines, newline, bom = _read_lines(path)
    before = len(load(path))
    key = new["company"].lower()
    at = len(lines)
    for i, line in enumerate(lines[1:], start=1):
        old = _parse(line)
        if old["company"].lower() == key and old["ats"].lower() == new["ats"]:
            raise RegistryError(f"{old['company']} already has a {new['ats']} row; "
                                f"update it instead")
        if at == len(lines) and old["company"].lower() > key:
            at = i
    lines.insert(at, format_row(new))
    _commit(path, lines, newline, bom, before + 1)
    return new


def update_row(path: str | Path, company: str, changes: dict[str, Any],
               ats: str | None = None) -> dict[str, str]:
    """Change some fields of one row; the rest stay as they are."""
    path = Path(path)
    lines, newline, bom = _read_lines(path)
    before = len(load(path))
    i = _matches(path, lines, company, ats)[0]
    new = _parse(lines[i]) | _clean(changes)
    if not new["company"] or new["company"].startswith("#"):
        raise RegistryError("company must be set and must not start with '#'")
    lines[i] = format_row(new)
    _commit(path, lines, newline, bom, before)
    return new


def remove_row(path: str | Path, company: str,
               ats: str | None = None) -> dict[str, str]:
    """Delete one row and return what it held."""
    path = Path(path)
    lines, newline, bom = _read_lines(path)
    before = len(load(path))
    i = _matches(path, lines, company, ats)[0]
    old = _parse(lines.pop(i))
    _commit(path, lines, newline, bom, before - 1)
    return old


def write_template(path: str | Path) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=COLUMNS).writeheader()
