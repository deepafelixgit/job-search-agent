"""Cover letters, written to a senior-leader standard and checked against the evidence.

Steps (each enforced in code):
  1. Plan    - what the role most needs, the one or two stories that prove it, the angle,
               the opening and the closing topic. Saved in changes.txt.
  2. Draft   - only from the evidence passages, verified metrics and resume.
  3. Critique - scored against every point of the standard (STANDARD below) plus measured
               checks (length, hype words, weak verbs, generic opening/closing, slogans).
  4. Revise  - fix every failure (a second critique/revise round if needed).
  5. Claim check, last - career-evidence check_claim on every sentence about the
               candidate; anything unsupported is removed and every kept sentence is cited.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

from . import personal
from .claude_client import ask_json, ask_with_web_search
from .evidence_gate import (PostingEvidence, internal_terms, apply_scope_term, is_claim_about_candidate, judge_sentences, label_line,
                            public_acronyms, replace_codenames, scope_note, split_sentences, titles_conflicting,
                            unexplained_acronyms)
from .resume import resume_text, write_cover_letter
from .style import copied_from, echoes_line, prompt_block as style_block
from .tailor import _posting_block, numbers_in

# ------------------------------------------------------------------ cover letter

AGENT_FACTS = """FACTS ABOUT THE CANDIDATE'S AI PROJECT (rephrase in first person):
- Built a personal job search agent with Claude Code.
- Each morning it pulls new postings straight from companies' official career feeds \
(Greenhouse, Ashby, Lever, and company career sites), filters them by role, seniority, location \
and pay, and uses the Claude API to score each one against the resume with a written rationale.
- A small local web app shows the day's matches; for the roles selected, it drafts a tailored \
resume and cover letter. The resume tailoring has guardrails: it can only reword, reorder and \
select existing content, a second AI pass fact-checks every changed line, and every edit is \
logged for review.
- The candidate genuinely likes using AI tools to work better."""

MCP_FACTS = """FACTS ABOUT THE CANDIDATE'S CAREER-EVIDENCE MCP SERVER (rephrase in first person):
- Built a local MCP (Model Context Protocol) server, also with Claude Code, that lets Claude search the \
candidate's own performance reviews, peer reviews and a file of verified metrics, and check a claim \
against them.
- The job search agent uses it: before a resume or cover letter is saved, every sentence about the \
candidate's experience is checked against that evidence and anything unsupported is removed, with a \
source cited for each claim that stays."""

PROJECT_FACTS = {"job_search_agent": AGENT_FACTS, "mcp_server": MCP_FACTS}
PROJECT_WORDS = re.compile(r"\b(agent|Claude|MCP|Model Context Protocol|server|AI tools?|job search|"
                           r"guardrails?|tool|built)\b", re.I)
REVIEW_SAID = re.compile(r"\breviews? (said|noted)\b", re.I)


def _names_experience(sentence: str) -> bool:
    """Names an employer, product or school from the candidate's history (personal.experience_words in
    settings.yaml), or quotes a review."""
    return bool(personal.experience_match(sentence) or REVIEW_SAID.search(sentence))


def enabled_projects(settings: dict) -> Dict[str, str]:
    """my_projects in settings.yaml switches projects on/off for cover letters. Every letter describes
    the projects the same way, whatever the company."""
    flags = dict({"job_search_agent": True, "mcp_server": False}, **(settings.get("my_projects") or {}))
    return {k: PROJECT_FACTS[k] for k, on in flags.items() if on and k in PROJECT_FACTS}


def find_news(model: str, company: str, role: str, today: dt.date) -> List[dict]:
    month = today.strftime("%B %Y")
    prompt = (
        f"Today's date is {today.isoformat()}. Search the web for {company}'s most recent news "
        f"(for example search '{company} news {month}', '{company} announces', and topics related to "
        f"'{role}'). Pick the 2 best items published in the last 6 months - newest first - that a "
        f"candidate for '{role}' could mention in a cover letter: product launches, research, "
        f"partnerships, infrastructure, expansions or milestones. Skip layoffs, lawsuits and controversies. "
        f"Only return an empty list if you truly find nothing from the last 6 months.\n\n"
        f"Reply with ONLY a JSON array (no other text) of 1-2 objects with keys: "
        f'"title", "url" (copied exactly from a search result), "date" (publication date as YYYY-MM-DD), '
        f'"publisher", "summary" (1-2 sentences of what happened).'
    )
    text, results = ask_with_web_search(model, prompt, effort="medium")

    def key(url: str) -> str:
        return re.sub(r"^https?://(www\.)?", "", str(url)).split("#")[0].rstrip("/").lower()

    found = {key(r["url"]): r for r in results}
    items = []
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\[", text):          # find the first valid JSON list of objects
        try:
            candidate, _ = decoder.raw_decode(text[m.start():])
        except ValueError:
            continue
        if isinstance(candidate, list) and candidate and all(isinstance(x, dict) for x in candidate):
            items = candidate
            break
    news = []
    for it in items:
        hit = found.get(key(it.get("url", "")))
        try:
            published = dt.date.fromisoformat(str(it.get("date", ""))[:10])
            if (today - published).days > 190:
                continue   # older than ~6 months
        except ValueError:
            pass
        if hit:  # only keep links that really came back from the search
            it["url"] = hit["url"]
            it["date"] = it.get("date") or hit.get("page_age", "")
            news.append(it)
    if not news:
        from .config import LOG_DIR
        with open(LOG_DIR / "news_search.log", "a", encoding="utf-8") as f:
            f.write(f"\n[{dt.datetime.now():%Y-%m-%d %H:%M}] {company} - no verified news. "
                    f"{len(results)} search results. Reply was:\n{text[:3000]}\n")
    return news[:2]



def _flat(t: str) -> str:
    t = t.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    return re.sub(r"\s+", " ", t).strip()


# ------------------------------------------------------------------ the senior-letter standard

STANDARD = [
    ("opening", "Opens EITHER with one sentence connecting the candidate's experience to what the role does, OR "
                "with a specific technical or product fact from the company's own sources (the job posting or the "
                "company's own published material) followed immediately by a sentence tying it to a problem the "
                "candidate has handled. Never a generic news recap; never describes the role back to the company."),
    ("story", "Tells ONE story in depth: the situation, the decision made or direction set, who had to be "
              "aligned (teams, directors, VPs), the trade-off, and the measurable result."),
    ("proofs", "Adds at most TWO short supporting proofs, chosen because they match the posting's stated "
               "responsibilities most directly. No single-ticket or task-level wins (e.g. unblocking one request)."),
    ("ownership", "Uses ownership verbs for what the candidate actually did (set direction, decided, negotiated, "
                  "aligned, led, made the call). Never \"helped\", \"supported\", \"was involved in\", \"assisted\". "
                  "Never upgrades a role beyond what the evidence shows."),
    ("scope", "Shows scope - number of teams, executive reviews, budgets, programs - using only verified metrics."),
    ("plain_language", "Translates every Meta-internal term into plain language (an internal label gets a short "
                       "explanation). Never uses the INTERNAL SHORTHAND listed below or similar shorthand."),
    ("no_prescribing", "Never tells the company what it needs or should do. Shows what the candidate did and "
                       "lets the reader draw the connection."),
    ("why_company", "Includes one sentence on why this company, tied to something the candidate has actually done."),
    ("news", "Company news appears only if it fits naturally, and only from the VERIFIED NEWS provided "
             "(listed in sources.txt). Otherwise there is no news at all."),
    ("tone", "Confident and plain. No hype words (passionate, thrilled, excited, perfect fit, unique), no "
             "flattery of the company, no slogans or aphorisms, no dramatic declarations."),
    ("length", "About 250-350 words of body text, in 3-4 paragraphs (a few words over is fine)."),
    ("closing", "Closes by proposing a conversation about ONE specific topic: the company's work or a challenge "
                "named in the posting, phrased as something to discuss together. Never about the candidate's own "
                "past story, not a list of topics, and not \"I look forward to hearing from you\"."),
    ("backed", "Every claim about the candidate is backed by the evidence: a named program, outcome or metric."),
]
STANDARD_TEXT = "\n".join(f"- [{k}] {v}" for k, v in STANDARD)

HYPE = ["passionate", "thrilled", "excited", "perfect fit", "unique", "dream", "honored", "delighted",
        "i am writing to", "excited to apply", "dynamic", "fast-paced", "synergy", "proven track record",
        "esteemed", "cutting-edge", "game-changer", "in today's world", "i am confident that", "align perfectly",
        "world-class", "best-in-class", "rockstar", "ninja"]
WEAK_VERBS = ["helped", "help to", "supported", "was involved in", "were involved in", "involved in",
              "assisted", "contributed to", "participated in"]
GENERIC_CLOSINGS = ["look forward to hearing", "thank you for your consideration", "hope to hear",
                    "at your earliest convenience"]
SLOGAN_HINTS = [r"\bsomeone (has|needs|must|ought) to\b", r"\bI want to be (that|the one)\b",
                r"\bthat'?s (exactly )?(the|what)\b.*\b(love|do|work)\b", r"\bat the end of the day\b",
                r"\bisn'?t just\b", r"\bnot just .{1,40}\bbut\b"]

PRESCRIBING = re.compile(
    r"\b(you|your (team|company|org|organization)|{company})('s)?\b[^.]*?\b(need|needs|will need|must|"
    r"should|has to|have to|requires?|will require)\b|\b(need|needs) the same\b", re.I)

PLAN_RULES = f"""You are planning a cover letter for a senior technical program leader. Plan before anyone \
writes a word. Use only the evidence passages, the verified metrics (including TRADE-OFFS AND DECISIONS) and \
the resume provided.

1. responsibilities: the posting's stated responsibilities that matter most (2-3, quoted or closely paraphrased).
2. story: the ONE story from the evidence that best proves the candidate can deliver them - situation, \
decision or direction set, who had to be aligned, the trade-off, the measurable result (numbers only from \
verified metrics), and the evidence ids / verified-metrics lines that back it. One program only.
3. proofs: at most TWO short supporting proofs, each matched to the posting responsibility it proves most \
directly. Program-level outcomes only - never a single-ticket or task-level win.
4. opening: either (a) one sentence connecting the candidate's experience to what the role does, or (b) a \
specific technical or product fact from the company's OWN sources (the posting, or a VERIFIED NEWS item marked \
company_own_source) plus the next sentence tying it to a problem the candidate handled (from the evidence). No \
generic news recap; no describing the role back to the company. Say which type and the source.
5. why_company: one sentence on why this company, tied to something the candidate has actually done (cite it).
6. news: which VERIFIED NEWS item (if any) fits naturally and where; "none" if none fits - don't force it.
7. closing_topic: ONE specific topic for the closing conversation - about the company's work or a challenge \
named in the posting, phrased as something to discuss together (e.g. "how your team decides which release \
metrics to trust"). Never about the candidate's own past story or programs.
8. project_mention: one sentence on the AI project(s) in PROJECT FACTS, as relevant to this role ("" if none).

The letter will be held to this standard:
{STANDARD_TEXT}"""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "responsibilities": {"type": "array", "items": {"type": "string"}},
        "story": {"type": "object", "properties": {
            "title": {"type": "string"}, "situation": {"type": "string"}, "decision": {"type": "string"},
            "aligned": {"type": "string"}, "trade_off": {"type": "string"}, "result": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}}},
            "required": ["title", "situation", "decision", "aligned", "trade_off", "result", "evidence"],
            "additionalProperties": False},
        "proofs": {"type": "array", "items": {"type": "object", "properties": {
            "proof": {"type": "string"}, "matches_responsibility": {"type": "string"},
            "evidence": {"type": "array", "items": {"type": "string"}}},
            "required": ["proof", "matches_responsibility", "evidence"], "additionalProperties": False}},
        "opening": {"type": "string"},
        "opening_type": {"type": "string", "enum": ["experience", "company_fact"]},
        "opening_source": {"type": "string"},
        "why_company": {"type": "object", "properties": {
            "sentence_idea": {"type": "string"}, "tied_to": {"type": "string"}},
            "required": ["sentence_idea", "tied_to"], "additionalProperties": False},
        "news": {"type": "string"},
        "closing_topic": {"type": "string"},
        "project_mention": {"type": "string"},
    },
    "required": ["responsibilities", "story", "proofs", "opening", "opening_type", "opening_source",
                 "why_company", "news", "closing_topic",
                 "project_mention"],
    "additionalProperties": False,
}

WRITE_RULES = f"""You write cover letters for a senior technical program leader (Staff TPM level). \
Follow the PLAN exactly.

The standard (every point is checked afterwards):
{STANDARD_TEXT}

Hard rules:
- Facts about the candidate come ONLY from the EVIDENCE PASSAGES, VERIFIED METRICS and RESUME. Numbers only \
from VERIFIED METRICS, exactly as written; never round, combine or extrapolate. Never merge two programs.
- Use ownership verbs only where the evidence shows ownership; never upgrade a role.
- Never claim a title, scope or result that differs from the resume.
- Commercial names only, never internal codenames. Explain any Meta-internal concept in plain words; never write \
the INTERNAL SHORTHAND listed below or similar shorthand.
- Never tell the company what it needs, should do or will require. Describe what the candidate did.
- Company facts only from the JOB POSTING or the VERIFIED NEWS; leave news out if none fits naturally. An \
opening company fact must come from the posting or a news item marked company_own_source, and the next \
sentence must tie it to a problem the candidate handled.
- Use the STYLE EXAMPLES only for tone and structure. Never copy their sentences, opening or closing line, and \
never take facts from them.
- Never say how many companies the job search agent covers (no "200+ companies" or similar), even though the \
resume does.
- The AI project(s) in PROJECT FACTS get one or two sentences, inside a paragraph. Only state facts from \
PROJECT FACTS, in their wording about tools (if PROJECT FACTS don't name a tool, don't name one).
- First person. No gendered pronouns for the candidate. Vary sentence length; at most one em dash.
- Greeting: "Dear <Company> Hiring Team," unless the posting names a hiring manager. "closing" is only the \
sign-off word, e.g. "Sincerely,".
- Return each sentence labeled: "experience" (the candidate's past work, skills or results), "project" (the \
AI project), "company" (the company, role or news), "interest" (why this company, the closing proposal). \
Facts about the candidate are checked against the evidence automatically and removed if unsupported."""

LETTER_SCHEMA = {
    "type": "object",
    "properties": {"greeting": {"type": "string"},
                   "paragraphs": {"type": "array", "items": {"type": "object", "properties": {
                       "sentences": {"type": "array", "items": {"type": "object", "properties": {
                           "text": {"type": "string"},
                           "kind": {"type": "string", "enum": ["experience", "project", "company", "interest"]}},
                           "required": ["text", "kind"], "additionalProperties": False}}},
                       "required": ["sentences"], "additionalProperties": False}},
                   "closing": {"type": "string"}},
    "required": ["greeting", "paragraphs", "closing"], "additionalProperties": False,
}

CRITIQUE_RULES = f"""You are a demanding editor who has hired many senior program leaders. Score this cover \
letter against every point of the standard, then list EVERY sentence that falls short.

The standard:
{STANDARD_TEXT}

Also flag any sentence that states something about the candidate the evidence doesn't support, inflates a role \
(e.g. "contributed" -> "led"), merges two programs, or uses a number not in the verified metrics. The AUTOMATIC \
CHECKS listed are facts measured by code - treat each as a failure to fix. For each point give pass/fail and a \
short note. For each weak sentence: copy it exactly, name the point it fails, and say concretely how to fix it \
(or that it should be cut)."""

CRITIQUE_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {"type": "array", "items": {"type": "object", "properties": {
            "point": {"type": "string", "enum": [k for k, _ in STANDARD]},
            "passes": {"type": "boolean"}, "note": {"type": "string"}},
            "required": ["point", "passes", "note"], "additionalProperties": False}},
        "sentences": {"type": "array", "items": {"type": "object", "properties": {
            "sentence": {"type": "string"}, "point": {"type": "string"}, "fix": {"type": "string"}},
            "required": ["sentence", "point", "fix"], "additionalProperties": False}},
    },
    "required": ["scores", "sentences"], "additionalProperties": False,
}

REPAIR_RULES = WRITE_RULES + """

REPAIR TASK: the claim check would remove the sentences \
listed under WOULD BE REMOVED. Rewrite ONLY those sentences so they can be verified, and return the full \
letter with every other sentence exactly unchanged.
- State each fact in words close to the evidence passage or verified-metrics line it comes from (same facts, \
same numbers, one program per sentence). Keep the judgment - the decision or trade-off - where the evidence \
states it (e.g. "chose to delay the launch by two weeks to fix a safety issue", "documented the trade-offs for \
work that didn't make the plan", "flaky tests caused by fixed waits instead of a ready signal"). Drop \
framing the evidence doesn't contain.
- Translate any Meta-internal term into plain language; never tell the company what it needs.
- A company fact must come from the JOB POSTING or VERIFIED NEWS; otherwise cut it.
- If a sentence can't be made verifiable, cut it and keep the paragraph coherent.
- Stay at about 250-350 words and 3-4 paragraphs."""

REVISE_RULES = WRITE_RULES + """

You are revising your draft. Fix every failed point and every listed sentence, keeping what already works. \
Keep to the PLAN. Return the full revised letter."""


def _body(paragraphs: List[List[dict]]) -> str:
    return "\n\n".join(" ".join(x["text"].strip() for x in para) for para in paragraphs)


ROLE_BACK = re.compile(r"\b(this role|the role|this position|the position|you're (hiring|looking)|"
                       r"you are (hiring|looking)|your (team|role|posting|job description))\b", re.I)


def _shares_run(a: str, b: str, n: int) -> bool:
    wa, wb = re.findall(r"[a-z0-9]+", a.lower()), re.findall(r"[a-z0-9]+", b.lower())
    grams = {tuple(wb[i:i + n]) for i in range(len(wb) - n + 1)}
    return any(tuple(wa[i:i + n]) in grams for i in range(len(wa) - n + 1))


def prescribes(sentence: str, company: str) -> bool:
    """Tells the company what it needs / should do."""
    pat = re.compile(PRESCRIBING.pattern.replace("{company}", re.escape(company)), re.I)
    return bool(pat.search(sentence)) and not re.match(r"\s*(I|I'm|I've|I'd|My)\b", sentence)


COMPANY_COUNT = re.compile(
    r"\b(\d[\d,]*\+?|dozens|hundreds|scores|several hundred|over \d+|more than \d+)\s+(of\s+)?(target\s+)?"
    r"(companies|companies'|employers|career sites|company career sites|job boards)\b", re.I)


def counts_companies(sentence: str) -> bool:
    """Cover letters never say how many companies the job search agent covers."""
    return bool(COMPANY_COUNT.search(sentence))


def automatic_checks(paragraphs: List[List[dict]], company: str = "",
                     third_party_news: str = "", evidence_text: str = "") -> List[str]:
    """Measured by code, not judged by the model."""
    body = _body(paragraphs)
    low = body.lower()
    words = len(re.findall(r"\b[\w'’-]+\b", body))
    sents = [x["text"] for para in paragraphs for x in para]
    out = []
    if not 225 <= words <= 385:                     # target 250-350; a few words either way is fine
        out.append(f"[length] body is {words} words; aim for about 250-350.")
    if not 3 <= len(paragraphs) <= 4:
        out.append(f"[length] {len(paragraphs)} paragraphs; it must be 3-4.")
    out += [f'[tone] hype or filler phrase "{w}"' for w in HYPE if re.search(rf"\b{re.escape(w)}\b", low)]
    out += [f'[ownership] weak verb "{w}"' for w in WEAK_VERBS if re.search(rf"\b{re.escape(w)}\b", low)]
    first = sents[0] if sents else ""
    second = sents[1] if len(sents) > 1 else ""
    fp = re.compile(r"\b(I|I've|I'm|I'd|my|me)\b")
    if re.search(r"\b(apply|applying|application)\b", first, re.I):
        out.append(f'[opening] opens by announcing the application: "{first}"')
    if not fp.search(first):
        if ROLE_BACK.search(first) or re.match(r"\s*(Your|This role|The role)\b", first):
            out.append(f'[opening] describes the role back to the company: "{first}"')
        elif not fp.search(second):
            out.append(f'[opening] a company-fact opening must be followed by a sentence tying it to a problem '
                       f'you handled: "{first}" / "{second}"')
        elif third_party_news and _shares_run(first, third_party_news, 4):
            out.append(f'[opening] opens with third-party news, not the company\'s own sources: "{first}"')
    for i, t in enumerate(sents):
        src = copied_from(t, evidence_text)
        if src:
            out.append(f'[style] copies wording from the style example {src}: "{t}"')
    if first and echoes_line(first, "first"):
        out.append(f'[style] opening line resembles the style example {echoes_line(first, "first")}: "{first}"')
    if sents and echoes_line(sents[-1], "last"):
        out.append(f'[style] closing line resembles the style example {echoes_line(sents[-1], "last")}: "{sents[-1]}"')
    last_para = paragraphs[-1] if paragraphs else []
    close = [x["text"] for x in last_para if re.search(r"\b(discuss|conversation|talk|compare notes)\b", x["text"], re.I)]
    if not close:
        out.append("[closing] no closing sentence proposing a conversation on one specific topic")
    for c in close:
        if ":" in c or ";" in c or c.count(",") >= 2:
            out.append(f'[closing] proposes more than one topic - keep ONE: "{c}"')
        if re.search(r"\b(how I|what I|I (led|kept|built|ran|drove|scaled|handled|did)|my (own )?(story|work|"
                     r"program|programs|experience|time|approach|recovery))\b", c, re.I) or _names_experience(c):
            out.append(f'[closing] the topic is about your own past, not the company\'s work or a challenge in '
                       f'the posting: "{c}"')
    out += [f'[closing] generic closing "{w}"' for w in GENERIC_CLOSINGS
            if w in " ".join(x["text"] for x in last_para).lower()]
    for t in sents:
        if any(re.search(h, t, re.I) for h in SLOGAN_HINTS):
            out.append(f'[tone] slogan-like line: "{t}"')
        terms = internal_terms(t)
        if terms:
            out.append(f'[plain_language] internal term(s) {", ".join(terms)}: "{t}"')
        if company and prescribes(t, company):
            out.append(f'[no_prescribing] tells the company what it needs: "{t}"')
        if counts_companies(t):
            out.append(f'[project] says how many companies the agent covers: "{t}"')
    if body.count("—") > 1:
        out.append("[tone] more than one em dash")
    return out


def _paragraphs_from(letter: dict, name: str) -> List[List[dict]]:
    out = []
    for para in letter["paragraphs"]:
        sents = [x for x in para["sentences"] if x["text"].strip() and x["text"].strip().lower() != name.lower()]
        if sents:
            out.append(sents)
    return out


def write_letter(model: str, resume_path: Path, post, out_path: Path, name: str, today: dt.date,
                 posting_evidence: PostingEvidence, projects: Dict[str, str],
                 precheck_model: str = "") -> Tuple[List[dict], List[str]]:
    """Plan -> draft -> critique against the senior-letter standard -> revise -> claim check (last, in code).
    Returns (news items, lines for changes.txt)."""
    precheck_model = precheck_model or model
    news = find_news(model, post["company"], post["title"], today)
    news_block = json.dumps(news, indent=1, ensure_ascii=False) if news else \
        "(No verified recent news found - don't mention news.)"
    slug = re.sub(r"[^a-z0-9]", "", post["company"].lower())
    for n in news:          # the company's own sources may open the letter; third-party news may not
        dom = re.sub(r"^https?://(www\.)?", "", n.get("url", "")).split("/")[0].replace(".", "").replace("-", "")
        n["company_own_source"] = bool(slug and (slug in dom or slug in re.sub(r"[^a-z0-9]", "",
                                                                              n.get("publisher", "").lower())))
    news_block = json.dumps(news, indent=1, ensure_ascii=False) if news else news_block
    third_party = " ".join(f"{n.get('title', '')} {n.get('summary', '')}" for n in news if not n["company_own_source"])
    resume = resume_text(resume_path)
    ev_text = _evidence_text(posting_evidence, resume)
    facts = "\n\n".join(projects.values()) or "(No AI projects to mention.)"
    context = (f"=== {posting_evidence.prompt_block()}\n\n{posting_evidence.rules.facts_block()}\n\n"
               f"RESUME (titles, scope and results must not conflict with it):\n{resume}\n\n"
               f"JOB POSTING:\n{_posting_block(post)}\n\nRECENT NEWS ABOUT {post['company'].upper()}:\n"
               f"{news_block}\n\nPROJECT FACTS:\n{facts}\n\n{style_block()}")

    # 1. plan
    plan = ask_json(model, PLAN_RULES + personal.prompt_block(), context, PLAN_SCHEMA, effort="high")
    plan_text = json.dumps(plan, indent=1, ensure_ascii=False)
    notes = ["", "COVER LETTER PLAN",
             "- Posting responsibilities that matter most: " + "; ".join(plan["responsibilities"])]
    st = plan["story"]
    notes += [f"- Story (told in depth): {st['title']}",
              f"    Situation: {st['situation']}", f"    Decision/direction: {st['decision']}",
              f"    Aligned: {st['aligned']}", f"    Trade-off: {st['trade_off']}",
              f"    Result: {st['result']}", f"    Evidence: {', '.join(st['evidence'])}"]
    for i, pr in enumerate(plan["proofs"][:2], 1):
        notes += [f"- Supporting proof {i}: {pr['proof']}",
                  f"    Matches: {pr['matches_responsibility']}", f"    Evidence: {', '.join(pr['evidence'])}"]
    notes += [f"- Opening ({plan['opening_type']}; source: {plan['opening_source']}): {plan['opening']}",
              f"- Why this company: {plan['why_company']['sentence_idea']} (tied to: {plan['why_company']['tied_to']})",
              f"- News: {plan['news']}", f"- Closing topic: {plan['closing_topic']}"]
    if plan["project_mention"]:
        notes.append(f"- AI project tie-in: {plan['project_mention']}")
    from .style import load_examples
    examples = [n for n, _ in load_examples()]
    notes.append("- Style examples (tone and structure only): " + ", ".join(examples) if examples else
                 "- STYLE EXAMPLES: none found - style_examples/ is empty, so no tone reference was used.")

    # 2. draft
    letter = ask_json(model, WRITE_RULES + personal.prompt_block(), f"{context}\n\nPLAN:\n{plan_text}", LETTER_SCHEMA, effort="high")
    paragraphs = _paragraphs_from(letter, name)

    # 3-4. critique and revise. Each round also runs the claim check, so sentences that would
    # fail it get reworded to the evidence (or cut) here, instead of silently disappearing at
    # the end. Round 3 happens only if claim failures remain.
    for rnd in (1, 2, 3):
        _, _, would_fail = _evidence_gate(precheck_model, paragraphs, post, posting_evidence, projects, news, resume)
        auto = automatic_checks(paragraphs, post['company'], third_party, ev_text) + [
            f'[backed] the claim check would remove: "{sent}" ({why}). Reword it to match the evidence passage or '
            f'verified-metrics line closely (same facts, same numbers, one program), or cut it - and keep the '
            f'surrounding sentences coherent without it.' for sent, why in would_fail]
        if rnd == 3 and not would_fail:
            break
        critique = ask_json(model, CRITIQUE_RULES + personal.prompt_block(),
                            f"{context}\n\nPLAN:\n{plan_text}\n\nCOVER LETTER:\n{_body(paragraphs)}\n\n"
                            "AUTOMATIC CHECKS:\n" + ("\n".join(auto) or "(none failed)"),
                            CRITIQUE_SCHEMA, effort="high")
        failed = [x for x in critique["scores"] if not x["passes"]]
        notes += ["", f"COVER LETTER CRITIQUE (round {rnd}: scored against the senior-letter standard)"]
        notes += [f"- {x['point']}: {'pass' if x['passes'] else 'FAIL'} - {x['note']}" for x in critique["scores"]]
        notes += [f"- Automatic check: {a}" for a in auto]
        notes += [f'- Falls short ({x["point"]}): "{x["sentence"]}"\n    Fix: {x["fix"]}'
                  for x in critique["sentences"]]
        if not failed and not auto and not critique["sentences"]:
            notes.append("- Nothing to fix.")
            break
        revised = ask_json(model, REVISE_RULES + personal.prompt_block(),
                           f"{context}\n\nPLAN:\n{plan_text}\n\nYOUR DRAFT:\n{_body(paragraphs)}\n\n"
                           f"CRITIQUE:\n{json.dumps(critique, indent=1, ensure_ascii=False)}\n\nAUTOMATIC CHECKS:\n"
                           + ("\n".join(auto) or "(none failed)"), LETTER_SCHEMA, effort="high")
        letter, paragraphs = revised, _paragraphs_from(revised, name)
        notes.append(f"- Revised to fix the {len(failed)} failed point(s) and {len(critique['sentences'])} sentence(s).")

    # 4b. repair: rewrite only sentences the claim check would still remove (up to 2 passes)
    repair_flagged: List[Tuple[str, str]] = []
    for rep in (1, 2):
        _, _, would_fail = _evidence_gate(precheck_model, paragraphs, post, posting_evidence, projects, news, resume)
        if not would_fail:
            break
        notes += ["", f"COVER LETTER REPAIR (pass {rep}: sentences the claim check would remove, reworded to the evidence)"]
        notes += [f'- "{sent}"\n    Would fail: {why}' for sent, why in would_fail]
        repair_flagged += would_fail
        fixed = ask_json(model, REPAIR_RULES + personal.prompt_block(),
                         f"{context}\n\nPLAN:\n{plan_text}\n\nLETTER:\n{_body(paragraphs)}\n\nWOULD BE REMOVED:\n"
                         + "\n".join(f'- "{sent}" ({why})' for sent, why in would_fail),
                         LETTER_SCHEMA, effort="high")
        letter, paragraphs = fixed, _paragraphs_from(fixed, name)

    # 5. claim check, last
    kept, gate_notes, final_removed = _evidence_gate(model, paragraphs, post, posting_evidence, projects, news,
                                                     resume)
    closing = next((ln.strip() for ln in (letter["closing"] or "").splitlines()
                    if ln.strip() and name.lower() not in ln.lower()), "Sincerely,")
    write_cover_letter(resume_path, out_path, today.strftime("%B %-d, %Y"), letter["greeting"],
                       kept, closing, name)
    final_auto = automatic_checks([[{"text": s} for s in split_sentences(p)] for p in kept],
                                  post['company'], third_party, ev_text)
    words = len(re.findall(r"\b[\w'’-]+\b", " ".join(kept)))
    notes += ["", "FINAL LETTER (after the claim check)", f"- {words} words, {len(kept)} paragraphs."]
    notes += [f"- Still open: {a}" for a in final_auto] or ["- Meets every automatic check."]
    removed_section = ["", "REMOVED FOR LACK OF EVIDENCE - COVER LETTER (if a claim here is true, add it to your "
                             "evidence so it's kept next time)"]
    removed_section += [f'- REMOVED in the final check: "{x}"\n    Why: {why}' for x, why in final_removed]
    # claims the draft wanted but the evidence didn't support (style-only problems aren't evidence gaps)
    evidence_gaps = [(x, why) for x, why in repair_flagged
                     if not all(part.strip().startswith(("copies wording", "Meta-internal term", "tells the company",
                                                         "names Claude"))
                                for part in why.split(";") if part.strip())]
    removed_section += [f'- Reworded or cut before the final check: "{x}"\n    Why: {why}'
                        for x, why in evidence_gaps]
    if len(removed_section) == 2:
        removed_section.append("- Nothing was removed.")
    return news, notes + gate_notes + removed_section


def _evidence_gate(model: str, paragraphs: List[List[dict]], post, posting_evidence: PostingEvidence,
                   projects: Dict[str, str], news: List[dict], resume: str) -> Tuple[List[str], List[str], List[Tuple[str, str]]]:
    """Judge every sentence (in code, run last): facts about the candidate must be stated by the
    evidence (different words allowed, nothing added); perspective about the company/role needs
    no evidence but may not state an unsupported fact about the candidate's past.
    Returns (kept paragraphs, changes.txt lines, removed sentences with reasons)."""
    rules = posting_evidence.rules
    news_text = " ".join(f"{n.get('title', '')} {n.get('summary', '')}" for n in news)
    allowed = public_acronyms(post["company"], post["title"], post["description"], news_text)
    gate_notes, letter_scope, plan, to_judge = [], [], [], []
    for para in paragraphs:
        row = []
        for x in para:
            for s in split_sentences(x["text"]):
                s2, renamed = replace_codenames(s, rules)
                s2, used = apply_scope_term(s2, rules, posting_evidence.scope_term)
                if used:
                    letter_scope.append(f'"{s2[:70]}..."')
                gate_notes += [f'- Codename replaced in "{s2}": {r}' for r in renamed]
                kind = x["kind"]
                if kind == "project" and _names_experience(s2):
                    kind = "experience"                        # code overrides the label
                if kind == "project":
                    row.append((s2, "project"))
                else:
                    # code decides what must be judged as a fact; the judge decides the rest
                    must_fact = is_claim_about_candidate(s2, kind) and not _news_fact(s2, kind, news_text)
                    to_judge.append((s2, must_fact))
                    row.append((s2, "judge"))
        plan.append(row)
    company_text = (f"JOB POSTING:\n{_posting_block(post)}\n\nVERIFIED NEWS (each listed in sources.txt):\n"
                    + (json.dumps(news, indent=1, ensure_ascii=False) if news else "(none)"))
    judged = {j.sentence: j for j in judge_sentences(model, to_judge, posting_evidence, allowed, resume,
                                                     public_numbers_text=news_text, check_titles=True,
                                                     company_text=company_text,
                                                     project_text="\n\n".join(projects.values()))}
    fact_numbers = numbers_in(" ".join(projects.values()))
    kept, removed, labeled = [], [], []
    for row in plan:
        keep = []
        for s, how in row:
            if how == "project":
                problem = ""
                extra = sorted(numbers_in(s) - fact_numbers)
                acr = unexplained_acronyms(s, allowed)
                if extra:
                    problem = "number(s) not in the project facts: " + ", ".join(extra)
                elif re.search(r"\b(MCP|Model Context Protocol|evidence server)\b", s) and "mcp_server" not in projects:
                    problem = "mentions the MCP server, which is switched off in my_projects"
                elif acr:
                    problem = "internal term(s) not explained: " + ", ".join(acr)
                label = "fact: supported by project facts (my_projects in settings.yaml: " + ", ".join(projects) + ")"
            else:
                j = judged[s]
                problem = j.reason if not j.ok else ""
                label = label_line(j)
            hard = []                                  # rules that apply to every sentence
            terms = internal_terms(s)
            if terms:
                hard.append("Meta-internal term(s) not translated: " + ", ".join(terms))
            if how != "project" and prescribes(s, post["company"]):
                hard.append("tells the company what it needs")
            src = copied_from(s, _evidence_text(posting_evidence, resume))
            if src:
                hard.append(f"copies wording from the style example {src}")
            if counts_companies(s):
                hard.append("says how many companies the job search agent covers")
            if hard:
                problem = "; ".join(([problem] if problem else []) + hard)
            if problem:
                removed.append((s, problem))
                gate_notes.append(f'- REMOVED: "{s}"\n    Why: {problem}')
            else:
                keep.append(s)
                labeled.append(f'- {label}\n    "{s}"')
        if keep:
            kept.append(" ".join(keep))
    out = ["", "COVER LETTER SCOPE WORDING (team count)"] + (scope_note(posting_evidence, letter_scope)
                                                          or ["- No scope rule found."])
    out += ["", "COVER LETTER CLAIM CHECK (run last: facts must match the evidence; perspective may not "
                "state unsupported facts about you)"]
    out += gate_notes or ["- Nothing had to be removed."]
    out += ["", "COVER LETTER, EVERY SENTENCE LABELED (fact: supported by document | type | review period | "
                "location, or perspective)"] + labeled
    return kept, out, removed


def _evidence_text(ev: PostingEvidence, resume: str) -> str:
    return " ".join([p["text"] for p in ev.passages] + ev.rules.facts + [resume])


def _news_fact(sentence: str, kind: str, news_text: str) -> bool:
    """A company/news sentence whose numbers all come from the verified news item is a
    news fact, not a claim about the candidate."""
    from .evidence_gate import FIRST_PERSON_CLAIM
    nums = numbers_in(sentence)
    return (kind in ("company", "interest") and bool(nums) and nums <= numbers_in(news_text)
            and not FIRST_PERSON_CLAIM.search(sentence) and not _names_experience(sentence))
