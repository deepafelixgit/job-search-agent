"""A personalized LinkedIn message to the company's recruiter, written after the cover letter.

The agent only writes a .txt file; it never sends anything to LinkedIn or any website.

Steps (each enforced in code):
  A. Choices         - what the role exists to deliver, the closest area of the candidate's work,
                       the chain from that work to its outcome, two proof points. -> changes.txt
  B. Why this company - who benefits from what the company builds, backed by a fact from the posting
                       or the verified news; ONE web search (scoring model) only if neither has one.
  C. Draft           - connection note, InMail subject and body, follow-up.
  D. Checks          - measured by code (limits, exact title, cliches, underselling words, bare test
                       wording, overlap with the cover letter) plus the claim check pre-check (scoring
                       model); failures go back to the model for one or two revisions.
  E. Limits          - anything over a LinkedIn limit goes back to the model to be shortened (up to 2
                       tries); never cut in code. Still over: written with a TOO LONG warning at the top.
  F. Claim check     - last, the cover letter's _evidence_gate on every sentence (writing model).
"""
from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import personal
from .claude_client import ask_json, ask_with_web_search, can_afford
from .evidence_gate import (POSITIONING_RULES, PostingEvidence, impact_block, is_claim_about_candidate,
                            positioning_problems)
from .letter import HYPE, SLOGAN_HINTS, _evidence_gate, _shares_run
from .tailor import _posting_block

GREETING = "Hi [First name],"
TOP_LINE = "Send these yourself on LinkedIn. Replace [First name] before sending."

# LinkedIn limits (free account invitation note; InMail subject and body)
NOTE_MAX_CHARS = 200
SUBJECT_MAX_CHARS = 200
BODY_MIN_WORDS, BODY_MAX_WORDS, BODY_MAX_CHARS = 120, 180, 1900
FOLLOW_MIN_WORDS, FOLLOW_MAX_WORDS = 40, 70
REVISION_COST = 0.15   # rough USD for one revision with its claim pre-check, plus the final check and coverage report

CLICHES = ["hope this message finds you well", "hope this finds you well", "came across your profile",
           "passionate about", "i'm passionate", "i am passionate", "pick your brain", "dear hiring manager",
           "to whom it may concern", "innovative culture", "mission resonates", "resonates with me"]
FORBIDDEN = [(r"\b(salary|compensation|pay range|total comp)\b", "mentions pay"),
             (r"\brefer(ral|ring| me)\b", "asks for a referral"),
             (r"\b(available|start|join) immediately\b|\bimmediately available\b", "says available immediately"),
             (r"\bdesperate\b", "sounds desperate")]
EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")
MARKDOWN = re.compile(r"\*\*|__|`|^#+ |^\s*[-*] ", re.M)


# ------------------------------------------------------------------ counting (code, never the model)

def words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def chars(text: str) -> int:
    return len(text)


def assemble(draft: dict, title_url: str, name: str, contact: str = "") -> Dict[str, str]:
    """The three parts exactly as they'll be pasted into LinkedIn. Code adds the greeting, the posting
    link on its own line after the first paragraph, and the sign-off."""
    sign = "Thank you,\n" + name + (f"\n{contact}" if contact else "")
    note = " ".join([GREETING] + [x["text"].strip() for x in draft["note"] if x["text"].strip()])
    paras = [" ".join(x["text"].strip() for x in p if x["text"].strip()) for p in draft["body"]]
    paras = [p for p in paras if p]
    body_parts = [GREETING] + paras[:1] + ([title_url] if title_url else []) + paras[1:] + [sign]
    follow = " ".join(x["text"].strip() for x in draft["follow_up"] if x["text"].strip())
    return {"note": note, "subject": draft["subject"].strip(), "body": "\n\n".join(body_parts),
            "follow_up": "\n\n".join([GREETING, follow, sign]) if follow else ""}


def over_limits(parts: Dict[str, str]) -> List[str]:
    """Only the 'too long' problems (the ones the model shortens)."""
    out = []
    if chars(parts["note"]) > NOTE_MAX_CHARS:
        out.append(f"connection note is {chars(parts['note'])} characters; at most {NOTE_MAX_CHARS} "
                   f"(TOO LONG by {chars(parts['note']) - NOTE_MAX_CHARS})")
    if chars(parts["subject"]) > SUBJECT_MAX_CHARS:
        out.append(f"subject is {chars(parts['subject'])} characters; at most {SUBJECT_MAX_CHARS} "
                   f"(TOO LONG by {chars(parts['subject']) - SUBJECT_MAX_CHARS})")
    if words(parts["body"]) > BODY_MAX_WORDS:
        out.append(f"message body is {words(parts['body'])} words; at most {BODY_MAX_WORDS} "
                   f"(TOO LONG by {words(parts['body']) - BODY_MAX_WORDS} words)")
    if chars(parts["body"]) > BODY_MAX_CHARS:
        out.append(f"message body is {chars(parts['body'])} characters; at most {BODY_MAX_CHARS} "
                   f"(TOO LONG by {chars(parts['body']) - BODY_MAX_CHARS})")
    if words(parts["follow_up"]) > FOLLOW_MAX_WORDS:
        out.append(f"follow-up is {words(parts['follow_up'])} words; at most {FOLLOW_MAX_WORDS} "
                   f"(TOO LONG by {words(parts['follow_up']) - FOLLOW_MAX_WORDS} words)")
    return out


def under_limits(parts: Dict[str, str]) -> List[str]:
    out = []
    if words(parts["body"]) < BODY_MIN_WORDS:
        out.append(f"message body is {words(parts['body'])} words; at least {BODY_MIN_WORDS}")
    if words(parts["follow_up"]) < FOLLOW_MIN_WORDS:
        out.append(f"follow-up is {words(parts['follow_up'])} words; at least {FOLLOW_MIN_WORDS}")
    return out


def fit_to_limits(draft: dict, measure: Callable[[dict], List[str]],
                  shorten: Callable[[dict, List[str]], dict], tries: int = 2) -> Tuple[dict, List[str], List[str]]:
    """Ask the model to shorten whatever is over a limit, up to `tries` times. Never cuts text in code.
    Returns (draft, problems still open, notes for changes.txt)."""
    notes = []
    problems = measure(draft)
    for i in range(1, tries + 1):
        if not problems:
            break
        notes.append(f"- Shortening try {i}: " + "; ".join(problems))
        draft = shorten(draft, problems)
        problems = measure(draft)
    if problems:
        notes.append("- Still over a limit after " + str(tries) + " tries; written with a TOO LONG warning.")
    return draft, problems, notes


# ------------------------------------------------------------------ prompts

STEP_A_RULES = """You are choosing what a Staff Technical Program Manager tells a company's recruiter about \
themself, for ONE job posting. Think it through before anyone writes a word. Use only the evidence given: \
the evidence passages, verified metrics, resume, and the impact_of_my_work.md lines.

1. role_delivers: what this role exists to deliver - read the whole posting (responsibilities, requirements, \
what the team builds). hiring_manager_wants: what the hiring manager would most want to see.
2. ranked_areas: the top three areas of the candidate's work, closest match first (the sections of \
impact_of_my_work.md, or other evidence), each with why it matches. Use the GOOD FIT FOR lines as a guide.
3. chain: for the top area, the chain from the candidate's work to its outcome, one link per item (see the \
CHAIN EXAMPLE under CANDIDATE POSITIONING, if given: what the work made possible, then the launch or result, \
then who benefits). Each link names the line that backs it: backed_by = fact | perspective | public \
product fact, and source = the id (I.., E.., V<line>) or "resume". Leave out any link no line backs. A \
perspective link must read as the candidate's view ("that work kept developers productive"), never as a \
measured number. A public product fact is about the product; never that the candidate built the feature.
4. proof_points: the two strongest proof points for THIS posting, each with a number exactly as written in \
VERIFIED METRICS, each from one program, different from the chain's headline fact, with its source.
5. note_highlight: the single most relevant thing about the candidate for a 200-character connection note.
6. follow_up_proof: one more proof point with a number, not used in 3 or 4, for the follow-up message."""

STEP_A_SCHEMA = {"type": "object", "properties": {
    "role_delivers": {"type": "string"}, "hiring_manager_wants": {"type": "string"},
    "ranked_areas": {"type": "array", "items": {"type": "object", "properties": {
        "area": {"type": "string"}, "why": {"type": "string"}},
        "required": ["area", "why"], "additionalProperties": False}},
    "chain": {"type": "array", "items": {"type": "object", "properties": {
        "link": {"type": "string"},
        "backed_by": {"type": "string", "enum": ["fact", "perspective", "public product fact"]},
        "source": {"type": "string"}},
        "required": ["link", "backed_by", "source"], "additionalProperties": False}},
    "proof_points": {"type": "array", "items": {"type": "object", "properties": {
        "proof": {"type": "string"}, "matches": {"type": "string"}, "source": {"type": "string"}},
        "required": ["proof", "matches", "source"], "additionalProperties": False}},
    "note_highlight": {"type": "string"},
    "follow_up_proof": {"type": "object", "properties": {"proof": {"type": "string"}, "source": {"type": "string"}},
                        "required": ["proof", "source"], "additionalProperties": False}},
    "required": ["role_delivers", "hiring_manager_wants", "ranked_areas", "chain", "proof_points",
                 "note_highlight", "follow_up_proof"], "additionalProperties": False}

STEP_B_RULES = """You decide what one or two sentences of a recruiter message say about WHY THIS COMPANY. The \
candidate cares most about technology that benefits people and society. Use only the JOB POSTING, the \
VERIFIED NEWS and (if given) the WEB SEARCH RESULT.

1. who_benefits: who benefits from what this company builds, and how. Think past the obvious: drone delivery \
can get medical supplies to remote clinics; driverless cars aim to cut traffic deaths; warehouse or humanoid \
robots can take over dangerous or repetitive work; better AI chips or infrastructure make AI cheaper for \
everyone; developer tools give engineers back time; AI safety work makes AI safer for everyone who uses it.
2. fact / found_in / source: the specific fact that backs it - look first in the posting (most have an "About \
us" section), then the verified news, then the web search result if given. Quote or closely paraphrase it. \
found_in = "posting", "news", "web" or "none".
3. reason_type: "1" = how the company's technology benefits people and society, backed by a fact; "2" = only \
if there is nothing like that: something genuinely innovative (a problem it solves in a unique or unusually \
simple way), backed by a fact; "left out" = neither can be backed by a fact. No generic praise ever ("your \
innovative culture", "your mission resonates with me").
4. connection: if an example in impact_of_my_work.md (the PUBLIC PRODUCT FACTS and MY PERSPECTIVE lines) shares \
the same kind of benefit (accessibility, safety, developer productivity, health), name it in a few words and \
its id; otherwise "". If none fits, the sentence states the company fact and why it makes the role interesting, \
without claiming a personal value.
5. sentence_idea: the idea for the sentence(s), naming the specific product, mission or result ("" if left out).
6. needs_search: true only if NEITHER the posting nor the news backs a reason of type 1 or 2 (and no web \
search result is given yet)."""

STEP_B_SCHEMA = {"type": "object", "properties": {
    "who_benefits": {"type": "string"}, "fact": {"type": "string"},
    "found_in": {"type": "string", "enum": ["posting", "news", "web", "none"]}, "source": {"type": "string"},
    "reason_type": {"type": "string", "enum": ["1", "2", "left out"]},
    "connection": {"type": "string"}, "sentence_idea": {"type": "string"}, "needs_search": {"type": "boolean"}},
    "required": ["who_benefits", "fact", "found_in", "source", "reason_type", "connection", "sentence_idea",
                 "needs_search"], "additionalProperties": False}

SENT = {"type": "object", "properties": {
    "text": {"type": "string"},
    "kind": {"type": "string", "enum": ["experience", "project", "company", "interest"]}},
    "required": ["text", "kind"], "additionalProperties": False}
DRAFT_SCHEMA = {"type": "object", "properties": {
    "note": {"type": "array", "items": SENT},
    "subject": {"type": "string"},
    "body": {"type": "array", "items": {"type": "array", "items": SENT}},
    "follow_up": {"type": "array", "items": SENT}},
    "required": ["note", "subject", "body", "follow_up"], "additionalProperties": False}

WRITE_RULES = f"""You write a short LinkedIn message from a Staff Technical Program Manager to the recruiter \
for ONE job posting. Follow the CHOICES and WHY THIS COMPANY exactly. It is a short note to a person whose job is \
to fill this role - never the cover letter reworded, and never a sentence copied from it.

Return four parts. Code adds "{GREETING}" at the start of the note, the body and the follow-up, the posting \
link on its own line after the body's first paragraph, and the sign-off "Thank you," plus the name. Don't \
write any of those yourself.

1. note (connection request): at most {NOTE_MAX_CHARS} characters INCLUDING the greeting code adds \
("{GREETING} " is {len(GREETING) + 1} characters), so your sentences get about {NOTE_MAX_CHARS - len(GREETING) - 1}. \
The exact role title, that the candidate has applied, the single most relevant thing about them \
(note_highlight), and a light ask.
2. subject: at most {SUBJECT_MAX_CHARS} characters, e.g. "<exact role title> application - <name>".
3. body: paragraphs (each a list of sentences); {BODY_MIN_WORDS}-{BODY_MAX_WORDS} words and at most \
{BODY_MAX_CHARS} characters counting the greeting, link and sign-off (about 20 words), so your sentences get \
about {BODY_MIN_WORDS - 15}-{BODY_MAX_WORDS - 25} words:
   a. First paragraph: what the candidate applied for - the exact role title and that they applied ("I \
applied for the <exact role title> role", never "I've applied"). No date.
   b. Who they are in one or two sentences: the WHO I AM line under CANDIDATE POSITIONING, word for word, plus \
the top area (CHOICES), AND why that work mattered: the outcome it led to, from the chain (MY PERSPECTIVE as \
their view; PUBLIC PRODUCT FACTS as facts about the product). The depth wanted is shown by the DEPTH EXAMPLE \
under CANDIDATE POSITIONING: name the program, what the candidate owned and who the product helps - never just \
the product category.
   c. The two proof points from CHOICES, each with its number. Don't repeat the one used in (b).
   d. Why this company in one or two sentences, from WHY THIS COMPANY; leave it out if reason_type is "left out".
   e. A clear, low-pressure ask: a 15-minute call, or whether they're the right person for this role and if \
not, who is.
4. follow_up (about a week later): {FOLLOW_MIN_WORDS}-{FOLLOW_MAX_WORDS} words counting the greeting and \
sign-off (about 6 words): the exact role title, that the candidate is still very interested, follow_up_proof \
(not used in the first message), and the same ask. Polite, never pushy.

Hard rules:
- Facts about the candidate come ONLY from the EVIDENCE PASSAGES, VERIFIED METRICS, RESUME and IMPACT FACTS. \
Numbers only from VERIFIED METRICS, exactly as written; never round, combine or extrapolate. Never merge two \
programs or two metrics into one claim.
- PUBLIC PRODUCT FACTS are about the product only: never say or imply the candidate built those features. \
MY PERSPECTIVE lines read as the candidate's view, never as a measured result. MY FRAMING lines are the \
starting wording for (b), (c) and the note - the facts in them still have to be in the evidence.
- Company facts only from the JOB POSTING, VERIFIED NEWS or the WEB SEARCH RESULT.
- Commercial product names only, never internal codenames or internal shorthand.
- Direct, specific, warm, brief - a senior leader writing. No slogan-like or dramatic lines (e.g. "I want to \
be that owner", "someone has to hold the whole picture"). Any claim about how the candidate works or what they \
care about is backed by a specific example from their work.
- Never: "I hope this message finds you well", "I came across your profile", "I'm passionate about", "I'd love \
to pick your brain", "Dear Hiring Manager", generic praise, salary, a referral request, desperation, "available \
immediately". No emoji, no Markdown, plain text only. No gendered pronouns for the candidate.
- Label each sentence: "experience" (the candidate's past work, skills or results), "project" (the candidate's \
AI project), "company" (the company, its products or the role), "interest" (applying, interest, the ask). \
Every sentence is checked against the evidence and removed if unsupported.

{POSITIONING_RULES}"""

REVISE_RULES = WRITE_RULES + """

REVISION TASK: fix every problem listed under PROBLEMS and keep everything else that already works. A sentence \
the claim check would remove: reword it to match its evidence closely (same facts, same numbers, one program), \
or cut it. Return all four parts."""

SHORTEN_RULES = WRITE_RULES + """

SHORTENING TASK: the parts listed under OVER THE LIMIT are too long. Rewrite ONLY those parts so they fit, \
as complete sentences (never cut off mid-sentence): drop the least important clause or detail, keep the exact \
role title, the facts and the numbers exactly as they are, and add nothing new. Return all four parts; leave \
the parts that are within their limits exactly unchanged."""


# ------------------------------------------------------------------ checks measured by code

def automatic_checks(draft: dict, parts: Dict[str, str], post, letter_text: str) -> List[str]:
    out = [f"[limit] {p}" for p in over_limits(parts) + under_limits(parts)]
    title = post["title"].strip().lower()
    if title not in parts["note"].lower():
        out.append(f'[title] the connection note doesn\'t name the exact role title "{post["title"]}"')
    first = " ".join(x["text"] for x in (draft["body"][0] if draft["body"] else []))
    if title not in first.lower():
        out.append(f'[title] the body\'s first paragraph doesn\'t name the exact role title "{post["title"]}"')
    if title not in parts["follow_up"].lower():
        out.append(f'[title] the follow-up doesn\'t name the exact role title "{post["title"]}"')
    who = personal.positioning().get("who_i_am", "")
    if who and who.lower() not in parts["body"].lower():
        out.append(f'[who] the body doesn\'t say "{who}" (WHO I AM in settings.yaml)')
    every = ([("note", x) for x in draft["note"]] + [("body", x) for p in draft["body"] for x in p]
             + [("follow-up", x) for x in draft["follow_up"]])
    numbered = [x for p in draft["body"] for x in p if x["kind"] == "experience" and re.search(r"\d", x["text"])]
    if len(numbered) < 2:
        out.append("[proofs] the body needs at least two proof points with a number")
    full = "\n".join(parts.values())
    low = full.lower()
    out += [f'[tone] cliche or generic praise "{c}"' for c in CLICHES if c in low]
    out += [f'[tone] hype word "{w}"' for w in HYPE if re.search(rf"\b{re.escape(w)}\b", low)]
    out += [f"[rules] {why}" for pat, why in FORBIDDEN if re.search(pat, full, re.I)]
    if EMOJI.search(full):
        out.append("[format] contains emoji")
    if MARKDOWN.search(full):
        out.append("[format] contains Markdown formatting")
    for where, x in every:
        t = x["text"]
        if any(re.search(h, t, re.I) for h in SLOGAN_HINTS):
            out.append(f'[tone] slogan-like line in the {where}: "{t}"')
        if x["kind"] != "company" and is_claim_about_candidate(t, x["kind"]):
            out += [f'[ownership] {p} in the {where}: "{t}"' for p in positioning_problems(t)]
            if x["kind"] == "experience" and re.search(r"\b(we|our)\b", t, re.I):
                out.append(f'[ownership] "we" for the candidate\'s own work in the {where} (use "I"): "{t}"')
        if letter_text and _shares_run(t, letter_text, 7):
            out.append(f'[not the letter] repeats the cover letter\'s wording in the {where}: "{t}"')
    return out


# ------------------------------------------------------------------ step B helper: the one web search

def find_impact_fact(model: str, company: str, today: dt.date) -> Optional[dict]:
    """ONE web search for the company's own mission or impact page, or reputable reporting on its impact.
    Only kept if its link really came back from the search."""
    prompt = (f"Today's date is {today.isoformat()}. Run ONE web search for {company}'s own mission or impact "
              f"page, or reputable reporting on how {company}'s technology benefits people and society. Reply "
              f"with ONLY a JSON object (no other text) with keys \"title\", \"url\" (copied exactly from a search "
              f"result), \"date\" (YYYY-MM-DD, or \"\" if not stated), \"publisher\", \"summary\" (1-2 sentences "
              f"stating the specific benefit or result). Reply {{}} if nothing specific turns up.")
    text, results = ask_with_web_search(model, prompt, effort="low", max_searches=1)

    def key(url: str) -> str:
        return re.sub(r"^https?://(www\.)?", "", str(url)).split("#")[0].rstrip("/").lower()

    found = {key(r["url"]): r for r in results}
    m = re.search(r"\{.*\}", text, re.S)
    try:
        item = json.loads(m.group(0)) if m else {}
    except ValueError:
        return None
    hit = found.get(key(item.get("url", ""))) if item else None
    if not hit or not item.get("summary"):
        return None
    item["url"] = hit["url"]
    item["date"] = item.get("date") or hit.get("page_age", "")
    item["company_own_source"] = False
    return item


# ------------------------------------------------------------------ the whole step

def _draft_text(draft: dict) -> str:
    return json.dumps(draft, indent=1, ensure_ascii=False)


def _clean(draft: dict) -> dict:
    """Plain text only: strip emoji, and drop any greeting/sign-off/link the model added anyway."""
    def fix(x):
        t = EMOJI.sub("", x["text"]).replace("**", "").strip()
        return dict(x, text=t)

    def keep(x):
        t = x["text"].strip().lower()
        return t and not t.startswith(("hi [first name]", "hello [first name]", "thank you,")) \
            and not re.fullmatch(r"https?://\S+", t)
    return {"note": [fix(x) for x in draft["note"] if keep(x)], "subject": EMOJI.sub("", draft["subject"]).strip(),
            "body": [[fix(x) for x in p if keep(x)] for p in draft["body"]],
            "follow_up": [fix(x) for x in draft["follow_up"] if keep(x)]}


def write_message(model: str, check_model: str, post, ev: PostingEvidence, news: List[dict], resume: str,
                  letter_text: str, projects: Dict[str, str], name: str, today: dt.date, out_path: Path,
                  contact: str = "", final_model: str = "") -> Tuple[List[str], List[dict]]:
    """Write the recruiter message file. Returns (changes.txt lines, web items used for sources.txt)."""
    title = post["title"].strip()
    url = post["url"] or ""
    news_block = json.dumps(news, indent=1, ensure_ascii=False) if news else "(No verified news.)"
    context = (f"=== {ev.prompt_block()}\n\n{ev.rules.facts_block()}\n\nRESUME:\n{resume}\n\n"
               f"FROM impact_of_my_work.md:\n{impact_block(ev, ('impact fact', 'my framing', 'my perspective', 'public product fact', 'good fit'))}"
               f"\n\nJOB POSTING:\n{_posting_block(post)}\n\nVERIFIED NEWS ABOUT {post['company'].upper()}:\n{news_block}")
    notes: List[str] = []

    # A. choices
    a = ask_json(model, STEP_A_RULES + personal.prompt_block() + personal.positioning_block(), context, STEP_A_SCHEMA, effort="high")
    notes += ["", "RECRUITER MESSAGE: CHOICES",
              f"1. What this role exists to deliver: {a['role_delivers']}",
              f"   What the hiring manager would most want to see: {a['hiring_manager_wants']}",
              "2. Closest areas of my work (ranked):"]
    notes += [f"   {i}. {x['area']} - {x['why']}" for i, x in enumerate(a["ranked_areas"][:3], 1)]
    notes.append("3. Chain from my work to its outcome:")
    notes += [f"   -> {x['link']}  [{x['backed_by']}: {x['source']}]" for x in a["chain"]]
    notes.append("4. Proof points:")
    notes += [f"   - {x['proof']} (matches: {x['matches']}; source: {x['source']})" for x in a["proof_points"][:2]]
    notes += [f"   Connection note highlight: {a['note_highlight']}",
              f"   Follow-up proof point: {a['follow_up_proof']['proof']} (source: {a['follow_up_proof']['source']})"]

    # B. why this company (one web search only if the posting and news don't back a reason)
    web: List[dict] = []
    b_prompt = (f"JOB POSTING:\n{_posting_block(post)}\n\nVERIFIED NEWS:\n{news_block}\n\n"
                f"FROM impact_of_my_work.md:\n{impact_block(ev, ('my perspective', 'public product fact'))}")
    b = ask_json(model, STEP_B_RULES, b_prompt, STEP_B_SCHEMA, effort="medium")
    searched = "no (the posting or the verified news backs the reason)"
    if b["needs_search"]:
        item = find_impact_fact(check_model, post["company"], today)
        searched = "yes, one search with the scoring model: " + (
            f"{item['title']} ({item['url']})" if item else "nothing verifiable found")
        if item:
            web.append(item)
            b = ask_json(model, STEP_B_RULES, b_prompt + "\n\nWEB SEARCH RESULT:\n"
                         + json.dumps(item, indent=1, ensure_ascii=False), STEP_B_SCHEMA, effort="medium")
    notes += ["", "RECRUITER MESSAGE: WHY THIS COMPANY",
              f"1. Who benefits: {b['who_benefits']}",
              f"2. Backing fact: {b['fact'] or '(none)'} (found in: {b['found_in']}; source: {b['source'] or '-'})",
              f"   Web search: {searched}",
              f"3. Reason used: {b['reason_type']}"
              + {"1": " (benefit to people and society)", "2": " (genuinely innovative)",
                 "left out": " (nothing could be backed by a fact, so the message leaves it out)"}[b["reason_type"]],
              f"4. Connection to my work: {b['connection'] or '(none - states the company fact only)'}",
              f"   Sentence idea: {b['sentence_idea'] or '(left out)'}"]

    all_news = list(news) + web
    plan = (f"CHOICES:\n{json.dumps(a, indent=1, ensure_ascii=False)}\n\nWHY THIS COMPANY:\n"
            f"{json.dumps(b, indent=1, ensure_ascii=False)}")
    full_context = (context + ("\n\nWEB SEARCH RESULT:\n" + json.dumps(web, indent=1, ensure_ascii=False) if web else "")
                    + f"\n\nCANDIDATE NAME: {name}\nEXACT ROLE TITLE: {title}\n\nCOVER LETTER ALREADY SENT "
                      f"(don't reword it or copy its sentences):\n{letter_text}")

    def gate(m: str, d: dict):
        paras = [d["note"]] + d["body"] + [d["follow_up"]]
        return _evidence_gate(m, paras, post, ev, projects, all_news, resume, doc="RECRUITER MESSAGE",
                              keep_empty=True)

    # C. draft
    # the context and plan are the same for the draft, revision and shortening, so they are cached
    shared = f"{full_context}\n\n{plan}"
    draft = _clean(ask_json(model, WRITE_RULES + personal.prompt_block() + personal.positioning_block(),
                            "Write the message from the material above.", DRAFT_SCHEMA, effort="medium",
                            cache_context=shared))

    # D. code checks, then ONE revision if it fits the budget (the claim check runs once, at the end)
    for rnd in (1,):
        if not can_afford(REVISION_COST):
            notes += ["", "RECRUITER MESSAGE CHECKS", "- Skipped to stay within the budget per application. "
                      "The final claim check still ran."]
            break
        parts = assemble(draft, url, name, contact)
        problems = automatic_checks(draft, parts, post, letter_text)
        notes += ["", f"RECRUITER MESSAGE CHECKS (round {rnd})"] + ([f"- {p}" for p in problems] or ["- Nothing to fix."])
        if not problems:
            break
        draft = _clean(ask_json(model, REVISE_RULES + personal.prompt_block() + personal.positioning_block(),
                                f"YOUR DRAFT:\n{_draft_text(draft)}\n\nPROBLEMS:\n" + "\n".join(problems),
                                DRAFT_SCHEMA, effort="medium", cache_context=shared))

    # E. limits: the model shortens, code never cuts
    def shorten(d: dict, over: List[str]) -> dict:
        return _clean(ask_json(model, SHORTEN_RULES + personal.prompt_block() + personal.positioning_block(),
                               f"YOUR DRAFT:\n{_draft_text(d)}\n\nOVER THE LIMIT:\n" + "\n".join(over),
                               DRAFT_SCHEMA, effort="medium", cache_context=shared))
    draft, _, fit_notes = fit_to_limits(draft, lambda d: over_limits(assemble(d, url, name, contact)), shorten)
    if fit_notes:
        notes += ["", "RECRUITER MESSAGE LENGTH (the model shortened it; nothing was cut in code)"] + fit_notes

    # F. claim check, last (checking model): removes only code-rule failures, flags the rest
    kept, gate_notes, removed = gate(final_model or model, draft)
    n_note, n_body = 1, len(draft["body"])
    final = {"note": [{"text": kept[0], "kind": "interest"}] if kept[0] else [],
             "subject": draft["subject"],
             "body": [[{"text": t, "kind": "interest"}] for t in kept[1:1 + n_body]],
             "follow_up": [{"text": kept[1 + n_body], "kind": "interest"}] if kept[1 + n_body] else []}
    parts = assemble(final, url, name, contact)
    still_over = over_limits(parts)
    still_under = under_limits(parts)

    warnings = [f"TOO LONG: {p}" for p in still_over]
    warnings += [f"TOO SHORT after the claim check removed sentences: {p}" for p in still_under]
    n_flagged = sum(1 for ln in gate_notes if ln.startswith("- FLAGGED"))
    if n_flagged:
        warnings.append(f"{n_flagged} sentence(s) flagged for your review - see FLAGGED FOR YOUR REVIEW at the top "
                        "of changes.txt")
    if not final["note"]:
        warnings.append("The claim check removed the whole connection note; write your own before sending.")
    out_path.write_text(render(parts, warnings), encoding="utf-8")

    notes += gate_notes
    notes += ["", "REMOVED BY THE CODE RULES - RECRUITER MESSAGE (a number not in verified_metrics.txt, a "
                  "[CONFIRM] line or a title not on your resume)"]
    notes += [f'- REMOVED in the final check: "{s}"\n    Why: {why}' for s, why in removed]
    if not removed:
        notes.append("- Nothing was removed.")
    notes += ["", "RECRUITER MESSAGE: FINAL COUNTS",
              f"- Connection note: {chars(parts['note'])} of {NOTE_MAX_CHARS} characters",
              f"- Subject: {chars(parts['subject'])} of {SUBJECT_MAX_CHARS} characters",
              f"- Body: {words(parts['body'])} words ({BODY_MIN_WORDS}-{BODY_MAX_WORDS}), "
              f"{chars(parts['body'])} of {BODY_MAX_CHARS} characters",
              f"- Follow-up: {words(parts['follow_up'])} words ({FOLLOW_MIN_WORDS}-{FOLLOW_MAX_WORDS})"]
    notes += [f"- WARNING: {w}" for w in warnings]
    still = [c for c in automatic_checks(final, parts, post, letter_text)
             if not c.startswith(("[limit]", "[proofs]"))]     # limits are reported above; kinds are lost here
    notes += [f"- Still open: {c}" for c in still] or ["- Meets every automatic check."]
    return notes, web


def render(parts: Dict[str, str], warnings: List[str], body_words: Tuple[int, int] = None) -> str:
    """The .txt file: plain text, no Markdown, counts printed after each part.
    body_words: the (min, max) word range printed for the body (default: this module's limits)."""
    lo, hi = body_words or (BODY_MIN_WORDS, BODY_MAX_WORDS)
    lines = [TOP_LINE, ""]
    if warnings:
        lines += [f"WARNING - {w}" for w in warnings] + [""]
    lines += ["1. CONNECTION REQUEST NOTE (at most 200 characters, including spaces)", "",
              parts["note"], "", f"({chars(parts['note'])} of {NOTE_MAX_CHARS} characters)", "", "",
              "2. LINKEDIN MESSAGE (after they accept, or as an InMail)", "",
              f"Subject: {parts['subject']}", f"({chars(parts['subject'])} of {SUBJECT_MAX_CHARS} characters)", "",
              parts["body"], "",
              f"({words(parts['body'])} words, {lo}-{hi}; {chars(parts['body'])} of "
              f"{BODY_MAX_CHARS} characters)", "", "",
              "3. FOLLOW-UP MESSAGE (about a week later, if there's no reply)", "",
              parts["follow_up"], "",
              f"({words(parts['follow_up'])} words, {FOLLOW_MIN_WORDS}-{FOLLOW_MAX_WORDS})", ""]
    return "\n".join(lines)
