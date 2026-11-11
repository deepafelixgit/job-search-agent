"""Batch access to the career-evidence tools for the job agent.

The job agent runs in its own Python environment (3.9) that can't load the MCP
libraries, so it starts this module with the MCP server's environment and talks
to it through JSON on stdin/stdout. It calls the SAME functions the MCP server
exposes to Claude (search_evidence, check_claim, get_verified_metrics), so the
agent and Claude Code apply identical rules.

Input (stdin, JSON):
  {"search": [{"query": "...", "max_results": 4, "doc_type": "verified metrics" (optional)}, ...],
   "check":  ["claim sentence", ...],
   "metrics": true,
   "read_pdf": ["file.pdf"]}          (PDFs in style_examples/ only)
Output (stdout, JSON): {"search": [...], "check": [...], "metrics": {...}}

Run by agent/evidence_gate.py:  .venv-mcp/bin/python -m mcp_server.batch
"""
from __future__ import annotations

import json
import sys

from . import server


def _fn(tool):
    return getattr(tool, "fn", tool)


def main() -> None:
    req = json.load(sys.stdin)
    out = {}
    if req.get("metrics"):
        out["metrics"] = _fn(server.get_verified_metrics)(None)
    out["search"] = [_fn(server.search_evidence)(q["query"], doc_type=q.get("doc_type"),
                                                 max_results=q.get("max_results", 4))
                     for q in req.get("search", [])]
    out["check"] = [_fn(server.check_claim)(c) for c in req.get("check", [])]
    if req.get("read_pdf"):
        # text of PDFs in style_examples/ only (the job agent's own environment can't read PDFs)
        from pathlib import Path
        from pypdf import PdfReader
        allowed = (Path(__file__).resolve().parent.parent / "style_examples").resolve()
        out["read_pdf"] = {}
        for name in req["read_pdf"]:
            path = (allowed / name).resolve()
            if allowed not in path.parents or path.suffix.lower() != ".pdf":
                continue
            out["read_pdf"][name] = "\n".join(pg.extract_text() or "" for pg in PdfReader(str(path)).pages)
    json.dump(out, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
