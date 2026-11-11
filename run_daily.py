"""The daily job: fetch -> filter -> skip already-seen -> score -> save.

Run it yourself with:   .venv/bin/python run_daily.py
Show the resume text:   .venv/bin/python run_daily.py --show-resume
Test feeds, no Claude:  .venv/bin/python run_daily.py --dry-run
After loosening a rule: .venv/bin/python run_daily.py --recheck
   (postings that failed the rules before get checked again on the next run)

How "new" works:
- A posting is looked at once. If it fails the rules, only its ID is stored.
  If it passes, its full details and score are stored.
- The first time a company is checked (including every company on the first
  run after this was added), only postings published in the last
  `first_check_days` days are scored; older ones are just remembered as seen.
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import fcntl  # noqa: E402
import sys  # noqa: E402
import traceback  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402

from agent import db  # noqa: E402
from agent.config import DATA_DIR, LOG_DIR, load_settings, resume_path  # noqa: E402
from agent.fetchers import fetch_company  # noqa: E402
from agent.filters import apply_filters  # noqa: E402
from agent.resume import resume_text  # noqa: E402
from agent.scorer import score_posting  # noqa: E402


def log(msg: str) -> None:
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    with open(LOG_DIR / "runs.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(trigger: str = "manual", dry_run: bool = False, only: list = None, retry_of: int = None) -> dict:
    """only: company names to read (e.g. the failed companies of an earlier run); default all.
    retry_of: the run whose failed companies this run retries (recorded on that run)."""
    settings = load_settings()
    lock = open(DATA_DIR / "run.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Another run is already in progress - skipping.")
        return {"status": "busy"}

    today = dt.date.today().isoformat()
    run_id = None if dry_run else db.start_run(today, dt.datetime.now().isoformat(timespec="seconds"), trigger)
    fetched, matched, new, problems = 0, 0, 0, []
    try:
        resume = resume_text(resume_path(settings))
        window_days = int(settings.get("first_check_days", 7) or 7)
        cutoff = (dt.date.today() - dt.timedelta(days=window_days)).isoformat()
        already_checked = db.checked_companies()
        all_postings, first_time, fetched_ok = [], set(), []
        companies = settings.get("companies", [])
        if only:
            wanted = {n.strip().lower() for n in only}
            companies = [c for c in companies if c.get("name", "").strip().lower() in wanted]
            log(f"Retrying {len(companies)} companies: " + ", ".join(c["name"] for c in companies))
        seen = frozenset(db.seen_uids())

        def _fetch(company):
            first = company.get("name", "").strip().lower() not in already_checked
            try:
                return fetch_company(company, known=seen, older_than=cutoff if first else None), None
            except Exception as e:
                return None, e

        failed = []                                         # (company, error) for this run
        with ThreadPoolExecutor(max_workers=6) as pool:     # read 6 job boards at a time (see agent/http.py)
            fetched_lists = list(pool.map(_fetch, companies))
        for company, (found, err) in zip(companies, fetched_lists):
            try:
                if err:
                    raise err
                all_postings += found
                fetched_ok.append(company["name"])
                if company["name"].strip().lower() not in already_checked:
                    first_time.add(company["name"])
                log(f"{company['name']}: {len(found)} postings fetched"
                    + (f" (first check: only the last {window_days} days count)"
                       if company["name"] in first_time else ""))
            except Exception as e:
                problems.append(f"{company.get('name')}: {e}")
                failed.append((company.get("name"), str(e)))
                log(f"{company.get('name')}: FAILED to fetch after retries ({e})")
        fetched = len(all_postings)

        unseen = [p for p in all_postings if p.uid not in seen]
        kept = apply_filters(settings, unseen)
        kept_uids = {p.uid for p, _ in kept}
        filtered_out = [p.uid for p in unseen if p.uid not in kept_uids]
        too_old = [p for p, _ in kept if p.company in first_time and p.posted and p.posted < cutoff]
        too_old_uids = {p.uid for p in too_old}
        fresh = [(p, why) for p, why in kept if p.uid not in too_old_uids]
        matched = len(kept)
        log(f"{len(unseen)} postings not seen before; {matched} passed the filters; "
            f"{len(too_old)} skipped as older than {window_days} days (first check); {len(fresh)} to score")

        if dry_run:
            for p, why in kept:
                tag = "OLD " if p.uid in too_old_uids else "NEW "
                print(f"  {tag}| {p.company} | {p.title} | {p.location_display} | posted: {p.posted or '-'} | "
                      f"pay top: {p.pay_top or '-'} | kept because: {why}")
            return {"status": "dry-run", "matched": matched, "new": len(fresh)}

        # Only companies that were read successfully are marked as checked; a failed company keeps
        # its first-check window (first_check_days) for the next successful read.
        db.add_run_failures(run_id, failed)
        if retry_of:
            db.mark_failures_retried(retry_of, [c["name"] for c in companies], run_id)
        db.remember_ids(filtered_out, "filtered", today)
        db.remember_ids(list(too_old_uids), "old", today)
        for name in fetched_ok:
            db.mark_company_checked(name, today)

        model = settings["models"]["scoring"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(score_posting, model, resume, p): (p, why) for p, why in fresh}
            for fut in as_completed(futures):
                p, why = futures[fut]
                try:
                    s = fut.result()
                    db.save_posting(p, why, s["score"], s["reason"], today,
                                    dt.datetime.now().isoformat(timespec="seconds"))
                    new += 1
                    log(f"  scored {s['score']}/10: {p.company} - {p.title}")
                except Exception as e:  # not saved -> it will be retried on the next run
                    problems.append(f"Scoring failed for {p.company} - {p.title}: {e}")
                    log(f"  scoring FAILED (will retry next run): {p.company} - {p.title}: {e}")

        status = "ok" if not problems else "ok, with problems"
        message = f"{fetched} fetched, {len(unseen)} not seen before, {matched} matched, " \
                  f"{len(too_old)} too old (first check), {new} new and scored." + \
                  (" Problems: " + " | ".join(problems) if problems else "")
        db.finish_run(run_id, dt.datetime.now().isoformat(timespec="seconds"), status, fetched, matched, new, message)
        log("Run finished: " + message)
        return {"status": status, "new": new, "message": message}
    except Exception as e:
        with open(LOG_DIR / "runs.log", "a", encoding="utf-8") as f:
            f.write(traceback.format_exc())
        if run_id:
            db.finish_run(run_id, dt.datetime.now().isoformat(timespec="seconds"), "failed",
                          fetched, matched, new, f"Run failed: {e}")
        log(f"Run FAILED: {e}")
        return {"status": "failed", "message": str(e)}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--show-resume", action="store_true", help="print the text extracted from the resume")
    ap.add_argument("--dry-run", action="store_true", help="fetch + filter only; no Claude calls, nothing saved")
    ap.add_argument("--recheck", action="store_true",
                    help="forget postings that failed the rules, so the next run checks them again")
    ap.add_argument("--trigger", default="manual")
    args = ap.parse_args()
    if args.recheck:
        n = db.forget_filtered_ids()
        print(f"{n} filtered-out postings will be checked again on the next run.")
        sys.exit(0)
    if args.show_resume:
        print(resume_text(resume_path(load_settings())))
        sys.exit(0)
    result = run(trigger=args.trigger, dry_run=args.dry_run)
    sys.exit(1 if result.get("status") == "failed" else 0)
