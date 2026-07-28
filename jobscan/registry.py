"""Load and validate the company registry.

The registry is CSV. Read the README for why. One row per company board.
A company with two boards gets two rows with the same company name.
"""
from __future__ import annotations

import csv
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
    "site",         # workday site id, oracle siteNumber
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


def write_template(path: str | Path) -> None:
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=COLUMNS).writeheader()
