"""Create the output folder, tailored resume, cover letter, changes.txt and
sources.txt for each checked posting, then log them in Applications.xlsx."""
from __future__ import annotations

import datetime as dt
import re
import traceback
from pathlib import Path
from typing import Callable, List

from . import db
from .config import OUTPUT_DIR, LOG_DIR, load_settings, resume_path
from .sheet import SpreadsheetLocked, append_rows, link_lookup, upgrade_sheet
from .evidence import load_evidence
import docx

from .evidence_gate import coverage_report, find_evidence
from .resume import resume_text
from .letter import enabled_projects, hides_claude, write_letter
from .tailor import tailor_resume

ABBREVIATIONS = [
    (r"\bTechnical Program Manager\b", "TPM"),
    (r"\bEngineering Program Manager\b", "EPM"),
    (r"\bSenior\b", "Senior"),
]


def evidence_inputs(ev) -> List[str]:
    """changes.txt: the evidence passages given to Claude, and what the filter left out."""
    out = [f"EVIDENCE PASSAGES GIVEN TO CLAUDE ({len(ev.passages)}; verified_metrics.txt lines ranked first)"]
    out += [f"- [{p['id']}] {p['source']} | {p['doc_type']} | {p['period']} | {p['location']}" for p in ev.passages]
    out += ["", f"EVIDENCE LEFT OUT BY THE FILTER ({len(ev.excluded)}: growth areas, feedback, goals, single-task "
                "wins, unexplained acronyms)"]
    out += [f"- {p}\n    Why: {why}" for p, why in ev.excluded] or ["- Nothing was left out."]
    return out


def short_role(title: str) -> str:
    role = title
    for pattern, short in ABBREVIATIONS:
        role = re.sub(pattern, short, role, flags=re.I)
    role = re.sub(r"[^\w&+ ]+", " ", role)       # drop punctuation like , - ( ) / .
    role = role.replace("&", "and")
    role = re.sub(r"\s+", " ", role).strip()
    return role[:70].strip()


def _safe(s: str) -> str:
    return re.sub(r"[^\w\- ]+", "", s).strip()


def generate(uids: List[str], progress: Callable[[str, str], None], evidence_override: dict = None) -> dict:
    """progress(uid, message) is called as each step finishes."""
    settings = load_settings()
    model = settings["models"]["writing"]
    name = settings["candidate"]["name"]
    name_us = name.replace(" ", "_")
    src = resume_path(settings)
    today = dt.date.today()
    day_dir = OUTPUT_DIR / today.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    evidence, evidence_notes = load_evidence(settings)

    results, sheet_rows = [], []
    for post in db.get_postings(uids):
        uid = post["uid"]
        company = _safe(post["company"])
        role = _safe(short_role(post["title"]))
        folder = day_dir / f"{company}_{role.replace(' ', '_')}"
        n = 2
        while folder.exists():
            folder = day_dir / f"{company}_{role.replace(' ', '_')}_{n}"
            n += 1
        try:
            folder.mkdir(parents=True)
            progress(uid, "Tailoring resume (rewrite + fact-check)…")
            resume_file = folder / f"{name_us}_Resume_{company}_{role}.docx"
            progress(uid, "Searching your evidence for what the posting asks for…")
            posting_evidence = (evidence_override or {}).get(uid) or \
                find_evidence(model, post, filter_model=settings["models"]["scoring"])
            changes = tailor_resume(model, src, post, resume_file,
                                    pinned_bullets=settings.get("pinned_bullets") or [],
                                fixed_bullets=settings.get("fixed_bullets") or [],
                                    evidence=posting_evidence)

            progress(uid, "Searching recent news and writing cover letter…")
            letter_file = folder / f"{name_us}_Cover_Letter_{company}_{role}.docx"
            news, letter_notes = write_letter(model, src, post, letter_file, name, today,
                                              posting_evidence, enabled_projects(settings, post["company"]),
                                              precheck_model=settings["models"]["scoring"],
                                              hide_claude=hides_claude(settings, post["company"]))

            header = [f"Changes made for: {post['company']} - {post['title']}",
                      f"Posting: {post['url']}", f"Created: {today.isoformat()}", "",
                      "Only the Summary, Skills and Meta sections were changed. Everything else is identical.", ""]
            progress(uid, "Mapping the posting's responsibilities to your evidence…")
            letter_doc = "\n\n".join(p.text for p in docx.Document(str(letter_file)).paragraphs if p.text.strip())
            cov = coverage_report(settings["models"]["scoring"], posting_evidence, resume_text(resume_file),
                                  letter_doc)
            (folder / "changes.txt").write_text(
                "\n".join(header + cov["top"] + [""] + evidence_inputs(posting_evidence) + [""] + changes + [""] + cov["unused_resume"] + evidence_notes
                          + letter_notes + [""] + cov["unused_letter"]) + "\n", encoding="utf-8")

            src_lines = [f"News sources used in the cover letter for {post['company']} - {post['title']}", ""]
            if news:
                for i, item in enumerate(news, 1):
                    src_lines += [f"{i}. {item.get('title', '')}",
                                  f"   Publisher: {item.get('publisher', '')}",
                                  f"   Date: {item.get('date', '') or 'not stated'}",
                                  f"   Link: {item.get('url', '')}",
                                  f"   Summary: {item.get('summary', '')}", ""]
            else:
                src_lines.append("No recent news item could be verified, so the letter doesn't mention news.")
            (folder / "sources.txt").write_text("\n".join(src_lines) + "\n", encoding="utf-8")

            sheet_rows.append((today.isoformat(), post["company"], post["title"], post["url"]))
            results.append({"uid": uid, "ok": True, "folder": str(folder), "company": post["company"],
                            "title": post["title"]})
            progress(uid, "Done")
        except Exception as e:  # keep going with the other postings
            with open(LOG_DIR / "generate_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {uid}\n{traceback.format_exc()}")
            results.append({"uid": uid, "ok": False, "error": str(e), "folder": str(folder),
                            "company": post["company"], "title": post["title"]})
            progress(uid, f"Failed: {e}")

    sheet = record_in_spreadsheet(sheet_rows)
    return {"results": results, "sheet": sheet}


def record_in_spreadsheet(rows) -> dict:
    """Add rows (plus any waiting from an earlier attempt). If Excel has the
    file open, keep them waiting and tell the user."""
    waiting = db.pending_rows()
    all_rows = [(r["date"], r["company"], r["role"], r["url"] or "") for r in waiting] + list(rows)
    if not all_rows:
        return {"ok": True, "added": 0}
    try:
        serials = append_rows(all_rows)
        db.clear_pending_rows([r["id"] for r in waiting])
        return {"ok": True, "added": len(serials)}
    except SpreadsheetLocked:
        db.add_pending_rows(list(rows))
        return {"ok": False, "waiting": len(all_rows),
                "message": "Applications.xlsx is open in Excel, so it couldn't be updated. "
                           "Please close it and click 'Try again'."}


def add_link_column() -> dict:
    """One-time upgrade of Applications.xlsx: add 'Posting Link' and fill it in
    for existing rows from the database."""
    try:
        notes = upgrade_sheet(link_lookup(db.postings_for_links()))
        return {"ok": True, "notes": notes}
    except SpreadsheetLocked:
        return {"ok": False, "message": "Applications.xlsx is open in Excel. Please close it and try again."}


def generate_resume_only(uids: List[str], progress: Callable[[str, str], None], evidence_override: dict = None,
                         resume_source: Path = None) -> dict:
    """Tailor only the resume (no cover letter, no news search, no tracker row): cheaper, for
    when only the resume needs regenerating. Writes the resume and changes.txt."""
    settings = load_settings()
    model = settings["models"]["writing"]
    name_us = settings["candidate"]["name"].replace(" ", "_")
    src = resume_source or resume_path(settings)
    day_dir = OUTPUT_DIR / dt.date.today().isoformat()
    results = []
    for post in db.get_postings(uids):
        uid = post["uid"]
        company, role = _safe(post["company"]), _safe(short_role(post["title"]))
        folder = day_dir / f"{company}_{role.replace(' ', '_')}_resume"
        n = 2
        while folder.exists():
            folder = day_dir / f"{company}_{role.replace(' ', '_')}_resume_{n}"
            n += 1
        folder.mkdir(parents=True)
        progress(uid, "Searching your evidence for what the posting asks for…")
        ev = (evidence_override or {}).get(uid) or find_evidence(model, post, filter_model=settings["models"]["scoring"])
        progress(uid, "Tailoring resume (rewrite + fact-check)…")
        resume_file = folder / f"{name_us}_Resume_{company}_{role}.docx"
        changes = tailor_resume(model, src, post, resume_file, pinned_bullets=settings.get("pinned_bullets") or [],
                                fixed_bullets=settings.get("fixed_bullets") or [],
                                evidence=ev)
        header = [f"Resume changes for: {post['company']} - {post['title']}", f"Posting: {post['url']}",
                  f"Created: {dt.date.today().isoformat()}", f"Base resume: {src}", ""]
        (folder / "changes.txt").write_text("\n".join(header + evidence_inputs(ev) + [""] + changes) + "\n",
                                            encoding="utf-8")
        results.append({"uid": uid, "folder": str(folder), "resume": str(resume_file)})
        progress(uid, "Done")
    return {"results": results}
