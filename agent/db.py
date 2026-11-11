"""A small local database (data/jobs.db) that remembers what the agent has seen.

Tables:
  postings  - full details of every posting that passed the filters and was
              scored (shown in the web app if its score is high enough)
  seen_ids  - only the ID of postings that did NOT pass the filters, or were
              older than the first-check window, so they're skipped next time.
              kind = 'filtered' (failed a rule) or 'old' (posted too long ago)
  companies_checked - companies the agent has already read at least once
  runs      - one row per run (date, counts, status message)
  pending_sheet_rows - spreadsheet rows waiting because Excel had the file open
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator, List, Optional

from .config import DB_FILE, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    uid TEXT PRIMARY KEY,
    company TEXT, title TEXT, location TEXT, url TEXT,
    description TEXT, pay_top REAL, why_kept TEXT,
    score INTEGER, reason TEXT,
    run_date TEXT, first_seen TEXT
);
CREATE INDEX IF NOT EXISTS idx_postings_date ON postings(run_date);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT, started_at TEXT, finished_at TEXT,
    trigger TEXT, status TEXT, fetched INTEGER, matched INTEGER, new INTEGER, message TEXT
);
CREATE TABLE IF NOT EXISTS pending_sheet_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT, company TEXT, role TEXT
);
CREATE TABLE IF NOT EXISTS seen_ids (
    uid TEXT PRIMARY KEY, kind TEXT, first_seen TEXT
);
CREATE TABLE IF NOT EXISTS companies_checked (
    company TEXT PRIMARY KEY, first_checked TEXT
);
CREATE TABLE IF NOT EXISTS run_failures (
    run_id INTEGER, company TEXT, error TEXT, retried_by INTEGER
);
"""

# Columns added after the first version; older databases get them automatically.
NEW_COLUMNS = [("postings", "posted", "TEXT"), ("pending_sheet_rows", "url", "TEXT"),
               ("postings", "source", "TEXT")]       # source: "manual" = added with "Score a posting"


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        for table, col, kind in NEW_COLUMNS:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
        yield conn
        conn.commit()
    finally:
        conn.close()


def seen_uids() -> set:
    """Every posting ID the agent has already dealt with (scored, filtered out, or too old)."""
    with connect() as c:
        return ({r["uid"] for r in c.execute("SELECT uid FROM postings")} |
                {r["uid"] for r in c.execute("SELECT uid FROM seen_ids")})


def save_posting(p, why_kept: str, score: int, reason: str, run_date: str, now: str) -> None:
    with connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO postings (uid, company, title, location, url, description, pay_top, "
            "why_kept, score, reason, run_date, first_seen, posted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.uid, p.company, p.title, p.location_display, p.url, p.description,
             p.pay_top, why_kept, score, reason, run_date, now,
             p.posted if p.posted_reliable else None),      # None -> shown as "Unknown"
        )


def save_manual_posting(p, score: int, reason: str, run_date: str, now: str) -> None:
    """A posting added with "Score a posting" (filters not applied; labeled "Added manually")."""
    with connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO postings (uid, company, title, location, url, description, pay_top, "
            "why_kept, score, reason, run_date, first_seen, posted, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (p.uid, p.company, p.title, p.location_display, p.url, p.description, p.pay_top,
             "added manually", score, reason, run_date, now, p.posted if p.posted_reliable else None, "manual"))


def update_score(uid: str, score: int, reason: str) -> None:
    """Rescore: new score and reason; the posting stays on its original date."""
    with connect() as c:
        c.execute("UPDATE postings SET score=?, reason=? WHERE uid=?", (score, reason, uid))


def posting_by_url(normalize, url: str) -> Optional[sqlite3.Row]:
    """The saved posting whose link is the same as `url` (compared with normalize())."""
    want = normalize(url)
    with connect() as c:
        for r in c.execute("SELECT * FROM postings WHERE url IS NOT NULL AND url != ''"):
            if normalize(r["url"]) == want:
                return r
    return None


def remember_ids(uids: List[str], kind: str, run_date: str) -> None:
    """Store only the ID (no details) so the posting is skipped on later runs."""
    with connect() as c:
        c.executemany("INSERT OR IGNORE INTO seen_ids VALUES (?,?,?)", [(u, kind, run_date) for u in uids])


def forget_filtered_ids() -> int:
    """Used by --recheck after loosening a rule: filtered-out postings get looked at again."""
    with connect() as c:
        return c.execute("DELETE FROM seen_ids WHERE kind='filtered'").rowcount


def checked_companies() -> set:
    with connect() as c:
        return {r["company"] for r in c.execute("SELECT company FROM companies_checked")}


def mark_company_checked(company: str, run_date: str) -> None:
    with connect() as c:
        c.execute("INSERT OR IGNORE INTO companies_checked VALUES (?,?)", (company.strip().lower(), run_date))


def add_run_failures(run_id: int, failures: List[tuple]) -> None:
    """failures: [(company name, error text)] for companies whose fetch failed in this run."""
    with connect() as c:
        c.executemany("INSERT INTO run_failures (run_id, company, error) VALUES (?,?,?)",
                      [(run_id, name, err[:500]) for name, err in failures])


def failures_for(run_ids: List[int]) -> dict:
    """{run_id: [rows]} of failed companies, for showing on the run's day page."""
    if not run_ids:
        return {}
    with connect() as c:
        rows = c.execute(f"SELECT * FROM run_failures WHERE run_id IN ({','.join('?' * len(run_ids))}) "
                         "ORDER BY company", run_ids).fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r["run_id"], []).append(r)
    return out


def mark_failures_retried(run_id: int, companies: List[str], retry_run_id: int) -> None:
    with connect() as c:
        c.executemany("UPDATE run_failures SET retried_by=? WHERE run_id=? AND company=?",
                      [(retry_run_id, run_id, n) for n in companies])


def start_run(run_date: str, now: str, trigger: str) -> int:
    with connect() as c:
        cur = c.execute("INSERT INTO runs (run_date, started_at, trigger, status) VALUES (?,?,?,?)",
                        (run_date, now, trigger, "running"))
        return cur.lastrowid


def finish_run(run_id: int, now: str, status: str, fetched: int, matched: int, new: int, message: str) -> None:
    with connect() as c:
        c.execute("UPDATE runs SET finished_at=?, status=?, fetched=?, matched=?, new=?, message=? WHERE id=?",
                  (now, status, fetched, matched, new, message, run_id))


def run_dates() -> List[sqlite3.Row]:
    with connect() as c:
        return list(c.execute(
            # days with a run, plus days with a posting added manually
            "SELECT run_date FROM runs UNION SELECT run_date FROM postings WHERE source='manual' "
            "ORDER BY run_date DESC"))


def runs_on(run_date: str) -> List[sqlite3.Row]:
    with connect() as c:
        return list(c.execute("SELECT * FROM runs WHERE run_date=? ORDER BY id", (run_date,)))


def latest_run() -> Optional[sqlite3.Row]:
    with connect() as c:
        return c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()


def hidden_count(run_date: str, min_score: int) -> int:
    with connect() as c:
        return c.execute("SELECT COUNT(*) FROM postings WHERE run_date=? AND score<? "
                         "AND COALESCE(source, '') != 'manual'",
                         (run_date, min_score)).fetchone()[0]


def postings_for_links() -> List[sqlite3.Row]:
    with connect() as c:
        return list(c.execute("SELECT company, title, url FROM postings"))


def postings_on(run_date: str, min_score: int) -> List[sqlite3.Row]:
    with connect() as c:
        return list(c.execute(
            # best fit first; ties: most recently posted first, unknown dates last
            # manual postings always show, whatever their score
            "SELECT * FROM postings WHERE run_date=? AND (score>=? OR source='manual') "
            "ORDER BY score DESC, posted IS NULL, posted DESC, company, title",
            (run_date, min_score)))


def get_postings(uids: List[str]) -> List[sqlite3.Row]:
    with connect() as c:
        q = ",".join("?" * len(uids))
        return list(c.execute(f"SELECT * FROM postings WHERE uid IN ({q})", uids)) if uids else []


def add_pending_rows(rows: List[tuple]) -> None:
    with connect() as c:
        c.executemany("INSERT INTO pending_sheet_rows (date, company, role, url) VALUES (?,?,?,?)", rows)


def pending_rows() -> List[sqlite3.Row]:
    with connect() as c:
        return list(c.execute("SELECT * FROM pending_sheet_rows ORDER BY id"))


def clear_pending_rows(ids: List[int]) -> None:
    with connect() as c:
        c.executemany("DELETE FROM pending_sheet_rows WHERE id=?", [(i,) for i in ids])
