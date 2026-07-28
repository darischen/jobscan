"""Runner. Tier A only.

Tier A is pure HTTP and shares no mutable state, so it fans out wide.
Tier B (Playwright) lives in a separate process with its own semaphore,
because concurrent browser navigation corrupts shared tab state. Keeping
them in different processes makes that mistake impossible rather than
merely discouraged.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import httpx

from .adapters import TIER_A, HEADERS
from .core import Job, classify

# Every way a board writes a US place. Two-letter codes have to be matched
# on a letter boundary or they hit inside foreign names: GA lands in
# PortuGAl, IN in DublIN, CA in CAnada.
US_PLACES = (
    "AL", "ALABAMA", "AK", "ALASKA", "AZ", "ARIZONA", "AR", "ARKANSAS",
    "CA", "CALIFORNIA", "CO", "COLORADO", "CT", "CONNECTICUT", "DE", "DELAWARE",
    "FL", "FLORIDA", "GA", "GEORGIA", "HI", "HAWAII", "ID", "IDAHO",
    "IL", "ILLINOIS", "IN", "INDIANA", "IA", "IOWA", "KS", "KANSAS",
    "KY", "KENTUCKY", "LA", "LOUISIANA", "ME", "MAINE", "MD", "MARYLAND",
    "MA", "MASSACHUSETTS", "MI", "MICHIGAN", "MN", "MINNESOTA", "MS", "MISSISSIPPI",
    "MO", "MISSOURI", "MT", "MONTANA", "NE", "NEBRASKA", "NV", "NEVADA",
    "NH", "NEW HAMPSHIRE", "NJ", "NEW JERSEY", "NM", "NEW MEXICO", "NY", "NEW YORK",
    "NC", "NORTH CAROLINA", "ND", "NORTH DAKOTA", "OH", "OHIO", "OK", "OKLAHOMA",
    "OR", "OREGON", "PA", "PENNSYLVANIA", "RI", "RHODE ISLAND", "SC", "SOUTH CAROLINA",
    "SD", "SOUTH DAKOTA", "TN", "TENNESSEE", "TX", "TEXAS", "UT", "UTAH",
    "VT", "VERMONT", "VA", "VIRGINIA", "WA", "WASHINGTON", "WV", "WEST VIRGINIA",
    "WI", "WISCONSIN", "WY", "WYOMING",
    "DC", "D.C.", "DISTRICT OF COLUMBIA",
    "US", "U.S.", "USA", "U.S.A.", "UNITED STATES",
)

# Longest first so NEW YORK is tried before NY. The lookarounds are letter
# only, so punctuation around a token never blocks a match: "US-CA-Santa
# Clara" and "New York, NY" both hit.
_US_RE = re.compile(
    r"(?<![A-Z])(?:"
    + "|".join(re.escape(p) for p in sorted(US_PLACES, key=len, reverse=True))
    + r")(?![A-Z])"
)


def is_us_location(loc: str | None) -> bool:
    """True when a posting is US based.

    A blank location passes. Boards that hide the field would otherwise
    lose every row, and a missing location is not evidence of a foreign one.
    """
    if not loc:
        return True
    upper = loc.upper()
    if _US_RE.search(upper):
        return True
    # Bare "Remote" is a US listing on a US board. "France, Remote;
    # Germany, Remote" is not, and the separator is what tells them apart.
    return "REMOTE" in upper and "," not in upper and ";" not in upper


async def _one(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict[str, Any], retries: int, errors: list[tuple[str, str, str]]) -> list[Job]:
    fn = TIER_A[row["ats"]]
    async with sem:
        for attempt in range(retries + 1):
            try:
                jobs = await fn(client, row["company"], row)
                for j in jobs:
                    j.ticker = row.get("ticker", "")
                return jobs
            except Exception as e:  # noqa: BLE001
                if attempt == retries:
                    errors.append((row["company"], row["ats"], f"{type(e).__name__}: {e}"))
                    return []
                await asyncio.sleep(1.5 * (attempt + 1))
    return []


async def scan(rows: list[dict[str, Any]], concurrency: int = 20, timeout: float = 30.0,
               retries: int = 1) -> tuple[list[Job], list[tuple[str, str, str]]]:
    sem = asyncio.Semaphore(concurrency)
    errors: list[tuple[str, str, str]] = []
    limits = httpx.Limits(max_connections=concurrency + 10,
                          max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=timeout, limits=limits,
                                 follow_redirects=True, headers=HEADERS) as client:
        coros = (_one(client, sem, r, retries, errors) for r in rows)
        batches: list[list[Job]] = await asyncio.gather(*coros)
    jobs: list[Job] = []
    for b in batches:
        jobs.extend(b)
    for j in jobs:
        j.bucket, j.role = classify(j.title)
    jobs = [j for j in jobs if is_us_location(j.location)]
    return jobs, errors
