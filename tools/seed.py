#!/usr/bin/env python3
"""Build a pending.txt from index constituents. No LLM in the loop.

Asking a model to list the S&P 500 gets you hallucinated tickers, stale
constituents, and silent omissions. Wikipedia's constituent tables are
maintained, carry GICS sectors, and parse in one line.

    python tools/seed.py                      # S&P 500 tech + comms
    python tools/seed.py --sectors all        # all 503
    python tools/seed.py --index nasdaq100
    python tools/seed.py --index both -o pending.txt

Output is `Name<TAB>TICKER` per line, which is what discover.py --batch
expects. Pipe it straight in.
"""
from __future__ import annotations

import argparse
import io
import re
import sys
from typing import Any

import httpx
import pandas as pd

SOURCES: dict[str, tuple[str, int | None, str, str, str]] = {
    "sp500": ("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
              0, "Security", "Symbol", "GICS Sector"),
    "nasdaq100": ("https://en.wikipedia.org/wiki/List_of_NASDAQ-100_companies",
                  None, "Company", "Ticker", "ICB Industry[1]"),
}
TECH_SECTORS = {"Information Technology", "Communication Services",
                # ICB names used by the Nasdaq-100 table
                "Technology", "Telecommunications"}

# Companies whose engineering hiring makes them worth including even though
# GICS files them elsewhere.
EXTRAS = {"AMZN", "TSLA", "ABNB", "UBER", "BKNG", "DASH", "COIN", "PYPL",
          "SHOP", "MELI", "EBAY", "CART", "RBLX", "SQ", "XYZ"}

CLEAN = re.compile(r"\s*\((?:Class|The)[^)]*\)|,?\s+(?:Inc|Corp|Corporation|"
                   r"Co|Ltd|Ltd\.|plc|PLC|N\.V\.|S\.A\.|Group|Holdings|"
                   r"Company|Companies|Technologies|International)\.?$",
                   re.I)


def clean_name(s: str) -> str:
    prev = None
    while prev != s:
        prev = s
        s = CLEAN.sub("", s).strip().rstrip(",.")
    return s


def fetch(index: str) -> pd.DataFrame:
    url, table_idx, name_col, sym_col, sec_col = SOURCES[index]
    r = httpx.get(url, headers={"User-Agent": "Mozilla/5.0"},
                  timeout=45, follow_redirects=True)
    r.raise_for_status()
    tables = pd.read_html(io.StringIO(r.text))
    if table_idx is not None:
        t = tables[table_idx]
    else:
        t = next(x for x in tables
                 if {name_col, sym_col}.issubset(set(x.columns)))
    if sec_col not in t.columns:
        sec_col = next((c for c in t.columns
                        if "sector" in str(c).lower()
                        or "industry" in str(c).lower()), None)
    t = t.rename(columns={name_col: "name", sym_col: "ticker",
                          **({sec_col: "sector"} if sec_col else {})})
    keep = ["name", "ticker"] + (["sector"] if "sector" in t.columns else [])
    t = t[keep].copy()
    if "sector" not in t.columns:
        t["sector"] = ""
    return t


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", choices=("sp500", "nasdaq100", "both"),
                    default="sp500")
    ap.add_argument("--sectors", default="tech",
                    help="'tech' (IT + Communication Services + extras) or 'all'")
    ap.add_argument("--out", "-o", help="write here instead of stdout")
    ap.add_argument("--exclude-registry", default="registry/companies.csv",
                    help="skip tickers already present, '' to disable")
    a = ap.parse_args()

    frames: list[pd.DataFrame] = []
    for idx in (["sp500", "nasdaq100"] if a.index == "both" else [a.index]):
        frames.append(fetch(idx))
    t = pd.concat(frames, ignore_index=True)

    if a.sectors == "tech":
        t = t[t["sector"].isin(TECH_SECTORS) | t["ticker"].isin(EXTRAS)]

    t["name"] = t["name"].astype(str).map(clean_name)
    t["ticker"] = t["ticker"].astype(str).str.strip().str.upper()
    # Alphabet ships as GOOG and GOOGL, same careers site
    t = t.drop_duplicates(subset="name", keep="first")
    t = t.drop_duplicates(subset="ticker", keep="first")

    have: set[str] = set()
    if a.exclude_registry:
        try:
            reg = pd.read_csv(a.exclude_registry)
            have = set(reg["ticker"].dropna().astype(str).str.upper()) | \
                   set(reg["company"].dropna().astype(str).str.lower())
        except Exception:  # noqa: BLE001
            pass
    rows: list[tuple[Any, Any]] = [(n, k) for n, k in zip(t["name"], t["ticker"])
            if k not in have and n.lower() not in have]

    lines = "\n".join(f"{n}\t{k}" for n, k in sorted(rows))
    if a.out:
        open(a.out, "w", encoding="utf-8").write(lines + "\n")
        print(f"{len(rows)} companies -> {a.out}", file=sys.stderr)
    else:
        print(lines)
        print(f"# {len(rows)} companies", file=sys.stderr)


if __name__ == "__main__":
    main()
