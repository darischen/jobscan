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
from collections.abc import Callable
from typing import Any

import httpx

from .adapters import TIER_A, HEADERS
from .core import Job, classify

# Deciding whether a posting is reachable from inside the US.
#
# The signals are deliberately split by strength, because a bare two-letter
# code is much weaker evidence than a spelled-out place, and treating them
# alike leaked foreign postings. "Rio de Janeiro, Brazil" was reading as US
# because `de` matched Delaware; "Belen La Ribera, Costa Rica" via Louisiana;
# "Abu Dhabi, Al Sila Tower" via Alabama.

# Strong: unambiguous however it is cased. No foreign place name contains one.
US_NAMES = (
    "ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO",
    "CONNECTICUT", "DELAWARE", "FLORIDA", "GEORGIA", "HAWAII", "IDAHO",
    "ILLINOIS", "INDIANA", "IOWA", "KANSAS", "KENTUCKY", "LOUISIANA",
    "MAINE", "MARYLAND", "MASSACHUSETTS", "MICHIGAN", "MINNESOTA",
    "MISSISSIPPI", "MISSOURI", "MONTANA", "NEBRASKA", "NEVADA",
    "NEW HAMPSHIRE", "NEW JERSEY", "NEW MEXICO", "NEW YORK",
    "NORTH CAROLINA", "NORTH DAKOTA", "OHIO", "OKLAHOMA", "OREGON",
    "PENNSYLVANIA", "RHODE ISLAND", "SOUTH CAROLINA", "SOUTH DAKOTA",
    "TENNESSEE", "TEXAS", "UTAH", "VERMONT", "VIRGINIA", "WASHINGTON",
    "WEST VIRGINIA", "WISCONSIN", "WYOMING",
    "DISTRICT OF COLUMBIA", "PUERTO RICO",
    "UNITED STATES", "U.S.A.", "U.S.", "USA",
)

# Weak: two-letter codes, matched case-sensitively so only a real abbreviation
# counts. Boards write these uppercase ("Austin, TX", "US-CA-Santa Clara"),
# while the particles that caused the leaks are lower or title case.
US_CODES = (
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "D.C.", "PR", "US",
)

# Codes that double as ordinary words in non-English place names. When one of
# these is the *only* US evidence and a foreign country is also named, the
# code is almost certainly the foreign word.
AMBIGUOUS_CODES = frozenset({"DE", "LA", "AL", "IN", "OR", "MA", "PA", "ID",
                             "MI", "MO", "CO", "DA", "SO", "VA"})

# Not exhaustive, and does not need to be. It only has to name the countries
# that show up on these boards, and it is consulted solely to break a tie
# against an ambiguous two-letter code.
FOREIGN = (
    "AFGHANISTAN", "ALBANIA", "ALGERIA", "ARGENTINA", "ARMENIA", "AUSTRALIA",
    "AUSTRIA", "AZERBAIJAN", "BAHRAIN", "BANGLADESH", "BELARUS", "BELGIUM",
    "BOLIVIA", "BOSNIA", "BRAZIL", "BULGARIA", "CAMBODIA", "CAMEROON",
    "CANADA", "CHILE", "CHINA", "COLOMBIA", "COSTA RICA", "CROATIA", "CYPRUS",
    "CZECH", "CZECHIA", "DENMARK", "DOMINICAN", "ECUADOR", "EGYPT",
    "EL SALVADOR", "ESTONIA", "ETHIOPIA", "FINLAND", "FRANCE", "GERMANY",
    "GHANA", "GREECE", "GUATEMALA", "HONDURAS", "HONG KONG", "HUNGARY",
    "ICELAND", "INDIA", "INDONESIA", "IRAQ", "IRELAND", "ISRAEL", "ITALY",
    "JAPAN", "JORDAN", "KAZAKHSTAN", "KENYA", "KOREA", "KUWAIT", "LATVIA",
    "LEBANON", "LITHUANIA", "LUXEMBOURG", "MALAYSIA", "MALTA", "MEXICO",
    "MOLDOVA", "MOROCCO", "NETHERLANDS", "NEW ZEALAND", "NICARAGUA",
    "NIGERIA", "NORWAY", "OMAN", "PAKISTAN", "PANAMA", "PARAGUAY", "PERU",
    "PHILIPPINES", "POLAND", "PORTUGAL", "QATAR", "ROMANIA", "RUSSIA",
    "SAUDI ARABIA", "SERBIA", "SINGAPORE", "SLOVAKIA", "SLOVENIA",
    "SOUTH AFRICA", "SPAIN", "SRI LANKA", "SWEDEN", "SWITZERLAND", "TAIWAN",
    "TANZANIA", "THAILAND", "TUNISIA", "TURKEY", "TRKIYE", "UGANDA",
    "UKRAINE", "UNITED ARAB EMIRATES", "UNITED KINGDOM", "URUGUAY",
    "UZBEKISTAN", "VENEZUELA", "VIETNAM", "ZAMBIA", "ZIMBABWE",
    "ENGLAND", "SCOTLAND", "WALES", "NORTHERN IRELAND",
)


def _boundary(words, flags=0):
    """Match any of `words` as a whole token.

    Lookarounds exclude letters and digits, so punctuation never blocks a hit
    ("US-CA-Santa Clara", "New York, NY", "Santa Clara, CA 95051" all match)
    while an alphanumeric site code cannot fake one: Accenture's real
    `Cairo-CO55-Uvenues` would otherwise read CO as Colorado. Longest first so
    NEW YORK beats NY.
    """
    return re.compile(
        r"(?<![A-Za-z0-9])(?:"
        + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
        + r")(?![A-Za-z0-9])", flags)


_US_NAME_RE = _boundary(US_NAMES, re.I)      # case-insensitive: unambiguous
_US_CODE_RE = _boundary(US_CODES)            # case-SENSITIVE: uppercase only
_FOREIGN_RE = _boundary(FOREIGN, re.I)


# Vocabulary a board uses when it is describing an arrangement rather than
# naming a place. Whatever survives removing these is a real geographic name,
# which is what separates "Fully Remote" from "Remote - India" and
# "Location Negotiable" from "Bengaluru".
_PLACELESS = re.compile(r"""\b(
    REMOTE | WFH | WORK | FROM | HOME | ANYWHERE | DISTRIBUTED | VIRTUAL
  | TELECOMMUTE | TELEWORK | FULLY | FULL | PARTLY | PARTIAL | HYBRID
  | FLEXIBLE | OPTIONAL | ELIGIBLE | FRIENDLY | BASED | LOCATION | LOCATIONS
  | MULTIPLE | VARIOUS | OTHER | ANY | OR | AND | TIME | PART | ONSITE
  | ON | SITE | OFFICE | FIELD | TRAVEL | NATIONWIDE
  | NEGOTIABLE | UNSPECIFIED | UNDISCLOSED | TBD | TBA | N\.?A\.?
)\b""", re.X)
# Kept as the old name so nothing that imported it breaks.
_REMOTE_FILLER = _PLACELESS


def _names_no_place(loc: str) -> bool:
    """True when the string describes an arrangement and names nowhere.

    "Remote", "Fully Remote", "Location Negotiable", "Multiple Locations".
    Treated the same as a blank field: a non-answer is not evidence of a
    foreign location, and Accenture alone sends 292 "Location Negotiable"
    postings that would otherwise be dropped on no evidence at all.
    """
    return not re.search(r"[A-Z]", _PLACELESS.sub(" ", loc.upper()))


def _names_us(loc: str) -> bool:
    """Whether a location string names somewhere in the US.

    A spelled-out state or country wins outright. A bare two-letter code wins
    too, unless it is one of the codes that doubles as a foreign word and the
    string also names a foreign country: "Rio de Janeiro, Brazil" offers only
    `de` plus Brazil, which is Portuguese, not Delaware. A non-ambiguous code
    still wins alongside a foreign country, because "Dublin, Ireland; Austin,
    TX" is a real multi-location req that a US applicant can take.
    """
    if _US_NAME_RE.search(loc):
        return True
    codes = {m.group(0) for m in _US_CODE_RE.finditer(loc)}
    if not codes:
        return False
    if codes - AMBIGUOUS_CODES:
        return True
    return not _FOREIGN_RE.search(loc)


def is_us_location(loc: str | None) -> bool:
    """True when a posting is open to someone who will relocate within the US.

    A blank location passes. Boards that hide the field would otherwise lose
    every row, and a missing location is not evidence of a foreign one.

    Multi-location postings pass on any US option, since a req listing
    "Dublin, Ireland; Austin, TX" is one a US applicant can take.

    A string that names no place at all is treated like a blank one, for the
    same reason: "Remote", "Location Negotiable" and "Multiple Locations" are
    non-answers, not foreign answers. "Remote - India" names a place and is
    dropped.
    """
    if not loc:
        return True
    if _names_us(loc):
        return True
    return _names_no_place(loc)


async def _one(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict[str, Any], retries: int, errors: list[tuple[str, str, str]], on_board_done: Callable[[str, int], None] | None = None) -> list[Job]:
    fn = TIER_A[row["ats"]]
    jobs: list[Job] = []
    try:
        async with sem:
            for attempt in range(retries + 1):
                try:
                    jobs = await fn(client, row["company"], row)
                    for j in jobs:
                        j.ticker = row.get("ticker", "")
                    return jobs
                except Exception as e:  # noqa: BLE001
                    if attempt == retries:
                        errors.append((row["company"], row["ats"],
                                       f"{type(e).__name__}: {e}"))
                        jobs = []
                        return jobs
                    await asyncio.sleep(1.5 * (attempt + 1))
        return jobs
    finally:
        # Fires on success and on give-up alike, so a caller tracking progress
        # never stalls on a board that failed. asyncio.gather reports nothing
        # until every coroutine finishes, so this hook is the only way to see
        # progress during a long scan.
        if on_board_done is not None:
            on_board_done(row["company"], len(jobs))


async def scan(rows: list[dict[str, Any]], concurrency: int = 20, timeout: float = 30.0,
               retries: int = 1,
               on_board_done: Callable[[str, int], None] | None = None) -> tuple[list[Job], list[tuple[str, str, str]]]:
    sem = asyncio.Semaphore(concurrency)
    errors: list[tuple[str, str, str]] = []
    limits = httpx.Limits(max_connections=concurrency + 10,
                          max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(timeout=timeout, limits=limits,
                                 follow_redirects=True, headers=HEADERS) as client:
        coros = (_one(client, sem, r, retries, errors, on_board_done) for r in rows)
        batches: list[list[Job]] = await asyncio.gather(*coros)
    jobs: list[Job] = []
    for b in batches:
        jobs.extend(b)
    for j in jobs:
        j.bucket, j.role = classify(j.title)
    jobs = [j for j in jobs if is_us_location(j.location)]
    return jobs, errors
