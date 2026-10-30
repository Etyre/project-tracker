"""Linear mirror: issues assigned to you appear as tasks; edits and completions here go back to Linear."""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
import urllib.error
import urllib.request

from . import config, db

log = logging.getLogger("linear")
API = "https://api.linear.app/graphql"

# Linear priority: 0 none, 1 urgent, 2 high, 3 medium, 4 low
PRIORITY_TO_IMPORTANCE = {0: None, 1: 5, 2: 4, 3: 3, 4: 2}     # Linear 0 = no priority
IMPORTANCE_TO_PRIORITY = {5: 1, 4: 2, 3: 3, 2: 4, 1: 4, None: 0}


class LinearError(RuntimeError):
    pass


def enabled() -> bool:
    return bool(config.load()["linear"]["enable"] and config.secret("linear"))


def gql(query: str, variables: dict | None = None) -> dict:
    key = config.secret("linear")
    if not key:
        raise LinearError("LINEAR_API_KEY not set")
    req = urllib.request.Request(API, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Authorization": key, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        raise LinearError(f"Linear HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
    except urllib.error.URLError as e:
        raise LinearError(f"Linear unreachable: {e}") from e
    if out.get("errors"):
        raise LinearError("; ".join(er.get("message", "?") for er in out["errors"]))
    return out["data"]


ISSUE_FIELDS = """
  id identifier title description url priority dueDate estimate updatedAt createdAt completedAt canceledAt
  state { id name type } team { id name } project { id name }
"""


def fetch_my_issues(include_recent_closed_days: int = 14) -> list[dict]:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=include_recent_closed_days)).isoformat()
    out: list[dict] = []
    cursor = None
    while True:
        data = gql(
            """query($after: String, $since: DateTimeOrDuration) {
                 issues(first: 100, after: $after, filter: {
                   assignee: { isMe: { eq: true } },
                   or: [ { state: { type: { nin: ["completed", "canceled"] } } },
                         { completedAt: { gte: $since } } ]
                 }) { nodes { %s } pageInfo { hasNextPage endCursor } }
               }""" % ISSUE_FIELDS,
            {"after": cursor, "since": since},
        )
        page = data["issues"]
        out.extend(page["nodes"])
        if not page["pageInfo"]["hasNextPage"]:
            return out
        cursor = page["pageInfo"]["endCursor"]


_states_cache: dict[str, tuple[float, list[dict]]] = {}


def team_states(team_id: str) -> list[dict]:
    hit = _states_cache.get(team_id)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    data = gql("query($id: String!) { team(id: $id) { states { nodes { id name type position } } } }", {"id": team_id})
    states = sorted(data["team"]["states"]["nodes"], key=lambda s: s["position"])
    _states_cache[team_id] = (time.time(), states)
    return states


def _state_of_type(team_id: str, typ: str) -> str | None:
    for s in team_states(team_id):
        if s["type"] == typ:
            return s["id"]
    return None


def _project_for_issue(issue: dict) -> int:
    cfg = config.load()["linear"]
    group = issue.get("project") if cfg["group_by"] == "project" else issue.get("team")
    name = (group or issue.get("team") or {}).get("name") or "Ungrouped"
    key = (group or issue.get("team") or {}).get("id")
    root = db.ensure_project_path([cfg["category_name"]])
    p = db.row("SELECT id FROM projects WHERE parent_id=? AND linear_key=? AND archived_at IS NULL", (root, key))
    if p:
        return p["id"]
    p = db.row("SELECT id FROM projects WHERE parent_id=? AND lower(name)=lower(?) AND archived_at IS NULL", (root, name))
    if p:
        db.update_project(p["id"], linear_key=key)
        return p["id"]
    return db.create_project(name, root, linear_key=key)


def _ts(s: str | None) -> float | None:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def pull() -> dict:
    """Linear -> local. Remote wins when the issue changed since we last saw it."""
    issues = fetch_my_issues()
    created = updated = closed = 0
    for iss in issues:
        local = db.task_by_linear_id(iss["id"])
        closed_remote = iss["state"]["type"] in ("completed", "canceled")
        fields = dict(
            title=iss["title"],
            description=iss.get("description") or "",
            deadline=iss.get("dueDate"),
            importance=PRIORITY_TO_IMPORTANCE.get(iss.get("priority") or 0),
            linear_identifier=iss["identifier"],
            linear_url=iss["url"],
            linear_state=iss["state"]["name"],
            linear_team_id=(iss.get("team") or {}).get("id"),
            linear_updated_at=iss["updatedAt"],
        )
        if iss.get("estimate") and not (local and local.get("estimate_minutes")):
            fields["estimate_minutes"] = int(iss["estimate"]) * 60   # points ~ hours, a crude but useful prior
        if local is None:
            if closed_remote:
                continue
            pid = _project_for_issue(iss)
            db.create_task(pid, iss["title"], source="linear", linear_id=iss["id"], **{k: v for k, v in fields.items() if k != "title"})
            created += 1
            continue
        if (local.get("linear_updated_at") or "") >= iss["updatedAt"]:
            continue                                   # nothing new remotely
        db.update_task(local["id"], **fields)
        updated += 1
        if closed_remote and not local["completed_at"]:
            db.complete_task(local["id"], _ts(iss.get("completedAt") or iss.get("canceledAt")) or time.time())
            closed += 1
        elif not closed_remote and local["completed_at"]:
            db.uncomplete_task(local["id"])
    db.set_setting("linear_last_sync", time.time())
    return {"fetched": len(issues), "created": created, "updated": updated, "closed": closed}


def push_task(tid: int) -> None:
    """Local -> Linear for one task (called right after a local edit or completion)."""
    t = db.task(tid)
    if not t or not t.get("linear_id") or not enabled():
        return
    inp: dict = {
        "title": t["title"],
        "description": t["description"] or None,
        "dueDate": (t["deadline"] or None) and t["deadline"][:10],
        "priority": IMPORTANCE_TO_PRIORITY.get(t["importance"], 0),
    }
    if t.get("linear_team_id"):
        want = "completed" if t["completed_at"] else None
        if want:
            sid = _state_of_type(t["linear_team_id"], want)
            if sid:
                inp["stateId"] = sid
        elif t.get("linear_state") and t["linear_state"].lower() in ("done", "completed", "closed", "canceled"):
            sid = _state_of_type(t["linear_team_id"], "unstarted")
            if sid:
                inp["stateId"] = sid
    data = gql("mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) "
               "{ success issue { updatedAt state { name } } } }", {"id": t["linear_id"], "input": inp})
    iss = data["issueUpdate"]["issue"]
    db.update_task(tid, linear_updated_at=iss["updatedAt"], linear_state=iss["state"]["name"])


def safe_push(tid: int) -> str | None:
    try:
        push_task(tid)
        return None
    except LinearError as e:
        log.warning("linear push failed for task %s: %s", tid, e)
        return str(e)
