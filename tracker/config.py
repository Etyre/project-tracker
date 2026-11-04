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
EXERCISE_PATH = ROOT / "Exercise plan.md"          # the user's own brief to Claude on how to schedule exercise
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
        "meeting_alert_minutes": 45,         # the feed shows "N minutes until X" this long before a meeting; 0 = any meeting left today
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
        "auto_project": True,                # file each timer under the Toggl project its history / Claude implies
        "project": "",                       # optional Toggl project name for all entries
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
        "apps_script_url": "",               # the deployed Apps Script's /exec URL (read + write-back)
        "exercise_script_url": "",           # the exercise app's Google Sheet sync script (/exec URL); read with ?action=export
        "daily_checklist_csv": "",           # CSV export URL of the daily checklist sheet
        "exercise_csv": "",                  # CSV export URL of the exercise sheet
        "cache_minutes": 30,
        "card_items": ["Anki review: 20"],   # checklist rows that get their own feed card every day ("name: minutes")
    },
    "exercise": {
        "rotation": ["run", "strength", "mobility"],   # fallback when Claude is not configured
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


def exercise_text() -> str:
    return EXERCISE_PATH.read_text() if EXERCISE_PATH.exists() else ""


def keychain_set(account: str, value: str) -> None:
    """Store a secret in the login Keychain (service "TaskTracker") and make it live for this process."""
    subprocess.run(["security", "add-generic-password", "-U", "-s", "TaskTracker", "-a", account, "-w", value],
                   capture_output=True)
    env_names = {"toggl": "TOGGL_API_TOKEN", "linear": "LINEAR_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                 "sheets": "SHEETS_TOKEN"}
    os.environ[env_names.get(account, account.upper())] = value
    secret.cache_clear()


# ---- editing from the app's Settings page --------------------------------------------------------------
SECRETS = {"anthropic": "ANTHROPIC_API_KEY", "toggl": "TOGGL_API_TOKEN", "linear": "LINEAR_API_KEY", "sheets": "SHEETS_TOKEN"}


def _toml_value(v: Any) -> str:
    import json
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps("" if v is None else str(v))


def save(data: dict[str, Any]) -> dict[str, Any]:
    """Write config.toml from a full config dict (sections of scalars and lists) and reload it. Only sections
    and keys known from DEFAULTS are written, coerced to the default's type."""
    merged = _merge(load(), {k: v for k, v in data.items() if isinstance(v, dict)})
    lines = ["# Task Tracker configuration, edited from the app's Settings page.",
             '# Secrets are not here: they live in the login Keychain (service "TaskTracker") or the environment.', ""]
    for sec, defaults in DEFAULTS.items():
        lines.append(f"[{sec}]")
        for k, d in defaults.items():
            v = merged.get(sec, {}).get(k, d)
            if isinstance(d, bool):
                v = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on")
            elif isinstance(d, int) and not isinstance(v, bool):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    v = d
            elif isinstance(d, list):
                v = [str(x).strip() for x in (v if isinstance(v, (list, tuple)) else str(v).replace("\n", ",").split(",")) if str(x).strip()]
            elif isinstance(d, str):
                v = "" if v is None else str(v)
            lines.append(f"{k} = {_toml_value(v)}")
        lines.append("")
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text("\n".join(lines))
    return reload()


def secret_status() -> dict[str, str]:
    """For each secret: "env" (shell / env file), "keychain", or "" when it is not set anywhere."""
    out = {}
    for name, env in SECRETS.items():
        out[name] = "env" if os.environ.get(env) and _keychain(name) != os.environ.get(env) else ("keychain" if _keychain(name) else "")
    return out
