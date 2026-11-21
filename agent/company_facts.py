"""Company facts from the Daily Tech Briefing archive. No Claude API calls.

  python -m agent.company_facts "<company>"      stories from the last 120 days that name the company

The archive is a folder of YYYY-MM-DD.json files (schema_version 1), set as briefing_archive_dir in
settings.yaml (empty = skip). A story counts only when the company is named in its headline or summary;
being in its "companies" list alone isn't enough.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional

DAYS = 120


def _names(company: str) -> re.Pattern:
    base = re.sub(r"\s*\(.*?\)\s*", " ", company).strip()          # "Pure Storage (Everpure)" -> "Pure Storage"
    return re.compile(rf"(?<![\w-]){re.escape(base)}(?![\w-])", re.I)


def stories(archive: Optional[str], company: str, today: dt.date = None, days: int = DAYS) -> List[Dict]:
    """Stories naming the company in the headline or summary, newest first, from the last `days` days."""
    if not archive:
        return []
    folder = Path(archive).expanduser()
    if not folder.is_dir():
        return []
    today = today or dt.date.today()
    pat = _names(company)
    out, seen = [], set()
    for f in sorted(folder.glob("[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json"), reverse=True):
        try:
            day = dt.date.fromisoformat(f.stem)
        except ValueError:
            continue
        if (today - day).days > days or day > today:
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("schema_version") != 1:
            continue
        for s in data.get("stories") or []:
            text = f"{s.get('headline', '')} {s.get('summary', '')}"
            key = s.get("url") or s.get("headline")
            if pat.search(text) and key not in seen:
                seen.add(key)
                out.append({"briefing": f.stem, "headline": s.get("headline", ""), "published": s.get("published", ""),
                            "source": s.get("source", ""), "url": s.get("url", ""), "summary": s.get("summary", "")})
    return out


def main(argv: List[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    from .config import load_settings
    archive = load_settings().get("briefing_archive_dir") or ""
    if not archive:
        print("briefing_archive_dir is empty in settings.yaml: skipped.")
        return 0
    found = stories(archive, " ".join(argv))
    print(json.dumps(found, indent=1, ensure_ascii=False) if found else
          f"No briefing stories in the last {DAYS} days name {' '.join(argv)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
