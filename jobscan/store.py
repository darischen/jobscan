"""SQLite sink.

Two jobs here:
  1. Dedupe across runs so you only ever look at what is new.
  2. Answer "when did this posting appear" even when the board hides the date.

posted_at is what the ATS claims. first_seen is when your scanner first saw
the requisition. closed_at gets stamped when a requisition that was on the
board last run is gone this run. Disappearance beats any per-URL liveness
check and costs one UPDATE.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import DEFAULT_KEEP, Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key           TEXT PRIMARY KEY,
    company       TEXT NOT NULL,
    ticker        TEXT,
    ats           TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    location      TEXT,
    department    TEXT,
    bucket        TEXT,
    posted_at     TEXT,
    posted_source TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    closed_at     TEXT,
    applied_at    TEXT,
    starred       INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_first_seen ON jobs(first_seen DESC);
CREATE INDEX IF NOT EXISTS idx_company    ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_ticker     ON jobs(ticker);
CREATE INDEX IF NOT EXISTS idx_open       ON jobs(closed_at) WHERE closed_at IS NULL;

CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT,
    companies  INTEGER, jobs_seen INTEGER,
    new_jobs   INTEGER, closed_jobs INTEGER, errors INTEGER,
    command    TEXT, result_file TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    def _migrate(self):
        """Add columns to databases created before they existed."""
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for col, ddl in (("ticker", "TEXT"),):
            if col not in have:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}
        for col, ddl in (("command", "TEXT"), ("result_file", "TEXT")):
            if col not in have:
                self.db.execute(f"ALTER TABLE runs ADD COLUMN {col} {ddl}")

    def close(self):
        self.db.close()

    def upsert(self, jobs: list[Job], scanned_companies: set[str]) -> dict[str, Any]:
        """Insert or refresh, then close anything that vanished.

        Only companies actually scanned this run are eligible for closing,
        so a partial run never marks the rest of the registry as dead.
        """
        ts = now()
        new_keys = []
        cur = self.db.cursor()
        for j in jobs:
            row = cur.execute("SELECT key FROM jobs WHERE key=?", (j.key,)).fetchone()
            if row is None:
                new_keys.append(j.key)
                cur.execute(
                    """INSERT INTO jobs (key, company, ticker, ats, title, url,
                       location, department, bucket, posted_at, posted_source,
                       first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (j.key, j.company, j.ticker, j.ats, j.title, j.url, j.location,
                     j.department, j.bucket, j.posted_at, j.posted_source, ts, ts),
                )
            else:
                cur.execute(
                    """UPDATE jobs SET last_seen=?, closed_at=NULL, title=?, url=?,
                       location=?, bucket=?, ticker=COALESCE(NULLIF(?,''), ticker),
                       posted_at=COALESCE(posted_at, ?)
                       WHERE key=?""",
                    (ts, j.title, j.url, j.location, j.bucket, j.ticker,
                     j.posted_at, j.key),
                )

        closed = 0
        if scanned_companies:
            qs = ",".join("?" * len(scanned_companies))
            closed = cur.execute(
                f"""UPDATE jobs SET closed_at=?
                    WHERE closed_at IS NULL AND last_seen < ?
                      AND company IN ({qs})""",
                (ts, ts, *scanned_companies),
            ).rowcount
        self.db.commit()
        return {"new": len(new_keys), "closed": closed, "new_keys": new_keys}

    def record_run(self, started: str, companies: int, seen: int, new: int, closed: int, errors: int,
                   command: str = "", result_file: str = "") -> None:
        self.db.execute(
            """INSERT INTO runs (started_at, finished_at, companies, jobs_seen,
               new_jobs, closed_jobs, errors, command, result_file)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (started, now(), companies, seen, new, closed, errors,
             command, result_file),
        )
        self.db.commit()

    def _find(self, ref: str) -> sqlite3.Row | None:
        """One posting by key or url. A url is tried as given, then with a
        trailing slash added or removed, since that is how a pasted link
        most often differs from the stored one."""
        ref = ref.strip()
        for candidate in (ref, ref.rstrip("/"), ref.rstrip("/") + "/"):
            row = self.db.execute(
                "SELECT * FROM jobs WHERE key=? OR url=?",
                (candidate, candidate)).fetchone()
            if row is not None:
                return row
        return None

    def mark(self, refs: list[str], applied: bool | None = None,
             starred: bool | None = None) -> dict[str, Any]:
        """Set or clear applied_at and starred on postings named by key or url.

        None leaves a field alone. Applying twice keeps the first time, so
        the date stays the day the application went in. Open and closed
        postings alike: an application outlives its posting. References
        that match nothing come back in `unknown` rather than passing
        silently.
        """
        ts = now()
        done, unknown = [], []
        for ref in refs:
            row = self._find(ref)
            if row is None:
                unknown.append(ref)
                continue
            if applied is True:
                self.db.execute(
                    "UPDATE jobs SET applied_at=COALESCE(applied_at, ?) WHERE key=?",
                    (ts, row["key"]))
            elif applied is False:
                self.db.execute("UPDATE jobs SET applied_at=NULL WHERE key=?",
                                (row["key"],))
            if starred is not None:
                self.db.execute("UPDATE jobs SET starred=? WHERE key=?",
                                (int(starred), row["key"]))
            done.append(dict(self.db.execute(
                "SELECT * FROM jobs WHERE key=?", (row["key"],)).fetchone()))
        self.db.commit()
        return {"jobs": done, "unknown": unknown}

    def tracked(self, applied: bool | None = None, starred: bool | None = None,
                include_closed: bool = True) -> list[dict[str, Any]]:
        """Postings applied to or starred, closed ones included by default.

        applied / starred narrow to one mark; with neither, either mark
        qualifies.
        """
        sql = "SELECT * FROM jobs WHERE (applied_at IS NOT NULL OR starred=1)"
        if applied is not None:
            sql += f" AND applied_at IS {'NOT ' if applied else ''}NULL"
        if starred is not None:
            sql += f" AND COALESCE(starred, 0)={int(starred)}"
        if not include_closed:
            sql += " AND closed_at IS NULL"
        sql += " ORDER BY COALESCE(applied_at, first_seen) DESC, company, title"
        return [dict(r) for r in self.db.execute(sql)]

    def stats(self, since: str | None = None,
              company: str | None = None) -> dict[str, Any]:
        """Counts over the stored postings, overall and per company.

        `shown` is the default buckets (what a scan prints), `new` is open
        postings first seen at or after `since`. Companies are ordered by
        early-career postings, then shown, since that is what the list is
        read for; companies with nothing open are left out.
        """
        where, args = "", []
        if company:
            where, args = " WHERE company = ?", [company]
        shown = ",".join("?" * len(DEFAULT_KEEP))
        since = since or "9999"
        rows = self.db.execute(f"""
            SELECT company,
                   SUM(closed_at IS NULL) AS open,
                   SUM(closed_at IS NOT NULL) AS closed,
                   SUM(closed_at IS NULL AND bucket = 'explicit_early') AS explicit_early,
                   SUM(closed_at IS NULL AND bucket IN ({shown})) AS shown,
                   SUM(closed_at IS NULL AND first_seen >= ?) AS new,
                   SUM(applied_at IS NOT NULL) AS applied,
                   SUM(COALESCE(starred, 0) = 1) AS starred
              FROM jobs{where} GROUP BY company""",
            [*DEFAULT_KEEP, since, *args]).fetchall()
        companies = sorted((dict(r) for r in rows if r["open"]),
                           key=lambda c: (-c["explicit_early"], -c["shown"],
                                          c["company"]))
        buckets = dict(self.db.execute(
            f"SELECT bucket, COUNT(*) FROM jobs WHERE closed_at IS NULL"
            f"{' AND company = ?' if company else ''} GROUP BY bucket ORDER BY 2 DESC",
            args).fetchall())
        total = {k: sum(r[k] for r in rows)
                 for k in ("open", "closed", "new", "applied", "starred")}
        return {**total, "by_bucket": buckets, "companies": companies}

    def open_jobs(self, buckets: tuple[str, ...] | None = None, since: str | None = None, only_new_keys: list[str] | None = None,
                  companies: list[str] | None = None,
                  applied: bool | None = None,
                  starred: bool | None = None) -> list[dict[str, Any]]:
        """Open postings. `companies` scopes results to what this run scanned,
        so --company nvidia never leaks rows from other boards in the db.
        `applied` / `starred` keep only postings with (True) or without
        (False) that mark."""
        sql = "SELECT * FROM jobs WHERE closed_at IS NULL"
        args: list = []
        if applied is not None:
            sql += f" AND applied_at IS {'NOT ' if applied else ''}NULL"
        if starred is not None:
            sql += f" AND COALESCE(starred, 0)={int(starred)}"
        if companies is not None:
            if companies:
                sql += f" AND company IN ({','.join('?' * len(companies))})"
                args += list(companies)
            else:
                return []
        if buckets:
            sql += f" AND bucket IN ({','.join('?' * len(buckets))})"
            args += list(buckets)
        if since:
            sql += " AND first_seen >= ?"
            args.append(since)
        if only_new_keys is not None:
            if not only_new_keys:
                return []
            sql += f" AND key IN ({','.join('?' * len(only_new_keys))})"
            args += list(only_new_keys)
        sql += " ORDER BY COALESCE(posted_at, first_seen) DESC, company, title"
        return [dict(r) for r in self.db.execute(sql, args)]
