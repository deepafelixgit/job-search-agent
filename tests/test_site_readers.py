"""Offline tests for reading career sites politely (no network, no private settings).

Covers: the location pre-check before detail requests, keeping partial results when a site stops
answering, the newest-first stop rule, resuming a full sweep, and Retry-After handling.

Run:  .venv/bin/python -m unittest discover tests
"""
import datetime as dt
import unittest
from pathlib import Path
from unittest import mock

import requests
import yaml

from agent import http, site_readers as sr
from agent.filters import Filter

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = yaml.safe_load((ROOT / "settings.example.yaml").read_text(encoding="utf-8"))
F = Filter(EXAMPLE)
MS = {"name": "Microsoft", "board": "eightfold", "url": "https://apply.careers.microsoft.com",
      "domain": "microsoft.com", "api": "pcsx", "pause": 0, "location": "United States", "newest_first": True}
TODAY = dt.date.today()


def job(i, title="Senior Technical Program Manager", locs=("United States, California, Mountain View",), days_ago=0):
    ts = int(dt.datetime.combine(TODAY - dt.timedelta(days=days_ago), dt.time(12)).timestamp())
    return {"id": i, "name": title, "locations": list(locs), "postedTs": ts}


class FakeSite:
    """Answers search and details requests from a list of result pages; can refuse from a given call."""
    def __init__(self, pages, refuse_from=None):
        self.pages, self.refuse_from, self.calls = pages, refuse_from, []

    def __call__(self, method, url, *, params=None, json_body=None, html=False):
        self.calls.append((url.rsplit("/", 1)[-1], dict(params or {})))
        if self.refuse_from is not None and len(self.calls) >= self.refuse_from:
            r = requests.Response()
            r.status_code, r.url = 429, url
            raise requests.HTTPError("429 Client Error: Too Many Requests", response=r)
        if url.endswith("/search"):
            page = params["start"] // 10
            items = self.pages[page] if page < len(self.pages) else []
            return {"data": {"positions": items, "count": sum(len(p) for p in self.pages)}}
        return {"data": {"jobDescription": "Base pay range USD $150,000 - $250,000 per year", "locations": []}}

    def kinds(self):
        return [k for k, _ in self.calls]


def run(site, state=None, seen=()):
    """Read Microsoft with a fake site and an in-memory reader state."""
    store = dict({"sweep_due": None, "resume_query": None, "resume_page": None, "last_sweep": None,
                  "last_complete": None}, **(state or {}))
    with mock.patch.object(sr, "_request", site), \
         mock.patch.object(sr, "_the_filter", lambda: F), \
         mock.patch("agent.db.reader_state", lambda name: dict(store)), \
         mock.patch("agent.db.search_ids", lambda name: set(map(str, seen))):
        return sr.run_reader(MS)


class PreCheck(unittest.TestCase):
    def test_outside_locations_are_not_detailed(self):
        self.assertFalse(sr.passes_precheck("Senior Technical Program Manager", ["Japan, Tokyo-to, Tokyo"], F))
        self.assertFalse(sr.passes_precheck("Principal Technical Program Manager", ["Ireland, Dublin, Dublin"], F))

    def test_a_location_the_filter_keeps_is_detailed(self):
        locs = ["United States, Washington, Redmond", "United States, California, Mountain View"]
        self.assertTrue(sr.passes_precheck("Principal Technical Program Manager", locs, F))
        self.assertEqual(sr.passes_precheck("Principal Technical Program Manager", locs, F), F.any_location_ok(locs))

    def test_unclear_locations_always_get_details(self):
        for locs in ([], [""], ["United States, Multiple Locations, Multiple Locations"], ["Redmond"],
                     ["2 Locations"], ["Remote"], ["United States, Remote"]):
            self.assertTrue(sr.location_unclear(locs), locs)
            self.assertTrue(sr.passes_precheck("Senior Technical Program Manager", locs, F), locs)

    def test_title_rules_match_the_full_filter(self):
        for title in ("Senior Technical Program Manager", "Program Manager Intern", "Business Program Manager"):
            expected = bool(F.title_re.search(title)) and not F.exclude_re.search(title)
            locs = ["United States, California, Mountain View"]
            self.assertEqual(sr.passes_precheck(title, locs, F), expected, title)

    def test_never_drops_a_job_the_full_filter_keeps(self):
        """Whatever the pre-check skips, the full filter would reject anyway (title or location)."""
        cases = [("Senior Technical Program Manager", ["Japan, Tokyo-to, Tokyo"]),
                 ("Senior Technical Program Manager", ["United States, California, Mountain View"]),
                 ("Senior Program Manager Intern", ["United States, California, Mountain View"]),
                 ("Principal TPM", ["United States, Washington, Redmond"])]
        from agent.fetchers import Posting
        for title, locs in cases:
            if not sr.passes_precheck(title, locs, F):
                p = Posting(company="X", source_id="1", title=title, locations=locs, url="", description="",
                            pay_top=500000)
                self.assertFalse(F.check(p)[0], (title, locs))

    def test_detail_requests_only_for_jobs_that_pass(self):
        page = [job(1, locs=("Japan, Tokyo-to, Tokyo",)), job(2), job(3, locs=("United States, Multiple Locations",))]
        site = FakeSite([page])
        found = run(site, state={"last_sweep": TODAY.isoformat(), "last_complete": TODAY.isoformat()})
        self.assertEqual(site.kinds().count("position_details"), 2)     # jobs 2 and 3, not Tokyo
        self.assertEqual({p.source_id for p in found}, {"1", "2", "3"})   # Tokyo is still returned (filtered later)


class PartialResults(unittest.TestCase):
    def test_keeps_what_was_read_when_the_site_refuses(self):
        pages = [[job(i) for i in range(1, 11)], [job(i) for i in range(11, 21)], [job(i) for i in range(21, 31)]]
        site = FakeSite(pages, refuse_from=12)       # page 1 + 10 details, then page 2 is refused
        found = run(site)                            # no sweep yet -> full sweep
        self.assertTrue(found.partial)
        self.assertEqual(len(found), 10)
        self.assertIn('query "technical program manager", page 2', found.stopped_at)
        self.assertIn("429", found.error)
        # the sweep resumes at the refused page next time
        self.assertEqual((found.state["resume_query"], found.state["resume_page"]), (0, 1))
        self.assertTrue(found.state["sweep_due"])
        self.assertEqual(found.search_ids, {str(i) for i in range(1, 11)})

    def test_resumed_sweep_starts_where_it_stopped(self):
        site = FakeSite([[job(i) for i in range(1, 11)], [job(i) for i in range(11, 16)]])
        found = run(site, state={"sweep_due": 1, "resume_query": 2, "resume_page": 1})
        searches = [p for k, p in site.calls if k == "search"]
        self.assertEqual((searches[0]["query"], searches[0]["start"]), ("program management", 10))
        self.assertFalse(found.partial)
        self.assertIsNone(found.state["resume_query"])
        self.assertEqual(found.state["last_sweep"], TODAY.isoformat())

    def test_partial_newest_first_read_makes_a_full_sweep_due(self):
        site = FakeSite([[job(i) for i in range(1, 11)]], refuse_from=1)
        found = run(site, state={"last_sweep": TODAY.isoformat(), "last_complete": TODAY.isoformat()})
        self.assertTrue(found.partial)
        self.assertTrue(found.state["sweep_due"])


class NewestFirst(unittest.TestCase):
    def setUp(self):
        # a sweep finished yesterday; today is not the weekly sweep day for this test
        self.state = {"last_sweep": (TODAY - dt.timedelta(days=1)).isoformat(),
                      "last_complete": (TODAY - dt.timedelta(days=1)).isoformat()}
        if TODAY.weekday() == sr.SWEEP_WEEKDAY:
            self.state["last_sweep"] = TODAY.isoformat()

    def test_stops_at_the_first_page_of_jobs_already_seen(self):
        pages = [[job(i, title="Software Engineer") for i in range(1, 11)],
                 [job(i, title="Software Engineer") for i in range(11, 21)],
                 [job(i) for i in range(21, 31)]]
        site = FakeSite(pages)
        found = run(site, state=self.state, seen=range(11, 21))
        searches = [p for k, p in site.calls if k == "search"]
        self.assertEqual(len(searches), 2)                         # page 2 was all seen -> stop
        self.assertEqual(searches[0].get("sort_by"), "timestamp")
        self.assertEqual(searches[0].get("location"), "United States")
        self.assertEqual(searches[0]["query"], "program manager")
        self.assertFalse(found.state["sweep_due"])
        self.assertEqual(found.state["last_complete"], TODAY.isoformat())

    def test_jobs_posted_before_the_last_complete_read_count_as_covered(self):
        old = sr.COVERED_MARGIN_DAYS + 5
        pages = [[job(i, title="Software Engineer") for i in range(1, 11)],
                 [job(i, title="Software Engineer", days_ago=old) for i in range(11, 21)],
                 [job(i) for i in range(21, 31)]]
        site = FakeSite(pages)
        run(site, state=self.state)                                # nothing recorded yet
        self.assertEqual(len([1 for k, _ in site.calls if k == "search"]), 2)

    def test_running_out_of_pages_without_reaching_seen_jobs_makes_a_sweep_due(self):
        pages = [[job(i * 100 + k, title="Software Engineer") for k in range(10)] for i in range(sr.MAX_DAILY_PAGES + 2)]
        found = run(FakeSite(pages), state=self.state)
        self.assertTrue(found.state["sweep_due"])

    def test_no_sweep_company_stays_newest_first_with_a_page_cap(self):
        pages = [[job(i * 100 + k, title="Software Engineer") for k in range(10)] for i in range(10)]
        site = FakeSite(pages)
        state = {"sweep_due": 1, "resume_query": 0, "resume_page": 3}          # a sweep was due
        with mock.patch.dict(MS, {"full_sweep": False, "max_pages": 3}):
            found = run(site, state=state)
        searches = [p for k, p in site.calls if k == "search"]
        self.assertEqual(len(searches), 3)                                    # capped, never a sweep
        self.assertTrue(all(p.get("sort_by") == "timestamp" for p in searches))
        self.assertFalse(found.state["sweep_due"])
        self.assertIsNone(found.state["resume_query"])
        self.assertEqual(found.state["last_complete"], TODAY.isoformat())

    def test_no_sweep_company_partial_read_does_not_force_a_sweep(self):
        with mock.patch.dict(MS, {"full_sweep": False}):
            found = run(FakeSite([[job(1)]], refuse_from=1))
        self.assertTrue(found.partial)
        self.assertFalse(found.state["sweep_due"])

    def test_weekly_sweep(self):
        sunday = TODAY + dt.timedelta(days=(sr.SWEEP_WEEKDAY - TODAY.weekday()) % 7)
        self.assertTrue(sr._sweep_needed({"last_sweep": (sunday - dt.timedelta(days=7)).isoformat()}, sunday))
        self.assertFalse(sr._sweep_needed({"last_sweep": sunday.isoformat()}, sunday))
        self.assertFalse(sr._sweep_needed({"last_sweep": (sunday - dt.timedelta(days=1)).isoformat()}, sunday))
        monday = sunday + dt.timedelta(days=1)
        self.assertFalse(sr._sweep_needed({"last_sweep": sunday.isoformat()}, monday))
        self.assertTrue(sr._sweep_needed({"last_sweep": (monday - dt.timedelta(days=9)).isoformat()}, monday))
        self.assertTrue(sr._sweep_needed({}, monday))                       # never swept


def response(status, retry_after=None):
    r = requests.Response()
    r.status_code = status
    if retry_after is not None:
        r.headers["Retry-After"] = retry_after
    r.request = requests.Request("GET", "https://example.com/x").prepare()
    return r


class RetryAfter(unittest.TestCase):
    def fetch(self, answers):
        answers = list(answers)
        with mock.patch.object(http.requests, "request", side_effect=lambda *a, **k: answers.pop(0)) as req, \
             mock.patch.object(http, "_wait_turn", lambda host: None), \
             mock.patch.object(http.time, "sleep") as sleep:
            r = http.fetch("GET", "https://example.com/x")
        return r, req.call_count, [c.args[0] for c in sleep.call_args_list]

    def test_429_without_retry_after_waits_a_minute_and_tries_once_more(self):
        r, calls, waits = self.fetch([response(429), response(200)])
        self.assertEqual((r.status_code, calls, waits), (200, 2, [60]))
        r, calls, waits = self.fetch([response(429), response(429)])
        self.assertEqual((r.status_code, calls, waits), (429, 2, [60]))

    def test_429_with_retry_after_waits_that_long_once(self):
        r, calls, waits = self.fetch([response(429, "120"), response(200)])
        self.assertEqual((r.status_code, calls, waits), (200, 2, [120.0]))

    def test_429_twice_stops(self):
        r, calls, _ = self.fetch([response(429, "30"), response(429, "30")])
        self.assertEqual((r.status_code, calls), (429, 2))

    def test_retry_after_over_five_minutes_stops(self):
        r, calls, waits = self.fetch([response(429, "600")])
        self.assertEqual((r.status_code, calls, waits), (429, 1, []))

    def test_retry_after_as_a_date(self):
        when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=90)
        secs = http.retry_after_seconds(when.strftime("%a, %d %b %Y %H:%M:%S GMT"))
        self.assertTrue(80 <= secs <= 91, secs)

    def test_server_errors_still_retry(self):
        r, calls, waits = self.fetch([response(503), response(503), response(200)])
        self.assertEqual((r.status_code, calls), (200, 3))

    def test_microsoft_is_a_slow_host(self):
        self.assertTrue(http._slow("apply.careers.microsoft.com"))


if __name__ == "__main__":
    unittest.main()


class PausedCompanies(unittest.TestCase):
    def test_a_paused_company_is_not_read(self):
        import tempfile
        import run_daily
        from agent import db
        tmp = Path(tempfile.mkdtemp())
        today = dt.date.today()
        companies = [{"name": "PausedCo", "board": "x", "paused_until": (today + dt.timedelta(days=3)).isoformat()},
                     {"name": "OldPause", "board": "x", "paused_until": (today - dt.timedelta(days=1)).isoformat()}]
        read = []
        with mock.patch.object(db, "DB_FILE", tmp / "jobs.db"), mock.patch.object(db, "DATA_DIR", tmp), \
             mock.patch.object(run_daily, "DATA_DIR", tmp), mock.patch.object(run_daily, "LOG_DIR", tmp), \
             mock.patch.object(run_daily, "load_settings", lambda: dict(EXAMPLE, companies=companies)), \
             mock.patch.object(run_daily, "resume_text", lambda p: "resume"), \
             mock.patch.object(run_daily, "fetch_company", lambda c, **k: read.append(c["name"]) or []):
            run_daily.run(trigger="test", dry_run=True)
        self.assertEqual(read, ["OldPause"])
