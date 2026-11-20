"""Offline tests for the recruiter message limits and wording rules (no API calls).

Run:  .venv/bin/python -m unittest discover tests
"""
import unittest

from agent import recruiter as r
from agent.evidence_gate import positioning_problems

URL = "https://example.com/jobs/123"
NAME = "Test Person"


def sent(text, kind="experience"):
    return {"text": text, "kind": kind}


def draft(note="I applied for the Staff TPM role.", body_words=60, follow_words=30):
    """A draft whose body and follow-up have about the given number of words (before code adds the
    greeting, link and sign-off)."""
    filler = lambda n: " ".join(["word"] * (n - 1)) + " end."
    return {"note": [sent(note, "interest")], "subject": "Staff TPM application - Test Person",
            "body": [[sent("I applied for the Staff TPM role.", "interest")], [sent(filler(body_words))]],
            "follow_up": [sent(filler(follow_words), "interest")]}


class Counting(unittest.TestCase):
    def test_assemble_adds_greeting_link_and_signoff(self):
        parts = r.assemble(draft(), URL, NAME)
        self.assertTrue(parts["note"].startswith("Hi [First name], "))
        lines = parts["body"].split("\n\n")
        self.assertEqual(lines[0], "Hi [First name],")
        self.assertEqual(lines[2], URL)                        # the link on its own line, after paragraph 1
        self.assertTrue(parts["body"].endswith("Thank you,\n" + NAME))
        self.assertTrue(parts["follow_up"].endswith("Thank you,\n" + NAME))

    def test_contact_line_only_when_set(self):
        self.assertTrue(r.assemble(draft(), URL, NAME, contact="555-0100")["body"].endswith(NAME + "\n555-0100"))

    def test_note_limit_is_200_characters_including_spaces(self):
        room = r.NOTE_MAX_CHARS - len(r.GREETING) - 1
        ok = r.assemble(draft(note="x" * room), URL, NAME)
        self.assertEqual(len(ok["note"]), 200)
        self.assertFalse([p for p in r.over_limits(ok) if "connection note" in p])
        over = r.assemble(draft(note="x" * (room + 5)), URL, NAME)
        msg = [p for p in r.over_limits(over) if "connection note" in p]
        self.assertEqual(len(msg), 1)
        self.assertIn("TOO LONG by 5", msg[0])

    def test_subject_limit(self):
        d = draft()
        d["subject"] = "s" * 201
        self.assertTrue(any("subject" in p and "TOO LONG by 1" in p for p in r.over_limits(r.assemble(d, URL, NAME))))

    def test_body_word_limits(self):
        parts = r.assemble(draft(body_words=150), URL, NAME)
        n = r.words(parts["body"])
        self.assertTrue(r.BODY_MIN_WORDS <= n <= r.BODY_MAX_WORDS, n)
        self.assertFalse(r.over_limits(parts))
        self.assertFalse([p for p in r.under_limits(parts) if "body" in p])
        long = r.assemble(draft(body_words=200), URL, NAME)
        self.assertTrue(any("body" in p and "words" in p for p in r.over_limits(long)))
        short = r.assemble(draft(body_words=50), URL, NAME)
        self.assertTrue(any("body" in p for p in r.under_limits(short)))

    def test_body_character_limit(self):
        d = draft()
        d["body"][1] = [sent("x" * 1950 + ".")]                 # few words, too many characters
        parts = r.assemble(d, URL, NAME)
        self.assertTrue(any("characters" in p and "body" in p for p in r.over_limits(parts)))

    def test_follow_up_limits(self):
        ok = r.assemble(draft(follow_words=50), URL, NAME)
        self.assertTrue(r.FOLLOW_MIN_WORDS <= r.words(ok["follow_up"]) <= r.FOLLOW_MAX_WORDS)
        self.assertTrue(any("follow-up" in p for p in r.over_limits(r.assemble(draft(follow_words=80), URL, NAME))))
        self.assertTrue(any("follow-up" in p for p in r.under_limits(r.assemble(draft(follow_words=20), URL, NAME))))


class Shortening(unittest.TestCase):
    LONG_NOTE = ("I applied for the Staff Technical Program Manager, Wearables Platform role, and I owned the "
                 "critical path for the automated hardware-in-the-loop test infrastructure behind Ray-Ban Meta "
                 "across 16 feature teams. Would you be open to a short call?")
    SHORT_NOTE = "I applied for the Staff TPM role and led automated CI testing for 16 teams. Open to a call?"

    def measure(self, d):
        return r.over_limits(r.assemble(d, URL, NAME))

    def test_too_long_draft_is_shortened_by_the_model_not_cut(self):
        calls = []

        def fake_model(d, problems):
            calls.append(problems)
            out = dict(d)
            out["note"] = [sent(self.SHORT_NOTE, "interest")]
            return out

        d = draft(note=self.LONG_NOTE)
        self.assertTrue(self.measure(d))
        fixed, still, notes = r.fit_to_limits(d, self.measure, fake_model)
        self.assertEqual(len(calls), 1)
        self.assertEqual(still, [])
        note = r.assemble(fixed, URL, NAME)["note"]
        self.assertLessEqual(len(note), 200)
        self.assertTrue(note.endswith("?"))                     # whole sentences, as the model wrote them
        self.assertIn(self.SHORT_NOTE, note)
        self.assertTrue(any("Shortening try 1" in n for n in notes))

    def test_still_too_long_gets_a_warning_and_is_never_cut(self):
        d = draft(note=self.LONG_NOTE)
        fixed, still, notes = r.fit_to_limits(d, self.measure, lambda d, p: d, tries=2)
        self.assertTrue(still)
        note = r.assemble(fixed, URL, NAME)["note"]
        self.assertEqual(note, "Hi [First name], " + self.LONG_NOTE)        # untouched, not truncated
        over = len(note) - 200
        text = r.render(r.assemble(fixed, URL, NAME), [f"TOO LONG: {p}" for p in still])
        first_lines = text.splitlines()[:4]
        self.assertEqual(first_lines[0], r.TOP_LINE)
        self.assertTrue(any(l.startswith("WARNING - TOO LONG") and f"TOO LONG by {over}" in l for l in first_lines))
        self.assertTrue(any("2 tries" in n for n in notes))

    def test_render_prints_counts_and_is_plain_text(self):
        parts = r.assemble(draft(body_words=150, follow_words=50), URL, NAME)
        text = r.render(parts, [])
        self.assertTrue(text.startswith("Send these yourself on LinkedIn. Replace [First name] before sending."))
        self.assertIn(f"({len(parts['note'])} of 200 characters)", text)
        self.assertIn(f"({r.words(parts['body'])} words, 120-180; {len(parts['body'])} of 1900 characters)", text)
        for label in ("1. CONNECTION REQUEST NOTE", "2. LINKEDIN MESSAGE", "3. FOLLOW-UP MESSAGE"):
            self.assertIn(label, text)
        self.assertFalse(r.MARKDOWN.search(text))
        self.assertFalse(r.EMOJI.search(text))


class Wording(unittest.TestCase):
    def test_underselling_words_are_flagged(self):
        for w in ("helped", "supported", "assisted", "was involved in", "contributed to", "participated in",
                  "worked on", "tried to"):
            self.assertTrue(positioning_problems(f"I {w} the Orion launch."), w)
        self.assertFalse(positioning_problems("I led the Orion launch as release captain."))

    def test_bare_test_wording_is_flagged(self):
        for s in ("I scaled the test lab from 1 to 215+ devices.", "I owned the test infrastructure.",
                  "I ran testing for 16 teams."):
            self.assertTrue(any("bare test" in p for p in positioning_problems(s)), s)
        for s in ("I owned the automated hardware-in-the-loop test infrastructure behind Ray-Ban Meta.",
                  "I led automated continuous integration (CI) testing for 16 feature teams."):
            self.assertFalse(positioning_problems(s), s)

    def test_clean_strips_emoji_and_model_written_greetings(self):
        d = draft()
        d["note"] = [sent("Hi [First name],", "interest"), sent("I applied for the role 🚀.", "interest")]
        d["follow_up"].append(sent("Thank you,", "interest"))
        c = r._clean(d)
        self.assertEqual([x["text"] for x in c["note"]], ["I applied for the role ."])
        self.assertFalse(any(x["text"].startswith("Thank you") for x in c["follow_up"]))


if __name__ == "__main__":
    unittest.main()
