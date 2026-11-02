"""Toggl Track v9: start/stop timers; every entry this app creates carries its own tag."""
from __future__ import annotations

import base64
import datetime as dt
import json
import logging
import time
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


def entries(days: int = 90) -> list[dict]:
    """The user's recent time entries (any source), newest first."""
    end = dt.date.today() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=min(days, 90) + 1)
    return _req("GET", f"/me/time_entries?start_date={start.isoformat()}&end_date={end.isoformat()}") or []


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


# ---- the right project and tags for a timer ------------------------------------------------------------
# Deterministic first: a description the user has tracked before gets the project and tags it had last time.
# Otherwise Claude chooses from the user's own projects (with example descriptions from the history) and tags,
# and only a confident choice is applied; anything uncertain stays without a project.
_index_cache: tuple[float, dict] | None = None
log = logging.getLogger("toggl")


def history_index(days: int = 90, max_age: float = 1800) -> dict:
    """{exact: {description.lower(): {project_id, project, tags}}, projects: [{id, name, count, examples}], tags: [name]}"""
    global _index_cache
    if _index_cache and time.time() - _index_cache[0] < max_age:
        return _index_cache[1]
    names = {}
    try:
        names = {p["id"]: p["name"] for p in (_req("GET", "/me/projects") or [])}
    except TogglError as e:
        log.warning("toggl projects unavailable: %s", e)
    exact: dict[str, dict] = {}
    per: dict[int | None, dict] = {}
    tag_count: dict[str, int] = {}
    try:
        ents = entries(days)
    except TogglError as e:
        log.warning("toggl history unavailable: %s", e)
        ents = []
    for e in sorted(ents, key=lambda x: str(x.get("start") or ""), reverse=True):
        desc = str(e.get("description") or "").strip()
        pid = e.get("project_id")
        tags = [str(t) for t in (e.get("tags") or [])]
        if desc and desc.lower() not in exact:
            exact[desc.lower()] = {"project_id": pid, "project": names.get(pid), "tags": tags}
        slot = per.setdefault(pid, {"count": 0, "examples": []})
        slot["count"] += 1
        if desc and desc not in slot["examples"] and len(slot["examples"]) < 12:
            slot["examples"].append(desc)
        for t in tags:
            tag_count[t] = tag_count.get(t, 0) + 1
    projects = [{"id": pid, "name": names.get(pid, f"#{pid}"), **v} for pid, v in per.items() if pid]
    for pid, name in names.items():            # projects without recent entries are still choosable
        if pid not in per:
            projects.append({"id": pid, "name": name, "count": 0, "examples": []})
    projects.sort(key=lambda p: -p["count"])
    idx = {"exact": exact, "projects": projects, "tags": sorted(tag_count, key=lambda t: -tag_count[t])}
    _index_cache = (time.time(), idx)
    return idx


def update_entry(entry_id: int, project_id: int | None = None, tags: list[str] | None = None) -> dict | None:
    body: dict = {}
    if project_id is not None:
        body["project_id"] = project_id
    if tags is not None:
        body["tags"] = tags
    if not body:
        return None
    return _req("PUT", f"/workspaces/{workspace_id()}/time_entries/{entry_id}", body)


def classify(description: str, context: str = "") -> dict:
    """{project_id, project, tags, source, reason}. Source: "history" (same description before), "claude", or
    "none" (uncertain: no project)."""
    idx = history_index()
    hit = idx["exact"].get(description.strip().lower())
    if hit and (hit["project_id"] or hit["tags"]):
        return {**hit, "source": "history", "reason": "tracked with this description before"}
    from . import llm
    if not llm.enabled() or not idx["projects"]:
        return {"project_id": None, "project": None, "tags": [], "source": "none", "reason": "no history for this description"}
    try:
        res = llm.pick_toggl(description, context, idx["projects"], idx["tags"][:40])
    except Exception as e:                       # never let classification break a timer
        log.warning("toggl classification failed: %s", e)
        return {"project_id": None, "project": None, "tags": [], "source": "none", "reason": str(e)}
    by_name = {p["name"].lower(): p for p in idx["projects"]}
    chosen = by_name.get(str(res.get("project") or "").strip().lower())
    tags = [t for t in (res.get("tags") or []) if t in idx["tags"]]
    if res.get("confidence") != "high" or not chosen:
        return {"project_id": None, "project": None, "tags": tags if res.get("confidence") == "high" else [], "source": "none", "reason": str(res.get("reason") or "uncertain")}
    return {"project_id": chosen["id"], "project": chosen["name"], "tags": tags, "source": "claude", "reason": str(res.get("reason") or "")}
