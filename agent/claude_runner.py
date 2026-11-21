"""Start Claude Code in the background to write the queued applications (generation_mode: claude_code).

Billing safety: Claude Code bills the API instead of your Claude plan whenever an API key (or another
API credential) is in its environment; in `claude -p` mode the key is always used when present. So the
background run gets an environment with every such variable removed, and it only starts after
`claude auth status` (run with that same environment) reports your Claude plan login.

Only one background run at a time. Each run's output goes to data/logs/claude_run_<time>.log.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import app_queue
from .config import DATA_DIR, LOG_DIR, ROOT

# Environment variables that make Claude Code use API billing or a cloud provider instead of your plan
BILLING_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_BEARER_TOKEN_BEDROCK", "CLAUDE_CODE_API_KEY")
BILLING_NAMES = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_PROFILE"}
PLAN_LOGINS = {"claude.ai"}               # `claude auth status` authMethod for a Claude subscription login

PY = ".venv/bin/python"
ALLOWED_TOOLS = [
    "Read", "Glob", "Grep", "WebSearch", "WebFetch", "Agent", "TodoWrite",
    "Write(./output/**)", "Edit(./output/**)",
    f"Write(/{ROOT}/output/**)", f"Edit(/{ROOT}/output/**)",     # the same folder by absolute path ("//...")
    f"Bash({PY} -m agent.render_application *)",
    f"Bash({PY} -m agent.app_queue *)",
    f"Bash({PY} -m agent.company_facts *)",
    "mcp__career-evidence__*", "mcp__job-postings__get_posting",
]
DENIED_TOOLS = ["Read(./.env)", "Read(./.env.*)", "Read(**/.env)"]
LIMIT_RE = re.compile(r"hit your (session |weekly |opus |sonnet |usage )?limit|usage limit reached", re.I)
STATE = DATA_DIR / "claude_run.json"

_lock = threading.Lock()
_proc: Optional[subprocess.Popen] = None


def clean_env(env: Dict[str, str] = None) -> Dict[str, str]:
    """A copy of the environment without any variable that could make Claude Code bill the API."""
    env = dict(os.environ if env is None else env)
    for k in list(env):
        if k in BILLING_NAMES or k.startswith(BILLING_PREFIXES):
            del env[k]
    return env


def claude_exe() -> Optional[str]:
    for p in (shutil.which("claude"), str(Path.home() / ".local/bin/claude"), "/opt/homebrew/bin/claude",
              "/usr/local/bin/claude"):
        if p and Path(p).exists():
            return p
    return None


def plan_login() -> Tuple[bool, str]:
    """(ok, message). ok only for a Claude plan login, checked with the same clean environment the run uses."""
    exe = claude_exe()
    if not exe:
        return False, "Claude Code isn't installed (the 'claude' command wasn't found)."
    try:
        out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, timeout=60,
                             env=clean_env(), cwd=str(ROOT))
        status = json.loads(out.stdout or "{}")
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        return False, f"Couldn't check the Claude Code login ({e})."
    method = status.get("authMethod", "none")
    if not status.get("loggedIn") or method not in PLAN_LOGINS:
        return False, (f"Claude Code isn't logged in with your Claude plan (login method: {method}). In a terminal, "
                       "run 'claude' and type /login, choose your Claude account, then try again. Nothing was written.")
    return True, f"Logged in with your Claude plan ({status.get('subscriptionType') or method})."


def command(exe: str) -> List[str]:
    return [exe, "-p", "/write-application", "--output-format", "stream-json", "--verbose",
            "--permission-prompts", "none",
            "--allowedTools", *ALLOWED_TOOLS, "--disallowedTools", *DENIED_TOOLS]


def is_running() -> bool:
    if _proc is not None and _proc.poll() is None:
        return True
    try:
        pid = json.loads(STATE.read_text()).get("pid")
        if pid:
            os.kill(int(pid), 0)        # still alive (started by an earlier web app process)
            return True
    except (OSError, ValueError, FileNotFoundError):
        pass
    return False


def start() -> Tuple[bool, str]:
    """Start a background run if none is running. (ok, message)."""
    global _proc
    with _lock:
        if is_running():
            return True, "Claude Code is already writing; the new postings were added to its queue."
        if not app_queue.has_open():
            return True, "Nothing is waiting in the queue."
        ok, msg = plan_login()
        if not ok:
            return False, msg
        LOG_DIR.mkdir(exist_ok=True)
        log = LOG_DIR / f"claude_run_{dt.datetime.now():%Y-%m-%d_%H%M%S}.log"
        out = open(log, "w", encoding="utf-8")
        env = dict(clean_env(), JOB_AGENT_BACKGROUND="1")      # the skill: nobody is there to answer questions
        _proc = subprocess.Popen(command(claude_exe()), cwd=str(ROOT), env=env, stdout=out,
                                 stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        STATE.write_text(json.dumps({"pid": _proc.pid, "started": dt.datetime.now().isoformat(timespec="seconds"),
                                     "log": str(log)}))
        threading.Thread(target=_watch, args=(_proc, out, log), daemon=True).start()
        return True, "Claude Code started writing in the background."


def _watch(proc: subprocess.Popen, out, log: Path) -> None:
    code = proc.wait()
    out.close()
    finish(code, log.read_text(encoding="utf-8", errors="replace"))
    try:
        STATE.unlink()
    except FileNotFoundError:
        pass


def finish(code: int, output: str) -> str:
    """After a run ends: usage limit -> pause what's left (drafts are kept); another failure -> the posting
    being written needs your review. Returns what happened."""
    result = _final_result(output)
    if LIMIT_RE.search(output):
        n = app_queue.pause_open(_limit_note(output))
        return f"paused {n} posting(s): Pro usage limit"
    if code != 0:
        for it in app_queue.items():
            if it["status"] == app_queue.WRITING:
                app_queue.set_status(it["uid"], app_queue.REVIEW,
                                     f"The background run stopped before finishing: {result[:200]}")
        return "stopped with an error"
    return "finished"


def _final_result(output: str) -> str:
    for line in reversed(output.splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "result":
            return str(ev.get("result") or ev.get("subtype") or "")
    return output.strip().splitlines()[-1] if output.strip() else ""


def _limit_note(output: str) -> str:
    m = re.search(r"(hit your[^\n\"]{0,80})", output, re.I)
    return (m.group(1).strip() if m else "Pro usage limit reached") + " - click Resume after it resets"
