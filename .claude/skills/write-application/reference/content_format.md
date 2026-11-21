# content.json (read by `python -m agent.render_application <folder>`)

Run `.venv/bin/python -m agent.render_application --resume-map` first: it prints the base resume's
summary paragraphs, skills (label and items), Meta intro, subheading IDs (G1...), bullet IDs (M1...) with
their subheading, pinned bullets (P1...) and which original bullet each pinned one replaces (that original
is left out; the pinned bullet takes its place).

```json
{
  "uid": "Company|12345",
  "posting": {"company": "Company", "title": "Exact role title", "url": "https://...",
              "score": 8, "posted": "YYYY-MM-DD", "run_date": "YYYY-MM-DD"},
  "resume": {
    "summary": ["the whole summary as one paragraph"],
    "skills": [["Label:", ["item", "item"]]],
    "meta_intro": "the line under the Meta title, if the base resume has one",
    "meta_groups": [
      ["G1", [["M3", "reworded bullet"], ["P1", "pinned bullet, word for word"]]],
      ["G2", [["M1", "..."]]]
    ]
  },
  "cover_letter": {"greeting": "Dear Company Hiring Team,", "paragraphs": ["...", "..."],
                   "closing": "Sincerely,"},
  "recruiter_message": {"note": "Hi [First name], ... Would you be open to a quick chat?",
                        "subject": "...",
                        "body": "Hi [First name],\n\nThank you for connecting. ...\n\nThank you for your time,\nName",
                        "follow_up": "Hi [First name],\n\nI hope your week is going well. ...\n\nThank you,\nName"},
  "add_to_spreadsheet": true
}
```

- `posting`: copy from `app_queue next` (or the database row). Its company and title name the files.
- `skills` and `meta_intro` may be left out to keep the base resume's.
- `meta_groups`: subheadings in the new order; inside each, bullets in the new order. Every original
  bullet (except one replaced by a pinned bullet) and every pinned bullet appears exactly once.
- The renderer writes the resume, cover letter and recruiter message files with the agent's own names and
  formatting, adds page numbers, counts pages (cover letter 1, resume at most 2), adds the Applications.xlsx
  row (once; if Excel has the file open, the row waits until it's closed) and writes `render_report.json`.
- Exit code 2 means ERRORS: fix content.json and render again. Warnings are for your judgment.
