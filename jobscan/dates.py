"""Normalize the many ways an ATS reports when a posting went live.

Every adapter hands raw values here and gets back an ISO 8601 UTC string
or None. Never guess. None means the board did not tell us, and the
first_seen column in the store becomes the fallback signal.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def from_iso(value: Any) -> str | None:
    """Greenhouse updated_at, Ashby publishedAt, SmartRecruiters releasedDate."""
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    for cut in (None, 19, 10):
        try:
            return _iso(datetime.fromisoformat(v if cut is None else v[:cut]))
        except ValueError:
            continue
    return None


def from_epoch_ms(value: Any) -> str | None:
    """Lever createdAt."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n > 1e12:
        n = n / 1000
    if n < 946684800:  # before year 2000, almost certainly bogus
        return None
    return _iso(datetime.fromtimestamp(n, tz=timezone.utc))


_REL = re.compile(
    r"posted\s+(?:(today)|(yesterday)|(\d+)\+?\s*(day|week|month|year)s?\s*ago)",
    re.I,
)
_DELTA = {"day": 1, "week": 7, "month": 30, "year": 365}


def from_workday_relative(value: Any, now: datetime | None = None) -> str | None:
    """Workday gives 'Posted 3 Days Ago' or 'Posted Today', not a timestamp.

    Resolution is one day at best, and '30+ Days Ago' is a ceiling, not a
    date. Treat the result as approximate.
    """
    if not value or not isinstance(value, str):
        return None
    now = now or datetime.now(timezone.utc)
    m = _REL.search(value)
    if not m:
        return from_iso(value)
    if m.group(1):
        return _iso(now)
    if m.group(2):
        return _iso(now - timedelta(days=1))
    n, unit = int(m.group(3)), m.group(4).lower()
    return _iso(now - timedelta(days=n * _DELTA[unit]))


def pick(*candidates: tuple[str, str | None]) -> tuple[str | None, str]:
    """Take (field_name, parsed_value) pairs, return the first non-null."""
    for name, value in candidates:
        if value:
            return value, name
    return None, ""
