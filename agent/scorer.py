"""Score each posting 1-10 for fit against the resume, with a short reason."""
from __future__ import annotations

import re

from .claude_client import ClaudeError, ask_json

SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "enum": list(range(1, 11))},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
    "additionalProperties": False,
}

INSTRUCTIONS = """You assess how well a candidate fits a job posting. You'll get the \
candidate's resume (below) and one posting.

Score 1-10:
 9-10  Exceptional fit: the core of the role matches what the candidate has done repeatedly, at the right level.
 7-8   Strong fit: most key requirements are clearly covered; one or two gaps.
 5-6   Partial fit: transferable experience, but meaningful gaps in domain or required skills.
 3-4   Weak fit: few key requirements covered.
 1-2   Poor fit.

Judge on the posting's actual requirements (domain, technical depth, scope, seniority), not \
keyword overlap. The reason must be 2-3 sentences: name the strongest specific matches from \
the resume and the most important gap, if any. Plain, direct language. Refer to the person \
as "the candidate"; never use gendered pronouns (he/she/her/his).

CANDIDATE RESUME:
"""


def score_posting(model: str, resume_text: str, p) -> dict:
    system = [{"type": "text", "text": INSTRUCTIONS + resume_text, "cache_control": {"type": "ephemeral"}}]
    pay = f"${p.pay_top:,.0f} (top of posted base range)" if p.pay_top else "not listed"
    prompt = (f"POSTING\nCompany: {p.company}\nTitle: {p.title}\nLocation: {p.location_display}\n"
              f"Pay: {pay}\n\nDescription:\n{p.description}")
    try:
        result = ask_json(model, system, prompt, SCHEMA, effort="medium", max_tokens=16000)
    except ClaudeError as e:
        if "cut off" not in str(e):
            raise
        # The answer (thinking included) ran past the limit: retry once with more room and less thinking.
        result = ask_json(model, system, prompt, SCHEMA, effort="low", max_tokens=32000)
    return {"score": int(result["score"]), "reason": _two_or_three_sentences(result["reason"])}


def _two_or_three_sentences(reason: str) -> str:
    """Keep the reason to at most 3 sentences."""
    reason = re.sub(r"[\s}\]\"]+$", "", reason.strip())       # stray JSON characters at the end
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", reason)
    return " ".join(parts[:3])
