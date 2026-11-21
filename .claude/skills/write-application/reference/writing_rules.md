# Writing rules: resume, cover letter, recruiter message

Specifics about the candidate live in `settings.yaml` under `personal.positioning` (private, never copied
here): `who_i_am`, `level`, `products`, `program_types`, `ai_lead`, `personal_ai_projects`, `safety_tie`,
`boundaries`, `benchmark_dir`. The reviewed example application in `benchmark_dir` is the model for
structure, depth and tone. Never copy its wording.

Standing rules from CLAUDE.md always apply: commercial names only (never internal codenames), numbers
exactly as in verified_metrics.txt, never merge two programs or metrics, never use a [CONFIRM] line,
"automated hardware-in-the-loop test infrastructure" or "automated continuous integration testing" (never
bare "test infrastructure", "testing" or "test lab"), the strongest ownership verb the record supports,
no underselling words ("helped", "supported", "assisted", "worked on"...), "I" for what the candidate did.

Senior voice, every time the candidate's role is introduced (letter opening, message's who-I-am line):
- Claim the work with an ownership sentence and the scale: "That is the work I led as <who_i_am>, across
  <number> first-of-their-kind <products or programs>". Name the number of programs, teams or products
  the evidence states.
- Never soft framings that turn ownership into activity: "keeping work visible and moving is what I did",
  "X was much of the job", "I was part of", "I was responsible for helping". Say what was led and what
  changed.
- Spell out abbreviations a recruiter may not know: "electrical engineering", not "EE"; "units that
  failed quality checks", not "QA-failed units". The renderer rejects unknown abbreviations that aren't
  in the posting.

## Resume (Summary, Skills and Meta only)

- Follow the candidate's tailoring guidance file (`candidate.tailoring_guidance_file`). Leave everything
  before Meta untouched (the renderer checks it).
- Keep every Meta bullet: each original bullet ID and each pinned bullet appears exactly once, in its own
  subheading (pinned bullets may go under any subheading). Reword and reorder for the posting. Pinned
  bullets stay word for word. The renderer rejects a missing, moved or duplicated bullet.
- Skills: only items already listed under each label; reorder labels and items for the posting.
- A rewritten bullet keeps its facts and numbers; it may not add a number that isn't in the original or in
  verified_metrics.txt.
- Summary, up to 40% longer than the base summary:
  1. Sentence 1: exactly as in the base resume.
  2. Sentence 2: what the candidate led, naming the `products` relevant to this posting and the
     `program_types`.
  3. Sentence 3: how the candidate ran them, restated in the posting's own terms for its top asks, with
     scope from verified_metrics.txt (teams, reviews). See the summary in the `benchmark_dir` resume.
  4. Last sentence (engineering roots): exactly as in the base resume.

## Cover letter: 330 to 430 words of body text, 5 paragraphs or fewer, one page

- Greeting "Dear <Company> Hiring Team," unless the posting names the hiring manager; closing "Sincerely,".
- Paragraph 1, opening: a specific, sourced company fact about why now (from the company facts step), tied
  in the next sentence to the problem that fact creates and to what the candidate led. If no usable fact
  exists, open with one sentence connecting the candidate's experience to what the role does.
- Paragraphs 2 to 4: each names one of the posting's asks in the posting's own words, then answers it with
  the story or proof from the asks map. The first ask gets the in-depth story; the second and third one
  strong proof each (a proof may combine two short related examples). Vary how each ask is introduced so it
  never reads like a template: not "The posting's first ask is... The posting's second ask is...".
- Closing paragraph, 3 sentences at most: why this company, tied to something the candidate did where
  that's true (for an autonomy or safety company, `safety_tie`), and a polite request to talk.
- AI: only when the posting values AI, ML or automation, and then as the answer to that ask, never a
  stand-alone sentence. Lead with `ai_lead`. `personal_ai_projects` only for roles about AI products,
  agents or developer tools.
- Never tell the company what it needs or should do. Naming the posting's own asks, attributed to the
  posting or the role ("The role also calls for..."), is fine.
- The candidate's Meta employment is in the past ("I was", "I led"), never "I'm a ... at Meta".
- Never claim anything in `boundaries`.

## Recruiter message (model: the recruiter message in `benchmark_dir`)

Three parts. The renderer checks the fixed phrases below word for word.

Connection note: at most 200 characters including "Hi [First name], ". One line on the role and one on
the strongest proof, ending exactly with "Would you be open to a quick chat?" Fit the limit by
shortening other words, never by dropping the ask.

Subject: at most 200 characters, e.g. "<role> - <title> from <company>".

Message body: 120 to 220 words and at most 1,900 characters, in this order:

    Hi [First name],

    Thank you for connecting. I recently applied for the <exact role title> role and wanted to introduce myself:
    <posting link>

    <who_i_am>, leading ... (past tense; name the products relevant to the posting)
    I'd like to share three areas where my experience matches what the team is looking for:

    - <ask in plain words>. <proof with its number>
    - <second ask>. <proof>
    - <third ask>. <proof>                (two bullets when the posting has only two distinct asks)

    <one sentence about the company, using a fact from the company facts step>
    Would you be open to a brief conversation about the role? If someone else would be the better person
    to speak with, I'd be grateful if you could point me to them.

    Thank you for your time,
    <name>

Plain "- " lines only; no **, #, backticks or other Markdown. Proof points are different from the cover
letter's sentences (shared facts are fine; don't repeat 12 or more words in a row).

Follow-up (about a week later), 40 to 70 words:

    Hi [First name],

    I hope your week is going well. I wanted to follow up on my application for the <role> role.
    <one NEW proof point, not used in the message>. I'd be grateful for 15 minutes to talk, whenever suits you.

    Thank you,
    <name>

Courtesy: warm and polite, confident about the work and polite toward the reader at once. Every ask is a
request. Never an imperative aimed at the recruiter, "Let me know", "Please respond", or "Are you the
right person...?" as a bare question.

## Sounds human (cover letter and message)

Specific nouns (the product, the team, the number), plain verbs, natural contractions, sentences of
different lengths, each paragraph shaped by its content. Avoid: the same template in every paragraph, "not
just X, but Y", habitual lists of three, stacked abstract nouns, "Moreover"/"Furthermore"/"Additionally",
"I am writing to...", more than one em dash, summing-up lines ("This experience taught me..."), adjectives
doing a fact's work ("robust", "seamless", "impactful"), perfect parallelism everywhere.
