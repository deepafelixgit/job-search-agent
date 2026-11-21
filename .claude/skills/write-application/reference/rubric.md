# Rubric (the fresh-eyes reviewer scores each criterion pass or fail and quotes the sentences it relies on)

Which documents each criterion applies to: letter = cover letter, message = recruiter message,
summary = resume summary (the resume's other edits are checked for R3, R5 and R7).

- R1 Maps the asks (letter, message): the posting's top three asks are each named in the posting's own
  words and answered with a specific story or result of the candidate's.
- R2 Why this company, now (letter, message): a specific, sourced company fact (benefit to people and
  society first; otherwise genuine innovation), connected to the candidate in a way that's true.
- R3 Senior voice (all): reads like the `level` in settings.yaml (personal.positioning). Ownership verbs,
  scale, stakes and results; no underselling words. The opening and the message's who-I-am line claim the
  work with an ownership verb and the scale (number of first-of-their-kind programs, teams). Fails on soft
  framings that turn ownership into activity ("keeping that work visible and moving is what I did", "X was
  much of the job").
- R4 Coherence (letter, message): every paragraph has one job and flows; no disconnected sentences.
- R5 Accuracy (all): every fact about the candidate matches the evidence, with exact numbers; nothing in
  `boundaries` is claimed; product features in "Public fact" lines are never claimed as built by the
  candidate.
- R6 Courtesy (message): warm and polite; every ask is a request, never an order.
- R7 Wording (all): no abbreviations a recruiter may not know ("EE", "QA"); never bare "test infrastructure", "testing" or "test lab" (say "automated
  hardware-in-the-loop test infrastructure" or "automated continuous integration testing"). Commercial
  names only. No slogans or cliches. Never tells the company what it needs (naming the posting's own asks
  is fine). Every claim about what the candidate cares about or how they work is backed by an example.
- R8 Resume summary (summary): names the candidate's products and restates the programs in the posting's
  own terms (sentence structure in writing_rules.md).
- R9 Sounds human (letter, message): would a recruiter who reads hundreds of AI-written applications
  suspect this one? Fails on: every paragraph on the same template or every sentence the same rhythm; "not
  just X, but Y" and habitual lists of three; stacked abstract nouns where a concrete detail would do;
  stock transitions ("Moreover", "Furthermore", "Additionally"); throat-clearing ("I am writing to
  express"); more than one em dash; summing-up lines ("This experience taught me..."); adjectives doing
  the work facts should do ("robust", "seamless", "impactful"); perfect parallelism everywhere. Passes on
  specific nouns, plain verbs, natural contractions and varied sentence lengths.

Not claims about the candidate (never checked against the evidence): greetings, thanks, requests to talk,
courtesy lines, sentences about the company or the role, and the posting's own asks.

Reviewer output, per criterion: PASS or FAIL, the quoted sentences, and for a FAIL the exact sentence-level
fix. Then a list of every fact about the candidate with the evidence that backs it (or "not found").
