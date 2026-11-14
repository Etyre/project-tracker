"""The executive-assistant agent's door into the tracker: everything under /api/agent/.

Whatever arrives here is attributed to the agent. Tasks and projects it creates carry source "agent"; context it adds
to any item is a separate note that keeps its author (never merged into the user's own text); a schedule it sets is
marked as the agent's on the plan page and the automatic re-planner leaves it alone.

Access: an optional bearer token (Settings -> Agent, or AGENT_API_TOKEN). The server only listens on 127.0.0.1, so
without a token any local process is accepted. The agent names itself with an X-Agent-Name header (default
"assistant"); that name is what the user sees next to its notes. Interactive documentation of every call is at /docs.
"""
from __future__ import annotations

import datetime as dt
import secrets as _secrets
import threading
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from . import config, db, estimates, exercise, llm, scheduler, sheets, tags

DEFAULT_NAME = "assistant"


# ============================================================ identity
def token_required() -> bool:
    return bool(config.secret("agent"))


def identity(authorization: str | None = Header(None), x_agent_name: str | None = Header(None)) -> str:
    """Check the bearer token when one is configured; return the agent's display name."""
    want = config.secret("agent")
    if want:
        got = (authorization or "").removeprefix("Bearer ").strip()
        if not got or not _secrets.compare_digest(got, want):
            raise HTTPException(401, "agent token missing or wrong (Authorization: Bearer <token>)")
    name = (x_agent_name or "").strip()[:40]
    return name or DEFAULT_NAME


router = APIRouter(prefix="/api/agent", tags=["agent"])


# ============================================================ models
class ProjectIn(BaseModel):
    name: str
    parent_id: int | None = None
    parent_path: list[str] | None = None   # ["Work", "Paper"]: the existing project to create it under
    notes: str = ""


class TaskIn(BaseModel):
    title: str
    project_id: int | None = None
    project_path: list[str] | None = None  # an existing project by name path; created when `create_project` is set
    create_project: bool = False
    parent_id: int | None = None           # sub-task of this task (its project is used)
    description: str = ""
    deadline: str | None = None            # the hard deadline: YYYY-MM-DD, or "fri", "tomorrow", "nov3"
    soft_deadline: str | None = None       # the soft one, ideally by; a task may have both
    blocking: bool = False
    blocking_for: str = ""
    importance: int | None = None          # 1 (low) .. 5 (critical)
    chunkable: bool = True
    work_kind: str | None = None           # "task" (quick, fits a gap) | "focus" (focused work); Claude judges when left out
    reading: bool | None = None            # mainly reading something (offered by the daily reading card); Claude judges when left out
    writing: bool | None = None            # mainly writing; Claude judges when left out
    estimate_minutes: int | None = None    # normally left to the app's forecast
    note: str | None = None                # context attached as the agent's note straight away
    # With neither project nor parent given, Claude files the task like the quick-add bar (Inbox without Claude).


class QuickAddIn(BaseModel):
    text: str


class NoteIn(BaseModel):
    body: str
    task_id: int | None = None
    project_id: int | None = None


class ScheduleItemIn(BaseModel):
    kind: str = "task"                     # task | routine | exercise | journal | break | meeting
    title: str = ""                        # defaults to the task's title
    start: str                             # HH:MM local
    end: str
    task_id: int | None = None
    note: str = ""


class ScheduleIn(BaseModel):
    items: list[ScheduleItemIn]
    date: str | None = None
    work_start: str | None = None
    work_end: str | None = None
    rationale: str = ""
    merge_meetings: bool = True            # calendar meetings the schedule left out are added
    validate_windows: bool = True          # blocks are clipped to the free windows between meetings


class InstructionsIn(BaseModel):
    text: str
    date: str | None = None
    standing: bool = False                 # guidance for every day (kept until replaced), not just `date`
    rebuild: bool = True                   # re-plan the day with the instructions now (synchronous)


class TaskPriorityIn(BaseModel):
    """What the agent knows about a task's priority and urgency; every field is optional. Each change is written as
    a signed note on the task (with `why`), so the planner and the user both see where it came from."""
    importance: int | None = None          # 1 (low) .. 5 (critical)
    deadline: str | None = None            # the hard deadline: YYYY-MM-DD or "fri", "tomorrow"; "" clears it
    soft_deadline: str | None = None       # the soft deadline (ideally by); "" clears it
    blocking: bool | None = None
    blocking_for: str | None = None
    chunkable: bool | None = None
    work_kind: str | None = None           # "task" | "focus"; "" hands it back to Claude
    reading: bool | None = None
    writing: bool | None = None
    why: str = ""


class RebuildIn(BaseModel):
    date: str | None = None
    from_now: bool = False
    until: str | None = None


class MoveTaskIn(BaseModel):
    """Where a task goes: an existing project (id or path), or under another task (its project is used)."""
    project_id: int | None = None
    project_path: list[str] | None = None
    create_project: bool = False           # create a missing project_path
    parent_id: int | None = None           # make it a sub-task of this task
    after_id: int | None = None            # place it right after this sibling; otherwise last
    why: str = ""


class MoveProjectIn(BaseModel):
    """Where a project goes: under another project (id or path), or the top level."""
    parent_id: int | None = None
    parent_path: list[str] | None = None
    top_level: bool = False
    why: str = ""


# ============================================================ helpers
def _day(date: str | None) -> dt.date:
    try:
        return dt.date.fromisoformat(date) if date else dt.date.today()
    except ValueError:
        raise HTTPException(400, "date must be YYYY-MM-DD")


def _task_out(t: dict) -> dict:
    return {**t, "path": db.project_path(t["project_id"]), "tracked_minutes": round(db.task_seconds(t["id"]) / 60),
            "notes_added": db.notes_for("task", t["id"])}


def _project_index(nodes: list[dict], path: list[str], out: list[dict]) -> None:
    for p in nodes:
        here = [*path, p["name"]]
        out.append({"id": p["id"], "parent_id": p["parent_id"], "name": p["name"], "subtitle": p.get("subtitle") or "", "path": here, "notes": p["notes"],
                    "source": p.get("source", "local"), "open_tasks": len(p["tasks"]), "notes_added": p.get("notes_added", [])})
        _project_index(p["children"], here, out)


def _resolve_project(project_id: int | None, path: list[str] | None, create: bool = False) -> int | None:
    if project_id is not None:
        if not db.row("SELECT id FROM projects WHERE id=? AND archived_at IS NULL", (project_id,)):
            raise HTTPException(404, f"project {project_id} not found (or archived)")
        return project_id
    if path:
        clean = [s.strip() for s in path if s.strip()]
        pid = db.find_project_by_path(clean)
        if pid is None:
            if not create:
                raise HTTPException(404, f"no project at path {clean}; pass create_project=true or an existing path")
            pid, where = db.resolve_project_path(clean, allow_new_top=True)   # never a duplicate of an existing project
            if where.startswith("new"):
                db.update_project(pid, source="agent")
        return pid
    return None


def _forecast_later(tid: int) -> None:
    from . import web
    web._forecast_estimate_later(tid)


def _schedule_context(day: dt.date) -> dict:
    """Everything the agent needs to set a day's schedule: the current plan, the free windows, the prioritised
    candidate tasks with calibrated estimates, routines, the day's exercise and any standing instructions."""
    from . import calendar_feed
    date = day.isoformat()
    cfg = config.load()["general"]
    p = db.plan(date)
    ws = scheduler.parse_hm(p["work_start"] if p else cfg["work_start"])
    we = scheduler.parse_hm(p["work_end"] if p else cfg["work_end"])
    events = calendar_feed.events_for(day)
    windows = scheduler.free_windows(day, events, ws, we)
    now = dt.datetime.now()
    settled = {i["task_id"] for i in (p["items"] if p else []) if i["status"] in ("done", "skipped", "active") and i["task_id"]}
    cands = [{k: c[k] for k in ("id", "title", "path", "deadline", "soft_deadline", "blocking", "blocking_for", "importance", "chunkable", "work_kind",
                                "estimate_minutes", "expected_minutes", "score", "agent_notes")}
             | {"description": (c["description"] or "")[:300], "settled_today": c["id"] in settled}
             for c in scheduler.candidates(day)]
    decision = exercise.decide(date)
    return {
        "date": date, "weekday": day.strftime("%A"), "now": now.strftime("%H:%M") if day == now.date() else None,
        "work_window": [scheduler.hm(ws), scheduler.hm(we)],
        "free_windows": [[scheduler.hm(a), scheduler.hm(b)] for a, b in windows],
        "total_free_minutes": sum(b - a for a, b in windows),
        "min_block_minutes": cfg["min_block_minutes"],
        "meetings": [{"title": e["title"], "start": e["start"].strftime("%H:%M"), "end": e["end"].strftime("%H:%M")}
                     for e in events if not e["all_day"]],
        "all_day_events": [e["title"] for e in events if e["all_day"]],
        "candidates": cands,
        "routines_today": sheets.routines_for_today() if day == now.date() else [],
        "exercise_today": {k: decision.get(k) for k in ("kind", "title", "minutes", "reason")},
        "estimate_calibration": estimates.calibration(),
        "instructions": scheduler.day_instructions(date) or None,
        "standing_guidance": scheduler.guidance() or None,
        "plan": p and {"built_at": p["built_at"], "source": p["raw"].get("source"), "agent_plan": p["raw"].get("agent_plan"),
                       "rationale": p["rationale"], "work_start": p["work_start"], "work_end": p["work_end"],
                       "dropped": p["raw"].get("dropped") or [], "exercise_note": p["raw"].get("exercise_note"), "items": p["items"]},
        "rules_document": config.rules_text(),
        "planning_log": db.plan_log(date),
    }


def _since(value: str | None) -> float | None:
    """`since` as epoch seconds, from an ISO date-time (local) or a number."""
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        try:
            return dt.datetime.fromisoformat(value).timestamp()
        except ValueError:
            raise HTTPException(400, "since must be an ISO date-time or epoch seconds")


def _log_since_last_visit(agent: str) -> list[dict]:
    """Everything the app planned since this agent last looked (by name), then the cursor moves to now."""
    key = f"agent_log_seen:{agent}"
    last = db.get_setting(key)
    out = db.plan_log(since=float(last), limit=500) if last else []
    db.set_setting(key, dt.datetime.now().timestamp())
    return out


# ============================================================ routes
@router.get("")
@router.get("/")
def ping(agent: str = Depends(identity)):
    """Who the server thinks you are, plus the map of what you can do here."""
    cfg = config.load()["general"]
    return {
        "ok": True, "app": "Project Tracker", "agent": agent, "token_required": token_required(),
        "date": db.today(), "time": dt.datetime.now().strftime("%H:%M"), "work_window": [cfg["work_start"], cfg["work_end"]],
        "claude": llm.enabled(), "docs": "/docs#/agent",
        "endpoints": {
            "GET /api/agent/tree": "projects and open tasks with ids, paths and the notes you added",
            "GET /api/agent/tasks?q=&project_id=": "find open tasks",
            "GET /api/agent/tasks/{id}": "one task with its path, tracked time and notes",
            "PATCH /api/agent/tasks/{id}": "set a task's priority and urgency (importance, deadline = hard, soft_deadline = ideally by, blocking, work_kind...) with `why`; recorded as your note on the task and used by every plan",
            "POST /api/agent/tasks/{id}/move": "move a task (with its sub-tasks) to another project (project_id | project_path) or under another task (parent_id); recorded as your note",
            "POST /api/agent/projects/{id}/move": "move a project (with everything in it) under another project (parent_id | parent_path) or to the top level; recorded as your note",
            "POST /api/agent/projects": "add a project (name, parent_id | parent_path, notes)",
            "POST /api/agent/tasks": "add a task (title, project_id | project_path | parent_id, fields, note); no project -> Claude files it",
            "POST /api/agent/quick_add": "free text; Claude turns it into tasks in the right projects",
            "POST /api/agent/notes": "add context to a task or project, shown as yours",
            "GET /api/agent/notes?task_id=|project_id=": "your notes on an item",
            "DELETE /api/agent/notes/{id}": "take a note back",
            "GET /api/agent/schedule?date=": "the day's plan, free windows, prioritised candidates, routines, exercise, the day's planning log and everything the app planned since you last looked",
            "GET /api/agent/schedule/log?date=&since=&limit=": "the planning log: every build, re-plan (with its reason), exercise decision, schedule you set, instructions, guidance, priorities",
            "PUT /api/agent/schedule": "set the day's schedule block by block: it replaces the app's plan, and the app then re-plans it on drift as usual, keeping your choices where they still fit; the day's exercise, once decided, does not change",
            "PUT /api/agent/schedule/instructions": "text the planner keeps in mind: for one day (date), or standing=true for every day until replaced; rebuild re-plans the day with it now",
            "DELETE /api/agent/schedule/instructions?date=&standing=": "drop the day's instructions, or the standing guidance",
            "POST /api/agent/schedule/rebuild": "let the app plan the day now (from_now, until)",
        },
    }


# ------------------------------------------------ reading
@router.get("/tree")
def tree(agent: str = Depends(identity)):
    t = db.project_tree(done=False)
    index: list[dict] = []
    _project_index(t, [], index)
    return {"projects": index, "tree": t}


@router.get("/tasks")
def find_tasks(q: str = "", project_id: int | None = None, limit: int = 50, agent: str = Depends(identity)):
    out = []
    ql = q.strip().lower()
    for t in db.open_tasks(project_id):
        if ql and ql not in t["title"].lower() and ql not in (t["description"] or "").lower():
            continue
        out.append({**t, "path": db.project_path(t["project_id"])})
        if len(out) >= max(1, min(limit, 500)):
            break
    return out


@router.get("/tasks/{tid}")
def get_task(tid: int, agent: str = Depends(identity)):
    t = db.task(tid)
    if not t:
        raise HTTPException(404, "task not found")
    return _task_out(t)


@router.patch("/tasks/{tid}")
def set_priority(tid: int, p: TaskPriorityIn, agent: str = Depends(identity)):
    """Priority and urgency from the agent. The fields are the ones every plan is built from (importance, deadline,
    blocking, kind of work); the change and its `why` become the agent's note on the task."""
    t = db.task(tid)
    if not t:
        raise HTTPException(404, "task not found")
    fields: dict[str, Any] = {}
    said: list[str] = []
    if p.importance is not None:
        fields["importance"] = max(1, min(5, int(p.importance)))
        said.append(f"importance {fields['importance']}")
    for k, label in (("deadline", "hard deadline"), ("soft_deadline", "soft deadline")):
        v = getattr(p, k)
        if v is None:
            continue
        if v.strip():
            parsed = tags.parse_date(v)
            if not parsed:
                raise HTTPException(400, f"could not read {label} '{v}' (use YYYY-MM-DD)")
            fields[k] = parsed
            said.append(f"{label} {parsed}")
        else:
            fields[k] = None
            said.append(f"no {label}")
    if p.blocking is not None:
        fields["blocking"] = int(p.blocking)
        said.append("blocking" if p.blocking else "not blocking")
    if p.blocking_for is not None:
        fields["blocking_for"] = p.blocking_for.strip()
        if fields["blocking_for"]:
            fields["blocking"] = 1
            said.append(f"blocking {fields['blocking_for']}")
    if p.chunkable is not None:
        fields["chunkable"] = int(p.chunkable)
        said.append("can be split" if p.chunkable else "one sitting")
    if p.work_kind is not None:
        if p.work_kind and p.work_kind not in db.WORK_KINDS:
            raise HTTPException(400, f"work_kind must be one of {sorted(db.WORK_KINDS)} or empty")
        fields["work_kind"] = p.work_kind or None
        said.append({"focus": "focused work", "task": "quick task"}.get(p.work_kind, "kind of work left to Claude"))
    for flag in ("reading", "writing"):
        v = getattr(p, flag)
        if v is not None:
            fields[flag] = int(v)
            said.append(flag if v else f"not {flag}")
    if not fields and not p.why.strip():
        raise HTTPException(400, "nothing to set")
    if fields:
        db.update_task(tid, **fields)
    body = (p.why.strip() + (" " if p.why.strip() and said else "") + (f"(set {', '.join(said)})" if said else "")).strip()
    db.add_note("task", tid, body, agent)
    db.log_plan_event(db.today(), "task", f"{agent} set priority on '{t['title']}': {', '.join(said) or 'a note'}"
                      + (f"; {p.why.strip()[:200]}" if p.why.strip() else ""), {"task_id": tid, "fields": fields, "why": p.why}, actor=f"agent:{agent}")
    return _task_out(db.task(tid))


@router.post("/tasks/{tid}/move")
def move_task(tid: int, m: MoveTaskIn, agent: str = Depends(identity)):
    """Move a task, sub-tasks included, to another project or under another task. Into Someday / maybe it loses its
    deadline, priority and blocking tag like a task the user moves there. The move is written as the agent's note."""
    t = db.task(tid)
    if not t:
        raise HTTPException(404, "task not found")
    parent = None
    if m.parent_id is not None:
        parent = db.task(m.parent_id)
        if not parent:
            raise HTTPException(404, "parent task not found")
        pid = parent["project_id"]
    else:
        pid = _resolve_project(m.project_id, m.project_path, create=m.create_project)
        if pid is None:
            raise HTTPException(400, "give project_id, project_path or parent_id")
    before = " / ".join(db.display_name(n) for n in db.project_path(t["project_id"]))
    try:
        db.move_task(tid, pid, m.parent_id, m.after_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    from . import web
    if web._someday(pid):
        db.update_task(tid, deadline=None, soft_deadline=None, importance=None, blocking=0, blocking_for="")
    after = " / ".join(db.display_name(n) for n in db.project_path(pid)) + (f" under '{parent['title']}'" if parent else "")
    body = f"moved here from {before}" + (f": {m.why.strip()}" if m.why.strip() else "")
    db.add_note("task", tid, body, agent)
    db.log_plan_event(db.today(), "task", f"{agent} moved '{t['title']}' from {before} to {after}"
                      + (f"; {m.why.strip()[:200]}" if m.why.strip() else ""), {"task_id": tid, "from": before, "to": after, "why": m.why}, actor=f"agent:{agent}")
    return _task_out(db.task(tid))


@router.post("/projects/{pid}/move")
def move_project(pid: int, m: MoveProjectIn, agent: str = Depends(identity)):
    """Move a project, with its sub-projects and tasks, under another project or to the top level."""
    p = db.row("SELECT id, name, parent_id FROM projects WHERE id=? AND archived_at IS NULL", (pid,))
    if not p:
        raise HTTPException(404, "project not found (or archived)")
    if m.top_level:
        parent = None
    else:
        parent = _resolve_project(m.parent_id, m.parent_path)
        if parent is None:
            raise HTTPException(400, "give parent_id, parent_path or top_level=true")
        from . import web
        if web._someday(parent):
            raise HTTPException(400, "Someday / maybe holds no projects")
    before = " / ".join(db.display_name(n) for n in db.project_path(pid))
    try:
        db.move_project(pid, parent)
    except ValueError as e:
        raise HTTPException(400, str(e))
    after = " / ".join(db.display_name(n) for n in db.project_path(pid))
    db.add_note("project", pid, f"moved here from {before}" + (f": {m.why.strip()}" if m.why.strip() else ""), agent)
    db.log_plan_event(db.today(), "project", f"{agent} moved project '{db.display_name(p['name'])}' from {before} to {after}"
                      + (f"; {m.why.strip()[:200]}" if m.why.strip() else ""), {"project_id": pid, "from": before, "to": after, "why": m.why}, actor=f"agent:{agent}")
    return {"id": pid, "path": db.project_path(pid), "parent_id": parent}


# ------------------------------------------------ adding
@router.post("/projects")
def create_project(p: ProjectIn, agent: str = Depends(identity)):
    if not p.name.strip():
        raise HTTPException(400, "name required")
    parent = _resolve_project(p.parent_id, p.parent_path)
    pid = db.create_project(p.name, parent, p.notes, source="agent")
    return {"id": pid, "path": db.project_path(pid), "source": "agent"}


@router.post("/tasks")
def create_task(t: TaskIn, agent: str = Depends(identity)):
    title, tagged = tags.parse_tags(t.title.strip())
    if not title:
        raise HTTPException(400, "title required")
    fields: dict[str, Any] = {
        "description": t.description, "deadline": tags.parse_date(t.deadline) if t.deadline else None,
        "soft_deadline": tags.parse_date(t.soft_deadline) if t.soft_deadline else None,
        "blocking": int(t.blocking), "blocking_for": t.blocking_for,
        "importance": None if t.importance is None else max(1, min(5, int(t.importance))),
        "chunkable": int(t.chunkable), "estimate_minutes": t.estimate_minutes, "source": "agent",
        "work_kind": t.work_kind if t.work_kind in db.WORK_KINDS else None,
        "reading": None if t.reading is None else int(t.reading),
        "writing": None if t.writing is None else int(t.writing),
    }
    if t.deadline and not fields["deadline"]:
        raise HTTPException(400, f"could not read deadline '{t.deadline}' (use YYYY-MM-DD)")
    if t.soft_deadline and not fields["soft_deadline"]:
        raise HTTPException(400, f"could not read soft_deadline '{t.soft_deadline}' (use YYYY-MM-DD)")
    fields.update(tagged)
    note = ""
    if t.parent_id:
        parent = db.task(t.parent_id)
        if not parent:
            raise HTTPException(404, "parent task not found")
        pid = parent["project_id"]
        fields["parent_id"] = t.parent_id
    else:
        pid = _resolve_project(t.project_id, t.project_path, create=t.create_project)
        if pid is None:
            if llm.enabled():
                try:
                    res = llm.quick_add(t.title + ("\n" + t.description if t.description else ""), scheduler.tree_text(), db.today())
                    first = (res.get("tasks") or [{}])[0]
                    pid, where = db.resolve_project_path(first.get("project_path") or [])
                    if pid is None:
                        pid = db.ensure_project_path(["Inbox"])
                    note = res.get("note", "") + (f" ({where})" if where else "")
                    for k in ("estimate_minutes",):
                        if fields.get(k) is None and first.get(k) is not None:
                            fields[k] = first[k]
                except llm.LLMError as e:
                    raise HTTPException(502, f"Claude could not file the task: {e}")
            else:
                pid = db.ensure_project_path(["Inbox"])
                note = "Claude not configured; filed in Inbox."
    try:
        tid = db.create_task(pid, title, **fields)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if t.note and t.note.strip():
        db.add_note("task", tid, t.note, agent)
    _forecast_later(tid)
    return {**_task_out(db.task(tid)), "note": note}


@router.post("/quick_add")
def quick_add(q: QuickAddIn, agent: str = Depends(identity)):
    text = q.text.strip()
    if not text:
        raise HTTPException(400, "empty")
    if not llm.enabled():
        pid = db.ensure_project_path(["Inbox"])
        first, tagged = tags.parse_tags(text.splitlines()[0][:200])
        tid = db.create_task(pid, first, description="\n".join(text.splitlines()[1:]), source="agent", **tagged)
        return {"created": [_task_out(db.task(tid))], "note": "Claude not configured; filed in Inbox."}
    try:
        res = llm.quick_add(text, scheduler.tree_text(), db.today())
    except llm.LLMError as e:
        raise HTTPException(502, str(e))
    created = []
    for t in res["tasks"]:
        pid, where = db.resolve_project_path(t["project_path"])
        if pid is None:
            pid = db.ensure_project_path(["Inbox"])
        title, tagged = tags.parse_tags(t["title"])
        tid = db.create_task(pid, title, **{"description": t["description"], "deadline": t["deadline"], "soft_deadline": t.get("soft_deadline"),
                             "blocking": int(t["blocking"]), "blocking_for": t["blocking_for"],
                             "importance": None if t["importance"] is None else max(1, min(5, int(t["importance"]))),
                             "estimate_minutes": t["estimate_minutes"], "chunkable": int(t["chunkable"]), "source": "agent", **tagged})
        created.append(_task_out(db.task(tid)))
    return {"created": created, "note": res.get("note", "")}


# ------------------------------------------------ context
@router.post("/notes")
def add_note(n: NoteIn, agent: str = Depends(identity)):
    if (n.task_id is None) == (n.project_id is None):
        raise HTTPException(400, "give exactly one of task_id, project_id")
    kind, target = ("task", n.task_id) if n.task_id is not None else ("project", n.project_id)
    try:
        nid = db.add_note(kind, target, n.body, agent)
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e) else 400, str(e))
    return db.note(nid)


@router.get("/notes")
def list_notes(task_id: int | None = None, project_id: int | None = None, agent: str = Depends(identity)):
    if task_id is not None:
        return db.notes_for("task", task_id)
    if project_id is not None:
        return db.notes_for("project", project_id)
    return db.all_notes()


@router.delete("/notes/{nid}")
def delete_note(nid: int, agent: str = Depends(identity)):
    n = db.note(nid)
    if not n:
        raise HTTPException(404, "note not found")
    if n["source"] != "agent":
        raise HTTPException(403, "not an agent note")
    db.delete_note(nid)
    return {"ok": True}


# ------------------------------------------------ the schedule
@router.get("/schedule")
def get_schedule(date: str | None = None, agent: str = Depends(identity)):
    ctx = _schedule_context(_day(date))
    ctx["since_your_last_visit"] = _log_since_last_visit(agent)
    return ctx


@router.get("/schedule/log")
def get_log(date: str | None = None, since: str | None = None, limit: int = 200, agent: str = Depends(identity)):
    """Every planning decision the app made (and every schedule the agent set): `date` for one day, `since` (ISO
    local date-time or epoch seconds) for everything after a moment, newest last."""
    return db.plan_log(_day(date).isoformat() if date else None, _since(since), limit)


@router.put("/schedule")
def put_schedule(s: ScheduleIn, agent: str = Depends(identity)):
    day = _day(s.date)
    try:
        p = scheduler.set_plan(day, [i.model_dump() for i in s.items], s.work_start, s.work_end, s.rationale,
                               source=f"agent:{agent}", merge_meetings=s.merge_meetings,
                               validate_windows=s.validate_windows)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "dropped": p["raw"].get("dropped") or [], "exercise_note": p["raw"].get("exercise_note"), "plan": p}


@router.put("/schedule/instructions")
def put_instructions(i: InstructionsIn, agent: str = Depends(identity)):
    day = _day(i.date)
    if i.standing:
        scheduler.set_guidance(i.text, actor=f"agent:{agent}")
    else:
        scheduler.set_day_instructions(day.isoformat(), i.text, actor=f"agent:{agent}")
    plan = None
    if i.rebuild:
        now = dt.datetime.now()
        from_now = day == now.date() and now.hour * 60 + now.minute > scheduler.parse_hm(config.load()["general"]["work_start"])
        plan = scheduler.build(day, from_now=from_now, actor=f"agent:{agent}",
                               reason="the agent set standing guidance" if i.standing else "the agent set instructions for the day")
    return {"ok": True, "date": day.isoformat(), "instructions": scheduler.day_instructions(day.isoformat()),
            "standing_guidance": scheduler.guidance() or None, "plan": plan, "claude": llm.enabled()}


@router.delete("/schedule/instructions")
def delete_instructions(date: str | None = None, standing: bool = False, agent: str = Depends(identity)):
    day = _day(date)
    if standing:
        scheduler.set_guidance("", actor=f"agent:{agent}")
    else:
        scheduler.set_day_instructions(day.isoformat(), "", actor=f"agent:{agent}")
    return {"ok": True, "date": day.isoformat(), "standing": standing}


@router.post("/schedule/rebuild")
def rebuild(r: RebuildIn, agent: str = Depends(identity)):
    return scheduler.build(_day(r.date), from_now=r.from_now, until=r.until, actor=f"agent:{agent}",
                           reason="the agent asked the app to plan the day")
