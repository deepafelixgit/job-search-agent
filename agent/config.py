"""Loads settings.yaml and knows where every file lives."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "settings.yaml"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"
LOG_DIR = DATA_DIR / "logs"
DB_FILE = DATA_DIR / "jobs.db"
SPREADSHEET = ROOT / "Applications.xlsx"


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        raise FileNotFoundError(
            "settings.yaml is missing. Copy settings.example.yaml to settings.yaml "
            "and fill in your own values.")
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        settings = yaml.safe_load(f) or {}
    DATA_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)
    return settings


def resume_path(settings: dict) -> Path:
    return ROOT / settings["candidate"]["resume_file"]


def tailoring_guidance(settings: dict) -> str:
    """The candidate's own resume-tailoring guidance (candidate.tailoring_guidance_file in
    settings.yaml), or "" when none is set. A missing file is an error, so a typo never
    silently drops the guidance."""
    name = (settings.get("candidate") or {}).get("tailoring_guidance_file")
    if not name:
        return ""
    path = ROOT / name
    if not path.is_file():
        raise FileNotFoundError(f"tailoring_guidance_file in settings.yaml points to {name}, which doesn't exist.")
    return path.read_text(encoding="utf-8").strip()


class APIWritingDisabled(RuntimeError):
    """Raised when an API writing step is called while generation_mode is claude_code."""


def ensure_api_writing_allowed(settings: dict = None) -> None:
    """Applications are written by Claude Code on the Claude plan (generation_mode: claude_code, the
    default). Then the API writing steps (evidence search, resume tailoring, cover letter, recruiter
    message, claim check, coverage report) refuse to run, so writing an application never costs API
    money. Scoring is not affected."""
    mode = ((settings if settings is not None else load_settings()).get("generation_mode") or "claude_code")
    if mode != "api":
        raise APIWritingDisabled(
            "Applications are written by Claude Code on your Claude plan (generation_mode: claude_code), so the "
            "API writing step was not run. Use the Create button or /write-application. To use the old API path, "
            "set generation_mode: api in settings.yaml.")
