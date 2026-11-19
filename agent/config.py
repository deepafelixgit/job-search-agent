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
