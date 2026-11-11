"""Collect the candidate's evidence: the resume, plus any extra files listed
under candidate.evidence_files in settings.yaml (self reviews, peer reviews,
performance reviews...). The cover letter may only make claims about the
candidate that this evidence backs up.

Supported: .docx, .txt, .md. Files live in the evidence/ folder, which git ignores.
"""
from __future__ import annotations

from typing import List, Tuple

import docx

from .config import ROOT, resume_path
from .resume import resume_text


def _read(path) -> str:
    if path.suffix.lower() == ".docx":
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(t for t in parts if t.strip())
    if path.suffix.lower() in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="replace")
    raise ValueError(f"{path.name}: only .docx, .txt and .md evidence files are supported")


def load_evidence(settings: dict) -> Tuple[str, List[str]]:
    """Returns (all evidence as one text block, notes about files that couldn't be read)."""
    blocks = [f"=== EVIDENCE: RESUME ===\n{resume_text(resume_path(settings))}"]
    notes = []
    for name in settings.get("candidate", {}).get("evidence_files") or []:
        path = (ROOT / name).resolve()
        try:
            if ROOT not in path.parents:
                raise ValueError("must be inside the job-search-agent folder")
            blocks.append(f"=== EVIDENCE: {path.name} ===\n{_read(path)}")
        except Exception as e:
            notes.append(f"- Evidence file '{name}' was skipped: {e}")
    return "\n\n".join(blocks), notes
