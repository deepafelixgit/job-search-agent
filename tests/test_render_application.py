"""Offline tests for writing applications in Claude Code (no network, no API, no private settings needed).

Covers: the recruiter message's courtesy checks and fixed phrases, the note ending, "- " bullets,
the resume summary rule, the prescribing exception, the briefing archive reader, the queue, and billing
safety for the background run (no API variables, plan login required, no API library loaded).

Run:  .venv/bin/python -m unittest discover tests
"""
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent import app_queue, claude_runner, company_facts, render_application as ra
from agent.letter import prescribes

ROOT = Path(__file__).resolve().parent.parent
NAME = "Test Person"
TITLE = "Technical Program Manager, Platform"
URL = "https://example.com/jobs/1"


def good_message(**over):
    body = (f"Hi [First name],\n\nThank you for connecting. I recently applied for the {TITLE} role and wanted to "
            f"introduce myself:\n{URL}\n\nI was a Staff TPM at Example Labs, leading engineering execution for new "
            "hardware programs across firmware, electrical and software teams. I'd like to share three areas where my "
            "experience matches what the team is looking for:\n\n"
            "- An operating rhythm that gets decisions to leaders fast. I ran a daily standup with engineering directors "
            "that took build compliance from 20% to 100% in two months.\n"
            "- Getting ahead of hardware risks. I scaled a prototype fleet eight times over by escalating the shortage "
            "early and repurposing units instead of waiting for new builds.\n"
            "- Using automation to give a lean team time back. I led a program that cut the time from failure to owner "
            "action from 19 hours to 2.5.\n\n"
            "Example Co's plan to triple its network is the kind of mission I would be glad to help deliver. Would you be "
            "open to a brief conversation about the role? If someone else would be the better person to speak with, "
            f"I'd be grateful if you could point me to them.\n\nThank you for your time,\n{NAME}")
    follow = ("Hi [First name],\n\nI hope your week is going well. I wanted to follow up on my application for the "
              f"Platform TPM role. One more point of fit: I was release captain for a new product and ran its go/no-go "
              f"reviews. I'd be grateful for 15 minutes to talk, whenever suits you.\n\nThank you,\n{NAME}")
    msg = {"note": "Hi [First name], I applied for the Platform TPM role. I led execution for new hardware and "
                   "scaled a prototype fleet. Would you be open to a quick chat?",
           "subject": "Platform TPM - Staff TPM", "body": body, "follow_up": follow}
    msg.update(over)
    return msg


def problems(msg):
    with mock.patch.object(ra, "wording_problems", lambda text: []):
        return ra.check_message(ra.assemble_message(msg), TITLE, URL, NAME)


class Courtesy(unittest.TestCase):
    def test_a_good_message_passes(self):
        errors, _ = problems(good_message())
        self.assertEqual(errors, [])

    def test_note_must_end_with_the_ask(self):
        errors, _ = problems(good_message(note="Hi [First name], I applied for the Platform TPM role. Happy to connect."))
        self.assertTrue(any("must end with" in e for e in errors), errors)

    def test_note_limit(self):
        errors, _ = problems(good_message(note="Hi [First name], " + "x" * 190 + " Would you be open to a quick chat?"))
        self.assertTrue(any("characters (at most 200)" in e for e in errors), errors)

    def test_orders_are_flagged(self):
        for bad in ("Let me know if you have time.", "Please respond when you can.", "Are you the right person?",
                    "Send me the details."):
            m = good_message()
            m["body"] = m["body"].replace("Would you be open to a brief conversation", bad + " Would you be open to a brief conversation")
            errors, _ = problems(m)
            self.assertTrue(any("sounds like an order" in e for e in errors), (bad, errors))

    def test_fixed_phrases_are_required(self):
        m = good_message()
        m["body"] = m["body"].replace("If someone else would be the better person to speak with, I'd be grateful if "
                                      "you could point me to them.", "")
        errors, _ = problems(m)
        self.assertTrue(any("point me to them" in e for e in errors), errors)
        m = good_message(follow_up="Hi [First name],\n\nJust checking in.\n\nThank you,\n" + NAME)
        errors, _ = problems(m)
        self.assertTrue(any("I hope your week is going well" in e for e in errors), errors)

    def test_plain_bullets_allowed_but_not_markdown(self):
        self.assertFalse(any("Markdown" in e for e in problems(good_message())[0]))
        m = good_message()
        m["body"] = m["body"].replace("- An operating rhythm", "- **An operating rhythm**")
        self.assertTrue(any("Markdown" in e for e in problems(m)[0]))

    def test_present_tense_meta_is_flagged(self):
        self.assertTrue(any("in the past" in p for p in ra.wording_problems("I'm a Staff TPM at Meta Reality Labs.")))
        self.assertFalse(any("in the past" in p for p in ra.wording_problems("I was a Staff TPM at Meta Reality Labs.")))


class Abbreviations(unittest.TestCase):
    def test_unknown_abbreviations_must_be_spelled_out(self):
        got = ra.abbreviations("I worked with EE to retrofit units and repurposed QA-failed ones.", "")
        self.assertEqual(len(got), 2, got)

    def test_known_ones_and_the_postings_own_are_fine(self):
        self.assertEqual(ra.abbreviations("As a TPM I led AI and ML work reviewed by VPs, shipping NPI builds.",
                                          "Experience with NPI is required."), [])


class SummaryRule(unittest.TestCase):
    BASE = {"summary": ["First sentence stays. At Example, led release programs for 12+ engineering teams. Roots in "
                        "radar signal processing."],
            "skills": [["Program Leadership:", ["Roadmaps", "Risk"]]], "meta_intro": "", "groups": [["G1", "Programs"]],
            "bullets": {"M1": {"group": "G1", "text": "Led a program that cut incidents 26%."}},
            "pinned": {"P1": "Pinned bullet, exactly."}, "replaced_by_pin": {}}

    def check(self, summary, groups=None):
        content = {"summary": [summary],
                   "meta_groups": groups or [["G1", [["M1", "Led a program that cut incidents 26%."],
                                                     ["P1", "Pinned bullet, exactly."]]]]}
        with mock.patch.object(ra, "_verified_numbers", lambda: {"26", "12", "20", "100"}), \
             mock.patch.object(ra, "wording_problems", lambda text: []):
            return ra.check_resume(content, self.BASE)

    def test_four_sentence_summary_within_40_percent_passes(self):
        s = ("First sentence stays. At Example, led release programs for 12+ engineering teams. Ran the operating "
             "reviews. Roots in radar signal processing.")
        errors, warnings = self.check(s)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])

    def test_more_than_40_percent_longer_fails(self):
        s = ("First sentence stays. At Example, led release programs for 12+ engineering teams. " + "Ran things. " * 12
             + "Roots in radar signal processing.")
        self.assertTrue(any("of the base summary's length" in e for e in self.check(s)[0]))

    def test_unverified_number_fails(self):
        s = "First sentence stays. Led programs for 40 teams. Roots in radar signal processing."
        self.assertTrue(any("not in the resume or verified_metrics" in e for e in self.check(s)[0]))

    def test_every_bullet_kept_and_pinned_exact(self):
        errors, _ = self.check(self.BASE["summary"][0], groups=[["G1", [["P1", "Pinned bullet, reworded."]]]])
        self.assertTrue(any("missing: M1" in e for e in errors), errors)
        self.assertTrue(any("pinned bullet P1" in e for e in errors), errors)


class Prescribing(unittest.TestCase):
    def test_the_postings_own_asks_are_allowed(self):
        self.assertFalse(prescribes("The role also calls for a TPM who must keep the operating rhythm.", "Acme"))
        self.assertFalse(prescribes("The posting's first ask is an operating rhythm the team needs.", "Acme"))

    def test_telling_the_company_what_it_needs_is_flagged(self):
        self.assertTrue(prescribes("Acme needs a TPM who can scale its network.", "Acme"))
        self.assertTrue(prescribes("Your team must fix its release process.", "Acme"))


class Briefing(unittest.TestCase):
    def test_reads_only_stories_naming_the_company_in_the_last_120_days(self):
        today = dt.date(2026, 11, 20)
        with tempfile.TemporaryDirectory() as d:
            def day(date, stories, version=1):
                Path(d, f"{date}.json").write_text(json.dumps({"schema_version": version, "stories": stories}))
            day("2026-11-18", [{"headline": "Acme raises $1.5B", "summary": "x", "url": "u1", "companies": ["Acme"]},
                               {"headline": "Wing expands", "summary": "drones", "url": "u2", "companies": ["Acme", "Wing"]}])
            day("2026-06-01", [{"headline": "Acme old news", "summary": "", "url": "u3"}])     # older than 120 days
            day("2026-11-19", [{"headline": "A deal", "summary": "Uber and Acme launch", "url": "u4"}])
            day("2026-11-17", [{"headline": "Acme v2", "summary": "", "url": "u5"}], version=2)
            got = company_facts.stories(d, "Acme", today)
        self.assertEqual([s["url"] for s in got], ["u4", "u1"])

    def test_empty_setting_skips(self):
        self.assertEqual(company_facts.stories("", "Acme"), [])


class Queue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.patches = [mock.patch.object(app_queue, "QUEUE", d / "queue.json"),
                        mock.patch.object(app_queue, "LOCK", d / "queue.lock"),
                        mock.patch.object(app_queue, "DATA_DIR", d)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_add_pause_resume(self):
        app_queue.add([{"uid": "A|1", "company": "A", "title": "TPM"}, {"uid": "B|2", "company": "B", "title": "TPM"}])
        app_queue.add([{"uid": "A|1", "company": "A", "title": "TPM"}])            # not added twice
        self.assertEqual([i["uid"] for i in app_queue.items()], ["A|1", "B|2"])
        app_queue.set_status("A|1", app_queue.WRITING, folder="/tmp/x")
        self.assertEqual(app_queue.pause_open("limit"), 2)
        self.assertTrue(all(i["status"] == app_queue.PAUSED for i in app_queue.items()))
        app_queue.resume_paused()
        st = {i["uid"]: i["status"] for i in app_queue.items()}
        self.assertEqual(st, {"A|1": app_queue.WRITING, "B|2": app_queue.QUEUED})   # A continues its drafts

    def test_usage_limit_pauses_and_other_failures_need_review(self):
        app_queue.add([{"uid": "A|1"}, {"uid": "B|2"}])
        app_queue.set_status("A|1", app_queue.WRITING)
        claude_runner.finish(1, '{"type":"result","result":"You\'ve hit your session limit · resets 3:45pm"}')
        self.assertEqual({i["status"] for i in app_queue.items()}, {app_queue.PAUSED})
        app_queue.resume_paused()
        app_queue.set_status("A|1", app_queue.WRITING)
        claude_runner.finish(1, '{"type":"result","result":"something broke"}')
        self.assertEqual(app_queue.items()[0]["status"], app_queue.REVIEW)


class BillingSafety(unittest.TestCase):
    def test_api_variables_are_removed(self):
        env = {"ANTHROPIC_API_KEY": "sk-x", "ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_BASE_URL": "u",
               "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1",
               "AWS_BEARER_TOKEN_BEDROCK": "b", "PATH": "/bin", "HOME": "/h"}
        self.assertEqual(claude_runner.clean_env(env), {"PATH": "/bin", "HOME": "/h"})

    def test_background_run_gets_the_clean_environment(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}), \
             mock.patch.object(claude_runner, "is_running", lambda: False), \
             mock.patch.object(claude_runner.app_queue, "has_open", lambda: True), \
             mock.patch.object(claude_runner, "plan_login", lambda: (True, "ok")), \
             mock.patch.object(claude_runner, "claude_exe", lambda: "/usr/bin/true"), \
             mock.patch.object(claude_runner, "STATE", Path(tempfile.mkdtemp()) / "run.json"), \
             mock.patch.object(claude_runner, "LOG_DIR", Path(tempfile.mkdtemp())), \
             mock.patch.object(claude_runner.threading, "Thread"), \
             mock.patch.object(claude_runner.subprocess, "Popen") as popen:
            popen.return_value.pid = 12345
            ok, _ = claude_runner.start()
        self.assertTrue(ok)
        env = popen.call_args.kwargs["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env.get("JOB_AGENT_BACKGROUND"), "1")
        cmd = popen.call_args.args[0]
        self.assertIn("/write-application", cmd)
        self.assertIn("Read(./.env)", cmd)

    def test_only_a_plan_login_is_accepted(self):
        for status, ok in (({"loggedIn": True, "authMethod": "claude.ai"}, True),
                           ({"loggedIn": True, "authMethod": "api_key"}, False),
                           ({"loggedIn": True, "authMethod": "api_key_helper"}, False),
                           ({"loggedIn": False, "authMethod": "none"}, False)):
            done = subprocess.CompletedProcess([], 0, stdout=json.dumps(status), stderr="")
            with mock.patch.object(claude_runner, "claude_exe", lambda: "/usr/bin/claude"), \
                 mock.patch.object(claude_runner.subprocess, "run", return_value=done) as run:
                self.assertEqual(claude_runner.plan_login()[0], ok, status)
                self.assertNotIn("ANTHROPIC_API_KEY", run.call_args.kwargs["env"])

    def test_not_logged_in_starts_nothing(self):
        with mock.patch.object(claude_runner, "is_running", lambda: False), \
             mock.patch.object(claude_runner.app_queue, "has_open", lambda: True), \
             mock.patch.object(claude_runner, "plan_login", lambda: (False, "not logged in")), \
             mock.patch.object(claude_runner.subprocess, "Popen") as popen:
            self.assertEqual(claude_runner.start(), (False, "not logged in"))
        popen.assert_not_called()

    def test_renderer_never_loads_the_api_library_or_the_env_file(self):
        code = ("import builtins, sys\n"
                "real = builtins.open\n"
                "def guard(f, *a, **k):\n"
                "    assert not str(f).endswith('.env'), 'opened .env'\n"
                "    return real(f, *a, **k)\n"
                "builtins.open = guard\n"
                "import agent.render_application, agent.app_queue, agent.company_facts, agent.claude_runner\n"
                "import agent.render_application as ra\n"
                "ra.check_letter({'paragraphs': ['I led it.']}, 'X')\n"
                "assert 'anthropic' not in sys.modules, 'anthropic loaded'\n"
                "assert 'dotenv' not in sys.modules, 'dotenv loaded'\n"
                "print('ok')\n")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True,
                             env=claude_runner.clean_env())
        self.assertIn("ok", out.stdout, out.stderr[-600:])


class Footer(unittest.TestCase):
    def test_page_numbers_bottom_right(self):
        import docx
        from agent.resume import add_page_numbers
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "x.docx")
            doc = docx.Document()
            doc.add_paragraph("hello")
            doc.save(str(p))
            add_page_numbers(p)
            footer = docx.Document(str(p)).sections[0].footer.paragraphs[0]
            self.assertIn("PAGE", footer._p.xml)
            self.assertEqual(str(footer.alignment), "RIGHT (2)")


if __name__ == "__main__":
    unittest.main()
