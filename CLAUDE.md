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

## Questions about the agent's results

For questions about postings the agent has found or scored (by date, company, score, role or
location), use the **job-postings** MCP server. It reads data/jobs.db read-only.

- When the user asks to see or list postings, use `export_to_spreadsheet`: it adds a new tab to
  Applications.xlsx in the same format as the main tab (with clickable links) and opens the file.
  Give the tab a short name that describes the query (e.g. "Nov 8+", "Google Nov 1-15"), then
  reply with the tab name and how many postings it holds.
- Use `query_postings` for counts or quick checks in the chat, `get_posting` for one posting's
  details, and `list_companies` to find a company's exact name.
- If the user's wording is ambiguous (for example "above 8"), say which reading you used.
- If Applications.xlsx is open in Excel, ask the user to close it and try again.

## Committing and pushing

- After the user approves a code change, commit it locally with a clear message.
- Then show the list of changed files, confirm no private files are included
  (`settings.yaml`, `.env`, `evidence/`, `style_examples/`, `output/`, `data/`,
  `Applications.xlsx`, resumes or cover letters), and ask whether to push to GitHub.
- Never push without the user's "approved".

## Privacy

- Never modify, move, or commit anything in `evidence/`. It is read-only and git-ignored.
- Never print or copy the API key in `.env`.
