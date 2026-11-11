"""Respect each website's robots.txt (its published rules for automated readers).

Every request the agent makes goes through allowed(url) first. Rules are read
the standard way (RFC 9309, the way Google reads them): the most specific
(longest) matching rule wins, and Allow wins a tie. '*' and '$' wildcards work.
If a site has no robots.txt, reading it is allowed.
"""
from __future__ import annotations

import re
import threading
from typing import Dict, List, Tuple
from urllib.parse import urlsplit

import requests

AGENT = "personal-job-search-agent"
_cache: Dict[str, List[Tuple[bool, str]]] = {}
_lock = threading.Lock()


class RobotsDisallowed(Exception):
    pass


def _parse(text: str) -> List[Tuple[bool, str]]:
    """Return the (allow, path) rules that apply to us: our own group if the
    file has one, otherwise the '*' group."""
    groups: Dict[str, List[Tuple[bool, str]]] = {}
    agents: List[str] = []
    in_rules = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if in_rules:
                agents, in_rules = [], False
            agents.append(value.lower())
        elif key in ("allow", "disallow"):
            in_rules = True
            if not value:
                continue            # "Disallow:" with nothing = allow everything
            for a in agents:
                groups.setdefault(a, []).append((key == "allow", value))
    for name, rules in groups.items():
        if name != "*" and name in AGENT:
            return rules
    return groups.get("*", [])


def _matches(rule: str, path: str) -> bool:
    pattern = re.escape(rule).replace(r"\*", ".*")
    if pattern.endswith(r"\$"):
        pattern = pattern[:-2] + "$"
    return re.match(pattern, path) is not None


def _rules_for(base: str) -> List[Tuple[bool, str]]:
    with _lock:
        if base in _cache:
            return _cache[base]
    # Retried like any request. If robots.txt still can't be read (network error), the error is
    # raised and the company counts as failed for this run - never assumed to allow everything.
    from . import http
    r = http.fetch("GET", base + "/robots.txt", headers={"User-Agent": AGENT + "/1.0"})
    if r.status_code >= 500:
        raise requests.HTTPError(f"{base}/robots.txt returned {r.status_code}")
    rules = _parse(r.text) if r.status_code == 200 else []      # 404 etc.: the site has no rules
    with _lock:
        _cache[base] = rules
    return rules


def allowed(url: str) -> bool:
    parts = urlsplit(url)
    path = (parts.path or "/") + (("?" + parts.query) if parts.query else "")
    best_len, best_allow = -1, True
    for allow, rule in _rules_for(f"{parts.scheme}://{parts.netloc}"):
        if _matches(rule, path) and (len(rule) > best_len or (len(rule) == best_len and allow)):
            best_len, best_allow = len(rule), allow
    return best_allow


def check(url: str) -> None:
    if not allowed(url):
        raise RobotsDisallowed(f"{urlsplit(url).netloc} asks automated readers not to read this "
                               f"address (robots.txt), so it was skipped.")
