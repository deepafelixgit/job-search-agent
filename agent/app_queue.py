"""The queue of applications Claude Code writes (data/queue.json). No Claude API calls.

The web app adds postings; the write-application skill takes them one at a time. Every change goes
through here under a file lock, so the two never overwrite each other.

  python -m agent.app_queue next                 start the next posting: prints its details as JSON
                                                 (an interrupted one first, so drafts are continued)
  python -m agent.app_queue set <uid> <status> [note]
  python -m agent.app_queue add <uid> [<uid> ...]
  python -m agent.app_queue rewrite <uid> <folder>   write a posting again in its existing folder; the old
                                                     files move to <folder>/old_api_version/
  python -m agent.app_queue add-link <url> <company> <role title>   a posting that isn't in jobs.db
  python -m agent.app_queue list

Statuses: Queued, Writing, Ready, Needs your review, Paused: Pro usage limit, Failed
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional

from .config import DATA_DIR

QUEUE = DATA_DIR / "queue.json"
LOCK = DATA_DIR / "queue.lock"
QUEUED, WRITING, READY, REVIEW = "Queued", "Writing", "Ready", "Needs your review"
PAUSED, FAILED = "Paused: Pro usage limit", "Failed"
OPEN = (WRITING, PAUSED, QUEUED)          # still to be written, in the order they're taken


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


@contextmanager
def _locked() -> Iterator[Dict]:
    DATA_DIR.mkdir(exist_ok=True)
    with open(LOCK, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(QUEUE.read_text(encoding="utf-8")) if QUEUE.exists() else {"items": []}
            yield data
            tmp = QUEUE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(QUEUE)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def items() -> List[Dict]:
    with _locked() as q:
        return list(q["items"])


def add(postings: List[Dict], folder: str = "") -> List[str]:
    """postings: [{uid, company, title, url}]. Ones already open in the queue aren't added twice;
    finished ones are queued again (a new folder is made, unless `folder` names the one to write in)."""
    added = []
    with _locked() as q:
        by_uid = {it["uid"]: it for it in q["items"]}
        for p in postings:
            it = by_uid.get(p["uid"])
            if it and it["status"] in OPEN:
                continue
            new = {"uid": p["uid"], "company": p.get("company", ""), "title": p.get("title", ""),
                   "url": p.get("url", ""), "status": QUEUED, "folder": folder, "note": "", "updated": _now()}
            if it:
                q["items"][q["items"].index(it)] = new
            else:
                q["items"].append(new)
            added.append(p["uid"])
    return added


def rewrite(uid: str, folder: str) -> Dict:
    """Queue a posting to be written again in its existing application folder. The folder's current files
    (an application made the old way) move into <folder>/old_api_version/ first, so they're kept."""
    import shutil
    from . import db
    path = Path(folder).resolve()
    rows = db.get_postings([uid])
    if not rows or not path.is_dir():
        raise SystemExit(f"Unknown posting {uid} or folder {path}")
    old = path / "old_api_version"
    old.mkdir(exist_ok=True)
    moved = []
    for f in path.iterdir():
        if f.name != "old_api_version":
            shutil.move(str(f), str(old / f.name))
            moved.append(f.name)
    r = rows[0]
    added = add([{"uid": uid, "company": r["company"], "title": r["title"], "url": r["url"]}], folder=str(path))
    return {"uid": uid, "folder": str(path), "moved_to_old_api_version": moved, "queued": bool(added)}


def set_status(uid: str, status: str, note: str = "", folder: str = None) -> None:
    with _locked() as q:
        for it in q["items"]:
            if it["uid"] == uid:
                it["status"], it["note"], it["updated"] = status, note, _now()
                if folder is not None:
                    it["folder"] = folder
                return
    raise KeyError(uid)


def pause_open(note: str = "") -> int:
    """Mark every posting still to be written as paused (Pro usage limit). Drafts are kept."""
    n = 0
    with _locked() as q:
        for it in q["items"]:
            if it["status"] in (WRITING, QUEUED):
                it["status"], it["note"], it["updated"] = PAUSED, note, _now()
                n += 1
    return n


def resume_paused() -> int:
    n = 0
    with _locked() as q:
        for it in q["items"]:
            if it["status"] == PAUSED:
                it["status"], it["updated"] = QUEUED if not it["folder"] else WRITING, _now()
                n += 1
    return n


def has_open() -> bool:
    return any(it["status"] in (WRITING, QUEUED) for it in items())


def take_next() -> Optional[Dict]:
    """The next posting to write: an interrupted one (Writing, with its folder) first, then the oldest
    queued one. Marks it Writing and gives it its application folder (named the way the agent does)."""
    from . import db
    from .render_application import application_folder
    with _locked() as q:
        open_items = [it for it in q["items"] if it["status"] in (WRITING, QUEUED)]
        if not open_items:
            return None
        it = next((i for i in open_items if i["status"] == WRITING), open_items[0])
        rows = db.get_postings([it["uid"]])
        post = dict(rows[0]) if rows else {}
        if not it["folder"]:
            folder = application_folder(post.get("company") or it["company"], post.get("title") or it["title"])
            folder.mkdir(parents=True, exist_ok=True)
            it["folder"] = str(folder)
        it["status"], it["updated"] = WRITING, _now()
        background = os.environ.get("JOB_AGENT_BACKGROUND") == "1"
        drafts = [f.name for f in Path(it["folder"]).iterdir()
                  if f.name not in ("render_report.json", "old_api_version")]
        return {"uid": it["uid"], "folder": it["folder"], "continuing": bool(drafts), "drafts": sorted(drafts),
                "company": post.get("company") or it["company"], "title": post.get("title") or it["title"],
                "url": post.get("url") or it["url"], "score": post.get("score"), "posted": post.get("posted"),
                "run_date": post.get("run_date"), "location": post.get("location"),
                "in_database": bool(rows), "background": background}


def main(argv: List[str]) -> int:
    cmd = argv[0] if argv else "list"
    if cmd == "next":
        nxt = take_next()
        print(json.dumps(nxt or {"done": True}, indent=1, ensure_ascii=False))
    elif cmd == "set" and len(argv) >= 3:
        set_status(argv[1], argv[2], " ".join(argv[3:]))
        print(f"{argv[1]}: {argv[2]}")
    elif cmd == "add-link" and len(argv) >= 4:
        url = argv[1]
        uid = "link:" + hashlib.sha1(url.encode()).hexdigest()[:12]
        added = add([{"uid": uid, "company": argv[2], "title": " ".join(argv[3:]), "url": url}])
        print(json.dumps({"uid": uid, "queued": bool(added)}))
    elif cmd == "rewrite" and len(argv) == 3:
        print(json.dumps(rewrite(argv[1], argv[2])))
    elif cmd == "add" and len(argv) >= 2:
        from . import db
        rows = {r["uid"]: r for r in db.get_postings(argv[1:])}
        added = add([{"uid": u, "company": rows[u]["company"] if u in rows else "",
                      "title": rows[u]["title"] if u in rows else "", "url": rows[u]["url"] if u in rows else ""}
                     for u in argv[1:]])
        print(f"queued: {', '.join(added) or 'nothing new'}")
    else:
        for it in items():
            print(f"{it['status']:<26} {it['uid']:<34} {it['company']} - {it['title']}  {it['folder']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
