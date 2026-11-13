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


# Toggl meters API calls per sliding hour in two buckets: user-scoped paths (/me/...) get 30 an hour on every plan
# below Enterprise, workspace-scoped paths (/workspaces/...) get the plan's own allowance (30 on Free). So reads go
# through workspace paths where Toggl offers them, the rest is cached for the hour, and a 402 ("quota hit") stops
# further calls to that bucket until Toggl says it resets, instead of each caller burning a timeout on it.
quota: dict[str, dict] = {}             # bucket -> {remaining, resets_at}


def _bucket(path: str) -> str:
    return "user" if path.startswith("/me") else "workspace"


def quota_left(path: str = "/me") -> int | None:
    """Calls left in this hour for the bucket `path` falls in, as last reported; None when nothing is known yet."""
    q = quota.get(_bucket(path))
    if not q:
        return None
    return 0 if q.get("resets_at", 0) > time.time() and q.get("remaining") == 0 else q.get("remaining")


def _note_quota(path: str, headers, code: int | None = None) -> None:
    rem, reset = headers.get("X-Toggl-Quota-Remaining"), headers.get("X-Toggl-Quota-Resets-In")
    if rem is None and code != 402:
        return
    q = quota.setdefault(_bucket(path), {})
    q["remaining"] = int(rem) if rem is not None and rem.isdigit() else (0 if code == 402 else q.get("remaining"))
    q["resets_at"] = time.time() + (int(reset) if reset and reset.isdigit() else 3600)


def _req(method: str, path: str, body: dict | None = None):
    tok = config.secret("toggl")
    if not tok:
        raise TogglError("TOGGL_API_TOKEN not set")
    q = quota.get(_bucket(path))
    if q and q.get("remaining") == 0 and q.get("resets_at", 0) > time.time():
        raise TogglError(f"Toggl hourly API quota used up; resets in {int(q['resets_at'] - time.time())}s")
    auth = base64.b64encode(f"{tok}:api_token".encode()).decode()
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method,
                                 headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            _note_quota(path, r.headers)
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        _note_quota(path, e.headers, e.code)
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
        wid = workspace_id()
        raw: list[dict] = []
        for page in range(1, 20):                        # workspace-scoped reads: not the 30-an-hour user bucket
            batch = _req("GET", f"/workspaces/{wid}/projects?per_page=200&page={page}") or []
            raw += batch
            if len(batch) < 200:
                break
        cl = {int(c["id"]): str(c["name"]) for c in (_req("GET", f"/workspaces/{wid}/clients") or [])}
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
    _entries_cache.clear()


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


# Toggl entries are gerunds: "Responding to Sam", not "Respond to Sam". Claude phrases every task's title that way
# when the task is created (tasks.toggl_title); this is the fallback for a task it has not reached yet, or when Claude
# is off: the first word of an imperative title into its -ing form by the usual spelling rules, the rest unchanged.
# It only applies when the title starts with a verb in the imperative; a title that already starts with an -ing
# word, or a noun phrase ("Taxes"), is left as it is.
_ING = {"be": "being", "see": "seeing", "flee": "fleeing", "free": "freeing", "agree": "agreeing", "dye": "dyeing",
        "lie": "lying", "tie": "tying", "die": "dying", "set up": "setting up", "setup": "setting up", "fix": "fixing",
        "mix": "mixing", "box": "boxing", "draw": "drawing", "review": "reviewing", "follow": "following", "buy": "buying",
        "pay": "paying", "play": "playing", "stay": "staying", "enjoy": "enjoying", "open": "opening", "order": "ordering",
        "answer": "answering", "offer": "offering", "enter": "entering", "visit": "visiting", "edit": "editing",
        "listen": "listening", "happen": "happening", "cancel": "cancelling", "label": "labelling", "travel": "travelling",
        "ping": "pinging", "sing": "singing", "bring": "bringing", "ring": "ringing"}
_VOWELS = "aeiou"


def _ing(word: str) -> str:
    w = word.lower()
    if w in _ING:
        return _ING[w]
    if w.endswith("ie"):
        return w[:-2] + "ying"
    if w.endswith("e") and not w.endswith("ee") and not w.endswith("ye") and not w.endswith("oe"):
        return w[:-1] + "ing"
    # one syllable, consonant-vowel-consonant: double the last letter (run -> running), except w, x, y
    if (len(w) >= 3 and w[-1] not in _VOWELS + "wxy" and w[-2] in _VOWELS and w[-3] not in _VOWELS
            and sum(c in _VOWELS for c in w) == 1):
        return w + w[-1] + "ing"
    return w + "ing"


def gerund(title: str) -> str:
    """Rule-based gerund title for a Toggl entry: "Respond to Sam" -> "Responding to Sam"."""
    t = " ".join((title or "").split())
    if not t:
        return t
    first, _, rest = t.partition(" ")
    if not first.isalpha():
        return t
    if first.lower().endswith("ing") and len(first) > 4:      # already a gerund ("Writing the memo")
        return t
    g = _ing(first)
    g = g[0].upper() + g[1:] if first[0].isupper() else g
    return g + (" " + rest if rest else "")


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


_entries_cache: dict[int, tuple[float, list[dict]]] = {}


def entries(days: int = 90, max_age: float = 3600) -> list[dict]:
    """The user's recent time entries (any source), newest first. Toggl only offers this as a user-scoped read, so
    one fetch an hour is shared by everything that needs the history."""
    days = min(days, 90)
    hit = _entries_cache.get(days)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    end = dt.date.today() + dt.timedelta(days=1)
    start = end - dt.timedelta(days=days + 1)
    out = _req("GET", f"/me/time_entries?start_date={start.isoformat()}&end_date={end.isoformat()}") or []
    _entries_cache[days] = (time.time(), out)
    try:                                   # every fetch of the history trues up the app's own entries against Toggl's
        from . import db
        n = db.reconcile_with_toggl(out)
        if n:
            log.info("toggl history: %d of the app's entries corrected to Toggl's times", n)
    except Exception:
        log.exception("reconciling entries with toggl")
    return out


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
