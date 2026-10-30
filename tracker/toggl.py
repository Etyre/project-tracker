"""Toggl Track v9: start/stop timers; every entry this app creates carries its own tag."""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import urllib.error
import urllib.request
from functools import lru_cache

from . import config

log = logging.getLogger("toggl")
API = "https://api.track.toggl.com/api/v9"


class TogglError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.secret("toggl"))


def _req(method: str, path: str, body: dict | None = None):
    tok = config.secret("toggl")
    if not tok:
        raise TogglError("TOGGL_API_TOKEN not set")
    auth = base64.b64encode(f"{tok}:api_token".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:300]
        raise TogglError(f"Toggl HTTP {e.code}: {msg}") from e
    except urllib.error.URLError as e:
        raise TogglError(f"Toggl unreachable: {e}") from e


@lru_cache(maxsize=1)
def me() -> dict:
    return _req("GET", "/me")


def workspace_id() -> int:
    return int(me()["default_workspace_id"])


@lru_cache(maxsize=1)
def _projects() -> dict[str, int]:
    try:
        return {p["name"].lower(): p["id"] for p in (_req("GET", "/me/projects") or [])}
    except TogglError:
        return {}


def start(description: str, extra_tags: list[str] | None = None) -> dict:
    cfg = config.load()["toggl"]
    wid = workspace_id()
    tags = [cfg["tag"], *(extra_tags or [])]
    body = {
        "created_with": "task-tracker",
        "description": description,
        "tags": tags,
        "start": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration": -1,
        "workspace_id": wid,
        "wid": wid,
    }
    if cfg.get("project"):
        pid = _projects().get(cfg["project"].lower())
        if pid:
            body["project_id"] = pid
    return _req("POST", f"/workspaces/{wid}/time_entries", body)


def current() -> dict | None:
    return _req("GET", "/me/time_entries/current")


def stop(entry_id: int | None = None) -> dict | None:
    if entry_id is None:
        cur = current()
        if not cur:
            return None
        entry_id = cur["id"]
    wid = workspace_id()
    return _req("PATCH", f"/workspaces/{wid}/time_entries/{entry_id}/stop")


def is_ours(entry: dict | None) -> bool:
    return bool(entry) and config.load()["toggl"]["tag"] in (entry.get("tags") or [])
