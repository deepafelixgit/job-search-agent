---
name: write-application
description: Write the tailored resume, cover letter and recruiter message for a job posting, on the Claude plan with no API cost. Use when the user types /write-application, with a posting id or link, or with nothing to work through the queue that the web app filled.
argument-hint: "[posting id or posting link]"
disable-model-invocation: true
---

# Write an application (no API cost)

You (Claude Code) write the application yourself. HARD RULES, never broken:
- Never run anything that calls the Claude API: no `run_daily.py`, no `agent.generate`, no scoring, no
  benchmark or audit commands, never import `anthropic`, never read `.env` or any API key.
- The only commands you run are `.venv/bin/python -m agent.app_queue ...`,
  `.venv/bin/python -m agent.company_facts ...` and `.venv/bin/python -m agent.render_application ...`,
  each on its own (never chained with `&&`, `;` or `cd`). No other shell commands (no `ls`, `cat`,
  `python -c`): use Glob and Read to look at files. In a background run anything else is refused. Everything else is your own tools: Read, Glob,
  Grep, WebSearch, WebFetch, subagents (Agent), the career-evidence and job-postings MCP tools, and
  Write/Edit inside the posting's folder under `output/`.
- Facts about the candidate come only from the evidence (below). Never invent one. Numbers exactly as in
  `evidence/verified_metrics.txt`. Follow every rule in CLAUDE.md ("Resume and cover letter work",
  "Positioning", "Writing applications").

Read these once per session and reuse them for every posting:
- [reference/writing_rules.md](reference/writing_rules.md): what the resume, letter and message must say
- [reference/rubric.md](reference/rubric.md): how drafts are judged (R1 to R9)
- [reference/content_format.md](reference/content_format.md): the content.json the renderer reads

## Which postings

- `$ARGUMENTS` is a posting id (for example `Company|12345`): run
  `.venv/bin/python -m agent.app_queue add <id>`, then process the queue.
- `$ARGUMENTS` is a link: if `mcp__job-postings__get_posting` (or the database) doesn't have it, WebFetch
  the page, find the company and exact role title, run
  `.venv/bin/python -m agent.app_queue add-link "<url>" "<company>" "<role title>"`, then process the queue.
- No arguments: process the queue.

Process the queue: run `.venv/bin/python -m agent.app_queue next`. It prints the next posting (an
interrupted one first) with its `folder`, or `{"done": true}` when nothing is left. Write that posting
(steps 1 to 10), then run `next` again, until done.

`"background": true` in that output means nobody is there to answer questions (the web app started this
run). Then never ask: write the best version you can WITHOUT the missing fact, never inventing one, and
put the question under NEEDS YOUR REVIEW at the top of notes.txt. In an interactive session, ask the user
directly when a fact you need isn't in the evidence (don't guess, don't silently leave it out).

## Saving as you go (usage limits)

The plan has usage limits. After each step, save its result in the posting's folder under `drafts/`
(`asks.md`, `company_facts.md`, `content.json`, `review.md`). When `next` says `"continuing": true`,
read `drafts/` first and continue from the first step that isn't saved, instead of starting over.

## Steps for one posting

1. **Posting.** Use `mcp__job-postings__get_posting` with the uid (or the details `next` printed). For a
   link that isn't in the database, save the posting text as `posting.txt` in the folder.
2. **Evidence** (once per session). Paths: the base resume and tailoring guidance are named in
   `settings.yaml` (`candidate.resume_file`, `candidate.tailoring_guidance_file`); also
   `evidence/impact_of_my_work.md`, `evidence/verified_metrics.txt`, the review files in `evidence/`, the
   style examples in `style_examples/`, and `settings.yaml` (`personal.positioning`, `internal_terms`,
   `translations`, `codename_words`, `pinned_bullets`, `fixed_bullets`). Use the career-evidence tools
   (`mcp__career-evidence__search_evidence`, `mcp__career-evidence__get_verified_metrics`,
   `mcp__career-evidence__check_claim`) to find and confirm facts, as CLAUDE.md
   requires. Run `.venv/bin/python -m agent.render_application --resume-map` for the resume's IDs.
3. **Map the asks.** The posting's top three asks, in the posting's own words. For each, the strongest
   story or result. The first gets the in-depth story (situation, decision or direction set, who was
   aligned, trade-off, measurable result); the second and third one strong proof each. Save `drafts/asks.md`
   with the evidence source (document, review period, page or section) for every fact.
4. **Company facts** (once, used by the letter and the message):
   a. The Daily Tech Briefing archive: run `.venv/bin/python -m agent.company_facts "<company>"`. It
      prints the stories of the last 120 days whose headline or summary names the company (it skips the
      archive when `briefing_archive_dir` is empty). Don't read the archive files one by one.
   b. Then WebSearch, at most 4 searches, starting with the company's own newsroom or press page.
      Company-level facts (funding, contracts, expansions, mission milestones): last 12 months. Product
      news: last 6 months. Check each date against today.
   c. Then the posting's About section.
   Prefer how the company's work benefits people and society; otherwise what is genuinely innovative.
   Keep the best two or three, each with its link and date, in `drafts/company_facts.md`; they go into
   `sources.txt`. If nothing usable is found, say so in notes.txt.
5. **Resume**, 6. **Cover letter**, 7. **Recruiter message**: follow reference/writing_rules.md exactly.
   Write them into `drafts/content.json` (reference/content_format.md). Check lengths and the code
   rules with `.venv/bin/python -m agent.render_application <folder> --dry-run --content drafts/content.json`
   (it prints the word and character counts and any ERRORS) instead of counting yourself; fix and re-check.
8. **Review with fresh eyes.** Start a subagent (Agent tool) that did not write the drafts. Give it the
   paths to the drafts, the posting, the evidence files and reference/rubric.md, and ask it to score R1
   to R9 pass or fail, quote the sentences it relies on, check every fact about the candidate against the
   evidence (exact numbers), and give sentence-level fixes. Revise, then have it check again: at most two
   rounds. Rewrite a sentence that doesn't match the evidence; never delete it and leave a hole. Don't
   treat greetings, thanks, requests to talk, sentences about the company or role, or the posting's own
   asks as claims about the candidate. Save `drafts/review.md`, then run the `--dry-run` check again.
9. **Render.** Copy `drafts/content.json` to `content.json` in the folder and run
   `.venv/bin/python -m agent.render_application <folder>`. It builds the files, adds the
   Applications.xlsx row and prints ERRORS (code rules) and Warnings. Fix every error in content.json and
   render again (at most three times). Then write in the folder:
   - `sources.txt`: every company fact with its link and date.
   - `notes.txt`, in this order: NEEDS YOUR REVIEW (missing facts as questions, rubric criteria still
     failing, render errors still open; leave the heading out if there's nothing), the asks map, the
     company facts used, what changed in the resume and why, the rubric result per criterion, and every
     claim about the candidate with its source (document, review period, page or section).
10. **Finish.** Run `.venv/bin/python -m agent.app_queue set "<uid>" "Ready"`, or
    `... set "<uid>" "Needs your review" "<one-line reason>"` when notes.txt has a NEEDS YOUR REVIEW
    section. Then show the user: the folder path; the connection note, message and follow-up as text to
    copy; the cover letter's text; the new resume summary; and anything under NEEDS YOUR REVIEW. Don't ask
    for approval of each step; ask only when a fact is missing (and only in an interactive session).
