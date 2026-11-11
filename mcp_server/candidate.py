"""The candidate's name, read from settings.yaml (private) so it isn't written in the code.

The MCP server's environment has no YAML library, so this reads the one line it needs:
the `name:` under `candidate:`.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List

SETTINGS = Path(__file__).resolve().parent.parent / "settings.yaml"


@lru_cache(maxsize=1)
def name() -> str:
    try:
        lines = SETTINGS.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    inside = False
    for line in lines:
        if re.match(r"^candidate:\s*$", line):
            inside = True
        elif inside and re.match(r"^\S", line):
            break
        elif inside:
            m = re.match(r"^\s+name:\s*[\"']?([^\"'#]+?)[\"']?\s*(#.*)?$", line)
            if m:
                return m.group(1).strip()
    return ""


def abbreviations() -> Dict[str, str]:
    """personal.abbreviations from settings.yaml, written on one line: {abbr: meaning, abbr: meaning}."""
    try:
        text = SETTINGS.read_text(encoding="utf-8")
    except OSError:
        return {}
    m = re.search(r"^\s+abbreviations:\s*\{([^}]*)\}", text, re.M)
    out = {}
    for pair in (m.group(1).split(",") if m else []):
        if ":" in pair:
            k, v = pair.split(":", 1)
            out[k.strip().strip("'\"").lower()] = v.strip().strip("'\"").lower()
    return out


def name_words() -> List[str]:
    return [w.lower() for w in name().split()]
