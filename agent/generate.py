"""Create the output folder, tailored resume, cover letter, recruiter message, changes.txt and
sources.txt for each checked posting, then log them in Applications.xlsx."""
from __future__ import annotations

import datetime as dt
import re
import traceback
from pathlib import Path
from typing import Callable, List

from . import db
from .config import OUTPUT_DIR, LOG_DIR, load_settings, resume_path, tailoring_guidance
from .sheet import SpreadsheetLocked, append_rows, days_since_posted, info_lookup, upgrade_sheet
from .evidence import load_evidence
from .claude_client import set_step, start_meter, stop_meter
import docx

from .evidence_gate import coverage_report, find_evidence, flags_report, start_flags
from .resume import resume_text
from .letter import enabled_projects, write_letter
from .recruiter import write_message
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
    check_model = settings["models"].get("checking") or model     # resume fact-check and final claim checks
    name = settings["candidate"]["name"]
    name_us = name.replace(" ", "_")
    src = resume_path(settings)
    today = dt.date.today()
    day_dir = OUTPUT_DIR / today.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    evidence, evidence_notes = load_evidence(settings)
    budget = float(settings.get("budget_per_application") or 1.00)

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
        cost = start_meter(budget)      # every Claude call for this posting is priced and added up
        start_flags()                   # sentences the claim check flags for your review
        try:
            folder.mkdir(parents=True)
            progress(uid, "Tailoring resume (rewrite + fact-check)…")
            resume_file = folder / f"{name_us}_Resume_{company}_{role}.docx"
            progress(uid, "Searching your evidence for what the posting asks for…")
            set_step("Finding your evidence for the posting")
            posting_evidence = (evidence_override or {}).get(uid) or \
                find_evidence(model, post, filter_model=settings["models"]["scoring"])
            set_step("Resume (rewrite + fact-check)")
            changes = tailor_resume(model, src, post, resume_file,
                                    pinned_bullets=settings.get("pinned_bullets") or [],
                                fixed_bullets=settings.get("fixed_bullets") or [],
                                    evidence=posting_evidence, guidance=tailoring_guidance(settings),
                                    new_bullets=int(settings.get("new_meta_bullets") or 0),
                                    check_model=check_model)

            progress(uid, "Searching recent news and writing cover letter…")
            letter_file = folder / f"{name_us}_Cover_Letter_{company}_{role}.docx"
            set_step("Cover letter")
            news, letter_notes = write_letter(model, src, post, letter_file, name, today,
                                              posting_evidence, enabled_projects(settings),
                                              precheck_model=settings["models"]["scoring"],
                                              check_model=check_model)

            letter_doc = "\n\n".join(p.text for p in docx.Document(str(letter_file)).paragraphs if p.text.strip())

            # recruiter message: a failure here never fails the posting (resume and letter are kept)
            progress(uid, "Writing the LinkedIn message to the recruiter…")
            message_file = folder / f"{name_us}_Recruiter_Message_{company}_{role}.txt"
            web_items = []
            set_step("Recruiter message")
            try:
                message_notes, web_items = write_message(
                    model, settings["models"]["scoring"], post, posting_evidence, news, resume_text(src),
                    letter_doc, enabled_projects(settings), name, today, message_file,
                    contact=str(settings["candidate"].get("message_contact") or "").strip(),
                    final_model=check_model)
            except Exception as e:
                with open(LOG_DIR / "generate_errors.log", "a", encoding="utf-8") as f:
                    f.write(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {uid} (recruiter message)\n"
                            f"{traceback.format_exc()}")
                message_notes = ["", "RECRUITER MESSAGE", f"- FAILED: {e}",
                                 "- The resume and cover letter were kept. Details: data/logs/generate_errors.log"]

            files = [resume_file.name, letter_file.name]
            files.append(message_file.name if message_file.exists() else f"{message_file.name} (not written: see "
                                                                         "RECRUITER MESSAGE below)")
            header = [f"Changes made for: {post['company']} - {post['title']}",
                      f"Posting: {post['url']}", f"Created: {today.isoformat()}", "",
                      "FILES IN THIS FOLDER"] + [f"- {f}" for f in files + ["changes.txt", "sources.txt"]] + [
                      "", "Only the Summary, Skills and Meta sections were changed. Everything else is identical.", ""]
            progress(uid, "Mapping the posting's responsibilities to your evidence…")
            set_step("Coverage report")
            cov = coverage_report(settings["models"]["scoring"], posting_evidence, resume_text(resume_file),
                                  letter_doc)
            header += cost.report() + [""] + flags_report() + [""]
            (folder / "changes.txt").write_text(
                "\n".join(header + cov["top"] + [""] + evidence_inputs(posting_evidence) + [""] + changes + [""] + cov["unused_resume"] + evidence_notes
                          + letter_notes + [""] + cov["unused_letter"] + message_notes) + "\n", encoding="utf-8")

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
            if web_items:
                src_lines += ["", "Used in the recruiter message (why this company; found by one web search):", ""]
                for i, item in enumerate(web_items, 1):
                    src_lines += [f"{i}. {item.get('title', '')}",
                                  f"   Publisher: {item.get('publisher', '')}",
                                  f"   Date: {item.get('date', '') or 'not stated'}",
                                  f"   Link: {item.get('url', '')}",
                                  f"   Summary: {item.get('summary', '')}", ""]
            (folder / "sources.txt").write_text("\n".join(src_lines) + "\n", encoding="utf-8")

            sheet_rows.append((today.isoformat(), post["company"], post["title"], post["score"],
                               days_since_posted(post["posted"], post["run_date"]), post["url"]))
            results.append({"uid": uid, "ok": True, "folder": str(folder), "company": post["company"],
                            "title": post["title"], "cost": round(cost.spent, 2)})
            progress(uid, f"Done (API cost ${cost.spent:.2f})")
        except Exception as e:  # keep going with the other postings
            with open(LOG_DIR / "generate_errors.log", "a", encoding="utf-8") as f:
                f.write(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {uid}\n{traceback.format_exc()}")
            results.append({"uid": uid, "ok": False, "error": str(e), "folder": str(folder),
                            "company": post["company"], "title": post["title"]})
            progress(uid, f"Failed: {e}")
        finally:
            stop_meter()

    sheet = record_in_spreadsheet(sheet_rows)
    return {"results": results, "sheet": sheet}


def record_in_spreadsheet(rows) -> dict:
    """Add rows (plus any waiting from an earlier attempt). If Excel has the
    file open, keep them waiting and tell the user."""
    waiting = db.pending_rows()
    all_rows = [(r["date"], r["company"], r["role"], r["score"], r["days"], r["url"] or "")
                for r in waiting] + list(rows)
    if not all_rows:
        return {"ok": True, "added": 0}
    try:
        serials = append_rows(all_rows, info_lookup(db.postings_for_links()))
        db.clear_pending_rows([r["id"] for r in waiting])
        return {"ok": True, "added": len(serials)}
    except SpreadsheetLocked:
        db.add_pending_rows(list(rows))
        return {"ok": False, "waiting": len(all_rows),
                "message": "Applications.xlsx is open in Excel, so it couldn't be updated. "
                           "Please close it and click 'Try again'."}


def upgrade_spreadsheet() -> dict:
    """One-time upgrade of Applications.xlsx: add any missing columns (Score, Days Since
    Posted, Posting Link) and fill them in for existing rows from the database. This also
    happens automatically the next time documents are generated."""
    try:
        notes = upgrade_sheet(info_lookup(db.postings_for_links()))
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
        cost = start_meter(float(settings.get("budget_per_application") or 1.00))
        start_flags()
        progress(uid, "Searching your evidence for what the posting asks for…")
        set_step("Finding your evidence for the posting")
        ev = (evidence_override or {}).get(uid) or find_evidence(model, post, filter_model=settings["models"]["scoring"])
        progress(uid, "Tailoring resume (rewrite + fact-check)…")
        resume_file = folder / f"{name_us}_Resume_{company}_{role}.docx"
        set_step("Resume (rewrite + fact-check)")
        try:
            changes = tailor_resume(model, src, post, resume_file, pinned_bullets=settings.get("pinned_bullets") or [],
                                    fixed_bullets=settings.get("fixed_bullets") or [],
                                    evidence=ev, guidance=tailoring_guidance(settings),
                                    new_bullets=int(settings.get("new_meta_bullets") or 0),
                                    check_model=settings["models"].get("checking") or model)
        finally:
            stop_meter()
        header = [f"Resume changes for: {post['company']} - {post['title']}", f"Posting: {post['url']}",
                  f"Created: {dt.date.today().isoformat()}", f"Base resume: {src}", ""] + cost.report() + [""] \
            + flags_report() + [""]
        (folder / "changes.txt").write_text("\n".join(header + evidence_inputs(ev) + [""] + changes) + "\n",
                                            encoding="utf-8")
        results.append({"uid": uid, "folder": str(folder), "resume": str(resume_file)})
        progress(uid, "Done")
    return {"results": results}
