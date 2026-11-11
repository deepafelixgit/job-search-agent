"""career-evidence: a local MCP server for searching the candidate's documented work history.

What this file is: an MCP *server*. Claude Code (the *host*) starts it as a helper
program and talks to it through stdin/stdout ("stdio") - there is no network port,
so nothing outside this Mac can reach it. The server offers four *tools*; Claude
reads each tool's description (the docstrings below) to decide when to call it.

IMPORTANT for stdio servers: stdout is the message channel to Claude Code, so this
program must never print() to stdout. Logging goes to stderr.

Run by Claude Code via .mcp.json; to try it by hand:  .venv-mcp/bin/python -m mcp_server.cli
"""
from __future__ import annotations

import logging
import re
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from .documents import Document, Passage, load_documents
from .metrics import Metrics, load_metrics
from .search import ABBREVIATIONS, Index, acronyms_from_text, contradicting_sentences, numbers_in, tokens

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="career-evidence: %(message)s")
log = logging.getLogger("career-evidence")

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "evidence"
METRICS_FILE = "verified_metrics.txt"
MAX_PASSAGE_WORDS = 120


# ------------------------------------------------------------------ the evidence, kept in memory

class Evidence:
    """Reads evidence/ once, and again only when a file is added, removed or changed."""

    def __init__(self, folder: Path):
        self.folder = folder
        self._stamp = None
        self._lock = threading.Lock()
        self.docs: List[Document] = []
        self.skipped: List[str] = []
        self.metrics: Optional[Metrics] = None
        self.index: Optional[Index] = None

    def _current_stamp(self):
        return tuple(sorted((p.name, p.stat().st_mtime, p.stat().st_size)
                            for p in self.folder.iterdir() if not p.name.startswith(".")))

    def get(self) -> "Evidence":
        with self._lock:
            stamp = self._current_stamp()
            if stamp != self._stamp:
                log.info("reading evidence files")
                self.docs, self.skipped = load_documents(self.folder)
                mpath = self.folder / METRICS_FILE
                self.metrics = load_metrics(mpath) if mpath.exists() else Metrics(file=METRICS_FILE)
                expansions = dict(ABBREVIATIONS)
                for code, name in self.metrics.codenames.items():
                    expansions[code.split(" (")[0]] = name.split(";")[0].split(",")[0]
                all_text = "\n".join(p.text for d in self.docs for p in d.passages)
                for abbr, words in acronyms_from_text(all_text).items():
                    expansions.setdefault(abbr, words)
                self.index = Index([p for d in self.docs for p in d.passages], expansions)
                self._stamp = stamp
            return self


EVIDENCE = Evidence(EVIDENCE_DIR)
mcp = FastMCP("career-evidence")


def _short(text: str, terms: Optional[List[str]] = None) -> str:
    words = text.split()
    if len(words) <= MAX_PASSAGE_WORDS:
        return text
    # keep the window with the most matching words
    best, best_i = -1, 0
    for i in range(0, len(words) - MAX_PASSAGE_WORDS + 1, 10):
        window = " ".join(words[i:i + MAX_PASSAGE_WORDS]).lower()
        hits = sum(1 for t in (terms or []) if t in window)
        if hits > best:
            best, best_i = hits, i
    out = " ".join(words[best_i:best_i + MAX_PASSAGE_WORDS])
    return ("… " if best_i else "") + out + " …"


def _passage_out(p: Passage, score: Optional[float] = None, terms=None) -> dict:
    d = {"source": p.source, "doc_type": p.doc_type, "period": p.period, "location": p.location,
         "text": _short(p.text, terms)}
    if p.period_note:
        d["period_note"] = p.period_note
    d.update(p.extra)
    if score is not None:
        d["relevance"] = round(score, 2)
    return d


def _metrics_header(m: Metrics) -> dict:
    return {
        "authority": f"{m.file} is the authoritative source for numbers and overrides every other document.",
        "rules": m.rules,
        "phrases_to_avoid": m.phrases_to_avoid,
        "codenames_internal_to_commercial": m.codenames,
    }


# ------------------------------------------------------------------ tools

@mcp.tool()
def list_documents() -> dict:
    """List the evidence documents available: file name, document type (manager review,
    peer review, self review, resume, verified metrics), the review periods each covers,
    whether its text is real or scanned, and how many passages it was split into.
    Use this first to see what evidence exists."""
    ev = EVIDENCE.get()
    return {
        "documents": [{"file": d.file, "doc_type": d.doc_type, "review_periods": d.periods,
                       "text_check": d.text_check, "passages": len(d.passages), "notes": d.notes}
                      for d in ev.docs],
        "skipped_files": ev.skipped,
        "how_periods_were_found": (
            "Manager reviews: printed on each review's first page, or worked out from its submitted date. "
            "Peer reviews: inferred from each reviewer's submitted date (Jun-Sep = first half/Mid-year, "
            "Nov-Feb = second half/Year-end; other months = off-cycle). Self reviews: from the headings."),
    }


@mcp.tool()
def search_evidence(query: str, doc_type: Optional[str] = None, period: Optional[str] = None,
                    max_results: int = 6) -> dict:
    """Search all evidence documents and return the most relevant SHORT passages, each with
    its source document, document type, review period, and page (PDFs) or section.

    query: words to look for, e.g. "test lab scaling devices" or "stakeholder escalation".
           Keyword search: try a second phrasing with different words if results are thin.
    doc_type (optional): one of "manager review", "peer review", "self review", "resume",
           "verified metrics".
    period (optional): e.g. "2024 Year-end", "H1 2021", "2023".
    max_results: 1-10 (default 6)."""
    ev = EVIDENCE.get()
    max_results = max(1, min(int(max_results or 6), 10))
    types = [doc_type.strip().lower()] if doc_type else None
    hits = ev.index.search(query, top_k=200 if period else max_results, doc_types=types)
    if period:
        want = period.strip().lower()
        hits = [h for h in hits if want in h[1].period.lower()][:max_results]
    terms = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
    out = {"query": query, "results": [_passage_out(p, s, terms) for s, p, _ in hits]}
    if not hits:
        out["message"] = "No passages matched. Try different or fewer words."
    used = ev.metrics.codenames_used(query)
    if used:
        out["codename_note"] = {k: f"internal codename - in resumes and cover letters say: {v}" for k, v in used.items()}
    return out


@mcp.tool()
def get_verified_metrics(program: Optional[str] = None) -> dict:
    """Get numbers and facts from verified_metrics.txt - the AUTHORITATIVE source for any
    number. Its figures override every other document. The response includes the file's
    rules, phrases to avoid, and the codename list; follow them exactly (use numbers as
    written, never round or combine, never merge two programs, use commercial names
    instead of internal codenames, and never use items marked as needing confirmation).

    program (optional): a program, product or topic, e.g. a product name, "fix rate",
    a tool name, or a codename. Leave empty for all."""
    ev = EVIDENCE.get()
    m = ev.metrics
    items = m.lookup(program)
    usable = [it for it in items if not it.needs_confirmation]
    blocked = [it for it in items if it.needs_confirmation]
    out = _metrics_header(m)
    out["program_searched"] = program or "(all)"
    out["metrics"] = [{"section": it.section, "program": it.program, "fact": it.text,
                       "line": it.line} for it in usable]
    if blocked:
        out["not_usable_until_confirmed"] = [{"program": it.program, "fact": it.text, "line": it.line}
                                             for it in blocked]
    if program and not items:
        out["message"] = (f"No verified metrics found for '{program}'. Do not state numbers for it. "
                          f"Programs in the file: {m.programs()}")
    used = m.codenames_used(program or "")
    if used:
        out["codename_note"] = {k: f"internal codename - say '{v}' in outbound documents" for k, v in used.items()}
    return out


def _avoid_hits(m: Metrics, claim: str) -> List[str]:
    """Check the claim against PHRASES TO AVOID (e.g. 'defined test thresholds')."""
    hits = []
    claim_terms = set(tokens(claim))
    for rule in m.phrases_to_avoid:
        tail = re.split(r"\bas having\b|\bas\b", rule, maxsplit=1)[-1]
        for phrase in re.split(r"\bor\b|,", tail):
            words = tokens(phrase)
            if len(words) < 2:
                continue
            verb, nouns = words[0], words[1:]
            verbs = {verb, "own", "defin", "set", "approv", "decid", "control"}
            if all(n in claim_terms for n in nouns) and claim_terms & verbs:
                hits.append(f"May break this rule from {m.file}: \"{rule}\"")
    return hits


@mcp.tool()
def check_claim(claim: str) -> dict:
    """Check whether a resume or cover-letter claim is backed by evidence. Returns the
    passages that may support it (verified metrics first) and a verdict:
      "supporting evidence found"  - passages cover the claim and every number in it
                                     appears in verified_metrics.txt
      "partly supported"           - something is missing (see 'problems')
      "contradicted by evidence - do not use" - the evidence says the opposite, or the
                                     claim breaks a rule in verified_metrics.txt
      "no supporting evidence found"
    Also flags internal codenames, phrases to avoid, and numbers that aren't verified.
    Always read the returned passages to confirm they say the same thing as the claim, and
    never combine passages about different programs into one claim."""
    ev = EVIDENCE.get()
    m = ev.metrics
    terms = [t for t in re.findall(r"[a-z0-9]+", claim.lower()) if len(t) > 2]
    metric_hits = ev.index.search(claim, top_k=4, doc_types=["verified metrics"])
    other_hits = ev.index.search(claim, top_k=6, doc_types=["manager review", "peer review", "self review", "resume"])
    relevant = [h for h in metric_hits + other_hits if h[2] >= 0.5]

    problems, warnings = [], []
    # numbers: every number in the claim must be in verified_metrics.txt
    nums = numbers_in(claim)
    metric_text = " ".join(f"{it.text}" for it in m.items if not it.needs_confirmation)
    number_report = {}
    for n in nums:
        in_metrics = re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", metric_text) is not None
        elsewhere = sorted({f"{p.source} ({p.location})" for _, p, _ in relevant
                            if re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", p.text)})
        number_report[n] = {"in_verified_metrics": in_metrics, "also_found_in": elsewhere[:4]}
        if not in_metrics:
            problems.append(f"The number {n} is not in {m.file}" +
                            (" (it appears in other evidence - check it there before using it)" if elsewhere
                             else " or in the matching passages - don't use it"))
    blocked = [it for it in m.items if it.needs_confirmation and any(
        re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", it.text) for n in nums)]
    for it in blocked:
        problems.append(f"Matches a line marked [CONFIRM] (line {it.line}); it can't be used yet")
    # any capitalisation counts (a codename written in lower case is still the codename)
    for code, name in {c.split(" (")[0]: n for c, n in m.codenames.items()
                       if re.search(rf"\b{re.escape(c.split(' (')[0])}\b", claim, re.I)}.items():
        warnings.append(f"'{code}' is an internal codename - use '{name}' in resumes and cover letters")
    avoid = list(dict.fromkeys(_avoid_hits(m, claim)))

    # evidence that talks about the same thing but says the opposite ("No ML model was built")
    contradictions = []
    for _, p, _ in metric_hits + other_hits:
        text = p.text
        if p.doc_type == "verified metrics" and ": " in text:
            # Verified-metric passages read "<program label>: <fact>". Only the fact can
            # contradict a claim; a label like "Side project (not covered in my
            # performance reviews)" must not count as a negation.
            text = text.split(": ", 1)[1]
        for sent in contradicting_sentences(claim, text):
            contradictions.append({"source": p.source, "location": p.location, "says": sent})

    best_cov = max((h[2] for h in relevant), default=0.0)
    if avoid or contradictions:
        verdict = "contradicted by evidence - do not use"
        problems += avoid
        problems += [f"{c['source']} ({c['location']}) says: \"{c['says']}\"" for c in contradictions[:3]]
    elif not relevant:
        verdict = "no supporting evidence found"
    elif best_cov >= 0.6 and not problems:
        verdict = "supporting evidence found"
    else:
        verdict = "partly supported"
        if best_cov < 0.6:
            problems.append("The closest passages only cover part of the claim's wording")
    return {
        "claim": claim,
        "verdict": verdict,
        "problems": problems,
        "warnings": warnings,
        "numbers": number_report,
        "verified_metrics_matches": [_passage_out(p, s, terms) for s, p, c in metric_hits if c >= 0.5],
        "other_supporting_passages": [_passage_out(p, s, terms) for s, p, c in other_hits if c >= 0.5],
        "reminder": "Confirm each passage actually states the claim. Use numbers exactly as in "
                    f"{m.file}. Never merge two programs or metrics into one claim.",
    }


def main() -> None:
    mcp.run()          # stdio: Claude Code talks to us through stdin/stdout


if __name__ == "__main__":
    main()
