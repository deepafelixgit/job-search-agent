"""Personal word lists, kept in settings.yaml (private) under `personal:`, never in code.

  internal_terms     employer-internal shorthand that must never reach a resume or letter
  explain_terms      internal terms allowed only with a short plain-language explanation
  translations       approved plain-language swaps: [[regex, plain words], ...]
  codename_words     internal codenames (also read from verified_metrics.txt)
  experience_words   employers, products and schools from your own history: a sentence naming
                     one of these is about you, so it must be backed by evidence

settings.yaml is re-read when it changes, so edits apply on the next resume or letter.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .config import SETTINGS_FILE, load_settings

_cache: dict = {"mtime": None, "data": {}}


def _data() -> dict:
    try:
        mtime = SETTINGS_FILE.stat().st_mtime
    except OSError:
        return {}
    if _cache["mtime"] != mtime:
        _cache["data"] = load_settings().get("personal") or {}
        _cache["mtime"] = mtime
    return _cache["data"]


def _list(key: str) -> List[str]:
    return [str(x) for x in _data().get(key) or [] if str(x).strip()]


def internal_terms() -> List[str]:
    return _list("internal_terms")


def explain_terms() -> List[str]:
    return _list("explain_terms")


def codename_words() -> List[str]:
    return _list("codename_words")


def experience_words() -> List[str]:
    return _list("experience_words")


def translations() -> List[Tuple[str, str]]:
    out = []
    for pair in _data().get("translations") or []:
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            out.append((str(pair[0]), str(pair[1])))
    return out


def experience_match(sentence: str) -> Optional[re.Match]:
    """Does the sentence name an employer, product or school from the candidate's history?"""
    words = experience_words()
    if not words:
        return None
    return re.search(r"\b(" + "|".join(re.escape(w) for w in words) + r")\b", sentence, re.I)


def readable(pattern: str) -> str:
    """'\\bbelow the plan\\b' -> 'below the plan' (for showing a translation in a prompt)."""
    return pattern.replace("\\b", "").replace("\\", "")


def prompt_block() -> str:
    """The personal terms, appended to the writing and checking prompts."""
    lines = []
    if internal_terms():
        lines.append("INTERNAL SHORTHAND - never write these unless explained in plain words: "
                     + ", ".join(f'"{t}"' for t in internal_terms()))
    if explain_terms():
        lines.append("Allowed only with a short plain-language explanation in the same sentence: "
                     + ", ".join(f'"{t}"' for t in explain_terms()))
    if translations():
        lines.append("APPROVED PLAIN-LANGUAGE TRANSLATIONS (each counts as the same meaning as the original): "
                     + "; ".join(f'"{readable(p)}" = "{plain}"' for p, plain in translations()))
    return ("\n\n" + "\n".join(lines)) if lines else ""
