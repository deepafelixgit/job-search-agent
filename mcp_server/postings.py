"""job-postings: a local MCP server for asking questions about the job search agent's results.

It reads the agent's database (data/jobs.db) READ-ONLY, so it can never change
the agent's data. Every posting the agent has scored is there, including the
lower-scored ones the web app hides, and postings added with "Score a posting".

export_to_spreadsheet writes a query's results to a NEW tab in Applications.xlsx
(same columns as the main tab, with clickable links) and opens the file. It never
changes the main tab or any existing tab, and backs the file up before saving.

Run by Claude Code via .mcp.json. Like the career-evidence server it talks to
Claude Code through stdin/stdout, so it must never print() to stdout.

Try it from Terminal, without Claude:
  .venv-mcp/bin/python -m mcp_server.postings query --from 2026-11-01 --to 2026-11-30 --min 8
  .venv-mcp/bin/python -m mcp_server.postings companies
  .venv-mcp/bin/python -m mcp_server.postings export --tab "Nov 8+" --from 2026-11-01 --to 2026-11-30 --min 8
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import List, Optional, Tuple

from mcp.server.fastmcp import FastMCP

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="job-postings: %(message)s")
log = logging.getLogger("job-postings")

ROOT = Path(__file__).resolve().parent.parent
DB_FILE = ROOT / "data" / "jobs.db"
SPREADSHEET = ROOT / "Applications.xlsx"
BACKUPS = ROOT / "data" / "backups"
MAX_ROWS = 500
MAX_EXPORT = 5000

# Same columns and widths as the main tab of Applications.xlsx (agent/sheet.py).
HEADERS = ["Serial Number", "Date", "Company", "Role", "Score", "Days Since Posted", "Posting Link"]
WIDTHS = [14, 12, 18, 60, 8, 18, 60]

mcp = FastMCP("job-postings")


# ------------------------------------------------------------------ reading the database

def _connect() -> sqlite3.Connection:
    if not DB_FILE.exists():
        raise FileNotFoundError(f"No database yet at {DB_FILE}. Run the agent at least once.")
    conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True, timeout=30)   # read-only
    conn.row_factory = sqlite3.Row
    return conn


def _check_date(value: Optional[str], name: str) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"{name} must be a date like 2026-11-01, not {value!r}") from None


def _days(posted: Optional[str], found: Optional[str]) -> Optional[int]:
    """Same rule as the web app: days from the published date to the day it was scored."""
    try:
        return max(0, (dt.date.fromisoformat(found or "") - dt.date.fromisoformat(posted or "")).days)
    except ValueError:
        return None


def _select(date_from=None, date_to=None, date_field="found", companies=None, min_score=None,
            max_score=None, scores=None, title_contains=None, location_contains=None,
            limit=MAX_ROWS) -> Tuple[int, list]:
    """Shared by query_postings and export_to_spreadsheet: (total matching, rows)."""
    date_from, date_to = _check_date(date_from, "date_from"), _check_date(date_to, "date_to")
    if date_field not in ("found", "posted"):
        raise ValueError('date_field must be "found" or "posted"')
    col = "run_date" if date_field == "found" else "posted"
    where, args = [], []
    if date_from:
        where.append(f"{col} >= ?"); args.append(date_from)
    if date_to:
        where.append(f"{col} <= ?"); args.append(date_to)
    if date_field == "posted":
        where.append("posted IS NOT NULL")
    if min_score is not None:
        where.append("score >= ?"); args.append(int(min_score))
    if max_score is not None:
        where.append("score <= ?"); args.append(int(max_score))
    if scores:
        where.append(f"score IN ({','.join('?' * len(scores))})"); args += [int(s) for s in scores]
    if companies:
        parts = []
        for c in companies:
            parts.append("(LOWER(company) = LOWER(?) OR LOWER(company) LIKE LOWER(?))")
            args += [c.strip(), c.strip() + " %"]
        where.append("(" + " OR ".join(parts) + ")")
    if title_contains:
        where.append("LOWER(title) LIKE LOWER(?)"); args.append(f"%{title_contains}%")
    if location_contains:
        where.append("LOWER(location) LIKE LOWER(?)"); args.append(f"%{location_contains}%")
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    with closing(_connect()) as conn:
        have = {r["name"] for r in conn.execute("PRAGMA table_info(postings)")}
        source = "source" if "source" in have else "NULL AS source"
        posted = "posted" if "posted" in have else "NULL AS posted"
        total = conn.execute(f"SELECT COUNT(*) FROM postings{sql_where}", args).fetchone()[0]
        rows = conn.execute(
            f"SELECT uid, run_date, company, title, location, score, pay_top, url, reason, "
            f"{posted}, {source} FROM postings{sql_where} "
            f"ORDER BY score DESC, run_date DESC, company, title LIMIT ?", args + [int(limit)]).fetchall()
    return total, rows


FILTER_HELP = """
    Scores are whole numbers from 1 to 10. All filters are optional and combine with AND.

    date_from, date_to: inclusive dates as YYYY-MM-DD. For "November 2026" use
        2026-11-01 to 2026-11-30.
    date_field: which date the range applies to:
        "found"  (default) = the day the agent found and scored the posting; this is the
                   date of the web app's day pages.
        "posted" = the job's published date from the company's feed (unknown for some
                   postings; those are left out when this field is used).
    companies: company names, matched case-insensitively as whole names or as a prefix
        ("Google" also matches "Google DeepMind"). Use list_companies to see exact names.
    min_score, max_score: inclusive bounds. "Scored above 8" literally means min_score=9;
        when the user's wording is ambiguous, say which reading you used.
    scores: exact scores to include, e.g. [7, 8] for "scored 7 and 8".
    title_contains, location_contains: case-insensitive text to look for."""


# ------------------------------------------------------------------ tools

def query_postings(date_from: Optional[str] = None, date_to: Optional[str] = None,
                   date_field: str = "found", companies: Optional[List[str]] = None,
                   min_score: Optional[int] = None, max_score: Optional[int] = None,
                   scores: Optional[List[int]] = None, title_contains: Optional[str] = None,
                   location_contains: Optional[str] = None, include_reason: bool = False,
                   limit: int = 200) -> dict:
    """Look up postings the job search agent has scored, and return them here in the chat.
    Use this for counts, quick checks, or questions about specific postings. When the user
    asks to SEE or LIST postings, use export_to_spreadsheet instead.
    """
    total, rows = _select(date_from, date_to, date_field, companies, min_score, max_score, scores,
                          title_contains, location_contains, max(1, min(int(limit), MAX_ROWS)))
    out = []
    for r in rows:
        item = {"date_found": r["run_date"], "company": r["company"], "role": r["title"],
                "score": r["score"], "days_since_posted": _days(r["posted"], r["run_date"]),
                "posted": r["posted"], "location": r["location"],
                "top_of_pay_range": r["pay_top"], "link": r["url"], "id": r["uid"]}
        if r["source"] == "manual":
            item["added_manually"] = True
        if include_reason:
            item["reason"] = r["reason"]
        out.append(item)
    return {"total_matching": total, "returned": len(out), "postings": out,
            "note": "Includes postings below the web app's display cutoff. "
                    "days_since_posted is null when the published date is unknown."}


query_postings.__doc__ += FILTER_HELP + """
    include_reason: also return the agent's explanation of each score (longer output).
    limit: maximum postings to return (up to 500); "total_matching" always gives the full count.
    """
mcp.tool()(query_postings)      # registered after the docstring is complete


def _tab_name(wanted: str, existing: List[str]) -> str:
    """Excel tab names: at most 31 characters, none of [ ] : * ? / \\ , and unique."""
    base = re.sub(r"[\[\]:*?/\\]", "-", (wanted or "").strip()).strip("'") or "Query"
    base = base[:31].rstrip()
    if base.lower() == "history":            # reserved by Excel
        base = "History query"
    taken = {n.lower() for n in existing}
    name, n = base, 2
    while name.lower() in taken:
        suffix = f" ({n})"
        name = base[:31 - len(suffix)] + suffix
        n += 1
    return name


def _open_file(path: Path) -> bool:
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            os.startfile(str(path))          # noqa: S606 (Windows only)
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as e:  # opening is a convenience; the tab is already saved
        log.info(f"could not open the file: {e}")
        return False


def export_to_spreadsheet(tab_name: str, date_from: Optional[str] = None, date_to: Optional[str] = None,
                          date_field: str = "found", companies: Optional[List[str]] = None,
                          min_score: Optional[int] = None, max_score: Optional[int] = None,
                          scores: Optional[List[int]] = None, title_contains: Optional[str] = None,
                          location_contains: Optional[str] = None, open_file: bool = True) -> dict:
    """Write the postings matching a query to a NEW tab in Applications.xlsx, in the same
    columns as the main tab (Serial Number, Date, Company, Role, Score, Days Since Posted,
    Posting Link) with clickable links, then open Applications.xlsx on that tab.
    Use this whenever the user asks to see or list postings.

    tab_name: a short name that describes the query, e.g. "Nov 8+", "Nov scored 7-8",
        "Google Nov 8+", "Google Nov 1-15". Excel allows 31 characters; if the name is
        taken, " (2)", " (3)" ... is added. In the new tab, "Date" is the date found
        (or the published date when date_field="posted") and Serial Number counts from 1.
    open_file: open Applications.xlsx when done (default true).

    Never changes the main tab or existing tabs; backs the file up to data/backups/ first.
    If Applications.xlsx is open in Excel, nothing is written: ask the user to close it
    and try again. If nothing matches, no tab is created.
    """
    total, rows = _select(date_from, date_to, date_field, companies, min_score, max_score, scores,
                          title_contains, location_contains, MAX_EXPORT)
    if total == 0:
        return {"ok": True, "rows_written": 0, "message": "No postings matched, so no tab was created."}
    if (SPREADSHEET.parent / ("~$" + SPREADSHEET.name)).exists():
        return {"ok": False, "message": "Applications.xlsx is open in Excel, so it couldn't be updated. "
                                        "Please close it and ask again."}

    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    if SPREADSHEET.exists():
        wb = load_workbook(SPREADSHEET)
        BACKUPS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SPREADSHEET, BACKUPS / f"Applications_{dt.datetime.now():%Y%m%d-%H%M%S-%f}.xlsx")
    else:                                   # keep the agent's main tab first, as it expects
        wb = Workbook()
        main = wb.active
        main.title = "Applications"
        main.append(HEADERS)
        for cell in main[1]:
            cell.font = Font(bold=True)
        for i, w in enumerate(WIDTHS, 1):
            main.column_dimensions[get_column_letter(i)].width = w

    name = _tab_name(tab_name, wb.sheetnames)
    ws = wb.create_sheet(name)
    ws.append(HEADERS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for i, w in enumerate(WIDTHS, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    date_col = "run_date" if date_field == "found" else "posted"
    for n, r in enumerate(rows, 1):
        ws.append([n, r[date_col], r["company"], r["title"], r["score"], _days(r["posted"], r["run_date"]), None])
        if r["url"]:
            link = ws.cell(row=ws.max_row, column=len(HEADERS), value=r["url"])
            link.hyperlink = r["url"]
            link.style = "Hyperlink"
    for sheet in wb.worksheets:              # open the file on the new tab
        sheet.sheet_view.tabSelected = False
    ws.sheet_view.tabSelected = True
    wb.active = wb.sheetnames.index(name)

    try:
        tmp = SPREADSHEET.with_name(".Applications.saving.xlsx")
        wb.save(tmp)
        os.replace(tmp, SPREADSHEET)
    except PermissionError:
        return {"ok": False, "message": "Applications.xlsx couldn't be saved (it may be open in Excel). "
                                        "Please close it and ask again."}
    opened = _open_file(SPREADSHEET) if open_file else False
    return {"ok": True, "tab": name, "rows_written": len(rows), "total_matching": total,
            "file": str(SPREADSHEET), "opened": opened,
            "date_column": "date found" if date_field == "found" else "date posted"}


export_to_spreadsheet.__doc__ += FILTER_HELP
mcp.tool()(export_to_spreadsheet)


@mcp.tool()
def list_companies(min_score: Optional[int] = None) -> dict:
    """List every company in the agent's results, with how many postings each has and its
    best score. Use it to find a company's exact name before filtering by company.
    min_score: count only postings scored at least this."""
    where, args = "", []
    if min_score is not None:
        where, args = " WHERE score >= ?", [int(min_score)]
    with closing(_connect()) as conn:
        rows = conn.execute(f"SELECT company, COUNT(*) AS n, MAX(score) AS best, MIN(run_date) AS first, "
                            f"MAX(run_date) AS last FROM postings{where} GROUP BY company "
                            f"ORDER BY n DESC, company", args).fetchall()
    return {"companies": [{"company": r["company"], "postings": r["n"], "best_score": r["best"],
                           "first_found": r["first"], "last_found": r["last"]} for r in rows]}


@mcp.tool()
def get_posting(id: str) -> dict:
    """Full details of one posting (by the "id" from query_postings): the agent's explanation
    of its score, why it passed the filters, pay, dates and the job description."""
    with closing(_connect()) as conn:
        r = conn.execute("SELECT * FROM postings WHERE uid = ?", (id,)).fetchone()
    if not r:
        return {"error": f"No posting with id {id!r}."}
    d = dict(r)
    d["days_since_posted"] = _days(d.get("posted"), d.get("run_date"))
    return d


# ------------------------------------------------------------------ Terminal use

def _cli() -> None:
    p = argparse.ArgumentParser(description="Query the job search agent's results without Claude.")
    sub = p.add_subparsers(dest="cmd", required=True)
    for cmd in ("query", "export"):
        q = sub.add_parser(cmd)
        q.add_argument("--from", dest="date_from"); q.add_argument("--to", dest="date_to")
        q.add_argument("--by", dest="date_field", default="found", choices=["found", "posted"])
        q.add_argument("--company", action="append"); q.add_argument("--min", type=int)
        q.add_argument("--max", type=int); q.add_argument("--score", type=int, action="append")
        q.add_argument("--title")
        if cmd == "export":
            q.add_argument("--tab", required=True)
            q.add_argument("--no-open", action="store_true")
    s = sub.add_parser("companies"); s.add_argument("--min", type=int)
    a = p.parse_args()
    filters = dict(date_from=a.date_from, date_to=a.date_to, date_field=a.date_field, companies=a.company,
                   min_score=a.min, max_score=a.max, scores=a.score, title_contains=a.title) if a.cmd != "companies" else {}
    if a.cmd == "query":
        res = query_postings(**filters)
    elif a.cmd == "export":
        res = export_to_spreadsheet(a.tab, open_file=not a.no_open, **filters)
    else:
        res = list_companies(a.min)
    print(json.dumps(res, indent=2, ensure_ascii=False))


def main() -> None:
    mcp.run()          # stdio: Claude Code talks to us through stdin/stdout


if __name__ == "__main__":
    if len(sys.argv) > 1:
        _cli()
    else:
        main()
