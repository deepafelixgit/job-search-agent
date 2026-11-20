"""Applications.xlsx - one master list of everything you've generated.

Rows are only ever added, never changed or removed. Before every save a
backup copy goes to data/backups/, so the file can't be lost.

Columns: Serial Number, Date, Company, Role, Score, Days Since Posted, Posting Link.
"Score" is the posting's fit score when its documents were generated.
"Days Since Posted" matches the web app: days from the job's published date to
the day the posting was scored (blank when the published date is unknown).
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
from typing import Dict, List, Optional, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .config import DATA_DIR, SPREADSHEET

HEADERS = ["Serial Number", "Date", "Company", "Role", "Score", "Days Since Posted", "Posting Link"]
LINK = "Posting Link"
SCORE = "Score"
DAYS = "Days Since Posted"
WIDTHS = {"Serial Number": 14, "Date": 12, "Company": 18, "Role": 60, SCORE: 8, DAYS: 18, LINK: 60}

# A row to add: (date, company, role, score, days_since_posted, link)
Row = Tuple[str, str, str, Optional[int], Optional[int], str]


class SpreadsheetLocked(Exception):
    pass


def is_open_in_excel() -> bool:
    # Excel creates a hidden "~$Applications.xlsx" file while the workbook is open.
    return (SPREADSHEET.parent / ("~$" + SPREADSHEET.name)).exists()


def days_since_posted(posted: Optional[str], scored_on: Optional[str]) -> Optional[int]:
    """Same rule as the web app: 0 = posted the day it was scored; None = unknown."""
    try:
        return max(0, (dt.date.fromisoformat(scored_on or "") - dt.date.fromisoformat(posted or "")).days)
    except ValueError:
        return None


def info_lookup(postings) -> dict:
    """postings: rows with company, title, url, score, posted, run_date.
    Returns {"by_url": {url: info}, "by_role": {(company, title): [info, ...]}}."""
    by_url: Dict[str, dict] = {}
    by_role: Dict[Tuple[str, str], List[dict]] = {}
    for p in postings:
        info = {"url": p["url"] or "", "score": p["score"],
                "days": days_since_posted(p["posted"], p["run_date"])}
        if info["url"]:
            by_url[info["url"]] = info
        by_role.setdefault((str(p["company"] or "").strip().lower(), str(p["title"] or "").strip().lower()),
                           []).append(info)
    return {"by_url": by_url, "by_role": by_role}


def _backup() -> None:
    backups = DATA_DIR / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    shutil.copy2(SPREADSHEET, backups / f"Applications_{dt.datetime.now():%Y%m%d-%H%M%S-%f}.xlsx")


def _save(wb) -> None:
    tmp = SPREADSHEET.with_name(".Applications.saving.xlsx")
    wb.save(tmp)
    os.replace(tmp, SPREADSHEET)


def _headers(ws) -> Dict[str, int]:
    """Header name -> column number (1-based)."""
    return {str(c.value).strip(): c.column for c in ws[1] if c.value is not None}


def _set_link(cell, url: str) -> None:
    if url:
        cell.value = url
        cell.hyperlink = url
        cell.style = "Hyperlink"


def _insert_column(ws, col: int, name: str) -> None:
    """Insert an empty column at `col`, shifting everything from there one to the
    right. openpyxl doesn't move column widths or hyperlink targets when it
    inserts, so both are corrected here."""
    widths = {}
    for i in range(col, ws.max_column + 1):
        letter = get_column_letter(i)
        if letter in ws.column_dimensions and ws.column_dimensions[letter].width:
            widths[i] = ws.column_dimensions[letter].width
    ws.insert_cols(col)
    for i in range(col, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(i)].width = widths.get(i - 1, 13)
    for row in ws.iter_rows():
        for cell in row:
            if cell.hyperlink is not None:
                cell.hyperlink.ref = cell.coordinate
    ws.cell(row=1, column=col, value=name).font = Font(bold=True)
    ws.column_dimensions[get_column_letter(col)].width = WIDTHS.get(name, 13)


def _match(ws, r: int, heads: Dict[str, int], lookup: dict) -> Tuple[Optional[dict], str]:
    """Find the database posting for row r: by its link first, else by company + role."""
    if LINK in heads:
        url = ws.cell(row=r, column=heads[LINK]).value
        if url and str(url) in lookup["by_url"]:
            return lookup["by_url"][str(url)], ""
    company = str(ws.cell(row=r, column=heads.get("Company", 3)).value or "").strip().lower()
    role = str(ws.cell(row=r, column=heads.get("Role", 4)).value or "").strip().lower()
    found = lookup["by_role"].get((company, role), [])
    if len(found) == 1:
        return found[0], ""
    return None, ("no matching posting in the database" if not found
                  else f"{len(found)} postings match, so it was left blank")


def _add_link_column(ws, lookup: dict) -> List[str]:
    """Add 'Posting Link' right after the last of Role/Score/Days (existing rows are
    kept) and fill in links found in the database. Returns notes about rows left blank."""
    heads = _headers(ws)
    after = max(heads.get(h, 0) for h in ("Role", SCORE, DAYS)) or 4
    col = after + 1
    if any(ws.cell(row=r, column=col).value not in (None, "") for r in range(1, ws.max_row + 1)):
        _insert_column(ws, col, LINK)      # something is already there: shift it right, don't overwrite
    else:
        ws.cell(row=1, column=col, value=LINK).font = Font(bold=True)
        ws.column_dimensions[get_column_letter(col)].width = WIDTHS[LINK]
    heads = _headers(ws)
    notes = []
    for r in range(2, ws.max_row + 1):
        company, role = ws.cell(row=r, column=heads.get("Company", 3)).value, ws.cell(row=r, column=heads.get("Role", 4)).value
        if not company and not role:
            continue
        info, why = _match(ws, r, heads, lookup)
        if info and info["url"]:
            _set_link(ws.cell(row=r, column=col), info["url"])
        else:
            notes.append(f"Row {ws.cell(row=r, column=1).value} ({company} - {role}): no link, {why or 'no link stored'}")
    return notes


def _add_score_columns(ws, lookup: dict) -> List[str]:
    """Add 'Score' and 'Days Since Posted' right after 'Role' (if missing) and fill
    them in for existing rows from the database. Returns notes about rows left blank."""
    added = []
    for name, prev in ((SCORE, "Role"), (DAYS, SCORE)):
        heads = _headers(ws)
        if name in heads:
            continue
        _insert_column(ws, heads.get(prev, 4) + 1, name)
        added.append(name)
    if not added:
        return []
    heads = _headers(ws)
    notes = []
    for r in range(2, ws.max_row + 1):
        company, role = ws.cell(row=r, column=heads.get("Company", 3)).value, ws.cell(row=r, column=heads.get("Role", 4)).value
        if not company and not role:
            continue
        info, why = _match(ws, r, heads, lookup)
        if not info:
            notes.append(f"Row {ws.cell(row=r, column=1).value} ({company} - {role}): score and days left blank, {why}")
            continue
        if SCORE in added:
            ws.cell(row=r, column=heads[SCORE], value=info["score"])
        if DAYS in added:
            ws.cell(row=r, column=heads[DAYS], value=info["days"])
    return notes


def _upgrade(ws, lookup: dict) -> List[str]:
    """Bring an older file up to the current columns. Existing rows are kept."""
    notes = _add_score_columns(ws, lookup)
    if LINK not in _headers(ws):
        notes += _add_link_column(ws, lookup)
    return notes


def _needs_upgrade(ws) -> bool:
    heads = _headers(ws)
    return any(h not in heads for h in (SCORE, DAYS, LINK))


def upgrade_sheet(lookup: dict) -> List[str]:
    """Add any missing columns (Score, Days Since Posted, Posting Link) to an existing
    file and fill them in for existing rows. Does nothing if they are already there."""
    if not SPREADSHEET.exists():
        return []
    wb = load_workbook(SPREADSHEET)
    ws = wb.worksheets[0]
    if not _needs_upgrade(ws):
        return []
    if is_open_in_excel():
        raise SpreadsheetLocked()
    try:
        _backup()
        notes = _upgrade(ws, lookup)
        _save(wb)
        return notes
    except PermissionError:
        raise SpreadsheetLocked() from None


def append_rows(rows: List[Row], lookup: Optional[dict] = None) -> List[int]:
    """rows = [(date, company, role, score, days, link), ...]. Returns the serial numbers used.
    `lookup` (from info_lookup) fills the new columns for older rows if the file needs upgrading."""
    if not rows:
        return []
    if is_open_in_excel():
        raise SpreadsheetLocked()
    try:
        if SPREADSHEET.exists():
            wb = load_workbook(SPREADSHEET)
            ws = wb.worksheets[0]
            serials = [c.value for c in ws["A"][1:] if isinstance(c.value, (int, float))]
            last = int(max(serials)) if serials else 0
            _backup()
            if _needs_upgrade(ws):
                _upgrade(ws, lookup or {"by_url": {}, "by_role": {}})
        else:
            wb = Workbook()
            ws = wb.active
            ws.title = "Applications"
            ws.append(HEADERS)
            for cell in ws[1]:
                cell.font = Font(bold=True)
            for i, name in enumerate(HEADERS, 1):
                ws.column_dimensions[get_column_letter(i)].width = WIDTHS[name]
            last = 0
        heads = _headers(ws)
        used = []
        for date, company, role, score, days, link in rows:
            last += 1
            r = ws.max_row + 1
            for name, value in (("Serial Number", last), ("Date", date), ("Company", company), ("Role", role),
                                (SCORE, score), (DAYS, days)):
                ws.cell(row=r, column=heads.get(name, HEADERS.index(name) + 1), value=value)
            _set_link(ws.cell(row=r, column=heads[LINK]), link or "")
            used.append(last)
        _save(wb)
        return used
    except PermissionError:
        raise SpreadsheetLocked() from None
