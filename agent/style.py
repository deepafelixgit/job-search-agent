"""Style examples: cover letters in style_examples/ used for TONE AND STRUCTURE ONLY.

They are shown to Claude when planning, drafting, critiquing, revising and repairing a
letter - never to the claim check, so no fact can be taken from them. Code also blocks
copying: any letter sentence sharing a run of 7+ words with an example, or an opening or
closing line close to an example's, is flagged during revision and removed by the final check.
"""
from __future__ import annotations

import difflib
import re
from functools import lru_cache
from pathlib import Path
from typing import List, Tuple

import docx

from .config import ROOT

STYLE_DIR = ROOT / "style_examples"
RUN = 7                       # shared consecutive words that count as copying


def _clean(text: str) -> str:
    # PDF text often comes out one word per line: rebuild normal spacing, keep paragraph breaks
    text = re.sub(r"[ \t]*\n[ \t]*\n[ \t]*", " ", text)
    text = re.sub(r"\s+", " ", text).replace(" ", "\n\n")
    return text.strip()


@lru_cache(maxsize=1)
def load_examples() -> Tuple[Tuple[str, str], ...]:
    if not STYLE_DIR.is_dir():
        return ()
    out, pdfs = [], []
    for f in sorted(STYLE_DIR.iterdir()):
        suf = f.suffix.lower()
        if suf in (".txt", ".md"):
            out.append((f.name, _clean(f.read_text(encoding="utf-8", errors="replace"))))
        elif suf == ".docx":
            out.append((f.name, _clean("\n\n".join(p.text for p in docx.Document(str(f)).paragraphs))))
        elif suf == ".pdf":
            pdfs.append(f.name)
    if pdfs:
        from .evidence_gate import run_batch
        for name, text in run_batch({"read_pdf": pdfs}).get("read_pdf", {}).items():
            out.append((name, _clean(text)))
    return tuple(out)


def prompt_block() -> str:
    ex = load_examples()
    if not ex:
        return ""
    return ("STYLE EXAMPLES - for TONE AND STRUCTURE ONLY. Never copy their sentences, opening line or closing "
            "line, and never take any fact from them (facts come only from the evidence). Where an example "
            "differs from the standard (e.g. how it opens or closes), the standard wins.\n\n"
            + "\n\n".join(f"--- {name} ---\n{text}" for name, text in ex))


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9$%+']+", text.lower().replace("’", "'"))


def _sentences(text: str) -> List[str]:
    """The letter body's sentences: after the "Dear ...," greeting, before the sign-off."""
    m = re.search(r"\bDear [^,\n]+,", text)
    body = text[m.end():] if m else text
    body = re.split(r"\b(Sincerely|Best regards|Regards|Thank you),", body)[0]
    return [s for s in re.split(r"(?<=[.!?])\s+", body.strip()) if len(_words(s)) >= 4]


PRONOUNS = {"i", "my", "me", "we", "our", "i'm", "i've", "i'd", "she", "her"}


def _cwords(text: str) -> List[str]:
    """Words for the copy check, ignoring pronouns ("I ran..." vs "ran..." is the same wording)."""
    return [w for w in _words(text) if w not in PRONOUNS]


def copied_from(sentence: str, evidence_text: str = "") -> str:
    """Name of the example this sentence copies from (7+ consecutive shared words), or "".
    Runs that also appear in the evidence don't count: the examples are the candidate's own letters,
    so they share wording with her records, and evidence wording is allowed."""
    w = _cwords(sentence)
    grams = {tuple(w[i:i + RUN]) for i in range(len(w) - RUN + 1)}
    if evidence_text:
        ev = _cwords(evidence_text)
        grams -= {tuple(ev[i:i + RUN]) for i in range(len(ev) - RUN + 1)}
    if not grams:
        return ""
    for name, text in load_examples():
        ew = _cwords(text)
        if grams & {tuple(ew[i:i + RUN]) for i in range(len(ew) - RUN + 1)}:
            return name
    return ""


def echoes_line(sentence: str, which: str) -> str:
    """Name of the example whose opening ('first') or closing ('last') line this sentence resembles."""
    for name, text in load_examples():
        sents = _sentences(text)
        if not sents:
            continue
        ref = sents[0] if which == "first" else sents[-1]
        if difflib.SequenceMatcher(None, " ".join(_words(sentence)), " ".join(_words(ref))).ratio() >= 0.6:
            return name
    return ""
