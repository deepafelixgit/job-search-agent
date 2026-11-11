"""Keyword search (BM25) over the evidence passages.

How it works, in plain words:
  1. Every passage is broken into words. Endings are trimmed ("scaling", "scaled"
     -> "scal") so different forms of a word match.
  2. Codenames and abbreviations are expanded before indexing and searching, so
     a codename also matches its product name and "MTTR" matches "mean time to resolve".
  3. BM25 gives each passage a score: words that appear in few passages (like
     a product name or a number) count much more than common words (like "team"), and a
     match in a short passage counts a bit more than in a long one.
Everything runs in memory on this Mac. No downloads, accounts or API calls.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, Iterable, List, Optional, Tuple

from .documents import Passage

STOP = set("""a an the and or but of to in on at for with by from as is are was were be been being it its this that
these those i me my we our you your he she they them their his her do did does have has had not no so
than then there here very really also just about into over under more most can could would should will may might
which who whom what when where how why all any each some such per via vs""".split())
from .candidate import name_words as _name_words
STOP |= set(_name_words())        # the candidate's own name (from settings.yaml) isn't a search word

# Common abbreviations in this work history (others are read from verified_metrics.txt)
ABBREVIATIONS = {
    "tpm": "technical program manager", "epm": "engineering program manager", "xfn": "cross functional",
    "ota": "over the air", "ci": "continuous integration", "kpi": "key performance indicator",
    "ee": "electrical engineering", "em": "engineering manager", "ems": "engineering managers",
    "npi": "new product introduction", "hil": "hardware in the loop", "qa": "quality assurance",
    "ar": "augmented reality", "vr": "virtual reality", "mr": "mixed reality", "ml": "machine learning",
    "okr": "objectives and key results",
}
from .candidate import abbreviations as _personal_abbreviations
ABBREVIATIONS.update(_personal_abbreviations())    # employer-specific ones, from settings.yaml (personal.abbreviations)


def stem(word: str) -> str:
    if word[0].isdigit():
        return word
    for suffix in ("ational", "ations", "ation", "ments", "ment", "ings", "ing", "ities", "ity", "ies",
                   "ied", "ers", "er", "ed", "es", "ly", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def tokens(text: str) -> List[str]:
    out = []
    for raw in re.findall(r"\d+(?:\.\d+)?%?|[a-z][a-z0-9'\-]*", text.lower()):
        raw = raw.strip("'-")
        if not raw or raw in STOP:
            continue
        if raw[0].isdigit():
            out.append(raw.rstrip("%"))
            continue
        for part in raw.split("-") if "-" in raw else [raw]:
            if part and part not in STOP:
                out.append(stem(part))
    return out


def numbers_in(text: str) -> List[str]:
    """Numbers stated in a claim: '50+' -> '50', '45%+' -> '45', '35-55' -> '35', '55'."""
    nums = []
    # Dates and labels aren't metrics: skip half/quarter labels like "H1", "Q3", "P1"
    # (a digit right after a letter) and short years like "'25".
    for n in re.findall(r"(?<![A-Za-z\d.'’])\d+(?:\.\d+)?", text):
        if re.fullmatch(r"(19|20)\d\d", n):          # a year, e.g. "2024", is a date, not a metric
            continue
        if n not in nums:
            nums.append(n)
    return nums


class Index:
    def __init__(self, passages: List[Passage], expansions: Dict[str, str]):
        self.passages = passages
        self.expansions = {k.lower(): v for k, v in expansions.items()}
        self.docs = [tokens(self._expand(self._index_text(p))) for p in passages]
        self.df: Counter = Counter()
        for d in self.docs:
            self.df.update(set(d))
        self.avg_len = sum(len(d) for d in self.docs) / max(1, len(self.docs))
        self.tf = [Counter(d) for d in self.docs]

    @staticmethod
    def _index_text(p: Passage) -> str:
        return f"{p.text} {p.location}"

    def _expand(self, text: str) -> str:
        extra = []
        low = text.lower()
        for key, value in self.expansions.items():
            if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", low):
                extra.append(value)
            if len(value) > 5 and value.lower() in low:
                extra.append(key)
        return text + (" " + " ".join(extra) if extra else "")

    def query_terms(self, query: str) -> List[str]:
        return tokens(self._expand(query))

    def score(self, terms: Iterable[str], i: int, k1: float = 1.5, b: float = 0.75) -> float:
        n = len(self.docs)
        dl = len(self.docs[i])
        s = 0.0
        for t in set(terms):
            f = self.tf[i].get(t, 0)
            if not f:
                continue
            idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / self.avg_len))
        return s

    def search(self, query: str, top_k: int = 8, doc_types: Optional[List[str]] = None,
               boost: Optional[Dict[str, float]] = None) -> List[Tuple[float, Passage, float]]:
        """Returns [(score, passage, coverage)] where coverage = share of the query's
        distinct words found in the passage."""
        terms = self.query_terms(query)
        if not terms:
            return []
        uniq = set(tokens(query)) or set(terms)      # coverage = the query's own words
        results = []
        for i, p in enumerate(self.passages):
            if doc_types and p.doc_type not in doc_types:
                continue
            s = self.score(terms, i)
            if s <= 0:
                continue
            s *= (boost or {}).get(p.doc_type, 1.0)
            coverage = len(uniq & set(self.tf[i])) / len(uniq)
            results.append((s, p, coverage))
        results.sort(key=lambda r: r[0], reverse=True)
        # don't return near-identical passages twice
        out, seen = [], set()
        for r in results:
            key = re.sub(r"\W+", "", r[1].text.lower())[:120]
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
            if len(out) >= top_k:
                break
        return out


def acronyms_from_text(text: str) -> Dict[str, str]:
    """Find 'MTTR (mean time to resolve)' and 'Build Success Rate (BSR)' pairs."""
    found = {}
    for m in re.finditer(r"\b([A-Z]{2,6})\s*\(([A-Za-z][A-Za-z \-]{3,60})\)", text):
        found[m.group(1)] = m.group(2)
    for m in re.finditer(r"([A-Z][A-Za-z \-]{3,60})\s*\(([A-Z]{2,6})\)", text):
        words = m.group(1).strip().split()[-len(m.group(2)):]
        found[m.group(2)] = " ".join(words)
    return found


NEGATION = re.compile(r"\b(?:not|no|never|none|neither|nor|without)\b|n't\b", re.I)


def contradicting_sentences(claim: str, text: str, min_cover: float = 0.5) -> List[str]:
    """Sentences that talk about the same thing as the claim but negate it
    (e.g. claim "Built an ML model" vs evidence "No ML model was built")."""
    if NEGATION.search(claim):
        return []
    claim_terms = set(tokens(claim))
    if not claim_terms:
        return []
    out = []
    for sent in re.split(r"(?<=[.;!?])\s+|\s*;\s*", text):
        st = set(tokens(sent))
        if NEGATION.search(sent) and len(claim_terms & st) / len(claim_terms) >= min_cover:
            out.append(sent.strip())
    return out
