"""Evidence gate: connects resume and cover-letter tailoring to the career-evidence tools.

Every step here runs in code, not at the model's discretion:
  1. find_evidence()   - Claude lists what the posting asks for; the code searches
                         the evidence for each item (search_evidence).
  2. drafting          - the drafting prompts receive only those passages (plus the
                         resume, which is itself an evidence document).
  3. gate              - the code runs check_claim on every sentence about the
                         candidate. Anything not "supporting evidence found" is
                         removed. Codenames are replaced with commercial names,
                         unexplained internal acronyms are blocked, and every kept
                         sentence gets a citation (document, review period, location).

The checks call the same functions the career-evidence MCP server gives Claude
Code (mcp_server/batch.py runs them in the server's own Python environment).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .claude_client import ask_json
from . import personal
from .config import ROOT

MCP_PYTHON = ROOT / ".venv-mcp" / "bin" / "python"
SUPPORTED = "supporting evidence found"

# Acronyms any reader understands. Anything else in capitals (e.g. internal team or
# metric names) must be explained in the same sentence, e.g. "Build Success Rate (BSR)".
COMMON_ACRONYMS = {
    "AI", "ML", "AR", "VR", "MR", "XR", "CI", "CD", "OTA", "NPI", "EE", "API", "APIs", "MCP", "RAG", "LLM",
    "LLMs", "VP", "VPs", "SVP", "US", "USA", "MBA", "OS", "KPI", "KPIs", "OKR", "OKRs", "PM", "TPM", "TPMs",
    "EPM", "QA", "UX", "UI", "IC", "SoC", "MCU", "BSP", "FW", "HW", "SW", "SDK", "GTM", "CEO", "CTO", "CFO",
    "PhD", "EMEA", "APAC", "IT", "CV", "NLP", "GPU", "GPUs", "CPU", "SQL", "AWS", "IoT", "RF", "PCB", "SLA",
    "SLAs", "R&D", "SaaS", "BLE", "USB", "LTE", "GPS", "ASIC", "FPGA", "P&L", "CI/CD", "DRI", "DRIs",
    "EVT", "DVT", "PVT", "iOS", "ID", "OEM", "ODM", "HR", "ROI", "IP", "PR", "UK", "EU", "AV", "ADAS",
}

FIRST_PERSON_CLAIM = re.compile(
    r"\b(I|we)\s+(?:also\s+|have\s+|had\s+)?(led|built|drove|owned|managed|ran|scaled|cut|raised|reduced|"
    r"delivered|launched|created|designed|shipped|grew|defined|negotiated|improved|instituted|pioneered|"
    r"partnered|aligned|established|introduced|turned|took|kept|served|helped|halved|increased)\b|\bI'?ve\b|"
    r"\bmy (team|teams|program|programs|work|role)\b", re.I)

TITLE_PATTERN = re.compile(
    r"\b((?:Senior|Sr\.?|Staff|Principal|Lead|Group|Head of|Director(?: of)?)\s+)?"
    r"(Technical Program Manager|Program Manager|Engineering Manager|Product Manager|TPM|Director)\b")


# Employer-internal shorthand that must never reach a resume or letter (rule: plain language only).
# The lists are personal, so they live in settings.yaml under `personal:` (see agent/personal.py).
EXPLAINED = r"[^.]*?(\(|, (meaning|i\.e\.|\w+ that)|which means|—|-- )"


def internal_terms(sentence: str) -> List[str]:
    """Internal shorthand in a sentence (personal.explain_terms are allowed only with an explanation)."""
    found = [t for t in personal.internal_terms() if re.search(rf"(?<![\w-]){re.escape(t)}s?(?![\w-])", sentence,
                                                               0 if t.isupper() else re.I)]
    for t in personal.explain_terms():
        if re.search(re.escape(t), sentence, re.I) and not re.search(re.escape(t) + EXPLAINED, sentence, re.I):
            found.append(f"{t} (needs a short plain-language explanation)")
    return found



class EvidenceError(RuntimeError):
    pass


def run_batch(payload: dict) -> dict:
    """Call the career-evidence tools (same code as the MCP server). Fails loudly:
    without the checks, nothing is generated."""
    if not MCP_PYTHON.exists():
        raise EvidenceError(f"The career-evidence environment is missing ({MCP_PYTHON}).")
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    proc = subprocess.run([str(MCP_PYTHON), "-m", "mcp_server.batch"], input=json.dumps(payload),
                          capture_output=True, text=True, cwd=str(ROOT), env=env, timeout=300)
    if proc.returncode != 0:
        raise EvidenceError("The career-evidence check failed: " + proc.stderr.strip()[-500:])
    return json.loads(proc.stdout)


# ------------------------------------------------------------------ names and acronyms

def _clean_name(name: str) -> str:
    """'the Example lab (say "Example lab")' -> 'Example lab';
    'for the build metric, say "the Platform org"; elsewhere, "the Devices division"' -> 'the Devices division'"""
    if "elsewhere" in name:
        q = re.findall(r'"([^"]+)"', name.split("elsewhere", 1)[1])
        if q:
            return q[0]
    q = re.findall(r'"([^"]+)"', name)
    if q:
        return q[0]
    return re.split(r"[;(]", name)[0].strip()


@dataclass
class Rules:
    codenames: Dict[str, str] = field(default_factory=dict)       # codename -> commercial name
    phrases_to_avoid: List[str] = field(default_factory=list)
    rules: List[str] = field(default_factory=list)
    scope_count: str = ""                     # e.g. "10+" (CAREER AND SCOPE section)
    scope_terms: List[str] = field(default_factory=list)   # e.g. ["engineering teams", "stakeholders"]
    scope_line: int = 0
    facts: List[str] = field(default_factory=list)          # usable verified metrics, "line N: ..."

    def facts_block(self) -> str:
        return ("VERIFIED METRICS (verified_metrics.txt; the only source for numbers and scope - teams, "
                "executive reviews, budgets, programs; [CONFIRM] lines are already excluded):\n"
                + "\n".join(f"- {f}" for f in self.facts))

    def prompt_block(self) -> str:
        return ("RULES FROM verified_metrics.txt (authoritative; overrides every other document):\n"
                + "\n".join(f"- {r}" for r in self.rules)
                + "\nPhrases to avoid:\n" + "\n".join(f"- {p}" for p in self.phrases_to_avoid)
                + "\nInternal codenames -> names to use instead (never write the codename):\n"
                + "\n".join(f"- {c} -> {n}" for c, n in self.codenames.items())
                + "\nNever write internal team names or internal metric names/acronyms unless the sentence "
                  "explains them in plain words.")


def rules_from(metrics: dict) -> Rules:
    names = {}
    for code, name in (metrics.get("codenames_internal_to_commercial") or {}).items():
        names[code.split(" (")[0].strip()] = _clean_name(name)
    r = Rules(codenames=names, phrases_to_avoid=metrics.get("phrases_to_avoid") or [],
              rules=metrics.get("rules") or [])
    r.facts = [f"line {it.get('line')}: [{it.get('section')} / {it.get('program')}] {it.get('fact')}"
               for it in metrics.get("metrics") or []]
    for it in metrics.get("metrics") or []:
        m = re.search(r"Worked with (\d+\+?) .*?Call them \"([^\"]+)\" or \"([^\"]+)\"", it.get("fact", ""))
        if m:
            r.scope_count, r.scope_terms, r.scope_line = m.group(1), [m.group(2), m.group(3)], it.get("line", 0)
    return r


def apply_scope_term(text: str, rules: "Rules", term: str) -> Tuple[str, bool]:
    """Make every mention of the team count use the chosen wording, e.g.
    '10+ engineering teams' <-> '10+ stakeholders'."""
    if not term or not rules.scope_count:
        return text, False
    words = sorted({w for t in rules.scope_terms for w in [t, t.replace("stakeholders", "stakeholder teams")]}
                   | {"engineering and stakeholder teams", "teams"}, key=len, reverse=True)
    pat = re.compile(rf"(?<![\w.]){re.escape(rules.scope_count)}\s+(?:{'|'.join(re.escape(w) for w in words)})\b",
                     re.I)
    new = pat.sub(f"{rules.scope_count} {term}", text)
    return new, bool(pat.search(text))


def replace_codenames(text: str, rules: Rules) -> Tuple[str, List[str]]:
    """Swap any internal codename (any capitalisation) for its commercial name."""
    changed = []
    for code, name in rules.codenames.items():
        last = name.split()[-1]
        # "codename triage" -> "device issue triage", not "... triage triage"
        pat = re.compile(rf"\b{re.escape(code)}\b(\s+{re.escape(last)}\b)?", re.I)
        if pat.search(text):
            text = pat.sub(name, text)
            changed.append(f"{code} -> {name}")
    return text, changed


def unexplained_acronyms(sentence: str, allowed: set) -> List[str]:
    out = []
    toks = [t for raw in re.findall(r"\b[A-Za-z]*[A-Z][A-Za-z&/]*[A-Z][A-Za-z]*\b", sentence)
            for t in (raw.split("/") if raw not in COMMON_ACRONYMS else [raw])]
    for tok in toks:
        if tok in allowed or tok in COMMON_ACRONYMS or tok.rstrip("s") in COMMON_ACRONYMS:
            continue
        if not re.search(r"[A-Z].*[A-Z]", tok) or tok[1:].islower():
            continue
        # explained: "Build Success Rate (BSR)" or "BSR (Build Success Rate)"
        if re.search(rf"\({re.escape(tok)}\)|\b{re.escape(tok)}\s*\([A-Za-z][^)]{{3,}}\)", sentence):
            continue
        out.append(tok)
    return out


def public_acronyms(*texts: str) -> set:
    """Acronyms used in public text (the posting, the company name) are fine to repeat."""
    return {t for txt in texts for raw in re.findall(r"\b[A-Za-z]*[A-Z][A-Za-z&/]*[A-Z][A-Za-z]*\b", txt or "")
            for t in [raw] + raw.split("/")}


# ------------------------------------------------------------------ step 1: find evidence

REQ_SCHEMA = {"type": "object", "properties": {"needs": {"type": "array", "items": {"type": "string"}}},
              "required": ["needs"], "additionalProperties": False}
REQ_RULES = ("List the 6 to 8 most important things this job posting asks a candidate to have done or be "
             "able to do. Each item: 3 to 8 plain keywords someone would search a performance review for "
             "(e.g. 'cross-functional hardware launch readiness', 'test infrastructure scaling devices'). "
             "No full sentences.")


@dataclass
class PostingEvidence:
    needs: List[str]
    passages: List[dict]                      # deduplicated, each with an "id" (E1, E2...)
    rules: Rules
    searched: List[Tuple[str, int]]           # (need, number of passages found)
    scope_term: str = ""                      # chosen wording for the team count
    scope_why: str = ""
    top_responsibilities: List[str] = field(default_factory=list)   # the posting's top 3, in its words
    excluded: List[Tuple[str, str]] = field(default_factory=list)   # (passage, why it was left out)

    def prompt_block(self) -> str:
        lines = ["EVIDENCE PASSAGES FOUND FOR THIS POSTING (the only facts about the candidate you may use, "
                 "besides the resume; each cites its source):"]
        for p in self.passages:
            who = f", reviewer: {p['reviewer']}" if p.get("reviewer") else ""
            lines.append(f"[{p['id']}] ({p['source']} | {p['doc_type']} | {p['period']} | {p['location']}{who})\n"
                         f"    {p['text']}")
        scope = (f"\nWhen mentioning the {self.rules.scope_count} teams, write exactly "
                 f"\"{self.rules.scope_count} {self.scope_term}\" (chosen for this posting)."
                 if self.scope_term else "")
        return "\n".join(lines) + "\n\n" + self.rules.prompt_block() + scope


# Review text that isn't evidence of results: growth areas, feedback, goals, aspirations, training asks.
NOT_EVIDENCE = re.compile(
    r"could be doing better|things? .{0,20}could .{0,10}better|areas? (to|for|of) improv|improvement areas?|"
    r"increase (their|her|my) impact|grow or improve|growth areas?|development areas?|new experiences|career goals?|"
    r"career aspirations?|want to work toward|becoming a people manager|developing new skills|Get to IC\d|"
    r"would like to (learn|grow|restart)|(request|requests|requested|asked) (for )?training|training (request|course)|"
    r"what new experiences|how can \w+ increase", re.I)

DEVELOP_HEADING = re.compile(r"\bDEVELOP\b")          # the review form's all-caps DEVELOP section only

FILTER_RULES = """You screen passages from a candidate's performance reviews and records before they are used as \
evidence for a cover letter. KEEP a passage only if it states results, impact, a decision the candidate made, \
or scope (teams, programs, budgets, reviews). EXCLUDE it if it is mainly:
- "growth": growth areas, feedback on what to improve, development goals, career aspirations, training requests;
- "single_task": a single-task or single-ticket win (e.g. unblocking one request, installing one batch, one meeting);
- "acronyms": its meaning depends on internal acronyms or codenames that it doesn't explain;
- "not_evidence": template text, review-form questions, intro sentences ("The following examples..."), or \
statements with no result, decision or scope.
Verified-metrics lines are the candidate's own checked record: keep them unless they are single-task wins."""

FILTER_SCHEMA = {"type": "object", "properties": {"verdicts": {"type": "array", "items": {"type": "object", "properties": {
    "id": {"type": "string"}, "keep": {"type": "boolean"},
    "category": {"type": "string", "enum": ["result", "impact", "decision", "scope", "growth", "single_task",
                                            "acronyms", "not_evidence"]},
    "reason": {"type": "string"}}, "required": ["id", "keep", "category", "reason"], "additionalProperties": False}}},
    "required": ["verdicts"], "additionalProperties": False}


def _internal_tokens(text: str, codes: set) -> List[str]:
    """Internal-looking terms (2+ capitals, digits allowed: ABC, XY2, AbcOS) that aren't common acronyms,
    codenames (those get swapped for commercial names) or explained in the passage."""
    out = []
    for t in re.findall(r"\b[A-Za-z]*[A-Z][A-Za-z0-9&]*[A-Z0-9][A-Za-z0-9]*\b", text):
        if sum(ch.isupper() for ch in t) < 2 or t in COMMON_ACRONYMS or t.rstrip("s") in COMMON_ACRONYMS:
            continue
        if t.lower() in codes or re.fullmatch(r"[A-Z]\d+|\d+[A-Z]+", t) or t.isdigit():   # H1, Q3, 4K
            continue
        if re.search(rf"\({re.escape(t)}\)|\b{re.escape(t)}\s*\([A-Za-z][^)]{{3,}}\)", text):
            continue
        out.append(t)
    return sorted(set(out))


def filter_evidence(model: str, cands: List[Tuple[str, dict]]) -> Tuple[List[Tuple[str, dict]], List[Tuple[str, str]]]:
    """Code drops growth/feedback/aspiration passages by their headings and wording; Claude then labels
    the rest, and only results, impact, decisions and scope are kept."""
    kept, excluded, rest = [], [], []
    codes = {c.lower() for c in personal.codename_words()}
    for label, h in cands:
        cite = _cite_passage(h) + f' - "{h["text"][:90]}..."'
        acr = [] if h["doc_type"] == "verified metrics" else _internal_tokens(h["text"], codes)
        if NOT_EVIDENCE.search(h["location"]) or NOT_EVIDENCE.search(h["text"]) or DEVELOP_HEADING.search(h["location"]):
            excluded.append((cite, "growth/feedback/goals section (code)"))
        elif len(set(acr)) >= 2:
            excluded.append((cite, "acronyms (code): depends on unexplained internal terms " + ", ".join(sorted(set(acr)))))
        else:
            rest.append((label, h))
    if not rest:
        return kept, excluded
    prompt = "\n".join(f"[C{i}] ({_cite_passage(h)}) {h['text']}" for i, (_, h) in enumerate(rest, 1))
    verdicts = {v["id"]: v for v in ask_json(model, FILTER_RULES, prompt, FILTER_SCHEMA, effort="medium",
                                              max_tokens=16000)["verdicts"]}
    for i, (label, h) in enumerate(rest, 1):
        v = verdicts.get(f"C{i}")
        if v and v["keep"] and v["category"] in ("result", "impact", "decision", "scope"):
            kept.append((label, h))
        else:
            excluded.append((_cite_passage(h) + f' - "{h["text"][:90]}..."',
                             f"{v['category']}: {v['reason']}" if v else "no verdict"))
    return kept, excluded


def find_evidence(model: str, post, filter_model: str = "") -> PostingEvidence:
    rules = rules_from(run_batch({"metrics": True})["metrics"])
    schema = {"type": "object", "properties": {
        "needs": {"type": "array", "items": {"type": "string"}},
        "top_responsibilities": {"type": "array", "items": {"type": "string"}}},
        "required": ["needs", "top_responsibilities"], "additionalProperties": False}
    system = (REQ_RULES + " Also list the posting's top 3 responsibilities (what the person in this role will "
              "do), each quoted or closely paraphrased from the posting, most important first.")
    if rules.scope_terms:
        schema["properties"].update({"scope_term": {"type": "string", "enum": rules.scope_terms},
                                     "scope_why": {"type": "string"}})
        schema["required"] = schema["required"] + ["scope_term", "scope_why"]
        system = (system + f" Also choose how to describe the candidate's {rules.scope_count} teams: "
                  + " or ".join(f'"{t}"' for t in rules.scope_terms) + ", whichever fits this posting better "
                  "(e.g. 'engineering teams' when it stresses engineering execution and technical depth, "
                  "'stakeholders' when it stresses cross-functional or business stakeholder management), "
                  "and give a one-sentence reason tied to the posting.")
    reply = ask_json(model, system, f"Title: {post['title']}\n\n{post['description']}", schema,
                     effort="low", max_tokens=4000)
    needs = reply["needs"][:8]
    top = reply.get("top_responsibilities", [])[:3]
    queries = needs + top
    labels = needs + [f"(top responsibility) {r}" for r in top]
    # each query twice: verified_metrics.txt only (preferred), then all evidence
    res = run_batch({"search": [q for x in queries for q in (
        {"query": x, "doc_type": "verified metrics", "max_results": 3}, {"query": x, "max_results": 8})]})
    found = res["search"]
    seen, cands = set(), []                     # (label, passage)
    for i, label in enumerate(labels):
        for h in found[2 * i].get("results", []) + found[2 * i + 1].get("results", []):
            key = (h["source"], h["location"], h["text"][:80])
            if key not in seen:
                seen.add(key)
                cands.append((label, h))
    kept, excluded = filter_evidence(filter_model or model, cands)
    per_label: Dict[str, List[dict]] = {}
    for label, h in kept:
        per_label.setdefault(label, []).append(h)
    passages, searched = [], []
    for label in labels:
        hits = sorted(per_label.get(label, []), key=lambda h: (h["doc_type"] != "verified metrics",
                                                               "TRADE-OFFS" not in h["location"]))[:4]
        searched.append((label, len(hits)))
        for h in hits:
            passages.append(dict(h, id=f"E{len(passages) + 1}"))
    return PostingEvidence(needs=needs, passages=passages, rules=rules, searched=searched,
                           scope_term=reply.get("scope_term", ""), scope_why=reply.get("scope_why", ""),
                           top_responsibilities=top, excluded=excluded)


def scope_note(ev: PostingEvidence, where: List[str]) -> List[str]:
    """changes.txt lines explaining the team-count wording."""
    if not ev.scope_term:
        return []
    out = [f"- Chose \"{ev.rules.scope_count} {ev.scope_term}\" (verified_metrics.txt, CAREER AND SCOPE, "
           f"line {ev.rules.scope_line}). Why: {ev.scope_why}"]
    out.append("  Used in: " + ", ".join(where) if where else "  Not mentioned in this document.")
    return out


# ------------------------------------------------------------------ step 3: check every sentence

ABBREVIATIONS = ("Sr.", "Jr.", "Dr.", "Mr.", "Ms.", "Mrs.", "St.", "Inc.", "Ltd.", "Co.", "vs.", "e.g.",
                 "i.e.", "etc.", "No.", "approx.")


def split_sentences(text: str) -> List[str]:
    """Split on sentence ends, but not after abbreviations like "Sr." or "U.S."."""
    text = text.strip()
    out, start = [], 0
    for m in re.finditer(r"(?<=[.!?])\s+(?=[A-Z\"'(\u201c])", text):
        before = text[start:m.start()]
        last = before.split()[-1] if before.split() else ""
        if last in ABBREVIATIONS or re.fullmatch(r"(?:[A-Z]\.){1,3}", last):   # Sr. / U.S. / J.
            continue
        out.append(before.strip())
        start = m.end()
    out.append(text[start:].strip())
    return [x for x in out if x]


def citation(result: dict) -> str:
    """Best source for a checked claim: verified metrics first, then reviews, then the resume."""
    cands = list(result.get("verified_metrics_matches") or [])
    others = list(result.get("other_supporting_passages") or [])
    cands += [p for p in others if p.get("doc_type") != "resume"] + [p for p in others if p.get("doc_type") == "resume"]
    if not cands:
        return "no source found"
    p = cands[0]
    who = f", {p['reviewer']}" if p.get("reviewer") else ""
    return f"{p['source']} | {p.get('doc_type', '')} | {p.get('period', '')} | {p.get('location', '')}{who}"


@dataclass
class Verdict:
    sentence: str
    ok: bool
    reason: str
    source: str


def check_sentences(sentences: List[str], allowed_acronyms: set) -> List[Verdict]:
    """check_claim on every sentence (one batch). A sentence passes only with the
    verdict "supporting evidence found", no codename warnings and no unexplained
    internal acronyms."""
    if not sentences:
        return []
    results = run_batch({"check": sentences})["check"]
    out = []
    for s, r in zip(sentences, results):
        problems = []
        if r["verdict"] != SUPPORTED:
            problems.append(r["verdict"] + (": " + "; ".join(r["problems"]) if r["problems"] else ""))
        problems += r.get("warnings") or []
        acr = unexplained_acronyms(s, allowed_acronyms)
        if acr:
            problems.append("internal term(s) not explained: " + ", ".join(acr))
        out.append(Verdict(s, not problems, "; ".join(problems), citation(r)))
    return out


def titles_conflicting(sentence: str, resume_text: str) -> List[str]:
    """Job titles claimed in a sentence that don't appear in the resume."""
    bad = []
    for m in TITLE_PATTERN.finditer(sentence):
        t = m.group(0).strip()
        if t.lower() not in resume_text.lower():
            bad.append(t)
    return bad


FIRST_PERSON = re.compile(r"\b(I|I'm|I'd|I've|my|me|we|our)\b", re.I)


def is_claim_about_candidate(sentence: str, kind: str) -> bool:
    """The model labels each sentence, but code decides what must be checked:
    anything labeled 'experience', anything that reads like a first-person
    accomplishment, and any number in a sentence about the candidate (first person
    or naming a past employer or program), whatever its label."""
    if kind == "experience" or FIRST_PERSON_CLAIM.search(sentence):
        return True
    named = personal.experience_match(sentence)          # an employer, product or school of the candidate's
    about_me = FIRST_PERSON.search(sentence) or named
    return bool(re.search(r"\d", sentence) and about_me) or bool(named)


# ------------------------------------------------------------------ meaning-based judgment

JUDGE_RULES = """You check sentences from a candidate's resume or cover letter against the candidate's evidence.

For each sentence decide its TYPE:
- "fact": anything about the candidate's own record - numbers, scope (teams, budgets, programs, reviews), \
titles, employers, programs, actions, results, decisions made, trade-offs weighed, skills used.
- "perspective": about the company, the role, the industry, or the candidate's view of how their experience \
applies to what the company will need; interest in the role; a proposal to talk.
If a sentence is marked must_be_fact, it is a fact.

FACT: "supported" is true only if the evidence states the same facts - different words are fine. In "added", \
list every number, scope, title, outcome or decision the sentence states that the evidence does NOT contain, \
including upgraded ownership (evidence says "partnered", sentence says "led"), merged programs, or a result \
attributed to an action the evidence doesn't connect it to. If "added" is not empty, "supported" is false. In \
"sources", give the ids of the passages or verified-metrics lines that state the facts (most specific first).

PERSPECTIVE: "supported" is true. In "added", list any fact about the candidate's past that the evidence does \
not contain (a perspective may mention the candidate's past only with facts the evidence states). Give sources \
for any past facts it does mention.

NOT NEW FACTS (never list these in "added"), as long as they add no number, scope, title, outcome or decision:
- a plain-language explanation of an internal term (e.g. what an internal build label means, what a metric measures);
- a duration calculated from the resume's dates (e.g. "four years at Acme" from "2019 - 2023").

COMPANY FACTS: any specific fact about the company in any sentence (a product, launch, number, date, event or \
plan) must appear in the JOB POSTING or VERIFIED NEWS provided; otherwise list it in "added".

Be strict about facts and generous about wording."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {"verdicts": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "string"},
        "type": {"type": "string", "enum": ["fact", "perspective"]},
        "supported": {"type": "boolean"},
        "added": {"type": "array", "items": {"type": "string"}},
        "sources": {"type": "array", "items": {"type": "string"}},
        "note": {"type": "string"}},
        "required": ["id", "type", "supported", "added", "sources", "note"], "additionalProperties": False}}},
    "required": ["verdicts"], "additionalProperties": False,
}


@dataclass
class Judged:
    sentence: str
    ok: bool
    label: str          # "fact" or "perspective"
    reason: str
    source: str


def _cite_passage(p: dict) -> str:
    who = f", {p['reviewer']}" if p.get("reviewer") else ""
    return f"{p['source']} | {p.get('doc_type', '')} | {p.get('period', '')} | {p.get('location', '')}{who}"


def _verified_lines(rules: Rules) -> List[Tuple[str, str, str]]:
    """[(id, citation, text)] for every usable verified-metrics line."""
    out = []
    for f in rules.facts:
        m = re.match(r"line (\d+): \[(.*?)\] (.*)", f, re.S)
        if m:
            out.append((f"V{m.group(1)}", f"verified_metrics.txt | {m.group(2)} | line {m.group(1)}", m.group(3)))
    return out


def judge_sentences(model: str, items: List[Tuple[str, bool]], ev: "PostingEvidence", allowed: set,
                    resume: str, public_numbers_text: str = "", check_titles: bool = False,
                    company_text: str = "", project_text: str = "") -> List[Judged]:
    """items: (sentence, must_be_fact). Code enforces the hard rules (every number in
    verified_metrics.txt - or, for perspective sentences, in the public news; no [CONFIRM]
    lines; no phrases to avoid; no unexplained internal terms; no title missing from the
    resume). Claude judges meaning: is it a fact or a perspective, and does the evidence
    state the same facts (different words allowed, nothing added)?"""
    if not items:
        return []
    sents = [s for s, _ in items]
    checks = run_batch({"check": sents})["check"]
    cites: Dict[str, str] = {}
    per_sentence = []
    for i, r in enumerate(checks, 1):
        cands = list(r.get("verified_metrics_matches") or []) + list(r.get("other_supporting_passages") or [])
        lines = []
        for j, p in enumerate(cands[:6], 1):
            pid = f"S{i}-P{j}"
            cites[pid] = _cite_passage(p)
            lines.append(f"  [{pid}] ({cites[pid]}) {p['text']}")
        per_sentence.append(f"S{i} candidate passages:\n" + ("\n".join(lines) or "  (none)"))
    for p in ev.passages:
        cites[p["id"]] = _cite_passage(p)
    verified = _verified_lines(ev.rules)
    for vid, cite, _ in verified:
        cites[vid] = cite
    cites["RESUME"] = "the candidate's resume (base resume file)"
    cites["PROJECT"] = "project facts (my_projects in settings.yaml)"
    prompt = ("SENTENCES:\n" + json.dumps([{"id": f"S{i}", "text": s, "must_be_fact": f}
                                           for i, (s, f) in enumerate(items, 1)], indent=1, ensure_ascii=False)
              + "\n\n" + "\n\n".join(per_sentence)
              + "\n\nEVIDENCE PASSAGES FOR THIS POSTING:\n"
              + "\n".join(f"[{p['id']}] ({cites[p['id']]}) {p['text']}" for p in ev.passages)
              + "\n\nVERIFIED METRICS (authoritative):\n" + "\n".join(f"[{v}] {t}" for v, _, t in verified)
              + f"\n\n[RESUME]\n{resume}"
              + (f"\n\n{company_text}" if company_text else "")
              + (f"\n\n[PROJECT] PROJECT FACTS (the evidence for claims about the candidate's AI projects):\n"
                 f"{project_text}" if project_text else ""))
    verdicts = {v["id"]: v for v in ask_json(model, JUDGE_RULES + personal.prompt_block(), prompt, JUDGE_SCHEMA, effort="medium",
                                              max_tokens=32000)["verdicts"]}
    public_nums = set(re.findall(r"\d+(?:\.\d+)?", (public_numbers_text or "").replace(",", "")))
    out = []
    for i, ((s, must_fact), r) in enumerate(zip(items, checks), 1):
        v = verdicts.get(f"S{i}")
        label = "fact" if must_fact or not v else v["type"]
        problems = []
        for n, rep in (r.get("numbers") or {}).items():
            if not rep.get("in_verified_metrics") and not (label == "perspective" and n in public_nums):
                problems.append(f"the number {n} is not in verified_metrics.txt")
        problems += [p for p in r.get("problems") or [] if p.startswith("Matches a line marked [CONFIRM]")]
        if r["verdict"].startswith("contradicted"):
            problems += [p for p in r.get("problems") or []
                         if " says: " not in p and not p.startswith(("The number", "The closest", "Matches a line"))]
        acr = unexplained_acronyms(s, allowed)
        if acr:
            problems.append("internal term(s) not explained: " + ", ".join(acr))
        if check_titles:
            t = titles_conflicting(s, resume)
            if t:
                problems.append("title not on your resume: " + ", ".join(t))
        if not v:
            problems.append("the judge gave no verdict")
        else:
            if label == "fact" and not v["supported"]:
                problems.append("the evidence doesn't state these facts" + (f" ({v['note']})" if v["note"] else ""))
            if v["added"]:
                problems.append("adds what the evidence doesn't contain: " + "; ".join(v["added"]))
        srcs = [cites[x] for x in (v["sources"] if v else []) if x in cites]
        if label == "fact" and not srcs and not problems:
            problems.append("no source named for this fact")
        out.append(Judged(s, not problems, label, "; ".join(problems), srcs[0] if srcs else ""))
    return out


def label_line(j: Judged) -> str:
    """changes.txt wording: 'fact: supported by [source]' or 'perspective'."""
    if j.label == "fact":
        return f"fact: supported by {j.source}"
    return "perspective" + (f" (past facts mentioned: {j.source})" if j.source else "")



# ------------------------------------------------------------------ coverage report for changes.txt

COVERAGE_RULES = """You review a tailored resume and cover letter against a job posting's top responsibilities and \
the candidate's evidence. Use only the documents and candidate items given.

1. For EACH responsibility: which candidate items (ids) the cover letter actually uses for it, and which the \
resume uses (an item is "used" if the document states its content, in any words). Status:
   - "used": at least one document uses matching evidence;
   - "available_not_used": matching evidence exists among the items but neither document uses it;
   - "no_evidence_found": no item genuinely matches the responsibility.
   Also give the best matching item ids even if unused.
Write every note and reason in plain words: no item ids, no internal codenames or acronyms.

2. RELEVANT BUT UNUSED: rank (most relevant first) up to 8 items per document that best match the \
responsibilities but that document does not use, each with a one-line reason it was likely left out (e.g. \
"story slot used by the stronger X story", "task-level win", "weaker match", "internal metric needing \
explanation", "space")."""

COVERAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "responsibilities": {"type": "array", "items": {"type": "object", "properties": {
            "responsibility": {"type": "string"},
            "status": {"type": "string", "enum": ["used", "available_not_used", "no_evidence_found"]},
            "letter_uses": {"type": "array", "items": {"type": "string"}},
            "resume_uses": {"type": "array", "items": {"type": "string"}},
            "best_matches": {"type": "array", "items": {"type": "string"}},
            "note": {"type": "string"}},
            "required": ["responsibility", "status", "letter_uses", "resume_uses", "best_matches", "note"],
            "additionalProperties": False}},
        "unused_letter": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["id", "reason"], "additionalProperties": False}},
        "unused_resume": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["id", "reason"], "additionalProperties": False}},
    },
    "required": ["responsibilities", "unused_letter", "unused_resume"], "additionalProperties": False,
}


def coverage_report(model: str, ev: PostingEvidence, resume_doc: str, letter_doc: str) -> Dict[str, List[str]]:
    """changes.txt sections: the posting's top 3 responsibilities and the evidence each document uses (or
    NO EVIDENCE FOUND), plus RELEVANT BUT UNUSED evidence for the resume and the letter."""
    items: Dict[str, Tuple[str, str]] = {}
    for p in ev.passages:              # verified-metrics lines are listed once, below, as V<line>
        if p.get("doc_type") != "verified metrics":
            items[p["id"]] = (_cite_passage(p), p["text"])
    for vid, cite, text in _verified_lines(ev.rules):
        items[vid] = (cite, text)
    if not ev.top_responsibilities:
        return {"top": [], "unused_letter": [], "unused_resume": []}
    prompt = ("TOP RESPONSIBILITIES:\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(ev.top_responsibilities, 1))
              + "\n\nCANDIDATE ITEMS:\n" + "\n".join(f"[{k}] ({c}) {t}" for k, (c, t) in items.items())
              + f"\n\nTAILORED RESUME:\n{resume_doc}\n\nCOVER LETTER:\n{letter_doc}")
    r = ask_json(model, COVERAGE_RULES, prompt, COVERAGE_SCHEMA, effort="medium", max_tokens=16000)

    def really_used(i: str, doc: str) -> bool:
        """Code check on the model's claim: an item with numbers counts as used only if one of its
        numbers appears in the document."""
        nums = set(re.findall(r"\d+(?:\.\d+)?", items[i][1])) - {"2020", "2021", "2022", "2023", "2024", "2025", "2026"}
        return not nums or bool(nums & set(re.findall(r"\d+(?:\.\d+)?", doc)))

    def name(i: str) -> str:
        c, t = items.get(i, ("", ""))
        return f"{c} - \"{t[:110]}{'...' if len(t) > 110 else ''}\"" if c else i

    top = ["THE POSTING'S TOP 3 RESPONSIBILITIES AND THE EVIDENCE USED FOR EACH"]
    for i, (resp, row) in enumerate(zip(ev.top_responsibilities, r["responsibilities"]), 1):
        top.append(f"{i}. {resp}")
        if row["status"] == "no_evidence_found":
            top.append("   NO EVIDENCE FOUND" + (f" - {row['note']}" if row["note"] else ""))
            continue
        lu = [x for x in dict.fromkeys(row["letter_uses"]) if x in items and really_used(x, letter_doc)][:3]
        ru = [x for x in dict.fromkeys(row["resume_uses"]) if x in items and really_used(x, resume_doc)][:3]
        top += [f"   Cover letter uses: {name(x)}" for x in lu] or ["   Cover letter uses: nothing for this responsibility"]
        top += [f"   Resume uses: {name(x)}" for x in ru] or ["   Resume uses: nothing for this responsibility"]
        if row["status"] == "available_not_used":
            top += [f"   Available but unused: {name(x)}" for x in row["best_matches"][:2] if x in items]
        if row["note"]:
            top.append(f"   Note: {row['note']}")

    def unused(rows, doc):
        out = [f"RELEVANT BUT UNUSED - {doc} (best matches to the posting's responsibilities, most relevant first)"]
        out += [f"{k}. {name(x['id'])}\n   Left out: {x['reason']}" for k, x in enumerate(
            [x for x in rows if x["id"] in items], 1)] or ["- None: the strongest matches are all used."]
        return out
    return {"top": top, "unused_letter": unused(r["unused_letter"], "COVER LETTER"),
            "unused_resume": unused(r["unused_resume"], "RESUME")}
