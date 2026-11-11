# Job search agent: instructions for Claude

## Resume and cover letter work: evidence only

For ANY resume or cover letter work in this project (writing, tailoring, editing, reviewing,
or answering questions about the candidate's experience), always use the **career-evidence** MCP
server, and only make claims it supports.

- Before writing a claim about the candidate's experience, find it with `search_evidence` or
  `get_verified_metrics`, and confirm it with `check_claim`.
- Numbers come only from `get_verified_metrics` (verified_metrics.txt is authoritative and
  overrides every other document). Use them exactly as written; never round, combine or
  extrapolate.
- Never merge two programs or two metrics into one claim.
- Use commercial names, not internal codenames (the server returns the codename list).
- Never use anything marked as needing confirmation ([CONFIRM]).
- If `check_claim` says "contradicted by evidence" or "no supporting evidence found",
  leave the claim out. If it says "partly supported", fix the claim to match the
  evidence or leave it out.
- When you show the candidate a claim, cite the source document, review period, and page or section.

## Privacy

- Never modify, move, or commit anything in `evidence/`. It is read-only and git-ignored.
- Never print or copy the API key in `.env`.
