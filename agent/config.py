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

