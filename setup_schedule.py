"""Turn the 10:00 AM daily run on or off (uses macOS's built-in scheduler, launchd).

  Turn on:   .venv/bin/python setup_schedule.py
  Turn off:  .venv/bin/python setup_schedule.py --off

If the Mac is asleep at 10:00, macOS runs it as soon as the Mac wakes up.
The schedule file contains only file paths - never the API key.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LABEL = "com.jobsearchagent.daily"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def off() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
    if PLIST.exists():
        PLIST.unlink()
    print("Daily schedule turned off.")


def on() -> None:
    (ROOT / "data" / "logs").mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(ROOT / ".venv" / "bin" / "python"), str(ROOT / "run_daily.py"), "--trigger", "schedule"],
        "WorkingDirectory": str(ROOT),
        "StartCalendarInterval": {"Hour": 10, "Minute": 0},
        "StandardOutPath": str(ROOT / "data" / "logs" / "schedule.out.log"),
        "StandardErrorPath": str(ROOT / "data" / "logs" / "schedule.err.log"),
    }
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
    with open(PLIST, "wb") as f:
        plistlib.dump(plist, f)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        print("Couldn't turn on the schedule:", r.stderr.strip())
        sys.exit(1)
    print("Daily schedule is ON: the agent will run every day at 10:00 AM.")


if __name__ == "__main__":
    off() if "--off" in sys.argv else on()
