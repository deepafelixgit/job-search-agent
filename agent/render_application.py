"""Build an application's documents from content.json. No Claude API calls, ever.

Claude Code (the write-application skill) writes the content; this builds the files with the same
code and formatting the agent uses, then runs the free code checks.

  python -m agent.render_application <folder>       build the documents in <folder> and check them
  python -m agent.render_application <folder> --dry-run [--content drafts/content.json]
                                                    check a draft (rules and lengths) without building
                                                    anything or touching Applications.xlsx
  python -m agent.render_application --resume-map   print the base resume's parts with their IDs

<folder>/content.json:
{
  "uid": "Company|id",
  "posting": {"company": ..., "title": ..., "url": ..., "score": 8, "posted": "YYYY-MM-DD", "run_date": "YYYY-MM-DD"},
  "resume": {
    "summary": ["paragraph", ...],
    "skills": [["Label:", ["item", ...]], ...],        (optional; leave out to keep the base resume's)
    "meta_intro": "..." ,                              (optional)
    "meta_groups": [["G1", [["M1", "bullet text"], ["P1", "pinned text"], ...]], ...]
  },
  "cover_letter": {"greeting": "Dear ... Hiring Team,", "paragraphs": ["...", ...], "closing": "Sincerely,"},
  "recruiter_message": {"note": "...", "subject": "...", "body": "...", "follow_up": "..."},
  "add_to_spreadsheet": true
}

Results go to <folder>/render_report.json and are printed. Exit code 0 = no errors, 2 = errors to fix.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import docx

from .config import ROOT, SPREADSHEET, load_settings, resume_path
from .resume import add_page_numbers, write_cover_letter, write_tailored_resume

# Rules for applications written in Claude Code (CLAUDE.md, "Writing applications")
LETTER_WORDS = (330, 430)
LETTER_MAX_PARAGRAPHS = 5
BODY_WORDS = (120, 220)
BODY_MAX_CHARS = 1900
NOTE_MAX_CHARS = 200
FOLLOW_WORDS = (40, 70)
SUMMARY_MAX_GROWTH = 1.40
NOTE_ENDING = "Would you be open to a quick chat?"
BODY_OPENING = "Thank you for connecting. I recently applied for the "
BODY_ASK = "Would you be open to a brief conversation about the role?"
BODY_REFERRAL = ("If someone else would be the better person to speak with, I'd be grateful if you could point "
                 "me to them.")
FOLLOW_OPENING = "I hope your week is going well. I wanted to follow up on my application for the "
FOLLOW_ASK = "I'd be grateful for 15 minutes to talk, whenever suits you."
GREETING = "Hi [First name],"
ORDERS = [(r"\blet me know\b", '"Let me know"'), (r"\bplease (respond|reply|get back)\b", '"Please respond"'),
          (r"(^|[.!?]\s+)are you the right person\b", 'the bare question "Are you the right person...?"'),
          (r"(^|[.!?]\s+)(send|forward|reply|respond|call|email|contact|connect|review|check)\s+me\b",
           "an instruction to the recruiter")]
AI_TELLS = [(r"\bnot (just|only) [^.]{1,60}\bbut\b", '"not just X, but Y"'),
            (r"\bit'?s not about\b", "\"it's not about X\""),
            (r"(^|[.!?]\s+)(Moreover|Furthermore|Additionally|That said)\b", "a stock transition"),
            (r"\bin today'?s\b", '"in today\'s..."'),
            (r"\bI am writing to\b|\bI wanted to take a moment\b", "throat-clearing"),
            (r"\b(This|That) experience taught me\b|(^|[.!?]\s+)In short\b", "a summing-up line"),
            (r"\b(robust|seamless|seamlessly|impactful|transformative)\b", "an adjective doing a fact's work")]
# Abbreviations a reader outside the candidate's old team may not know: spell them out in letters and
# messages ("EE" -> "electrical engineering", "QA-failed" -> "failed quality checks").
KNOWN_ABBREVIATIONS = {"TPM", "TPMs", "AI", "ML", "VP", "VPs", "SVP", "CEO", "CTO", "CFO", "MBA", "AR", "VR", "US",
                       "USA", "UK", "EU", "NASA", "FAA", "MCP", "API", "APIs", "LLM", "LLMs", "OK"}
PRESENT_META = re.compile(r"\bI(?:'m| am) (?:a |an |the )?[^.,;]{0,40}\bat Meta\b", re.I)


# ------------------------------------------------------------------ small helpers

def words(text: str) -> int:
    return len(re.findall(r"\b[\w'’-]+\b", text or ""))


def sentences(text: str) -> List[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"“(])", (text or "").strip()) if s]


def _verified_numbers() -> set:
    from .tailor import numbers_in
    try:
        from mcp_server.metrics import load_metrics
        m = load_metrics(ROOT / "evidence" / "verified_metrics.txt")
        return numbers_in(" ".join(it.text for it in m.items if not it.needs_confirmation))
    except Exception:  # noqa: BLE001 - reported as a warning by the caller
        return set()


def _codenames() -> List[str]:
    from . import personal
    return list((personal._data().get("codename_words") or []))


def wording_problems(text: str) -> List[str]:
    """Code checks shared by the letter and the message (CLAUDE.md standing rules)."""
    from .evidence_gate import internal_terms, positioning_problems
    out = []
    for s in sentences(text):
        out += [f'{p}: "{s}"' for p in positioning_problems(s)]
        terms = internal_terms(s)
        if terms:
            out.append(f'internal term(s) {", ".join(terms)}: "{s}"')
        names = [c for c in _codenames() if re.search(rf"\b{re.escape(c)}\b", s)]
        if names:
            out.append(f'internal codename(s) {", ".join(names)} (use the commercial name): "{s}"')
        if PRESENT_META.search(s):
            out.append(f'Meta is in the past: say "I was ...", not "I\'m ...": "{s}"')
    return out


def abbreviations(text: str, posting_text: str = "") -> List[str]:
    """Abbreviations in a letter or message that aren't widely known and aren't in the posting itself."""
    found = []
    for tok in sorted(set(re.findall(r"(?<![\w.-])([A-Z]{2,5}s?)(?![\w.])", text))):
        base = tok[:-1] if tok.endswith("s") and tok[:-1].isupper() else tok
        if tok in KNOWN_ABBREVIATIONS or base in KNOWN_ABBREVIATIONS:
            continue
        if re.search(rf"(?<![\w-]){re.escape(base)}(?![\w-])", posting_text or ""):
            continue
        found.append(f'abbreviation "{tok}": spell it out (a recruiter may not know it)')
    return found


def ai_tells(text: str) -> List[str]:
    out = [f'{why}: "{m.group(0).strip()}"' for pat, why in AI_TELLS for m in re.finditer(pat, text, re.I | re.M)]
    if text.count("—") > 1:
        out.append(f"{text.count('—')} em dashes (one at most)")
    return out


# ------------------------------------------------------------------ resume

def resume_map(settings: dict = None) -> dict:
    """The base resume's tailorable parts with IDs, pinned bullets and the bullets they replace."""
    from .tailor import _replaced_by_pin, _resume_parts
    settings = settings or load_settings()
    summary, skills, intro, groups, bullets = _resume_parts(resume_path(settings))
    pins = {f"P{i}": p.strip() for i, p in enumerate(settings.get("pinned_bullets") or [], 1) if p and p.strip()}
    replaced = {}
    for pid, ptext in pins.items():
        for bid in _replaced_by_pin(ptext, bullets):
            replaced.setdefault(bid, pid)
    return {"summary": summary, "skills": [[l, items] for l, items in skills], "meta_intro": intro,
            "groups": [[g, h] for g, h in groups],
            "bullets": {b: {"group": g, "text": t} for b, (g, t) in bullets.items() if b not in replaced},
            "pinned": pins, "replaced_by_pin": replaced}


def check_resume(content: dict, base: dict) -> Tuple[List[str], List[str]]:
    from .tailor import numbers_in
    errors, warnings = [], []
    verified = _verified_numbers()
    if not verified:
        warnings.append("couldn't read verified_metrics.txt, so numbers weren't checked")
    base_text = " ".join(base["summary"] + [b["text"] for b in base["bullets"].values()] + list(base["pinned"].values()))
    known_numbers = numbers_in(base_text) | verified

    # summary
    summ = [p.strip() for p in content.get("summary") or [] if p.strip()]
    if not summ:
        errors.append("resume summary is empty")
    else:
        new, old = " ".join(summ), " ".join(base["summary"])
        if len(new) > len(old) * SUMMARY_MAX_GROWTH:
            errors.append(f"summary is {len(new) / len(old):.0%} of the base summary's length (at most "
                          f"{SUMMARY_MAX_GROWTH:.0%})")
        ns, os_ = sentences(new), sentences(old)
        if ns and os_ and ns[0] != os_[0]:
            warnings.append(f'summary sentence 1 differs from the base resume: "{ns[0]}"')
        if ns and os_ and ns[-1] != os_[-1]:
            warnings.append(f'summary last sentence (engineering roots) differs from the base resume: "{ns[-1]}"')
        extra = sorted(numbers_in(new) - known_numbers)
        if extra:
            errors.append(f"summary has number(s) not in the resume or verified_metrics.txt: {', '.join(extra)}")

    # skills: only items already listed under each label
    if content.get("skills"):
        base_items = {l: set(items) for l, items in base["skills"]}
        for label, items in content["skills"]:
            if label not in base_items:
                errors.append(f"skills label not in the base resume: {label}")
                continue
            bad = [i for i in items if i not in base_items[label]]
            if bad:
                errors.append(f"skills under {label} not in the base resume: {', '.join(bad)}")

    # Meta bullets: every original and pinned bullet, once, originals in their own group
    groups = {g for g, _ in base["groups"]}
    seen: Dict[str, int] = {}
    for gid, items in content.get("meta_groups") or []:
        if gid not in groups:
            errors.append(f"unknown Meta group {gid}")
        for bid, text in items:
            seen[bid] = seen.get(bid, 0) + 1
            if bid in base["pinned"]:
                if text != base["pinned"][bid]:
                    errors.append(f"pinned bullet {bid} must be exactly as in settings.yaml")
                continue
            if bid not in base["bullets"]:
                errors.append(f"unknown bullet id {bid} (new bullets aren't allowed here)")
                continue
            orig = base["bullets"][bid]
            if orig["group"] != gid:
                errors.append(f"bullet {bid} moved from {orig['group']} to {gid}")
            extra = sorted(numbers_in(text) - numbers_in(orig["text"]) - verified)
            if extra:
                errors.append(f"bullet {bid} has number(s) not in its original or verified_metrics.txt: "
                              f"{', '.join(extra)}")
            if len(text) > len(orig["text"]) * 1.2 + 25:
                warnings.append(f"bullet {bid} is much longer than the original ({len(text)} vs "
                                f"{len(orig['text'])} characters)")
            errors += [f"bullet {bid}: {p}" for p in wording_problems(text)
                       if not p.startswith(("internal term", "Meta is in the past"))]
    expected = set(base["bullets"]) | set(base["pinned"])
    missing = sorted(expected - set(seen))
    if missing:
        errors.append(f"Meta bullet(s) missing: {', '.join(missing)} (every Meta bullet is kept)")
    twice = sorted(b for b, n in seen.items() if n > 1)
    if twice:
        errors.append(f"Meta bullet(s) used twice: {', '.join(twice)}")
    return errors, warnings


def build_resume(content: dict, base: dict, src: Path, dst: Path, settings: dict) -> List[str]:
    """Write the resume; returns errors from the after-write checks (earlier jobs, fixed bullets)."""
    from .tailor import _earlier_experience_unchanged
    plan = {"summary": content["summary"],
            "skills": [(l, items) for l, items in (content.get("skills") or base["skills"])],
            "meta_intro": content.get("meta_intro", base["meta_intro"]),
            "meta_groups": [(g, [(b, t) for b, t in items]) for g, items in content["meta_groups"]]}
    write_tailored_resume(src, dst, plan)
    errors = []
    try:
        _earlier_experience_unchanged(src, dst)
    except Exception as e:  # noqa: BLE001 - the tailor check deletes the file and raises
        errors.append(f"experience before Meta changed: {e}")
    if dst.exists():
        written = {p.text.strip() for p in docx.Document(str(dst)).paragraphs}
        for b in settings.get("fixed_bullets") or []:
            if b.strip() and b.strip() not in written:
                errors.append(f"fixed bullet changed or missing: {b[:70]}...")
    return errors


# ------------------------------------------------------------------ cover letter and message

def check_letter(letter: dict, company: str, posting_text: str = "") -> Tuple[List[str], List[str]]:
    from .letter import prescribes
    errors, warnings = [], []
    paras = [p.strip() for p in letter.get("paragraphs") or [] if p.strip()]
    body = "\n\n".join(paras)
    n = words(body)
    if not LETTER_WORDS[0] <= n <= LETTER_WORDS[1]:
        errors.append(f"cover letter body is {n} words ({LETTER_WORDS[0]}-{LETTER_WORDS[1]})")
    if len(paras) > LETTER_MAX_PARAGRAPHS:
        errors.append(f"cover letter has {len(paras)} paragraphs (at most {LETTER_MAX_PARAGRAPHS})")
    if paras and len(sentences(paras[-1])) > 3:
        errors.append(f"closing paragraph has {len(sentences(paras[-1]))} sentences (at most 3)")
    errors += wording_problems(body)
    errors += abbreviations(body, posting_text)
    errors += [f'tells the company what it needs: "{s}"' for s in sentences(body) if prescribes(s, company)]
    warnings += ai_tells(body)
    return errors, warnings


def assemble_message(msg: dict) -> Dict[str, str]:
    return {k: (msg.get(k) or "").strip() for k in ("note", "subject", "body", "follow_up")}


def check_message(msg: dict, title: str, url: str, name: str, posting_text: str = "") -> Tuple[List[str], List[str]]:
    errors, warnings = [], []
    note, body, follow = msg["note"], msg["body"], msg["follow_up"]
    if len(note) > NOTE_MAX_CHARS:
        errors.append(f"connection note is {len(note)} characters (at most {NOTE_MAX_CHARS})")
    if not note.endswith(NOTE_ENDING):
        errors.append(f'connection note must end with "{NOTE_ENDING}"')
    if not msg["subject"]:
        errors.append("message subject is empty")
    n = words(body)
    if not BODY_WORDS[0] <= n <= BODY_WORDS[1]:
        errors.append(f"message body is {n} words ({BODY_WORDS[0]}-{BODY_WORDS[1]})")
    if len(body) > BODY_MAX_CHARS:
        errors.append(f"message body is {len(body)} characters (at most {BODY_MAX_CHARS})")
    if not body.startswith(GREETING):
        errors.append(f'message body must start with "{GREETING}"')
    if BODY_OPENING + title not in body:
        errors.append(f'message body must say "{BODY_OPENING}{title} role and wanted to introduce myself:"')
    if url and url not in body:
        errors.append("message body must include the posting link")
    for phrase in (BODY_ASK, BODY_REFERRAL):
        if phrase not in body:
            errors.append(f'message body must include "{phrase}"')
    if not body.rstrip().endswith(f"Thank you for your time,\n{name}"):
        errors.append(f'message body must end with "Thank you for your time," then "{name}"')
    if not follow.startswith(GREETING):
        errors.append(f'follow-up must start with "{GREETING}"')
    if FOLLOW_OPENING not in follow:
        errors.append(f'follow-up must include "{FOLLOW_OPENING}<role> role."')
    if FOLLOW_ASK not in follow:
        errors.append(f'follow-up must include "{FOLLOW_ASK}"')
    if not follow.rstrip().endswith(f"Thank you,\n{name}"):
        errors.append(f'follow-up must end with "Thank you," then "{name}"')
    fw = words(follow)
    if not FOLLOW_WORDS[0] <= fw <= FOLLOW_WORDS[1]:
        warnings.append(f"follow-up is {fw} words ({FOLLOW_WORDS[0]}-{FOLLOW_WORDS[1]})")
    whole = "\n".join([note, body, follow])
    for pat, why in ORDERS:
        if re.search(pat, whole, re.I | re.M):
            errors.append(f"sounds like an order: {why}")
    if re.search(r"\*\*|__|`|^#+ |^\s*\* ", whole, re.M):
        errors.append("Markdown (**, #, `) isn't allowed; plain \"- \" lines are fine")
    bullets = [l for l in body.splitlines() if l.startswith("- ")]
    if len(bullets) > 3:
        errors.append(f"{len(bullets)} proof points (at most 3)")
    errors += wording_problems(whole)
    errors += abbreviations(whole.replace("[First name]", ""), posting_text)
    warnings += ai_tells(body)
    return errors, warnings


def posting_text(content: dict) -> str:
    """The posting's text (from jobs.db, or posting.txt saved in the folder) for the abbreviation check."""
    from . import db
    rows = db.get_postings([content.get("uid", "")]) if content.get("uid") else []
    post = content.get("posting") or {}
    return f"{post.get('title', '')}\n" + ((rows[0]["description"] or "") if rows else "")


# ------------------------------------------------------------------ pages

def soffice() -> Optional[str]:
    for p in (shutil.which("soffice"), "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        if p and Path(p).exists():
            return p
    return None


def page_count(path: Path) -> Optional[int]:
    """Pages when laid out (LibreOffice converts to PDF). None if LibreOffice isn't installed."""
    exe = soffice()
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", tmp, str(path)],
                       capture_output=True, timeout=180, check=False)
        pdf = Path(tmp) / (path.stem + ".pdf")
        if not pdf.exists():
            return None
        data = pdf.read_bytes()
        m = re.findall(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", data)
        return max(int(x) for x in m) if m else len(re.findall(rb"/Type\s*/Page\b(?!s)", data))


# ------------------------------------------------------------------ spreadsheet

def add_to_spreadsheet(posting: dict) -> str:
    """Add the posting's row the way the agent does (backup first; waits in jobs.db if Excel has the
    file open). Skips it if Applications.xlsx already lists this posting's link."""
    from openpyxl import load_workbook
    from .sheet import days_since_posted
    from . import db
    url = (posting.get("url") or "").strip()
    if SPREADSHEET.exists() and url:
        try:
            ws = load_workbook(SPREADSHEET, read_only=False).worksheets[0]
            for row in ws.iter_rows(min_row=2):
                for c in row:
                    target = c.hyperlink.target if c.hyperlink else None
                    if url in (str(c.value or ""), target or ""):
                        return f"already in Applications.xlsx (row {c.row}); not added again"
        except Exception:  # noqa: BLE001 - e.g. the file is mid-save; fall through to the normal path
            pass
    if any((r["url"] or "") == url for r in db.pending_rows()):
        return "already waiting to be added to Applications.xlsx (Excel had it open)"
    from .generate import record_in_spreadsheet
    row = (dt.date.today().isoformat(), posting.get("company", ""), posting.get("title", ""), posting.get("score"),
           days_since_posted(posting.get("posted"), posting.get("run_date")), url)
    res = record_in_spreadsheet([row])
    if res.get("ok"):
        return f"added to Applications.xlsx ({res.get('added', 0)} row)"
    return "Applications.xlsx is open in Excel, so the row is waiting; close Excel and click \"Add waiting rows\""


# ------------------------------------------------------------------ file names (the agent's own)

def file_names(company: str, title: str, name: str) -> Dict[str, str]:
    from .generate import _safe, short_role
    c, r, n = _safe(company), _safe(short_role(title)), name.replace(" ", "_")
    return {"resume": f"{n}_Resume_{c}_{r}.docx", "letter": f"{n}_Cover_Letter_{c}_{r}.docx",
            "message": f"{n}_Recruiter_Message_{c}_{r}.txt"}


def application_folder(company: str, title: str, day: dt.date = None) -> Path:
    """A new folder named the way the agent names it: output/<date>/<Company>_<Role>[_n]."""
    from .config import OUTPUT_DIR
    from .generate import _safe, short_role
    day_dir = OUTPUT_DIR / (day or dt.date.today()).isoformat()
    stem = f"{_safe(company)}_{_safe(short_role(title)).replace(' ', '_')}"
    folder, n = day_dir / stem, 2
    while folder.exists():
        folder, n = day_dir / f"{stem}_{n}", n + 1
    return folder


# ------------------------------------------------------------------ main

def counts(content: dict) -> Dict[str, int]:
    """Lengths the rules limit, for the skill to check a draft."""
    out = {}
    if content.get("cover_letter"):
        paras = [p for p in content["cover_letter"].get("paragraphs") or [] if p.strip()]
        out.update(letter_words=words("\n\n".join(paras)), letter_paragraphs=len(paras))
    if content.get("recruiter_message"):
        m = assemble_message(content["recruiter_message"])
        out.update(note_characters=len(m["note"]), body_words=words(m["body"]), body_characters=len(m["body"]),
                   follow_up_words=words(m["follow_up"]))
    if content.get("resume"):
        out["summary_characters"] = len(" ".join(content["resume"].get("summary") or []))
    return out


def check_only(content: dict) -> dict:
    """All the code checks on a draft, without building files or touching Applications.xlsx."""
    settings = load_settings()
    post = content.get("posting") or {}
    report = {"errors": [], "warnings": [], "counts": counts(content)}
    ptext = posting_text(content)
    for label, key, fn in (("Resume", "resume", lambda c: check_resume(c, resume_map(settings))),
                           ("Cover letter", "cover_letter", lambda c: check_letter(c, post.get("company", ""), ptext)),
                           ("Recruiter message", "recruiter_message",
                            lambda c: check_message(assemble_message(c), post.get("title", ""), post.get("url", ""),
                                                    settings["candidate"]["name"], ptext))):
        if content.get(key):
            e, w = fn(content[key])
            report["errors"] += [f"{label}: {x}" for x in e]
            report["warnings"] += [f"{label}: {x}" for x in w]
    return report


def render(folder: Path, add_row: bool = True) -> dict:
    settings = load_settings()
    name = settings["candidate"]["name"]
    content = json.loads((folder / "content.json").read_text(encoding="utf-8"))
    post = content.get("posting") or {}
    company, title, url = post.get("company", ""), post.get("title", ""), post.get("url", "")
    names = file_names(company, title, name)
    src = resume_path(settings)
    base = resume_map(settings)
    report = {"folder": str(folder), "errors": [], "warnings": [], "files": [], "pages": {}, "spreadsheet": "",
              "counts": counts(content)}

    def section(label, errs, warns):
        report["errors"] += [f"{label}: {e}" for e in errs]
        report["warnings"] += [f"{label}: {w}" for w in warns]

    if content.get("resume"):
        e, w = check_resume(content["resume"], base)
        section("Resume", e, w)
        if not any(x.startswith(("Meta bullet(s) missing", "unknown", "pinned")) for x in e):
            dst = folder / names["resume"]
            section("Resume", build_resume(content["resume"], base, src, dst, settings), [])
            if dst.exists():
                add_page_numbers(dst)
                report["files"].append(dst.name)
    if content.get("cover_letter"):
        letter = content["cover_letter"]
        section("Cover letter", *check_letter(letter, company, posting_text(content)))
        dst = folder / names["letter"]
        write_cover_letter(src, dst, letter.get("date") or dt.date.today().strftime("%B %-d, %Y"),
                           letter.get("greeting") or f"Dear {company} Hiring Team,",
                           [p.strip() for p in letter["paragraphs"] if p.strip()],
                           letter.get("closing") or "Sincerely,", name)
        add_page_numbers(dst)
        report["files"].append(dst.name)
    if content.get("recruiter_message"):
        from .recruiter import render as render_message
        msg = assemble_message(content["recruiter_message"])
        e, w = check_message(msg, title, url, name, posting_text(content))
        section("Recruiter message", e, w)
        warn_lines = [x for x in e if "words" in x or "characters" in x]
        (folder / names["message"]).write_text(render_message(msg, warn_lines, body_words=BODY_WORDS), encoding="utf-8")
        report["files"].append(names["message"])

    for key, limit in (("resume", 2), ("letter", 1)):
        path = folder / names[key]
        if not path.exists():
            continue
        pages = page_count(path)
        report["pages"][path.name] = pages
        if pages is None:
            report["warnings"].append(f"Pages: {path.name} not counted (LibreOffice isn't installed)")
        elif pages > limit:
            report["errors"].append(f"Pages: {path.name} is {pages} pages (at most {limit})")

    if add_row and content.get("add_to_spreadsheet", True) and not report["errors"]:
        report["spreadsheet"] = add_to_spreadsheet(post)
    elif report["errors"]:
        report["spreadsheet"] = "not added yet: fix the errors and render again"
    (folder / "render_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    return report


def main(argv: List[str]) -> int:
    if argv and argv[0] == "--resume-map":
        print(json.dumps(resume_map(), indent=1, ensure_ascii=False))
        return 0
    if not argv:
        print(__doc__)
        return 1
    folder = Path(argv[0]).expanduser().resolve()
    if "--dry-run" in argv:
        rel = argv[argv.index("--content") + 1] if "--content" in argv else "content.json"
        path = folder / rel
        if not path.exists():
            print(f"No {rel} in {folder}")
            return 1
        rep = check_only(json.loads(path.read_text(encoding="utf-8")))
        print("Counts: " + ", ".join(f"{k} {v}" for k, v in rep["counts"].items()))
        for label, items in (("ERRORS (fix these)", rep["errors"]), ("Warnings", rep["warnings"])):
            if items:
                print(f"\n{label}:")
                print("\n".join(f"- {x}" for x in items))
        if not rep["errors"]:
            print("No errors.")
        return 2 if rep["errors"] else 0
    if not (folder / "content.json").exists():
        print(f"No content.json in {folder}")
        return 1
    rep = render(folder, add_row="--no-spreadsheet" not in argv)
    print(f"Folder: {rep['folder']}")
    print("Files: " + ", ".join(rep["files"]))
    print("Pages: " + ", ".join(f"{k}: {v if v is not None else 'not counted'}" for k, v in rep["pages"].items()))
    print("Spreadsheet: " + rep["spreadsheet"])
    print("Counts: " + ", ".join(f"{k} {v}" for k, v in rep["counts"].items()))
    for label, items in (("ERRORS (fix these)", rep["errors"]), ("Warnings", rep["warnings"])):
        if items:
            print(f"\n{label}:")
            print("\n".join(f"- {x}" for x in items))
    return 2 if rep["errors"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
