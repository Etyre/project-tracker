"""Configuration: config.toml next to the package, plus env/keychain secrets."""
from __future__ import annotations

import os
import pathlib
import subprocess
import tomllib
from functools import lru_cache
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = pathlib.Path(os.environ.get("TASK_TRACKER_CONFIG") or (ROOT / "config.toml"))
RULES_PATH = ROOT / "Rules and heuristics for scheduling.md"
APP_SUPPORT = pathlib.Path(os.environ.get("TASK_TRACKER_DATA") or (pathlib.Path.home() / "Library" / "Application Support" / "TaskTracker"))
DB_PATH = APP_SUPPORT / "tracker.sqlite3"
ENV_FILE = APP_SUPPORT / "env"

DEFAULTS: dict[str, Any] = {
    "general": {
        "host": "127.0.0.1",
        "port": 8791,
        "timezone": "",                      # empty = system local
        "work_start": "09:00",
        "work_end": "18:00",
        "default_estimate_minutes": 30,
        "min_block_minutes": 15,
        "replan_drift_minutes": 20,          # re-plan when the next block is this late (or early)
        "overrun_minutes": 30,               # ... or the active block has run this far past its end
        "replan_min_interval_minutes": 10,   # never auto re-plan more often than this
    },
    "snapshots": {
        "enable": True,
        "time": "05:00",                     # daily Markdown snapshot of the whole outline (local time)
        "dir": "",                           # empty = <data folder>/snapshots
    },
    "llm": {
        "model": "claude-opus-5-5",
        "effort": "high",
        "quick_add_effort": "medium",
        "enable": True,
    },
    "toggl": {
        "tag": "task-tracker",
        "project": "",                       # optional Toggl project name for all entries
        "journal_description": "Journaling",
        "exercise_description": "Exercise",
    },
    "linear": {
        "enable": False,
        "sync_minutes": 10,
        "category_name": "Linear",           # top-level project the mirrored issues live under
        "group_by": "project",               # "project" | "team"
    },
    "calendar": {
        "ics_urls": [],                      # Google Calendar -> Settings -> "Secret address in iCal format"
        "ignore_titles": [],
        "busy_only": True,                   # ignore events marked free/transparent
    },
    "sheets": {
        "daily_checklist_csv": "",           # CSV export URL of the daily checklist sheet
        "exercise_csv": "",                  # CSV export URL of the exercise sheet
        "cache_minutes": 30,
    },
    "exercise": {
        "rotation": ["run", "strength", "mobility"],   # fallback when the sheet is not configured
        "minutes": 45,
    },
    "journal": {
        "app": "Logseq",                     # or "Roam Research" / a URL like "https://roamresearch.com/..."
        "desktop": 1,                        # Mission Control desktop number to switch to (0 = don't switch)
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    data: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            data = tomllib.load(f)
    return _merge(DEFAULTS, data)


def reload() -> dict[str, Any]:
    load.cache_clear()
    return load()


def _load_env_file() -> None:
    """A Finder-launched app has no shell env; ~/Library/Application Support/TaskTracker/env fills in."""
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


_load_env_file()


def _keychain(account: str) -> str | None:
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "TaskTracker", "-a", account, "-w"],
            capture_output=True, text=True, timeout=5,
        )
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


@lru_cache(maxsize=None)
def secret(name: str) -> str | None:
    """Secrets: env var first (TOGGL_API_TOKEN, LINEAR_API_KEY, ANTHROPIC_API_KEY), then Keychain
    (`security add-generic-password -s TaskTracker -a toggl -w`)."""
    env_names = {
        "toggl": "TOGGL_API_TOKEN",
        "linear": "LINEAR_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
    }
    v = os.environ.get(env_names.get(name, name.upper()))
    if v:
        return v
    v = _keychain(name)
    if v and name == "anthropic":
        os.environ.setdefault("ANTHROPIC_API_KEY", v)
    return v


def rules_text() -> str:
    return RULES_PATH.read_text() if RULES_PATH.exists() else ""


def keychain_set(account: str, value: str) -> None:
    """Store a secret in the login Keychain (service "TaskTracker") and make it live for this process."""
    subprocess.run(["security", "add-generic-password", "-U", "-s", "TaskTracker", "-a", account, "-w", value],
                   capture_output=True)
    env_names = {"toggl": "TOGGL_API_TOKEN", "linear": "LINEAR_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                 "sheets": "SHEETS_TOKEN"}
    os.environ[env_names.get(account, account.upper())] = value
    secret.cache_clear()
