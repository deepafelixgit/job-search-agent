"""Applications.xlsx - one master list of everything you've generated.

Rows are only ever added, never changed or removed. Before every save a
backup copy goes to data/backups/, so the file can't be lost.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
from typing import Dict, List, Tuple

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from .config import DATA_DIR, SPREADSHEET

HEADERS = ["Serial Number", "Date", "Company", "Role", "Posting Link"]
LINK = "Posting Link"


class SpreadsheetLocked(Exception):
    pass


def is_open_in_excel() -> bool:
    # Excel creates a hidden "~$Applications.xlsx" file while the workbook is open.
    return (SPREADSHEET.parent / ("~$" + SPREADSHEET.name)).exists()


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


def _add_link_column(ws, links: Dict[Tuple[str, str], set]) -> List[str]:
    """Add 'Posting Link' right after 'Role' (existing rows are kept) and fill
    in links found in the database. Returns notes about rows left blank."""
    heads = _headers(ws)
    col = heads.get("Role", 4) + 1
    if any(ws.cell(row=r, column=col).value not in (None, "") for r in range(1, ws.max_row + 1)):
        ws.insert_cols(col)          # something is already there: shift it right, don't overwrite
    ws.cell(row=1, column=col, value=LINK).font = Font(bold=True)
    ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = 60
    notes = []
    c_col, r_col = heads.get("Company", 3), heads.get("Role", 4)
    for r in range(2, ws.max_row + 1):
        company, role = ws.cell(row=r, column=c_col).value, ws.cell(row=r, column=r_col).value
        if not company and not role:
            continue
        found = links.get((str(company or "").strip().lower(), str(role or "").strip().lower()), set())
        if len(found) == 1:
            _set_link(ws.cell(row=r, column=col), next(iter(found)))
        else:
            serial = ws.cell(row=r, column=1).value
            notes.append(f"Row {serial} ({company} - {role}): "
                         + ("no matching posting in the database" if not found
                            else f"{len(found)} postings match, so it was left blank"))
    return notes


def link_lookup(postings) -> Dict[Tuple[str, str], set]:
    out: Dict[Tuple[str, str], set] = {}
    for p in postings:
        if p["url"]:
            out.setdefault((p["company"].strip().lower(), p["title"].strip().lower()), set()).add(p["url"])
    return out


def upgrade_sheet(links: Dict[Tuple[str, str], set]) -> List[str]:
    """One-time upgrade: add the Posting Link column to an existing file.
    Does nothing if the column is already there."""
    if not SPREADSHEET.exists():
        return []
    wb = load_workbook(SPREADSHEET)
    ws = wb.worksheets[0]
    if LINK in _headers(ws):
        return []
    if is_open_in_excel():
        raise SpreadsheetLocked()
    try:
        _backup()
        notes = _add_link_column(ws, links)
        _save(wb)
        return notes
    except PermissionError:
        raise SpreadsheetLocked() from None


def append_rows(rows: List[Tuple[str, str, str, str]]) -> List[int]:
    """rows = [(date, company, role, link), ...]. Returns the serial numbers used."""
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
            if LINK not in _headers(ws):
                _add_link_column(ws, {})
        else:
            wb = Workbook()
            ws = wb.active
            ws.title = "Applications"
            ws.append(HEADERS)
            for cell in ws[1]:
                cell.font = Font(bold=True)
            ws.column_dimensions["A"].width = 14
            ws.column_dimensions["B"].width = 12
            ws.column_dimensions["C"].width = 18
            ws.column_dimensions["D"].width = 60
            ws.column_dimensions["E"].width = 60
            last = 0
        heads = _headers(ws)
        used = []
        for date, company, role, link in rows:
            last += 1
            r = ws.max_row + 1
            for name, value in (("Serial Number", last), ("Date", date), ("Company", company), ("Role", role)):
                ws.cell(row=r, column=heads.get(name, HEADERS.index(name) + 1), value=value)
            _set_link(ws.cell(row=r, column=heads[LINK]), link or "")
            used.append(last)
        _save(wb)
        return used
    except PermissionError:
        raise SpreadsheetLocked() from None
