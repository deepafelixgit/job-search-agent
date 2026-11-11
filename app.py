"""Local web app: http://127.0.0.1:5055  (only reachable from this Mac).

Start it with:  .venv/bin/python app.py     (or double-click "Start Job Agent.command")
"""
from __future__ import annotations

import warnings

warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import datetime as dt  # noqa: E402
import secrets  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import uuid  # noqa: E402
import webbrowser  # noqa: E402
from pathlib import Path  # noqa: E402

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for  # noqa: E402

from agent import db, manual  # noqa: E402
from agent.config import OUTPUT_DIR, load_settings  # noqa: E402
from agent.generate import generate, record_in_spreadsheet  # noqa: E402
from agent.sheet import is_open_in_excel  # noqa: E402

HOST, PORT = "127.0.0.1", 5055
app = Flask(__name__)
CSRF_TOKEN = secrets.token_urlsafe(32)   # stops other websites from pressing buttons in this app
JOBS: dict = {}                          # background "create resume" jobs, by id
RUN_STATE = {"running": False, "last": None}


@app.before_request
def local_only():
    if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
        abort(403)
    if request.method == "POST" and request.form.get("csrf") != CSRF_TOKEN:
        abort(403)


@app.context_processor
def inject():
    return {"csrf": CSRF_TOKEN, "run_state": RUN_STATE}


# ------------------------------------------------------------------ pages

@app.route("/")
def home():
    dates = db.run_dates()
    if dates:
        return redirect(url_for("day", run_date=dates[0]["run_date"]))
    return render_template("day.html", dates=[], run_date=None, postings=[], runs=[], min_score=0, hidden=0,
                           failures={}, sheet_msg=request.args.get("sheet_msg"))


def _with_days(rows):
    """Add 'days' = days from the feed's published date to the day the posting
    was scored (0 = posted that same day; None = Unknown)."""
    out = []
    for r in rows:
        d = dict(r)
        try:
            scored = dt.date.fromisoformat(d.get("run_date") or "")
            d["days"] = max(0, (scored - dt.date.fromisoformat(d.get("posted") or "")).days)
        except ValueError:
            d["days"] = None
        out.append(d)
    return out


@app.route("/day/<run_date>")
def day(run_date):
    return _render_day(run_date)


def _render_day(run_date, **extra):
    settings = load_settings()
    min_score = int(settings.get("display", {}).get("min_score_to_show", 0) or 0)
    postings = _with_days(db.postings_on(run_date, min_score))
    for p in postings:                       # manual postings: the filters they'd have failed
        p["warnings"] = _warnings(settings, p) if p.get("source") == "manual" else []
    return render_template("day.html", dates=db.run_dates(), run_date=run_date,
                           postings=postings, runs=db.runs_on(run_date),
                           failures=db.failures_for([r["id"] for r in db.runs_on(run_date)]),
                           hidden=db.hidden_count(run_date, min_score),
                           min_score=min_score, error=request.args.get("error"),
                           sheet_msg=request.args.get("sheet_msg"), pending=len(db.pending_rows()), **extra)


def _warnings(settings, row):
    return manual.warnings_for(settings, row["company"], row["title"], row["location"], row["pay_top"], row["score"])


# ------------------------------------------------------------------ score a posting (manual)

def _manual_result(uid, existing):
    settings = load_settings()
    row = dict(db.get_postings([uid])[0])
    row["warnings"] = _warnings(settings, row)
    row["existing"] = existing
    return row


@app.route("/score-posting", methods=["POST"])
def score_posting():
    f = request.form
    form = {k: f.get(k, "") for k in ("url", "text", "company", "title", "location", "pay")}
    today = dt.date.today().isoformat()
    try:
        res = manual.submit(**form)
    except (manual.NeedsPaste, manual.Blocked) as e:
        return _render_day(today, manual_error=str(e), manual_form=form,
                           manual_blocked=isinstance(e, manual.Blocked))
    except Exception as e:      # e.g. scoring failed: show it, keep what was typed
        return _render_day(today, manual_error=f"Scoring failed: {e}", manual_form=form)
    row = _manual_result(res["uid"], res["existing"])
    return _render_day(row["run_date"] if res["existing"] else today, manual_result=row)


@app.route("/rescore", methods=["POST"])
def rescore():
    uid = request.form.get("uid", "")
    if not db.get_postings([uid]):
        abort(404)
    try:
        manual.rescore(load_settings(), uid)
    except Exception as e:
        row = _manual_result(uid, True)
        return _render_day(row["run_date"], manual_result=row, manual_error=f"Rescoring failed: {e}")
    row = _manual_result(uid, False)
    row["rescored"] = True
    return _render_day(row["run_date"], manual_result=row)


# ------------------------------------------------------------------ run now

def _run_in_background(**kwargs):
    from run_daily import run
    try:
        RUN_STATE["last"] = run(**dict({"trigger": "web"}, **kwargs))
    finally:
        RUN_STATE["running"] = False


@app.route("/run-now", methods=["POST"])
def run_now():
    if not RUN_STATE["running"]:
        RUN_STATE["running"] = True
        threading.Thread(target=_run_in_background, daemon=True).start()
    return redirect(request.referrer or url_for("home"))


@app.route("/retry-failed", methods=["POST"])
def retry_failed():
    """Read again only the companies whose fetch failed in one run."""
    run_id = int(request.form.get("run_id", "0") or 0)
    names = [r["company"] for r in db.failures_for([run_id]).get(run_id, []) if not r["retried_by"]]
    if names and not RUN_STATE["running"] and not _run_lock_held():
        RUN_STATE["running"] = True
        threading.Thread(target=_run_in_background, daemon=True,
                         kwargs={"trigger": "retry", "only": names, "retry_of": run_id}).start()
    return redirect(request.referrer or url_for("home"))


def _run_lock_held() -> bool:
    """True if a run (from this app or the 10 AM schedule) is in progress right now."""
    import fcntl
    from agent.config import DATA_DIR
    DATA_DIR.mkdir(exist_ok=True)
    with open(DATA_DIR / "run.lock", "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


@app.route("/api/run-status")
def run_status():
    latest = db.latest_run()
    return jsonify({"running": RUN_STATE["running"] or _run_lock_held(),
                    "latest_date": latest["run_date"] if latest else None})


# ------------------------------------------------------------------ create resumes

@app.route("/create", methods=["POST"])
def create():
    uids = request.form.getlist("uid")
    back = request.form.get("run_date") or ""
    if not uids:
        return redirect(url_for("day", run_date=back, error="Please check at least one posting first."))
    if is_open_in_excel():
        return redirect(url_for("day", run_date=back,
                                error="Applications.xlsx is open in Excel. Please close it and try again."))
    job_id = uuid.uuid4().hex[:10]
    posts = db.get_postings(uids)
    JOBS[job_id] = {"done": False, "run_date": back,
                    "items": {p["uid"]: {"company": p["company"], "title": p["title"], "status": "Waiting…"}
                              for p in posts},
                    "result": None}

    def progress(uid, msg):
        JOBS[job_id]["items"][uid]["status"] = msg

    def work():
        try:
            JOBS[job_id]["result"] = generate(uids, progress)
        except Exception as e:  # unexpected failure - show it on the page
            JOBS[job_id]["result"] = {"results": [], "sheet": {"ok": True}, "error": str(e)}
        JOBS[job_id]["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return redirect(url_for("job", job_id=job_id))


@app.route("/job/<job_id>")
def job(job_id):
    if job_id not in JOBS:
        return redirect(url_for("home"))
    return render_template("job.html", job_id=job_id, job=JOBS[job_id], dates=db.run_dates())


@app.route("/api/job/<job_id>")
def job_status(job_id):
    if job_id not in JOBS:
        abort(404)
    return jsonify(JOBS[job_id])


@app.route("/sheet/retry", methods=["POST"])
def sheet_retry():
    res = record_in_spreadsheet([])
    msg = (f"Applications.xlsx updated ({res.get('added', 0)} row(s) added)." if res["ok"]
           else res["message"])
    back = request.form.get("run_date")
    return redirect(url_for("day", run_date=back, sheet_msg=msg) if back else url_for("home", sheet_msg=msg))


@app.route("/open-folder", methods=["POST"])
def open_folder():
    path = Path(request.form.get("path", "")).resolve()
    if OUTPUT_DIR.resolve() not in path.parents or not path.is_dir():
        abort(400)
    subprocess.run(["open", str(path)], check=False)
    return ("", 204)


if __name__ == "__main__":
    url = f"http://{HOST}:{PORT}"
    print(f"Job Search Agent is running at {url}  (press Control-C to stop)")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
