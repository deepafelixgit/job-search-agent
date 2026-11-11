"""Read the resume .docx, and write tailored copies of it.

Only three parts are ever changed: the Professional Summary, the Core
Competencies & Technical Skills lines, and the Meta experience section.
Everything else in the file is left byte-for-byte as it was.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import docx
from docx.oxml.ns import qn


def _is_bullet(p) -> bool:
    ppr = p._p.pPr
    return ppr is not None and ppr.numPr is not None


def _is_bold_lead(p) -> bool:
    runs = [r for r in p.runs if r.text]
    return bool(runs) and bool(runs[0].bold)


def _is_heading(p) -> bool:
    t = p.text.strip()
    return bool(t) and t.upper() == t and _is_bold_lead(p) and not _is_bullet(p) and "\t" not in t


def _is_company_header(p) -> bool:
    return "\t" in p.text and _is_bold_lead(p) and not _is_bullet(p)


def split_skill_items(text: str) -> List[str]:
    """Split 'A, B (x, y), C' on commas that are not inside parentheses."""
    items, depth, cur = [], 0, ""
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            items.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        items.append(cur.strip())
    return [i for i in items if i]


@dataclass
class ResumeMap:
    """Where each tailorable part lives (paragraph positions in the document)."""
    summary: List[int] = field(default_factory=list)
    skills: List[Tuple[int, str, List[str]]] = field(default_factory=list)   # (para, label, items)
    skill_sections: List[List[int]] = field(default_factory=list)            # skill-line paras per section
    meta_header: int = -1
    meta_intro: int = -1
    meta_groups: List[Tuple[str, int, str]] = field(default_factory=list)    # (group id, para, heading text)
    meta_bullets: Dict[str, Tuple[int, str, str]] = field(default_factory=dict)  # id -> (para, group id, text)
    meta_end: int = -1


def map_resume(document, company: str = "Meta") -> ResumeMap:
    paras = document.paragraphs
    m = ResumeMap()
    heads = {i: p.text.strip().upper() for i, p in enumerate(paras) if _is_heading(p)}

    def section_after(keyword: str) -> List[int]:
        start = next((i for i, t in heads.items() if keyword in t), None)
        if start is None:
            raise ValueError(f"Couldn't find a heading containing '{keyword}' in the resume.")
        out = []
        for i in range(start + 1, len(paras)):
            if i in heads:
                break
            if paras[i].text.strip():
                out.append(i)
            elif out:
                break
        return out

    m.summary = section_after("SUMMARY")
    # Skill lines ("Label: item, item") can live in one section ("CORE COMPETENCIES &
    # TECHNICAL SKILLS") or two ("CORE COMPETENCIES" and "TECHNICAL SKILLS").
    # Each section's lines are tailored but stay in their own section.
    skill_heads = []
    for kw in ("COMPETENCIES", "SKILLS"):
        h = next((i for i, t in heads.items() if kw in t), None)
        if h is not None and h not in skill_heads:
            skill_heads.append(h)
    if not skill_heads:
        raise ValueError("Couldn't find a Skills or Core Competencies heading in the resume.")
    for h in skill_heads:
        group = []
        for i in section_after(heads[h]):
            runs = [r for r in paras[i].runs if r.text]
            label = runs[0].text.strip()
            rest = "".join(r.text for r in runs[1:])
            m.skills.append((i, label, split_skill_items(rest)))
            group.append(i)
        m.skill_sections.append(group)

    hdr = next((i for i, p in enumerate(paras)
                if _is_company_header(p) and p.text.strip().lower().startswith(company.lower())), None)
    if hdr is None:
        raise ValueError(f"Couldn't find the '{company}' experience header in the resume.")
    m.meta_header = hdr
    i = hdr + 2                       # skip the job-title line
    if not _is_bullet(paras[i]) and not _is_bold_lead(paras[i]):
        m.meta_intro = i
        i += 1
    gid, gcount, bcount = None, 0, 0
    while i < len(paras) and not _is_company_header(paras[i]) and i not in heads:
        p = paras[i]
        if not p.text.strip():
            i += 1
            continue
        if _is_bullet(p):
            if gid is None:
                gcount += 1
                gid = f"G{gcount}"
                m.meta_groups.append((gid, -1, ""))
            bcount += 1
            m.meta_bullets[f"M{bcount}"] = (i, gid, p.text.strip())
        else:
            gcount += 1
            gid = f"G{gcount}"
            m.meta_groups.append((gid, i, p.text.strip()))
        i += 1
    m.meta_end = i
    return m


def resume_text(path: Path) -> str:
    """The whole resume as plain text (what Claude reads)."""
    d = docx.Document(str(path))
    lines = []
    for p in d.paragraphs:
        t = p.text.replace("\t", "  |  ").rstrip()
        lines.append(("• " + t) if (t and _is_bullet(p)) else t)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ------------------------------------------------------------ writing

def _set_text(p_el, text: str, keep_first_runs: int = 0) -> None:
    """Replace a paragraph's visible text, keeping its formatting.

    The first `keep_first_runs` runs (e.g. a bold "Label:" run) are left alone;
    the next run receives `text`; any later runs are removed."""
    runs = [r for r in p_el.findall(qn("w:r")) if r.find(qn("w:t")) is not None]
    if len(runs) <= keep_first_runs:
        raise ValueError("Paragraph doesn't have the expected structure.")
    target = runs[keep_first_runs]
    ts = target.findall(qn("w:t"))
    ts[0].text = text
    ts[0].set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    for t in ts[1:]:
        target.remove(t)
    for r in runs[keep_first_runs + 1:]:
        r.getparent().remove(r)


def write_tailored_resume(src: Path, dst: Path, plan: dict, company: str = "Meta") -> None:
    """plan = {
        "summary": [paragraph text, ...],
        "skills": [(label, [items]), ...]          # order = new order
        "meta_intro": str or None,
        "meta_groups": [(group_id, [(bullet_id, text), ...]), ...]  # order = new order
    }"""
    d = docx.Document(str(src))
    m = map_resume(d, company)
    P = [p._p for p in d.paragraphs]

    # --- Summary
    els = [P[i] for i in m.summary]
    texts = plan["summary"]
    for idx, text in enumerate(texts):
        if idx < len(els):
            _set_text(els[idx], text)
        else:
            new = copy.deepcopy(els[-1])
            _set_text(new, text)
            els[-1].addnext(new)
            els.append(new)
    for extra in els[len(texts):]:
        extra.getparent().remove(extra)

    # --- Skills (reorder lines within their own section, rewrite items; label run untouched)
    label_of = {i: label for i, label, _ in m.skills}
    new_items = dict(plan["skills"])
    plan_order = [label for label, _ in plan["skills"]]
    for group in m.skill_sections:
        anchor = P[group[0]].getprevious()
        here = [label_of[i] for i in group]
        ordered = [l for l in plan_order if l in here] + [l for l in here if l not in plan_order]
        by_label = {label_of[i]: P[i] for i in group}
        for el in by_label.values():
            el.getparent().remove(el)
        for label in ordered:
            el = by_label[label]
            if label in new_items:
                first = next(t.text or "" for t in el.iter(qn("w:t")))
                gap = "" if first.endswith((" ", "\t")) else " "      # keep "Label: items" spacing
                _set_text(el, gap + ", ".join(new_items[label]), keep_first_runs=1)
            anchor.addnext(el)
            anchor = el

    # --- Meta
    intro_el = P[m.meta_intro] if m.meta_intro >= 0 else None
    if plan.get("meta_intro") and intro_el is not None:
        _set_text(intro_el, plan["meta_intro"])
    elif plan.get("meta_intro") == "" and intro_el is not None:
        intro_el = None                       # removed by the evidence check (anchor below skips it)
    group_el = {gid: (P[i] if i >= 0 else None) for gid, i, _ in m.meta_groups}
    bullet_el = {bid: P[i] for bid, (i, _, _) in m.meta_bullets.items()}
    for el in list(group_el.values()) + list(bullet_el.values()):
        if el is not None:
            el.getparent().remove(el)
    anchor = P[m.meta_header + 1]                      # the job-title line
    if m.meta_intro >= 0:
        if intro_el is None:
            P[m.meta_intro].getparent().remove(P[m.meta_intro])
        else:
            anchor = intro_el
    for gid, bullets in plan["meta_groups"]:
        if not bullets:
            continue                         # a subheading whose bullets were all dropped is left out
        if group_el.get(gid) is not None:
            anchor.addnext(group_el[gid])
            anchor = group_el[gid]
        for bid, text in bullets:
            # a pinned bullet (not in the original) borrows the formatting of an existing bullet
            el = copy.deepcopy(bullet_el.get(bid, next(iter(bullet_el.values()))))
            _set_text(el, text)
            anchor.addnext(el)
            anchor = el

    normalize_meta_bullet_spacing(d, company)
    dst.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(dst))


def _spacing(p_el):
    ppr = p_el.get_or_add_pPr()
    sp = ppr.find(qn("w:spacing"))
    if sp is None:
        sp = ppr.makeelement(qn("w:spacing"), {})
        ppr.append(sp)
    return sp


def normalize_meta_bullet_spacing(document, company: str = "Meta") -> None:
    """Give Meta bullets uniform spacing by POSITION, as in the base resume: extra space
    above the first bullet under each subheading, extra space below the last one, none
    in between. (Each bullet's own spacing would otherwise travel with it when tailoring
    reorders bullets.)"""
    m = map_resume(document, company)
    paras = document.paragraphs
    groups: Dict[str, List] = {}
    for _, (i, gid, _) in m.meta_bullets.items():
        groups.setdefault(gid, []).append(paras[i]._p)
    before = after = None
    for els in groups.values():                     # read the pattern from the document itself
        if len(els) >= 2:
            before = before or els[0].pPr.find(qn("w:spacing")).get(qn("w:before")) \
                if els[0].pPr is not None and els[0].pPr.find(qn("w:spacing")) is not None else before
            after = after or els[-1].pPr.find(qn("w:spacing")).get(qn("w:after")) \
                if els[-1].pPr is not None and els[-1].pPr.find(qn("w:spacing")) is not None else after
    before, after = before or "240", after or "240"
    for els in groups.values():
        for k, el in enumerate(els):
            sp = _spacing(el)
            for attr, val, on in ((qn("w:before"), before, k == 0), (qn("w:after"), after, k == len(els) - 1)):
                if on:
                    sp.set(attr, val)
                elif attr in sp.attrib:
                    del sp.attrib[attr]


def write_cover_letter(template: Path, dst: Path, date_line: str, greeting: str,
                       paragraphs: List[str], closing: str, name: str) -> None:
    """Build the cover letter from the resume file so it shares the same
    fonts, margins and name/contact header."""
    d = docx.Document(str(template))
    paras = d.paragraphs
    m = map_resume(d)
    body_template = copy.deepcopy(paras[m.summary[0]]._p)
    header_end = 2   # name line + contact line
    while header_end < len(paras) and paras[header_end].text.strip() and not _is_heading(paras[header_end]):
        header_end += 1
    for p in paras[header_end:]:
        p._p.getparent().remove(p._p)

    def add(text: str, space_after: bool = True) -> None:
        el = copy.deepcopy(body_template)
        _set_text(el, text)
        d.element.body.insert(len(d.element.body) - 1, el)   # before the section settings
        if not space_after:
            return
        blank = copy.deepcopy(body_template)
        _set_text(blank, "")
        d.element.body.insert(len(d.element.body) - 1, blank)

    add("", space_after=False)
    add(date_line)
    add(greeting)
    for para in paragraphs:
        add(para)
    add(closing, space_after=False)
    add(name, space_after=False)
    dst.parent.mkdir(parents=True, exist_ok=True)
    d.save(str(dst))
