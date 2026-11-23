"""Offline tests: every Create button reaches the Claude Code queue, the API writing steps refuse to run in
claude_code mode, and an app running older code refuses to create anything (no network, no API).

Run:  .venv/bin/python -m unittest discover tests
"""
import re
import unittest
from pathlib import Path
from unittest import mock

import app as webapp
from agent import claude_client, config

ROOT = Path(__file__).resolve().parent.parent
CLAUDE_CODE = {"generation_mode": "claude_code"}
ROW = {"uid": "Acme|1", "company": "Acme", "title": "Technical Program Manager", "url": "https://example.com/1"}


def post(client, data):
    return client.post("/create", data=dict(data, csrf=webapp.CSRF_TOKEN), environ_base={"REMOTE_ADDR": "127.0.0.1"})


class EveryCreateButton(unittest.TestCase):
    def test_every_create_form_posts_to_the_one_create_route(self):
        forms = []
        for t in (ROOT / "templates").glob("*.html"):
            for m in re.finditer(r'<form[^>]*action="([^"]+)"[^>]*>(.*?)</form>', t.read_text(), re.S):
                if re.search(r"Create", m.group(2)):
                    forms.append((t.name, m.group(1)))
        self.assertGreaterEqual(len(forms), 2, forms)          # the Score page's and the daily list's
        self.assertEqual({a for _, a in forms}, {"/create"}, forms)

    def test_create_goes_to_the_queue_not_the_api(self):
        payloads = [{"uid": "Acme|1", "run_date": "2026-11-20"},                     # Score page button
                    {"uid": ["Acme|1", "Acme|2"], "run_date": "2026-11-20"}]         # daily list
        for data in payloads:
            with mock.patch.object(webapp, "load_settings", lambda: CLAUDE_CODE), \
                 mock.patch.object(webapp, "code_is_stale", lambda: False), \
                 mock.patch.object(webapp.db, "get_postings", lambda uids: [dict(ROW, uid=u) for u in uids]), \
                 mock.patch.object(webapp.app_queue, "add") as add, \
                 mock.patch.object(webapp.claude_runner, "start", return_value=(True, "started")) as start, \
                 mock.patch.object(webapp, "generate", side_effect=AssertionError("API path used")):
                r = post(webapp.app.test_client(), data)
            self.assertEqual(r.status_code, 302, data)
            self.assertIn("/queue", r.headers["Location"])
            queued = [p["uid"] for p in add.call_args.args[0]]
            self.assertEqual(queued, data["uid"] if isinstance(data["uid"], list) else [data["uid"]])
            start.assert_called_once()


class Guard(unittest.TestCase):
    def test_api_writing_steps_refuse_in_claude_code_mode(self):
        from agent import evidence_gate, generate, letter, recruiter, tailor
        calls = [lambda: generate.generate(["Acme|1"], lambda u, m: None),
                 lambda: generate.generate_resume_only(["Acme|1"], lambda u, m: None),
                 lambda: evidence_gate.find_evidence("m", ROW),
                 lambda: evidence_gate.judge_sentences("m", [("I led it.", True)], None, set(), ""),
                 lambda: evidence_gate.coverage_report("m", None, "", ""),
                 lambda: tailor.tailor_resume("m", Path("x.docx"), ROW, Path("y.docx")),
                 lambda: letter.write_letter("m", Path("x.docx"), ROW, Path("y.docx"), "N", None, None, {}),
                 lambda: recruiter.write_message("m", "m", ROW, None, [], "", "", {}, "N", None, Path("z.txt"))]
        with mock.patch.object(config, "load_settings", lambda: CLAUDE_CODE), \
             mock.patch.object(claude_client, "_call", side_effect=AssertionError("API called")):
            for i, call in enumerate(calls):
                with self.assertRaises(config.APIWritingDisabled, msg=f"call {i}"):
                    call()

    def test_api_mode_is_allowed(self):
        config.ensure_api_writing_allowed({"generation_mode": "api"})      # no error

    def test_missing_setting_means_claude_code(self):
        with self.assertRaises(config.APIWritingDisabled):
            config.ensure_api_writing_allowed({})


class OldCode(unittest.TestCase):
    def test_an_app_running_older_code_creates_nothing(self):
        with mock.patch.object(webapp, "code_is_stale", lambda: True), \
             mock.patch.object(webapp.app_queue, "add") as add, \
             mock.patch.object(webapp, "generate", side_effect=AssertionError("API path used")):
            r = post(webapp.app.test_client(), {"uid": "Acme|1", "run_date": "2026-11-20"})
        self.assertEqual(r.status_code, 409)
        self.assertIn("restart", r.get_data(as_text=True).lower())
        add.assert_not_called()

    def test_fingerprint_changes_when_code_changes(self):
        before = webapp.code_fingerprint()
        f = ROOT / "agent" / "__init__.py"
        st = f.stat()
        import os
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        try:
            self.assertNotEqual(webapp.code_fingerprint(), before)
        finally:
            os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))


if __name__ == "__main__":
    unittest.main()
