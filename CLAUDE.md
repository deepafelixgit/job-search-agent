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

### impact_of_my_work.md (in evidence/)

Why the candidate's work mattered, by area. The career-evidence server labels each line:
- **Fact / Boundary** ("impact fact"): evidence of what the candidate did, or did not do.
- **Perspective** ("my perspective"): the candidate's own view. Present it as their view, never as a
  measured result, and never as evidence of a fact.
- **Public fact** ("public product fact"): what the product does for people, with a public source. A claim
  about the product only. Never say or imply the candidate built those features: they led the automated
  hardware-in-the-loop test infrastructure, automated continuous integration testing and programs behind
  the products; other teams built the features.
- **Say it like this** and the "How to position my work" section ("my framing"): the preferred starting
  wording. Never evidence: every fact in it still needs a fact line, a review or verified_metrics.txt.
- **Good fit for** ("good fit"): which roles an area suits. For choosing what to mention only.

### Positioning (standing rules for the resume, cover letter and recruiter message)

- Never call the candidate's work just "test infrastructure", "testing" or "test lab". Say "automated
  hardware-in-the-loop test infrastructure" or "automated continuous integration (CI) testing". On its own,
  "test infrastructure" sounds like a QA role, which theirs was not. The evidence check treats these as the
  same work as the "test lab" in the record.
- Position the work confidently, at the level in settings.yaml (`personal.positioning.level`):
  - Lead with the outcome and the stakes (`personal.positioning.stakes_examples`), then the number.
  - Use the strongest ownership verb the record supports: led, owned, drove, originated, turned around,
    scaled, built, aligned. Never a stronger one than the record shows.
  - Never undersell with "helped", "supported", "assisted", "was involved in", "contributed to",
    "participated in", "worked on" or "tried to". These are defects: code flags them and they get rewritten.
  - Name the scale the record states (teams, devices, programs). "I" for what the candidate did, not "we".
  - Confident is not inflated: numbers stay exact and the boundaries in the evidence still hold.
- The "Say it like this" lines in impact_of_my_work.md are available as starting wording for all three
  documents, subject to the evidence rules above.

### Recruiter message (API path, `generation_mode: api`)

For applications written in Claude Code, the rules in "Writing applications" below apply instead.
For every posting where the agent writes a resume and cover letter (not "resume only"), it also writes
`<Name>_Recruiter_Message_<Company>_<Role>.txt` in the same folder: a LinkedIn connection note (at most 200
characters), an InMail subject (at most 200 characters) and body (120-180 words, at most 1,900 characters),
and a follow-up (40-70 words). The candidate sends it themself; the agent never contacts LinkedIn or any
website. Its reasoning is in changes.txt under "Recruiter message: choices" and "Recruiter message: why this
company", and every sentence goes through the same claim check as the cover letter. Code agent/recruiter.py.

## Writing applications (Claude Code, no API cost)

Claude Code writes the tailored resume, cover letter and recruiter message with the write-application
skill (`/write-application`, or the web app's "Create custom resume and cover letter" button with
`generation_mode: claude_code`). The agent still finds and scores postings through the API.

- HARD RULE: writing an application costs $0 in API charges. Never run an agent command that calls the
  Claude API (scoring, `run_daily.py`, `agent.generate`, benchmark or audit commands), never import or call
  the anthropic library, and never read, print or use `.env` or the API key. Build the documents only with
  `python -m agent.render_application <folder>` (no API calls).
- Candidate specifics (products, AI work to lead with, safety tie-in, boundaries, the reviewed example
  application) are in `settings.yaml` under `personal.positioning`. Refer to them; never copy them into a
  committed file.
- Cover letter, asks-first: 330 to 430 words, 5 paragraphs or fewer, one page. Open with a specific,
  sourced company fact about why now, tied to the problem it creates and to what the candidate led.
  Paragraphs 2 to 4 each name one of the posting's top three asks in the posting's own words and answer it
  (the first with the in-depth story: situation, decision, who was aligned, trade-off, result; the others
  with one strong proof each), introduced in varied ways so it never reads like a template. Close in 3
  sentences or fewer: why this company, tied to something the candidate did where true, and a polite
  request to talk. AI only when the posting values AI, ML or automation, as the answer to that ask.
  Naming the posting's own asks is not prescribing; telling the company what it needs is.
- Recruiter message courtesy: the connection note (at most 200 characters) ends with "Would you be open
  to a quick chat?"; the body (120 to 220 words, at most 1,900 characters) opens "Thank you for
  connecting. I recently applied for the <role> role and wanted to introduce myself:", introduces the
  candidate in the past tense, gives up to three "- " proof points (each naming an ask, then the proof
  with its number), asks "Would you be open to a brief conversation about the role?", offers "If someone
  else would be the better person to speak with, I'd be grateful if you could point me to them." and signs
  off "Thank you for your time,". The follow-up opens "I hope your week is going well." and asks "I'd be
  grateful for 15 minutes to talk, whenever suits you." Never an imperative aimed at the recruiter, "Let
  me know", "Please respond" or a bare "Are you the right person...?".
- Resume summary rule: sentence 1 and the last sentence (engineering roots) stay as in the base resume;
  sentence 2 names the products relevant to the posting and the program types; sentence 3 restates how the
  candidate ran them in the posting's own terms for its top asks, with scope. Up to 40% longer than the
  base summary. Every Meta bullet is kept (reworded and reordered); pinned bullets stay word for word.
- Every draft is reviewed by a subagent that didn't write it, against the rubric in
  `.claude/skills/write-application/reference/rubric.md` (R1 to R9, including "sounds human"), at most two
  revision rounds. Unresolved items go under NEEDS YOUR REVIEW at the top of notes.txt.

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
  (`settings.yaml`, `.env`, `evidence/`, `style_examples/`, `benchmarks/`, `output/`, `data/`,
  `Applications.xlsx`, resumes, cover letters or recruiter messages), and ask whether to push to GitHub.
- Never push without the user's "approved".

## Privacy

- Never modify, move, or commit anything in `evidence/`. It is read-only and git-ignored.
- Never print or copy the API key in `.env`.
