"""Plain rules (no AI) that decide which postings to keep.

Keep a posting when ALL of these are true:
  1. The title is a TPM / EPM title.
  2. The title has no excluded word (Validation, Test, Quality, QA ...).
  3. Rule A (title says Senior/Staff/Principal/Lead) OR
     Rule B (top of posted base pay >= the threshold in settings).
  4. At least one location is near San Jose, or remote in the US.
"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .fetchers import Posting

US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee", "TX": "texas",
    "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}
US_WORDS = {"us", "u.s.", "usa", "u.s.a.", "united states", "united states of america", "america", "anywhere in the us"}
CA_WORDS = {"ca", "california", "calif."}
SPLIT_RE = re.compile(r"\s*(?:\||;|/|\n|\bor\b)\s*", re.I)
PREFIX_RE = re.compile(r"^\s*(?:hybrid|on-?site|in-?office)\s*[-–—:,(]?\s*", re.I)
REMOTE_RE = re.compile(r"\bremote\b", re.I)


def _words_re(words: List[str]) -> re.Pattern:
    alts = []
    for w in sorted(words, key=len, reverse=True):
        alts.append(re.escape(w).replace(r"\ ", r"\s+") + (r"\.?" if w.isalpha() else ""))
    return re.compile(r"(?<![A-Za-z])(?:" + "|".join(alts) + r")(?![A-Za-z])", re.I)


class Filter:
    def __init__(self, settings: dict):
        loc = settings.get("location", {})
        self.allow_remote = loc.get("allow_remote_us", True)
        places = list(loc.get("cities", [])) + list(loc.get("neighborhoods", [])) + list(loc.get("regions", []))
        self.places_re = _words_re(places)
        self.title_re = _words_re(settings.get("role_titles", []))
        self.senior_re = _words_re(settings.get("seniority_words", []))
        self.exclude_re = _words_re(settings.get("exclude_title_words", []))
        self.min_pay = float(settings.get("pay", {}).get("min_top_of_range_usd", 200000))

    # ---------------------------------------------------------- location
    def _qualifier_ok(self, rest: str, allow_any_us_state: bool) -> bool:
        """`rest` is whatever follows the place name (e.g. ', CA, USA').
        OK if empty, California, or US. Another state or a country -> not OK."""
        parts = [p.strip(" .()-–—").lower() for p in re.split(r"[,()]", rest) if p.strip(" .()-–—")]
        for p in parts:
            if p in CA_WORDS or p in US_WORDS:
                continue
            if allow_any_us_state and (p.upper() in US_STATES or p in US_STATES.values()):
                continue
            if p in ("hybrid", "remote", "on-site", "onsite", "office", "hq", "headquarters"):
                continue
            return False
        return True

    def location_ok(self, location: str) -> bool:
        seg = PREFIX_RE.sub("", location.strip())
        if not seg:
            return False
        if REMOTE_RE.search(seg):
            if not self.allow_remote:
                return False
            rest = REMOTE_RE.sub("", seg)
            m = self.places_re.search(rest)
            if m:
                rest = rest[:m.start()] + rest[m.end():]
            rest = re.sub(r"\b(?:based|in|within|the|only|first|friendly|position)\b", " ", rest, flags=re.I)
            rest = re.sub(r"\s*[-–—]\s*", ",", rest)
            return self._qualifier_ok(rest, allow_any_us_state=True)
        m = self.places_re.search(seg)
        if not m:
            return False
        before = seg[:m.start()].strip(" ,-–—")
        if before and before.lower() not in US_WORDS | {"us", "united states", "usa"}:
            # Something unusual before the city ("US, CA, Sunnyvale" style is fine)
            if not self._qualifier_ok(before, allow_any_us_state=False):
                return False
        return self._qualifier_ok(seg[m.end():], allow_any_us_state=False)

    def any_location_ok(self, locations: List[str]) -> bool:
        for loc in locations:
            for seg in SPLIT_RE.split(loc or ""):
                if seg and self.location_ok(seg):
                    return True
        return False

    # ---------------------------------------------------------- full check
    def check(self, p: Posting) -> Tuple[bool, str]:
        title = p.title
        if not self.title_re.search(title):
            return False, "not a TPM/EPM title"
        bad = self.exclude_re.search(title)
        if bad:
            return False, f"excluded word in title: {bad.group(0)}"
        senior = bool(self.senior_re.search(title))
        pay_ok = p.pay_top is not None and p.pay_top >= self.min_pay
        if not (senior or pay_ok):
            return False, "not senior-level and posted pay below threshold or not listed"
        if not self.any_location_ok(p.locations):
            return False, "location not near San Jose or US-remote"
        why = "senior-level title" if senior else f"pay top ${p.pay_top:,.0f}"
        return True, why


def apply_filters(settings: dict, postings: List[Posting]) -> List[Tuple[Posting, str]]:
    f = Filter(settings)
    kept = []
    for p in postings:
        ok, why = f.check(p)
        if ok:
            kept.append((p, why))
    return kept
