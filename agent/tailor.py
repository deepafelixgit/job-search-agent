"""Tailor the resume and write the cover letter for one posting.

Honesty safeguards for the resume (three layers):
  1. Claude is told it may only reword, reorder and select - and each Meta
     bullet must come from exactly ONE original bullet (no merging programs).
  2. Code checks: skills may only use items already on the resume; numbers
     in a rewritten line must already appear in its source; lengths stay close.
  3. A second, independent Claude pass fact-checks every changed line against
     the original. Anything that fails is put back to the original wording.
Everything is logged to changes.txt.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

import docx

from typing import Optional

from . import personal
from .claude_client import ask_json
from .evidence_gate import (Judged, PostingEvidence, _cite_passage, _verified_lines, apply_scope_term,
                            internal_terms, judge_sentences, label_line, scope_note, is_claim_about_candidate, public_acronyms,
                            replace_codenames, split_sentences, titles_conflicting, unexplained_acronyms)
from .resume import map_resume, resume_text, write_cover_letter, write_tailored_resume

MAX_META_BULLETS = 10

# Plain-language versions of Meta-internal terms, used only where the meaning is identical
VAGUE = re.compile(r"\b(drove|delivered|achieved) (gains|impact|results|improvements)\b|\bvarious\b|\bmany things\b", re.I)


def translate_internal(text: str) -> Tuple[str, List[str]]:
    """Swap internal terms for plain words with the same meaning (personal.translations in settings.yaml)."""
    done = []
    for pat, plain in personal.translations():
        if re.search(pat, text, re.I):
            text = re.sub(pat, plain, text, flags=re.I)
            done.append(f"{pat.replace(chr(92) + 'b', '')} -> {plain}")
    return text, done


def _evidence_candidates(ev: "PostingEvidence") -> Tuple[Dict[str, str], Dict[str, str]]:
    """Evidence items a new bullet may be written from: verified_metrics.txt lines, and the
    posting's filtered evidence passages that aren't verified lines (those are listed as V<line>)."""
    text, cite = {}, {}
    for vid, c, t in _verified_lines(ev.rules):
        text[vid], cite[vid] = t, c
    for p in ev.passages:
        if p.get("doc_type") != "verified metrics":
            text[p["id"]], cite[p["id"]] = p["text"], _cite_passage(p)
    return text, cite


class PinnedBulletChanged(RuntimeError):
    pass


class FixedTextMismatch(RuntimeError):
    pass

NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
                "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "fifteen": "15",
                "twenty": "20", "half": "half", "halving": "half", "halved": "half", "double": "double",
                "doubled": "double", "doubling": "double", "triple": "triple", "tripled": "triple"}


def numbers_in(text: str) -> set:
    found = {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}
    for word in re.findall(r"[A-Za-z]+", text.lower()):
        if word in NUMBER_WORDS:
            found.add(NUMBER_WORDS[word])
    return found


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


# ------------------------------------------------------------------ resume

TAILOR_RULES = """You are tailoring a resume for one specific job posting. You may ONLY \
reword, reorder, and select from what is already in the resume - plus up to 3 NEW Meta bullets written \
from the EVIDENCE CANDIDATES (below).

Hard rules (breaking any of these is a failure):
- Never invent or change metrics, numbers, dates, tools, technologies, titles, team names, \
products, scope or responsibilities. If a fact isn't in the resume, it doesn't go in.
- Never inflate: don't upgrade verbs (e.g. "partnered" -> "led", "supported" -> "owned"), \
and don't make scope bigger than the original states.
- Never combine separate programs or bullets into one claim. Each Meta bullet you write must \
come from exactly ONE original bullet (its source_id), and say nothing that isn't in that bullet.
- NEW bullets (ids N1-N3, at most 3): only when an evidence candidate matches the posting's top \
responsibilities better than existing bullets. Each comes from ONE program, cites its evidence ids, and uses \
numbers exactly as written there (never round, combine or calculate). Don't duplicate an existing bullet.
- Skills (the labeled lines under Core Competencies and Technical Skills): use only the exact \
items already listed under each label. You may reorder labels (each line stays in its own section), \
reorder items, and drop items that are irrelevant to this posting (keep at least half of each line).
- Meta bullets stay under their original subheading (new bullets go under the best-fitting one). You may \
reorder subheadings and the bullets within them.
- RANKING: rank EVERY candidate Meta bullet (existing ids, new ids and pinned ids) by how directly it matches \
the posting's TOP 3 RESPONSIBILITIES (1 = most direct), with a one-line reason. Only the top 10 are kept; \
pinned bullets are always kept.
- PLAIN LANGUAGE: translate Meta-internal terms into the posting's vocabulary only where the meaning is \
identical (use the APPROVED PLAIN-LANGUAGE TRANSLATIONS below). Never write the INTERNAL SHORTHAND below \
or internal metric names unless explained in plain words.
- SUMMARY: its second sentence must name the 2-3 program types most relevant to this posting, in the \
posting's own terms (e.g. "developer infrastructure, CI and release programs"), and list them in \
summary_program_types exactly as written in that sentence. No vague phrases like "drove gains".
- Keep each rewritten line about the same length as the original (never more than ~15% longer), \
so the resume layout doesn't change.

What good tailoring looks like: lead with the experience most relevant to the posting, use the \
posting's vocabulary where it honestly describes what the resume already says, and make the \
summary speak to this role. Summary: 1-2 paragraphs, same overall length as the original. \
For every change, give a one-sentence "why" tied to the posting."""

VERIFY_RULES = """You are a strict fact-checker for a resume. For each item below, compare the \
NEW text with its SOURCE text (and, for summary items, the full original resume).

Mark faithful=false if the new text does ANY of these:
- adds a metric, number, tool, technology, title, product, team, customer, or responsibility \
not present in the source;
- inflates scope or ownership (e.g. "contributed" -> "led", "a team" -> "the org", one program -> "programs");
- combines facts from separate programs/bullets into one claim (summary items may summarize the \
resume, but must not fuse two different programs into a single achievement);
- changes meaning in a way the source doesn't support.
Rewording, reordering, and using different but equivalent words is fine. The APPROVED PLAIN-LANGUAGE \
TRANSLATIONS of Meta-internal terms listed below count as the same meaning. Be strict but fair."""


def _resume_parts(path: Path):
    d = docx.Document(str(path))
    m = map_resume(d)
    paras = d.paragraphs
    summary = [paras[i].text.strip() for i in m.summary]
    skills = [(label, items) for _, label, items in m.skills]
    intro = paras[m.meta_intro].text.strip() if m.meta_intro >= 0 else ""
    groups = [(gid, heading) for gid, _, heading in m.meta_groups]
    bullets = {bid: (gid, text) for bid, (_, gid, text) in m.meta_bullets.items()}
    return summary, skills, intro, groups, bullets


def _tailor_schema(labels: List[str], gids: List[str], bids: List[str], new_ids: List[str] = ()) -> dict:
    why = {"type": "string"}
    all_ids = bids + list(new_ids)
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "object", "properties": {
                "paragraphs": {"type": "array", "items": {"type": "string"}}, "why": why},
                "required": ["paragraphs", "why"], "additionalProperties": False},
            "skills": {"type": "array", "items": {"type": "object", "properties": {
                "label": {"type": "string", "enum": labels},
                "items": {"type": "array", "items": {"type": "string"}}, "why": why},
                "required": ["label", "items", "why"], "additionalProperties": False}},
            "meta_intro": {"type": "object", "properties": {"text": {"type": "string"}, "why": why},
                           "required": ["text", "why"], "additionalProperties": False},
            "meta_groups": {"type": "array", "items": {"type": "object", "properties": {
                "group_id": {"type": "string", "enum": gids},
                "bullets": {"type": "array", "items": {"type": "object", "properties": {
                    "source_id": {"type": "string", "enum": all_ids},
                    "text": {"type": "string"}, "why": why},
                    "required": ["source_id", "text", "why"], "additionalProperties": False}}},
                "required": ["group_id", "bullets"], "additionalProperties": False}},
            "dropped_bullets": {"type": "array", "items": {"type": "object", "properties": {
                "source_id": {"type": "string", "enum": bids}, "why": why},
                "required": ["source_id", "why"], "additionalProperties": False}},
            "new_bullets": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string", "enum": list(new_ids) or ["N1"]},
                "group_id": {"type": "string", "enum": gids},
                "text": {"type": "string"},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "matches_responsibility": {"type": "string"}, "why": why},
                "required": ["id", "group_id", "text", "evidence_ids", "matches_responsibility", "why"],
                "additionalProperties": False}},
            "bullet_ranking": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string", "enum": all_ids}, "rank": {"type": "integer"},
                "reason": {"type": "string"}},
                "required": ["id", "rank", "reason"], "additionalProperties": False}},
            "summary_program_types": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "skills", "meta_intro", "meta_groups", "dropped_bullets", "new_bullets",
                     "bullet_ranking", "summary_program_types"],
        "additionalProperties": False,
    }


VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"}, "faithful": {"type": "boolean"}, "problem": {"type": "string"}},
        "required": ["id", "faithful", "problem"], "additionalProperties": False}}},
    "required": ["verdicts"], "additionalProperties": False,
}


def _words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def _replaced_by_pin(pin: str, bullets: Dict[str, Tuple[str, str]]) -> List[str]:
    """Original resume bullets that are an older wording of a pinned bullet
    (same numbers, mostly the same words). They are left out so the fact
    doesn't appear twice."""
    pin_nums, pin_words = numbers_in(pin), _words(pin)
    out = []
    for bid, (_, text) in bullets.items():
        if pin_nums and numbers_in(text) == pin_nums and \
                len(pin_words & _words(text)) >= 0.6 * len(pin_words):
            out.append(bid)
    return out


def check_pinned_against_evidence(pins: List[str]) -> List[str]:
    """Code check of each pinned bullet against evidence/verified_metrics.txt.
    It never changes a pinned bullet; it only reports problems in changes.txt."""
    from .config import ROOT
    path = ROOT / "evidence" / "verified_metrics.txt"
    if not pins:
        return []
    if not path.exists():
        return [f"- WARNING: couldn't check pinned bullets ({path.name} not found)."]
    try:
        from mcp_server.metrics import load_metrics
        m = load_metrics(path)
    except Exception as e:  # noqa: BLE001 - a broken file must not stop the resume
        return [f"- WARNING: couldn't read {path.name} to check pinned bullets ({e})."]
    usable = " ".join(it.text for it in m.items if not it.needs_confirmation)
    out = []
    for i, pin in enumerate(pins, 1):
        missing = sorted(n for n in numbers_in(pin)
                         if not re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", usable))
        codes = [c.split(" (")[0] for c in m.codenames
                 if re.search(rf"\b{re.escape(c.split(' (')[0])}\b", pin, re.I)]
        problems = []
        if missing:
            problems.append(f"numbers not in {path.name}: {', '.join(missing)}")
        if codes:
            problems.append(f"uses internal codename(s): {', '.join(codes)}")
        if problems:
            out.append(f"- WARNING: pinned bullet P{i} was used as written, but " + "; ".join(problems) +
                       ". Fix it in settings.yaml.")
        else:
            out.append(f"- Pinned bullet P{i}: every number is in {path.name} and it uses no internal codenames.")
    return out


def _posting_block(post) -> str:
    return (f"Company: {post['company']}\nTitle: {post['title']}\nLocation: {post['location']}\n\n"
            f"{post['description']}")


def tailor_resume(model: str, resume_path: Path, post, out_path: Path,
                  pinned_bullets: List[str] = (), evidence: Optional[PostingEvidence] = None,
                  fixed_bullets: List[str] = ()) -> List[str]:
    """Write the tailored resume to out_path. Returns the lines for changes.txt.

    pinned_bullets (from settings.yaml) are always included, character for
    character, in the Meta subheading Claude judges most relevant. They are
    never reworded, shortened, fact-check-reverted or dropped.

    evidence (from evidence_gate.find_evidence) is required for the evidence gate:
    every Summary and Meta sentence is run through check_claim and removed if
    unsupported (pinned bullets excepted), and changes.txt cites a source for each."""
    if evidence is None:
        raise ValueError("tailor_resume needs the posting's evidence (evidence_gate.find_evidence).")
    fixed = [b.strip() for b in fixed_bullets if b and b.strip()]
    base_paras = {p.text.strip() for p in docx.Document(str(resume_path)).paragraphs}
    missing_fixed = [b for b in fixed if b not in base_paras]
    if missing_fixed:          # checked before any Claude call, so a mismatch costs nothing
        raise FixedTextMismatch("Your base resume doesn't contain these fixed_bullets from settings.yaml character for "
                                "character, so no resume was made. Update the base resume or settings.yaml:\n- "
                                + "\n- ".join(missing_fixed))
    full_text = resume_text(resume_path)
    summary, skills, intro, groups, bullets = _resume_parts(resume_path)

    pins = {f"P{i}": p.strip() for i, p in enumerate(pinned_bullets, 1) if p and p.strip()}
    replaced = {}                                   # original bullet id -> pin id
    for pid, ptext in pins.items():
        for bid in _replaced_by_pin(ptext, bullets):
            replaced.setdefault(bid, pid)
    pin_home = {pid: next((bullets[b][0] for b, p in replaced.items() if p == pid), None) for pid in pins}
    replaced_text = {bid: bullets.pop(bid)[1] for bid in replaced}
    full_numbers = numbers_in(full_text) | numbers_in(" ".join(pins.values()))

    labels = [l for l, _ in skills]
    gids = [g for g, _ in groups]
    bids = list(bullets)

    view = ["SUMMARY (paragraphs):"] + [f"  [{i + 1}] {s}" for i, s in enumerate(summary)]
    view.append("\nSKILLS (label: items):")
    view += [f"  {l} " + " ; ".join(items) for l, items in skills]
    view.append(f"\nMETA INTRO: {intro}\n\nMETA SUBHEADINGS AND BULLETS:")
    for gid, heading in groups:
        view.append(f"  {gid}: {heading}")
        view += [f"     {bid}: {t}" for bid, (g, t) in bullets.items() if g == gid]
    if pins:
        view.append("\nPINNED BULLETS (must appear in the resume EXACTLY as written - copy the text "
                    "character for character, never reword or shorten. Put each one, with its ID as "
                    "source_id, under whichever Meta subheading is most relevant to this posting, at the "
                    "position that fits best. Never drop them.):")
        view += [f"  {pid}: {t}" for pid, t in pins.items()]
    if replaced_text:
        view.append("\n(Older wordings replaced by pinned bullets - do not use: " +
                    " | ".join(replaced_text.values()) + ")")
    bullet_why: Dict[str, str] = {}
    cand_text, cand_cite = _evidence_candidates(evidence)
    new_ids = ["N1", "N2", "N3"]
    view.append("\nTOP 3 RESPONSIBILITIES OF THE POSTING:\n" + "\n".join(
        f"  {i}. {r}" for i, r in enumerate(evidence.top_responsibilities, 1)))
    view.append("\nEVIDENCE CANDIDATES for new bullets (id: text):\n" + "\n".join(
        f"  {k}: {t}" for k, t in cand_text.items()))
    prompt = (f"FULL RESUME:\n{full_text}\n\n=== TAILORABLE PARTS (with IDs) ===\n" + "\n".join(view) +
              f"\n\n=== JOB POSTING ===\n{_posting_block(post)}\n\n=== {evidence.prompt_block()}\n\n"
              "Use the evidence passages to decide what to lead with. Every sentence you write must state only "
              "what the resume says AND what the evidence backs; numbers must match verified_metrics.txt exactly. "
              "Every sentence will be checked automatically and removed if unsupported.")
    out = ask_json(model, TAILOR_RULES + personal.prompt_block(), prompt, _tailor_schema(labels, gids, bids + list(pins), new_ids),
                   effort="high", max_tokens=32000)

    # ---- New bullets from evidence (code checks: real sources, numbers exactly as in the sources)
    new_b: Dict[str, Tuple[str, str, List[str]]] = {}
    new_rejected: List[str] = []
    for nb in out["new_bullets"][:3]:
        srcs = [x for x in nb["evidence_ids"] if x in cand_text]
        text = nb["text"].strip()
        extra = numbers_in(text) - numbers_in(" ".join(cand_text[x] for x in srcs))
        if nb["id"] in new_b or not text or not srcs:
            new_rejected.append(f'- {nb["id"]} rejected (no valid evidence ids): "{text}"')
        elif extra:
            new_rejected.append(f'- {nb["id"]} rejected (number not in its evidence: {", ".join(sorted(extra))}): "{text}"')
        else:
            new_b[nb["id"]] = (nb["group_id"], text, srcs)
            bullet_why[nb["id"]] = f'{nb["why"]} (matches: {nb["matches_responsibility"]})'
    rank = {r["id"]: (r["rank"], r["reason"]) for r in out["bullet_ranking"]}

    log: List[str] = []
    reverted: List[str] = []

    # ---- Summary (code checks)
    new_summary = [p.strip() for p in out["summary"]["paragraphs"] if p.strip()]
    orig_len = sum(len(s) for s in summary)
    problem = None
    if not 1 <= len(new_summary) <= 2:
        problem = "wrong number of paragraphs"
    elif sum(len(s) for s in new_summary) > orig_len * 1.15 + 40:
        problem = "too long"
    elif not numbers_in(" ".join(new_summary)) <= full_numbers:
        problem = "contains a number that isn't on the resume: " + ", ".join(
            sorted(numbers_in(" ".join(new_summary)) - full_numbers))
    if problem:
        reverted.append(f"Summary kept as original ({problem}).")
        new_summary = summary
    summary_why = out["summary"]["why"]

    # ---- Skills (code checks only - items must already exist)
    new_skills: List[Tuple[str, List[str]]] = []
    seen_labels = set()
    orig_skills = dict(skills)
    for s in out["skills"]:
        label = s["label"]
        if label in seen_labels:
            continue
        seen_labels.add(label)
        lookup = {_norm(i): i for i in orig_skills[label]}
        items, bad = [], []
        for it in s["items"]:
            key = _norm(it)
            if key in lookup and lookup[key] not in items:
                items.append(lookup[key])
            elif key not in lookup:
                bad.append(it)
        if bad or len(items) < (len(orig_skills[label]) + 1) // 2:
            reason = f"used items not on the resume: {', '.join(bad)}" if bad else "dropped too many items"
            reverted.append(f"Skills line '{label}' kept as original ({reason}).")
            items = orig_skills[label]
        new_skills.append((label, items))
        if items != orig_skills[label]:
            dropped = [i for i in orig_skills[label] if i not in items]
            log.append(f"- {label} reordered" + (f"; removed: {', '.join(dropped)}" if dropped else "") +
                       f". Why: {s['why']}")
    for label, items in skills:
        if label not in seen_labels:
            new_skills.append((label, items))
    if [l for l, _ in new_skills] != labels:
        log.insert(0, "- Skill lines reordered to: " + " | ".join(l for l, _ in new_skills))

    # ---- Meta intro
    new_intro = out["meta_intro"]["text"].strip() or intro
    if new_intro != intro and (len(new_intro) > len(intro) * 1.2 + 25 or not numbers_in(new_intro) <= numbers_in(intro)):
        reverted.append("Meta intro kept as original (too long or changed a number).")
        new_intro = intro

    # ---- Meta bullets
    used, plan_groups = set(), []
    seen_groups = set()
    for g in out["meta_groups"]:
        gid = g["group_id"]
        if gid in seen_groups:
            continue
        seen_groups.add(gid)
        items = []
        for b in g["bullets"]:
            bid, text = b["source_id"], b["text"].strip()
            if bid in new_b:                        # new bullet from evidence
                if bid not in used:
                    used.add(bid)
                    items.append([bid, new_b[bid][1]])
                continue
            if bid in pins:                         # pinned: any subheading, text forced exactly
                if bid not in used:
                    used.add(bid)
                    bullet_why[bid] = b["why"]
                    items.append([bid, pins[bid]])
                continue
            if bid not in bullets or bid in used or bullets[bid][0] != gid or not text:
                continue
            src = bullets[bid][1]
            if text != src:
                if len(text) > len(src) * 1.2 + 25:
                    reverted.append(f"{bid} kept as original (rewrite was too long).")
                    text = src
                elif not numbers_in(text) <= numbers_in(src):
                    reverted.append(f"{bid} kept as original (rewrite changed or added a number: "
                                    f"{', '.join(sorted(numbers_in(text) - numbers_in(src)))}).")
                    text = src
            used.add(bid)
            bullet_why[bid] = b["why"]
            items.append([bid, text])
        plan_groups.append([gid, items])
    for gid, _ in groups:
        if gid not in seen_groups:
            plan_groups.append([gid, []])
    drop_why = {d["source_id"]: d["why"] for d in out["dropped_bullets"]}
    for bid in [b for b in bids if b not in used]:     # every bullet competes in the ranking
        next(items for g, items in plan_groups if g == bullets[bid][0]).append([bid, bullets[bid][1]])
    for nid, (gid, text, _) in new_b.items():
        if nid not in used:
            next(items for g, items in plan_groups if g == gid).append([nid, text])
    for pid, ptext in pins.items():                 # a pinned bullet is never left out
        if pid not in used:
            gid = pin_home[pid] or gids[0]
            next(items for g, items in plan_groups if g == gid).insert(0, [pid, ptext])
            reverted.append(f"{pid} (pinned) was missing from the plan, so it was added at the top of "
                            f"'{dict(groups)[gid] or gid}'.")

    # ---- Independent fact-check of every changed line
    checks = []
    for i, p in enumerate(new_summary):
        if new_summary != summary:
            checks.append({"id": f"SUMMARY{i + 1}", "source": "(full resume above)", "new": p})
    if new_intro != intro:
        checks.append({"id": "INTRO", "source": intro, "new": new_intro})
    for _, items in plan_groups:
        for bid, text in items:
            if bid not in pins and bid not in new_b and text != bullets[bid][1]:   # pinned/new: not here
                checks.append({"id": bid, "source": bullets[bid][1], "new": text})
    verdicts = {}
    if checks:
        vprompt = (f"FULL ORIGINAL RESUME:\n{full_text}\n\nITEMS TO CHECK (JSON):\n" +
                   json.dumps(checks, indent=1, ensure_ascii=False))
        res = ask_json(model, VERIFY_RULES + personal.prompt_block(), vprompt, VERIFY_SCHEMA, effort="high")
        verdicts = {v["id"]: v for v in res["verdicts"]}
    unchecked = [c["id"] for c in checks if c["id"] not in verdicts]

    def failed(cid: str) -> str:
        if cid in unchecked:
            return "fact-check gave no verdict"
        v = verdicts.get(cid)
        return "" if (v is None or v["faithful"]) else v["problem"]

    summary_fail = [failed(f"SUMMARY{i + 1}") for i in range(len(new_summary)) if new_summary != summary]
    if any(summary_fail):
        reverted.append("Summary kept as original (fact-check: " + "; ".join(f for f in summary_fail if f) + ").")
        new_summary = summary
    if new_intro != intro and failed("INTRO"):
        reverted.append(f"Meta intro kept as original (fact-check: {failed('INTRO')}).")
        new_intro = intro
    for _, items in plan_groups:
        for item in items:
            bid = item[0]
            if bid in pins or bid in new_b:
                continue
            if item[1] != bullets[bid][1] and failed(bid):
                reverted.append(f"{bid} kept as original (fact-check: {failed(bid)}).")
                item[1] = bullets[bid][1]

    # ---- Evidence gate (career-evidence check_claim on every sentence; enforced here, in code)
    allowed = public_acronyms(post["company"], post["title"], post["description"])
    lines_now = ([("SUMMARY", i, p, summary[i] if i < len(summary) else "") for i, p in enumerate(new_summary)]
                 + ([("INTRO", 0, new_intro, intro)] if new_intro else [])
                 + [("BULLET", bid, text, bullets[bid][1] if bid in bullets else "") for _, items in plan_groups
                    for bid, text in items if bid not in pins])
    renamed = []
    sanitized = []
    scope_where = []
    translated_keys = set()                       # lines where an internal term was put in plain words
    for kind, key, text, orig in lines_now:
        text, ch = replace_codenames(text, evidence.rules)
        orig, _ = replace_codenames(orig, evidence.rules)
        text, used = apply_scope_term(text, evidence.rules, evidence.scope_term)
        orig, _ = apply_scope_term(orig, evidence.rules, evidence.scope_term)
        text, tr = translate_internal(text)
        orig, _ = translate_internal(orig)
        renamed += [f"{kind} {key}: {t}" for t in tr]
        if tr:
            translated_keys.add((kind, key))
        if used:
            scope_where.append(f"{kind.lower()} {key}" if kind != "SUMMARY" else "summary")
        renamed += [f"{kind} {key}: {c}" for c in ch]
        sanitized.append([kind, key, text, orig])
    def judge(sents):          # every resume sentence is a fact about you
        return judge_sentences(model, [(x, True) for x in sents], evidence, allowed, full_text)
    first = judge([s for _, _, t, _ in sanitized for s in split_sentences(t)])
    verdict = {v.sentence: v for v in first}
    # a rewritten line that fails goes back to the original wording, which is checked too
    retry = [ln for ln in sanitized if ln[2] != ln[3] and ln[3]
             and any(not verdict[s].ok for s in split_sentences(ln[2]))]
    rewrite_failed = []                           # Claude's rewrites the evidence didn't support
    for ln in retry:
        reverted.append(f"{ln[0]} {ln[1]} back to original wording (rewrite failed the evidence check).")
        rewrite_failed += [(f"{ln[0].lower()} {ln[1]}", x, verdict[x].reason)
                           for x in split_sentences(ln[2]) if not verdict[x].ok]
        ln[2] = ln[3]
    verdict.update({v.sentence: v for v in judge(
        [s for ln in retry for s in split_sentences(ln[2]) if s not in verdict])})
    evidence_log, removed_log = [], []
    gate_cut = set()                              # lines the evidence gate shortened or removed
    reverted_keys = {(ln[0], ln[1]) for ln in retry}
    final = {}
    for kind, key, text, _ in sanitized:
        keep = []
        for s in split_sentences(text):
            v = verdict[s]
            terms = internal_terms(s)
            if terms and v.ok:
                v = Judged(v.sentence, False, v.label, "Meta-internal term(s) not translated: " + ", ".join(terms),
                           v.source)
                verdict[s] = v
            if v.ok:
                keep.append(s)
                evidence_log.append(f"- {label_line(v)}\n    \"{s}\"")
            else:
                removed_log.append(f"- REMOVED from {kind.lower()} {key}: \"{s}\"\n    Why: {v.reason}")
        final[(kind, key)] = " ".join(keep)
        if final[(kind, key)] != text:
            gate_cut.add((kind, key))
    new_summary = [final[("SUMMARY", i)] for i in range(len(new_summary)) if final[("SUMMARY", i)]]
    if new_intro:
        new_intro = final[("INTRO", 0)]           # "" means the intro is removed
    for _, items in plan_groups:
        items[:] = [[b, (t if b in pins else final[("BULLET", b)])] for b, t in items
                    if b in pins or final[("BULLET", b)]]
    # ---- Keep the top 10 Meta bullets by match to the posting's top 3 responsibilities (pinned always kept)
    present = [b for _, items in plan_groups for b, _ in items]
    others = sorted([b for b in present if b not in pins], key=lambda b: rank.get(b, (999, ""))[0])
    keep_ids = set(pins) | set(others[:max(0, MAX_META_BULLETS - len([b for b in present if b in pins]))])
    dropped_rank = [b for b in others if b not in keep_ids]
    dropped_text = {b: t for _, items in plan_groups for b, t in items if b in dropped_rank}
    for _, items in plan_groups:
        items[:] = [it for it in items if it[0] in keep_ids]

    # ---- Summary rule: the second sentence names 2-3 program types in the posting's own terms
    def summary_rule(paras: List[str], types: List[str]) -> str:
        sents = split_sentences(" ".join(paras))
        second = sents[1] if len(sents) > 1 else ""
        posting = (post["title"] + " " + post["description"]).lower()
        if not 2 <= len(types) <= 3:
            return f"needs 2-3 program types, got {len(types)}"
        missing = [t for t in types if t.lower() not in second.lower() or t.lower() not in posting]
        if missing:
            return "not in the second sentence or not in the posting's words: " + ", ".join(missing)
        if VAGUE.search(second):
            return f'vague phrase "{VAGUE.search(second).group(0)}"'
        return ""
    summary_problem = summary_rule(new_summary, out["summary_program_types"])

    # pinned bullets and the sections tailoring never changes (AI Projects, earlier jobs, awards):
    # judged and reported only, never removed (one judge call for both)
    other = [x for t in _untailored_bullets(resume_path) for x in split_sentences(t)]
    both = judge(list(pins.values()) + other)
    pin_checks, other_checks = both[:len(pins)], both[len(pins):]

    # ---- Write it
    write_tailored_resume(resume_path, out_path, {
        "summary": new_summary, "skills": new_skills, "meta_intro": new_intro,
        "meta_groups": [(g, [(b, t) for b, t in items]) for g, items in plan_groups],
    })

    earlier_ok = _earlier_experience_unchanged(resume_path, out_path)
    written = {p.text for p in docx.Document(str(out_path)).paragraphs}
    for b in fixed:
        if b not in {w.strip() for w in written}:
            out_path.unlink(missing_ok=True)
            raise FixedTextMismatch(f"A fixed bullet changed during tailoring; the resume was not saved: {b[:80]}...")
    for pid, ptext in pins.items():
        if ptext not in written:
            out_path.unlink(missing_ok=True)
            raise PinnedBulletChanged(f"Pinned bullet {pid} is not in the tailored resume character for character; "
                                      "the resume was not saved.")

    # ---- Change log
    lines = ["PROFESSIONAL SUMMARY"]
    if any(k[0] == "SUMMARY" for k in reverted_keys | gate_cut):
        lines += ["- " + ("Claude's rewrite failed the evidence check, so the original wording was used. "
                          if any(k[0] == "SUMMARY" for k in reverted_keys) else "")
                  + ("Unsupported sentences were removed (see EVIDENCE CHECK below)."
                     if any(k[0] == "SUMMARY" for k in gate_cut) else ""),
                  "  BEFORE:"] + [f"    {x}" for x in summary] + ["  AFTER:"] + [f"    {x}" for x in new_summary]
    elif new_summary != summary:
        lines += ["- Rewritten for this role. Why: " + summary_why, "  BEFORE:"] + \
                 [f"    {s}" for s in summary] + ["  AFTER:"] + [f"    {s}" for s in new_summary]
    else:
        lines.append("- No change.")
    lines += ["", "CORE COMPETENCIES & TECHNICAL SKILLS"] + (log or ["- No change."])
    lines += ["", "META EXPERIENCE"]
    heading = dict(groups)
    order = [g for g, _ in plan_groups]
    if order != gids:
        lines.append("- Subheadings reordered to: " + " | ".join(heading[g] for g in order))
    if new_intro != intro:
        lines += [f"- Intro reworded. Why: {out['meta_intro']['why']}", f"  BEFORE: {intro}", f"  AFTER:  {new_intro}"]
    for bid, pid in replaced.items():
        lines += [f"- {bid} replaced by pinned bullet {pid} (same facts, your pinned wording).",
                  f"  REPLACED: {replaced_text[bid]}"]
    for gid, items in plan_groups:
        orig_order = [b for b, (g, _) in bullets.items() if g == gid]
        if [b for b, _ in items if b in bullets] != [b for b in orig_order if b in {x for x, _ in items}]:
            lines.append(f"- Bullets under '{heading[gid]}' reordered.")
        for bid, text in items:
            if bid in new_b:
                lines += [f"- {bid} NEW, written from your evidence, under '{heading[gid] or gid}'. "
                          f"Why: {bullet_why.get(bid, '')}", f"  NEW: {text}"]
                lines += [f"  Evidence: {cand_cite[x]}" for x in new_b[bid][2]]
            elif bid in pins:
                lines += [f"- {bid} (pinned, exactly as written) placed under '{heading[gid] or gid}'. "
                          f"Why: {bullet_why.get(bid, 'kept next to the bullets it replaced')}",
                          f"  PINNED: {text}"]
            elif ("BULLET", bid) in gate_cut:
                lines += [f"- {bid} shortened by the evidence check (see EVIDENCE CHECK below).",
                          f"  BEFORE: {bullets[bid][1]}", f"  AFTER:  {text}"]
            elif ("BULLET", bid) in translated_keys and translate_internal(bullets[bid][1])[0] == text:
                lines += [f"- {bid}: internal term put in plain words (see EVIDENCE CHECK below).",
                          f"  BEFORE: {bullets[bid][1]}", f"  AFTER:  {text}"]
            elif text != bullets[bid][1]:
                lines += [f"- {bid} reworded. Why: {bullet_why.get(bid, '')}",
                          f"  BEFORE: {bullets[bid][1]}", f"  AFTER:  {text}"]
    lines += ["", f"META BULLETS DROPPED (only the top {MAX_META_BULLETS} by match to the posting's top 3 "
                  "responsibilities are kept; pinned bullets are always kept)"]
    lines += [f"- {b} (rank {rank.get(b, (999, ''))[0] if b in rank else 'not ranked'}): "
              f"{rank.get(b, (0, drop_why.get(b, 'ranked below the top ' + str(MAX_META_BULLETS))))[1]}\n"
              f"  DROPPED: {dropped_text[b]}" for b in dropped_rank] or ["- None."]
    lines += new_rejected
    lines += ["", "SUMMARY RULE (second sentence names 2-3 program types in the posting's terms)"]
    lines.append(f"- Program types: {', '.join(out['summary_program_types'])}. "
                 + ("Met." if not summary_problem else f"NOT MET: {summary_problem}"))
    if len(lines) and lines[-1] == "META EXPERIENCE":
        lines.append("- No change.")
    lines += ["", "SAFETY CHECKS"]
    lines.append(f"- {len(checks)} changed line(s) were fact-checked against the original resume.")
    lines += [f"- REVERTED: {r}" for r in reverted] or ["- Nothing needed to be reverted."]
    lines += check_pinned_against_evidence(list(pins.values()))

    lines.append(f"- Experience before Meta: identical to your base resume ({earlier_ok}).")
    if fixed:
        lines.append(f"- Fixed bullets ({len(fixed)}, from settings.yaml): identical to your base resume, character "
                     "for character.")
    lines += ["", "SCOPE WORDING (team count)"] + (scope_note(evidence, [w for w in scope_where if w])
                                                 or ["- No scope rule found in verified_metrics.txt."])
    lines += ["", "EVIDENCE USED FOR THIS POSTING (career-evidence search_evidence)"]
    lines += [f"- \"{need}\": {n} passage(s)" for need, n in evidence.searched]
    lines += ["", "EVIDENCE CHECK - SUMMARY AND META (every sentence judged against the evidence; unsupported ones removed)"]
    lines += [f"- Codename or internal term replaced: {r}" for r in renamed] or ["- No codenames or internal terms to replace."]
    lines += ["", "REMOVED FOR LACK OF EVIDENCE - RESUME (if a claim here is true, add it to your evidence so it's "
                  "kept next time)"]
    lines += removed_log
    lines += [f"- Claude's rewrite of {where} was replaced by your original wording: \"{x}\"\n    Why: {why}"
              for where, x, why in rewrite_failed]
    if not removed_log and not rewrite_failed:
        lines.append("- Nothing was removed.")
    lines += ["", "EVERY SENTENCE KEPT, LABELED (fact: supported by document | type | review period | location)"]
    lines += evidence_log
    for pid, v in zip(pins, pin_checks):
        lines.append(f"- Pinned {pid} (kept word for word) - {label_line(v)}\n    \"{v.sentence}\""
                     + ("" if v.ok else f"\n    NOTE: check_claim says: {v.reason}"))
    lines += ["", "OTHER SECTIONS (not tailored, copied from your resume; checked and reported, not removed)"]
    flagged = [v for v in other_checks if not v.ok]
    lines += [f"- FLAGGED: \"{v.sentence}\"\n    Why: {v.reason}" for v in flagged]
    lines += [f"- {label_line(v)}\n    \"{v.sentence}\"" for v in other_checks if v.ok]
    return lines


def _untailored_bullets(path: Path) -> List[str]:
    """Bullet text outside the Meta section (AI Projects, earlier jobs, awards)."""
    from .resume import _is_bullet, map_resume
    d = docx.Document(str(path))
    m = map_resume(d)
    meta = {i for i, _, _ in m.meta_bullets.values()}
    return [p.text.strip() for i, p in enumerate(d.paragraphs)
            if _is_bullet(p) and i not in meta and p.text.strip()]


class EarlierExperienceChanged(RuntimeError):
    pass


def _earlier_experience_unchanged(base: Path, tailored: Path) -> str:
    """Experience before Meta (every earlier role) must stay exactly as in the base resume (verified_metrics.txt, CAREER AND
    SCOPE). Compares every paragraph after the Meta entry up to the next section heading,
    formatting included. If anything differs, the tailored resume is deleted and generation
    stops."""
    from lxml import etree
    from .resume import _is_heading, map_resume

    def block(path: Path) -> List[bytes]:
        d = docx.Document(str(path))
        m = map_resume(d)
        out = []
        for p in d.paragraphs[m.meta_end:]:
            if _is_heading(p):
                break
            out.append(etree.tostring(p._p))
        return out

    a, b = block(base), block(tailored)
    if not a:
        raise EarlierExperienceChanged("Couldn't find the experience entries after Meta in the base resume.")
    if a != b:
        tailored.unlink(missing_ok=True)
        raise EarlierExperienceChanged("Tailoring changed experience before Meta; the resume was not saved.")
    return f"{len(a)} paragraphs checked, formatting included"
