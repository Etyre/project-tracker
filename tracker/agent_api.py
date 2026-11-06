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
    deadline: str | None = None            # YYYY-MM-DD, or "fri", "tomorrow", "nov3"
    blocking: bool = False
    blocking_for: str = ""
    importance: int | None = None          # 1 (low) .. 5 (critical)
    chunkable: bool = True
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
    pin: bool = True                       # the drift re-planner leaves the day alone
    merge_meetings: bool = True            # calendar meetings the schedule left out are added
    validate_windows: bool = True          # blocks are clipped to the free windows between meetings


class InstructionsIn(BaseModel):
    text: str
    date: str | None = None
    rebuild: bool = True                   # re-plan the day with the instructions now (synchronous)


class RebuildIn(BaseModel):
    date: str | None = None
    from_now: bool = False
    until: str | None = None


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
        out.append({"id": p["id"], "parent_id": p["parent_id"], "name": p["name"], "path": here, "notes": p["notes"],
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
            pid = db.ensure_project_path(clean)
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
    cands = [{k: c[k] for k in ("id", "title", "path", "deadline", "blocking", "blocking_for", "importance", "chunkable",
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
        "plan": p and {"built_at": p["built_at"], "source": p["raw"].get("source"), "pinned": bool(p["raw"].get("pinned")),
                       "rationale": p["rationale"], "work_start": p["work_start"], "work_end": p["work_end"],
                       "dropped": p["raw"].get("dropped") or [], "items": p["items"]},
        "rules_document": config.rules_text(),
    }


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
            "POST /api/agent/projects": "add a project (name, parent_id | parent_path, notes)",
            "POST /api/agent/tasks": "add a task (title, project_id | project_path | parent_id, fields, note); no project -> Claude files it",
            "POST /api/agent/quick_add": "free text; Claude turns it into tasks in the right projects",
            "POST /api/agent/notes": "add context to a task or project, shown as yours",
            "GET /api/agent/notes?task_id=|project_id=": "your notes on an item",
            "DELETE /api/agent/notes/{id}": "take a note back",
            "GET /api/agent/schedule?date=": "the day's plan, free windows, prioritised candidates, routines, exercise",
            "PUT /api/agent/schedule": "set the day's schedule block by block (pinned against automatic re-planning)",
            "PUT /api/agent/schedule/instructions": "steer the day in prose; the app re-plans with it",
            "DELETE /api/agent/schedule/instructions?date=": "drop the instructions",
            "POST /api/agent/schedule/rebuild": "let the app plan the day (from_now, until)",
            "POST /api/agent/schedule/unpin?date=": "hand a pinned day back to the automatic re-planner",
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
        "blocking": int(t.blocking), "blocking_for": t.blocking_for,
        "importance": None if t.importance is None else max(1, min(5, int(t.importance))),
        "chunkable": int(t.chunkable), "estimate_minutes": t.estimate_minutes, "source": "agent",
    }
    if t.deadline and not fields["deadline"]:
        raise HTTPException(400, f"could not read deadline '{t.deadline}' (use YYYY-MM-DD)")
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
                    path = [s for s in first.get("project_path", []) if s.strip()] or ["Inbox"]
                    pid = db.find_project_by_path(path) or db.ensure_project_path(path)
                    note = res.get("note", "")
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
        path = [s for s in t["project_path"] if s.strip()] or ["Inbox"]
        pid = db.find_project_by_path(path) or db.ensure_project_path(path)
        title, tagged = tags.parse_tags(t["title"])
        tid = db.create_task(pid, title, **{"description": t["description"], "deadline": t["deadline"],
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
    return _schedule_context(_day(date))


@router.put("/schedule")
def put_schedule(s: ScheduleIn, agent: str = Depends(identity)):
    day = _day(s.date)
    try:
        p = scheduler.set_plan(day, [i.model_dump() for i in s.items], s.work_start, s.work_end, s.rationale,
                               source=f"agent:{agent}", pin=s.pin, merge_meetings=s.merge_meetings,
                               validate_windows=s.validate_windows)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "dropped": p["raw"].get("dropped") or [], "plan": p}


@router.put("/schedule/instructions")
def put_instructions(i: InstructionsIn, agent: str = Depends(identity)):
    day = _day(i.date)
    scheduler.set_day_instructions(day.isoformat(), i.text)
    plan = None
    if i.rebuild:
        now = dt.datetime.now()
        from_now = day == now.date() and now.hour * 60 + now.minute > scheduler.parse_hm(config.load()["general"]["work_start"])
        plan = scheduler.build(day, from_now=from_now)
    return {"ok": True, "date": day.isoformat(), "instructions": scheduler.day_instructions(day.isoformat()), "plan": plan,
            "claude": llm.enabled()}


@router.delete("/schedule/instructions")
def delete_instructions(date: str | None = None, agent: str = Depends(identity)):
    day = _day(date)
    scheduler.set_day_instructions(day.isoformat(), "")
    return {"ok": True, "date": day.isoformat()}


@router.post("/schedule/rebuild")
def rebuild(r: RebuildIn, agent: str = Depends(identity)):
    return scheduler.build(_day(r.date), from_now=r.from_now, until=r.until)


@router.post("/schedule/unpin")
def unpin(date: str | None = None, agent: str = Depends(identity)):
    p = scheduler.unpin(_day(date).isoformat())
    if not p:
        raise HTTPException(404, "no plan for that day")
    return p
