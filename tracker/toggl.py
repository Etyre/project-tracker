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


# ---- projects and clients ---------------------------------------------------------------------------------
# Toggl's free plan allows only a few dozen API calls an hour, so projects and clients are fetched together, once,
# and kept for the hour; anything that creates one refreshes the cache itself.
_projects_cache: tuple[float, list[dict], dict[int, str]] | None = None


def _load_projects_and_clients(max_age: float = 3600) -> tuple[list[dict], dict[int, str]]:
    global _projects_cache
    if _projects_cache and time.time() - _projects_cache[0] < max_age:
        return _projects_cache[1], _projects_cache[2]
    try:
        raw = _req("GET", "/me/projects") or []
        cl = {int(c["id"]): str(c["name"]) for c in (_req("GET", "/me/clients") or [])}
    except TogglError as e:
        log.warning("toggl projects unavailable: %s", e)
        return (_projects_cache[1], _projects_cache[2]) if _projects_cache else ([], {})
    out = [{"id": int(p["id"]), "name": str(p.get("name") or ""), "client_id": p.get("client_id"),
            "client": cl.get(p.get("client_id")), "active": bool(p.get("active", True))} for p in raw]
    out.sort(key=lambda p: (not p["active"], p["name"].lower()))
    _projects_cache = (time.time(), out, cl)
    return out, cl


def projects(max_age: float = 3600) -> list[dict]:
    """The user's Toggl projects: [{id, name, client_id, client, active}], active ones first."""
    return _load_projects_and_clients(max_age)[0]


def clients() -> list[str]:
    return sorted(_load_projects_and_clients()[1].values(), key=str.lower)


def forget_projects() -> None:
    global _projects_cache, _index_cache
    _projects_cache = None
    _index_cache = None


def find_project(name: str | None) -> dict | None:
    """A project by name, case-insensitively; None when the name is empty or unknown."""
    n = (name or "").strip().lower()
    if not n:
        return None
    return next((p for p in projects() if p["name"].lower() == n), None)


def create_project(name: str, client: str | None = None) -> dict:
    """A new Toggl project (and its client, when that is new too); returns the project as `projects()` lists it."""
    name = name.strip()
    existing = find_project(name)
    if existing:
        return existing
    wid = workspace_id()
    body: dict = {"name": name, "active": True}
    client = (client or "").strip()
    if client:
        cid = next((i for i, n in _load_projects_and_clients()[1].items() if n.lower() == client.lower()), None)
        if cid is None:
            cid = int(_req("POST", f"/workspaces/{wid}/clients", {"name": client, "wid": wid})["id"])
        body["client_id"] = cid
    created = _req("POST", f"/workspaces/{wid}/projects", body)
    forget_projects()
    log.info("toggl project created: %s (client %s)", name, client or "none")
    return {"id": int(created["id"]), "name": name, "client_id": body.get("client_id"), "client": client or None, "active": True}


def start(description: str, extra_tags: list[str] | None = None, project_id: int | None = None) -> dict:
    cfg = config.load()["toggl"]
    wid = workspace_id()
    tags = [t for t in [cfg["tag"], *(extra_tags or [])] if t]
    body = {
        "created_with": "task-tracker",
        "description": description,
        "tags": tags,
        "start": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration": -1,
        "workspace_id": wid,
        "wid": wid,
    }
    if project_id is None and cfg.get("project"):
        p = find_project(cfg["project"])
        project_id = p["id"] if p else None
    if project_id is not None:
        body["project_id"] = project_id
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


def update_entry(entry_id: int, project_id: int | None = None, tags: list[str] | None = None) -> dict | None:
    body: dict = {}
    if project_id is not None:
        body["project_id"] = project_id
    if tags is not None:
        body["tags"] = tags
    if not body:
        return None
    return _req("PUT", f"/workspaces/{workspace_id()}/time_entries/{entry_id}", body)


# ---- which project a timer belongs to -----------------------------------------------------------------------
# Three layers, cheapest first, and only the last one calls Claude:
#   1. routines (checklist rows, exercise sessions, journaling): the "label: Toggl project" list in Settings;
#   2. tasks: the Toggl project linked to the task's project (or the nearest ancestor project), kept in the app's
#      database and shown in the opened project;
#   3. anything still unresolved: Claude decides once, from the user's own projects, clients and the descriptions
#      filed under each, creating a Toggl project when nothing fits; the answer is written back to layer 1 or 2.
_index_cache: tuple[float, dict] | None = None


def routine_key(label: str) -> str:
    """The name a routine is listed under: exercise timers "going to do squats" are listed as "squats"."""
    k = " ".join((label or "").split()).strip().lower()
    for prefix in ("going to do ", "going to "):
        if k.startswith(prefix):
            return k[len(prefix):].strip()
    return k


def routine_line(label: str, project: str, tags: list[str] | None = None) -> str:
    """One line of the Settings list: "Anki review: Personal | learning, review"."""
    tags = [t.strip() for t in (tags or []) if t and t.strip()]
    return f"{label}: {project}" + (f" | {', '.join(dict.fromkeys(tags))}" if tags else "")


def parse_routine_line(line: str) -> tuple[str, str, list[str]] | None:
    """("anki review", "Personal", ["learning", "review"]) from a list line; None for a line with no project."""
    line = str(line)
    if ":" not in line:
        return None
    label, rest = line.split(":", 1)
    project, _, tags = rest.partition("|")
    if not label.strip() or not project.strip():
        return None
    return routine_key(label), project.strip(), [t.strip() for t in tags.split(",") if t.strip()]


def routine_map() -> dict[str, dict]:
    """{routine label (lower): {project, tags}} from `[toggl] routine_projects`."""
    out: dict[str, dict] = {}
    for line in config.load()["toggl"].get("routine_projects") or []:
        parsed = parse_routine_line(line)
        if parsed:
            out[parsed[0]] = {"project": parsed[1], "tags": parsed[2]}
    return out


def routine_project(label: str) -> dict | None:
    """The Toggl project (and tags) the Settings list names for a routine; None when unlisted or unknown."""
    entry = routine_map().get(routine_key(label))
    if not entry:
        return None
    p = find_project(entry["project"])
    if not p:
        log.warning("routine '%s' is listed under Toggl project '%s', which does not exist", label, entry["project"])
        return None
    return {"project_id": p["id"], "project": p["name"], "tags": entry["tags"], "source": "routine list"}


def remember_routine(label: str, project_name: str, tags: list[str] | None = None) -> None:
    """Add "label: project | tags" to the Settings list (replacing an earlier line for the same label)."""
    key = routine_key(label)
    lines = [l for l in (config.load()["toggl"].get("routine_projects") or [])
             if (parse_routine_line(l) or ("",))[0] != key]
    lines.append(routine_line(key, project_name, tags))
    config.save({"toggl": {"routine_projects": lines}})


def history_index(days: int = 90, max_age: float = 3600) -> dict:
    """{exact: {description.lower(): {project_id, project, tags}}, projects: [{id, name, client, count, examples}], tags: [name]}"""
    global _index_cache
    if _index_cache and time.time() - _index_cache[0] < max_age:
        return _index_cache[1]
    plist = projects()
    names = {p["id"]: p["name"] for p in plist}
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
    out = []
    for p in plist:                                  # every project is choosable, with its recent descriptions
        v = per.get(p["id"], {"count": 0, "examples": []})
        out.append({"id": p["id"], "name": p["name"], "client": p["client"], "active": p["active"], **v})
    out.sort(key=lambda p: (-p["count"], p["name"].lower()))
    idx = {"exact": exact, "projects": out, "tags": sorted(tag_count, key=lambda t: -tag_count[t])}
    _index_cache = (time.time(), idx)
    return idx


def history_match(description: str) -> dict | None:
    """The project and tags this exact description had last time it was tracked, if ever."""
    hit = history_index()["exact"].get(description.strip().lower())
    if hit and (hit["project_id"] or hit["tags"]):
        return {**hit, "source": "history"}
    return None


def decide_project(subject: str, context: str) -> dict:
    """Ask Claude which Toggl project `subject` belongs to (creating one when it says none fits), and for a routine
    which of the user's tags fit. Returns {project_id, project, client, tags, created, reason} or raises."""
    from . import llm
    idx = history_index()
    res = llm.pick_toggl_project(subject, context, idx["projects"], clients(), idx["tags"][:60])
    name = str(res.get("project") or "").strip()
    if not name:
        raise TogglError("Claude named no project")
    p = find_project(name)
    created = False
    if not p:
        if not res.get("create"):
            raise TogglError(f"Claude chose '{name}', which is not one of your Toggl projects")
        p = create_project(name, res.get("client"))
        created = True
    return {"project_id": p["id"], "project": p["name"], "client": p.get("client"), "created": created,
            "tags": [str(t).strip() for t in (res.get("tags") or []) if str(t).strip()], "reason": str(res.get("reason") or "")}


def suggest_routines(labels: list[str]) -> dict:
    """Claude's mapping for every routine at once (the Settings button). New projects it proposes are created.
    Returns {lines: ["label: project", ...], created: [project names], note}."""
    from . import llm
    idx = history_index()
    res = llm.map_routines(labels, idx["projects"], clients(), idx["tags"][:60])
    lines, created = [], []
    chosen = {str(m.get("label") or "").strip().lower(): m for m in res.get("mappings") or []}
    for label in labels:
        m = chosen.get(label.lower())
        name = str((m or {}).get("project") or "").strip()
        if not name:
            continue
        p = find_project(name)
        if not p and m.get("create"):
            p = create_project(name, m.get("client"))
            created.append(p["name"] + (f" (client {p['client']})" if p.get("client") else ""))
        if p:
            lines.append(routine_line(label, p["name"], [str(t) for t in (m.get("tags") or [])]))
    return {"lines": lines, "created": created, "note": str(res.get("note") or "")}
