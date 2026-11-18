"""FastAPI app: JSON API + the single-page dashboard, plus the background loop
(Toggl reconciliation, Linear sync, automatic plan build at the start of the work day)."""
from __future__ import annotations

import os
import sys
import contextlib
import datetime as dt
import logging
import random
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import tags, calendar_feed, config, db, estimates, exercise, journal, linear, llm, scheduler, sheets, toggl, snapshot
from . import agent_api
from . import feed as feed_mod

log = logging.getLogger("web")
HTML_PATH = config.ROOT / "tracker" / "dashboard.html"


# ============================================================ models
class ProjectIn(BaseModel):
    name: str
    parent_id: int | None = None
    notes: str = ""


class ProjectPatch(BaseModel):
    name: str | None = None
    subtitle: str | None = None          # "" clears it
    notes: str | None = None
    parent_id: int | None = None
    toggl_project: str | None = None     # "" unlinks: the next timer asks Claude again


class Reorder(BaseModel):
    parent_id: int | None = None
    ids: list[int]


class TaskIn(BaseModel):
    project_id: int
    title: str
    parent_id: int | None = None          # make it a sub-task of this task (project follows the parent)
    after_id: int | None = None           # insert right after this sibling instead of at the end
    description: str = ""
    deadline: str | None = None           # the hard deadline: cannot move
    soft_deadline: str | None = None      # ideally by: a preference; a task may have both
    blocking: bool = False
    blocking_for: str = ""
    importance: int | None = None         # None = no particular priority
    estimate_minutes: int | None = None
    chunkable: bool = True
    work_kind: str | None = None          # "task" | "focus" (focused work); left to Claude when not given
    reading: bool | None = None           # mainly reading something; left to Claude when not given
    writing: bool | None = None           # mainly writing; left to Claude when not given


class TaskPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    deadline: str | None = None
    soft_deadline: str | None = None
    clear_deadline: bool = False
    blocking: bool | None = None
    blocking_for: str | None = None
    importance: int | None = None
    estimate_minutes: int | None = None
    clear_estimate: bool = False
    chunkable: bool | None = None
    work_kind: str | None = None          # "task" | "focus"; "" clears it so Claude judges again
    reading: bool | None = None
    writing: bool | None = None
    project_id: int | None = None
    parent_id: int | None = None
    after_id: int | None = None
    # Any field sent explicitly (including null) is applied; the clear_* flags are kept for older callers.


class TagsIn(BaseModel):
    """A task's whole tag line ("#p2 #due:fri #focus"): what it names is set, what it leaves out is cleared."""
    text: str


class ApplyIn(BaseModel):
    """Set fields on several tasks at once: from hashtag text ("#p2 #due:fri") and/or explicit fields."""
    ids: list[int]
    text: str | None = None
    fields: dict[str, Any] | None = None


class OutlineItem(BaseModel):
    title: str
    depth: int = 0
    fields: dict[str, Any] | None = None   # notes, dates etc. when a deleted subtree is recreated


class OutlineIn(BaseModel):
    project_id: int
    parent_id: int | None = None
    after_id: int | None = None
    items: list[OutlineItem]


class TaskReorder(BaseModel):
    project_id: int
    parent_id: int | None = None
    ids: list[int]


class QuickAdd(BaseModel):
    text: str


class CardRef(BaseModel):
    card: str                   # "task:<id>" or "routine:<title>" (feed.key)
    label: str | None = None    # the reading / writing card with nothing queued: what is being read or written (names the timer)


class ExerciseStart(BaseModel):
    title: str | None = None      # a session other than the day's pick, e.g. the extra one offered once it is done


class ChecklistDone(BaseModel):
    item: str
    undo: bool = False


class RebuildIn(BaseModel):
    from_now: bool = False
    date: str | None = None
    until: str | None = None      # "HH:MM" - extend the work day for this build


# ============================================================ helpers
def _work_kind(v: str | None) -> str | None:
    """A work kind from the API: "task" | "focus" | None (empty string clears)."""
    v = (v or "").strip().lower()
    if v in ("", "none", "null"):
        return None
    if v in ("focused", "focused work", "focus work"):
        return "focus"
    if v not in db.WORK_KINDS:
        raise HTTPException(400, f"work_kind must be one of {', '.join(db.WORK_KINDS)}")
    return v


def _forecast_estimate_later(tid: int) -> None:
    """Estimates and the kind of work are learned, not typed: forecast them in the background for a task created
    without them (either one the user set is kept)."""
    t = db.task(tid)
    if not t or (t["estimate_minutes"] and t["work_kind"] and t["reading"] is not None and t["writing"] is not None and t["toggl_title"]) or not llm.enabled():
        return

    def run() -> None:
        try:
            path = " / ".join(db.project_path(t["project_id"]))
            res = llm.forecast_estimate(t["title"], path, t["description"], estimates.actuals())
            cur = db.task(tid)
            if not cur:
                return
            fields = {}
            if not cur["estimate_minutes"]:
                fields["estimate_minutes"] = res["estimate_minutes"]
            if not cur["work_kind"] and res.get("work_kind"):
                fields["work_kind"] = res["work_kind"]
            if cur["reading"] is None and res.get("reading") is not None:
                fields["reading"] = res["reading"]
            if cur["writing"] is None and res.get("writing") is not None:
                fields["writing"] = res["writing"]
            if not cur["toggl_title"]:
                fields["toggl_title"] = res.get("toggl_title") or toggl.gerund(cur["title"])
            if fields:
                db.update_task(tid, **fields)
        except Exception as e:  # noqa: BLE001 - a missing forecast must never break task creation
            log.warning("estimate forecast failed for task %s: %s", tid, e)

    threading.Thread(target=run, daemon=True, name=f"estimate-{tid}").start()


def _classify_backlog() -> int:
    """Judge the open tasks Claude has not seen yet (older than this feature, or added while it was off), a batch at
    a time, from the background loop. Returns how many were judged."""
    todo = db.tasks_without_work_kind(40)
    if not todo or not llm.enabled():
        return 0
    kinds = llm.classify_work_kinds([{"id": t["id"], "title": t["title"], "description": t["description"],
                                      "path": " / ".join(db.project_path(t["project_id"]))} for t in todo])
    for tid, judged in kinds.items():
        cur = db.task(tid)
        if not cur:
            continue
        fields = {}
        if not cur["work_kind"]:
            fields["work_kind"] = judged["work_kind"]
        if cur["reading"] is None:
            fields["reading"] = judged["reading"]
        if cur["writing"] is None:
            fields["writing"] = judged.get("writing", 0)
        if not cur["toggl_title"]:      # an empty answer gets the rule-based phrasing, so the task leaves the queue
            fields["toggl_title"] = judged.get("toggl_title") or toggl.gerund(cur["title"])
        if fields:
            db.update_task(tid, **fields)
    return len(kinds)


def _toggl_description(kind: str, label: str, task_id: int | None) -> str:
    """What the Toggl entry is called. A task's timer is named in the present participle ("Responding to Sam"): the
    phrasing Claude gave the task when it was created, else the rule-based one. Every other timer keeps its label."""
    if kind != "task" or not task_id:
        return label
    t = db.task(task_id)
    return (t["toggl_title"] if t and t.get("toggl_title") else None) or toggl.gerund(label)


def _task_flag_tags(task_id: int | None) -> list[str]:
    """Toggl tags a task's own timers carry from what it is: "reading" and "writing" (Settings), whatever its project."""
    t = db.task(task_id) if task_id else None
    if not t:
        return []
    cfg = config.load()
    out = []
    if t["reading"] == 1 and (cfg["reading"].get("tag") or "").strip():
        out.append(cfg["reading"]["tag"].strip())
    if t["writing"] == 1 and (cfg["writing"].get("tag") or "").strip():
        out.append(cfg["writing"]["tag"].strip())
    return out


SUGGEST_FLAGS = ("reading", "writing")       # daily cards that suggest a specific task from the tree
SUGGEST_VERB = {"reading": "Read", "writing": "Write"}


def _suggest_flag(title: str) -> str | None:
    """Which suggesting card a recurring card title is (its checklist row from Settings), if any."""
    t = (title or "").strip().lower()
    for flag in SUGGEST_FLAGS:
        r = (config.load()[flag].get("routine") or "").strip().lower()
        if r and (t == r or t.startswith(r) or r.startswith(t)):
            return flag
    return None


def _is_reading_routine(title: str) -> bool:
    return _suggest_flag(title) == "reading"


def _suggest_pick(flag: str) -> dict | None:
    """What the daily reading / writing card offers: the open task of that kind with the highest priority."""
    cands = db.flagged_tasks(flag)
    if not cands:
        return None
    today = dt.date.fromisoformat(db.today())
    best = max(cands, key=lambda t: (scheduler.priority_score(t, today), -t["id"]))
    return {"task": {**_tick_task_fields(best), "notes_added": db.notes_for("task", best["id"])}, "path": db.project_path(best["project_id"])}


def _reading_pick() -> dict | None:
    return _suggest_pick("reading")


def _tick_task_fields(t: dict) -> dict:
    t = dict(t)
    t["tracked_minutes"] = round(db.task_seconds(t["id"]) / 60)
    return t


def _someday(project_id: int | None) -> bool:
    """Someday / maybe items carry no deadline, priority or blocking tags: typed tags stay plain text."""
    p = db.row("SELECT someday_at FROM projects WHERE id=?", (project_id,)) if project_id is not None else None
    return bool(p and p["someday_at"])


def _timer_state() -> dict:
    e = db.running_entry()
    if not e:
        return {"running": False}
    return {"running": True, "entry_id": e["id"], "kind": e["kind"], "label": e["label"], "task_id": e["task_id"],
            "started_at": e["started_at"], "elapsed": int(time.time() - e["started_at"]), "toggl": bool(e["toggl_id"])}


def _stop_timer() -> dict | None:
    e = db.running_entry()
    if not e:
        return None
    err = None
    if e["toggl_id"] and toggl.enabled():
        try:
            cur = toggl.current()
            if cur and cur["id"] == e["toggl_id"]:
                toggl.stop(e["toggl_id"])
        except toggl.TogglError as ex:
            err = str(ex)
    db.stop_entry(e["id"])
    for it in db.rows("SELECT id FROM plan_items WHERE status='active'"):
        db.set_item_status(it["id"], "pending")
    return {"stopped": e["id"], "toggl_error": err}


def _toggl_project_for_timer(kind: str, label: str, task_id: int | None) -> dict | None:
    """The Toggl project a timer gets without asking anyone: the routine list for routines, the linked Toggl
    project of the task's project (or an ancestor) for tasks. {project_id, project, source} or None."""
    if not config.load()["toggl"].get("auto_project", True):
        return None
    if kind in SUGGEST_FLAGS:                         # filed by what is read or written, never by the card: last time's project for the same title
        hit = toggl.history_match(label)
        return {"project_id": hit["project_id"], "project": hit["project"], "tags": [], "source": "history"} if hit and hit.get("project_id") else None
    if kind != "task" or task_id is None:
        return toggl.routine_project(label)
    t = db.task(task_id)
    link = db.toggl_project_for(t["project_id"]) if t else None
    if not link:
        return None
    p = toggl.find_project(link["name"])
    if not p:
        log.warning("project %s is linked to Toggl project '%s', which does not exist", link["project_id"], link["name"])
        return None
    return {"project_id": p["id"], "project": p["name"], "source": "linked project"}


def _start_timer(kind: str, label: str, task_id: int | None = None, extra_tags: list[str] | None = None, context: str = "") -> dict:
    _stop_timer()
    toggl_id, err, chosen = None, None, None
    desc = _toggl_description(kind, label, task_id)         # the entry's name in Toggl; the card keeps the task's title
    if toggl.enabled():
        try:
            chosen = _toggl_project_for_timer(kind, desc, task_id)
        except Exception:                                   # a Toggl hiccup never stops a timer
            log.exception("choosing the Toggl project for '%s'", label)
        try:
            tags = list(dict.fromkeys([*(extra_tags or []), *_task_flag_tags(task_id), *((chosen or {}).get("tags") or [])]))
            toggl_id = toggl.start(desc, tags, project_id=chosen["project_id"] if chosen else None)["id"]
        except toggl.TogglError as ex:
            err = str(ex)
    eid = db.start_entry(kind, label, task_id, toggl_id)
    if toggl_id and chosen:
        log.info("toggl entry %s started under %s (%s)", toggl_id, chosen["project"], chosen["source"])
    elif toggl_id and config.load()["toggl"].get("auto_project", True):
        threading.Thread(target=_file_timer, args=(toggl_id, kind, desc, task_id, context, extra_tags or []), daemon=True, name="toggl-file").start()
    return {"entry_id": eid, "toggl_id": toggl_id, "toggl_error": err, "toggl_project": chosen["project"] if chosen else None}


def _project_subject(pid: int, task_title: str = "") -> tuple[str, str]:
    """What Claude is told about a project when deciding its Toggl project: path, notes, a few open tasks."""
    p = db.row("SELECT * FROM projects WHERE id=?", (pid,)) or {}
    path = " / ".join(db.project_path(pid))
    tasks = [t["title"] for t in db.open_tasks(pid)[:8]]
    subject = f"Project: {path}"
    ctx = []
    if (p.get("notes") or "").strip():
        ctx.append("Notes: " + p["notes"].strip()[:600])
    if tasks:
        ctx.append("Open tasks: " + "; ".join(tasks))
    if task_title:
        ctx.append("The timer just started is for the task: " + task_title)
    return subject, "\n".join(ctx)


def _file_timer(toggl_id: int, kind: str, label: str, task_id: int | None, context: str, extra_tags: list[str]) -> None:
    """After a timer starts with no project: Claude decides once and the answer is kept, so the next timer for the
    same routine or project needs no model. Without Claude, the project this exact description had last time."""
    try:
        if not llm.enabled():
            hit = toggl.history_match(label)
            if hit and hit["project_id"]:
                toggl.update_entry(toggl_id, hit["project_id"])
                log.info("toggl entry %s filed under %s (history)", toggl_id, hit["project"])
            return
        t = db.task(task_id) if kind == "task" and task_id else None
        if t and t["project_id"] is not None:
            subject, ctx = _project_subject(t["project_id"], t["title"])
            d = toggl.decide_project(subject, ctx)
            db.update_project(t["project_id"], toggl_project=d["project"])
            tags = []
        elif t:
            d = toggl.decide_project(f"Task: {t['title']}", (t["description"] or "")[:600])
            tags = []
        elif kind in SUGGEST_FLAGS:                   # a book, a paper, a piece: judged on its title each time, nothing remembered
            d = toggl.decide_project(f"{kind.capitalize()}: {label}", context or kind)
            tags = d["tags"]
        else:
            d = toggl.decide_project(f"Routine: {toggl.routine_key(label)}", context or kind)
            tags = d["tags"]
            toggl.remember_routine(label, d["project"], tags)
        all_tags = list(dict.fromkeys(t for t in [config.load()["toggl"]["tag"], *extra_tags, *tags] if t)) if tags else None
        toggl.update_entry(toggl_id, d["project_id"], all_tags)
        log.info("toggl entry %s filed under %s%s%s by Claude: %s", toggl_id, d["project"], " (new project)" if d["created"] else "",
                 f" tags {tags}" if tags else "", d["reason"])
    except Exception:
        log.exception("filing toggl entry %s", toggl_id)


def _routine_labels() -> list[str]:
    """Every recurring thing the app starts timers for: checklist rows with a card, the exercise sessions named in
    the exercise rules, journaling."""
    out: list[str] = []
    out += [c["title"] for c in sheets.card_items() if not _suggest_flag(c["title"])]   # reading and writing are filed by what is read or written
    rules = exercise.parse_rules(config.exercise_text())
    out += [r["name"] for r in rules.get("required", [])] + list(rules.get("fill", []))
    if config.load()["exercise"].get("after_done"):
        out.append(config.load()["exercise"]["after_done"])
    out.append("Journaling")
    seen: set[str] = set()
    return [x for x in out if not (x.lower() in seen or seen.add(x.lower()))]


def _going_to(title: str) -> str:
    """The exercise timer's name: the session with its travel time, as the user phrases it in Toggl."""
    t = (title or "exercise").strip()
    if t.lower().startswith("exercise:"):
        t = t[9:].strip()
    return t if t.lower().startswith("going to") else "going to do " + t


def _exercise_label(kind: str) -> str:
    """The exercise timer is named for the day's session ("going to do squats"), falling back to the kind."""
    d = exercise.decide(db.today())
    return _going_to(d.get("title") or kind)


def _item_label(it: dict) -> str:
    if it["kind"] == "exercise":
        return _exercise_label(it["title"])
    if it["kind"] in ("checklist", "routine"):
        return it["title"] if it["kind"] == "routine" else "Daily checklist"
    if it["kind"] == "journal":
        return "Journaling"
    return it["title"]


def _timer_is_for(it: dict, e: dict | None) -> bool:
    """Whether the running timer belongs to this feed card: the same task, the same routine by name (for the daily
    reading or writing card, also the task it offers or a timer of that kind), the day's exercise or journal session."""
    if not e:
        return False
    if it.get("status") == "active":
        return True
    if it["kind"] == "task":
        return bool(it["task_id"]) and e["task_id"] == it["task_id"]
    if it["kind"] in ("exercise", "journal"):
        return e["kind"] == it["kind"]
    flag = _suggest_flag(it["title"]) if it["kind"] == "routine" else None
    if flag:
        if e["kind"] == flag:
            return True
        pick = _suggest_pick(flag)
        if pick and e["task_id"] == pick["task"]["id"]:
            return True
    return toggl.routine_key(e["label"]) == toggl.routine_key(_item_label(it))      # routine, checklist


def _current_exercise(date: str) -> str:
    """What the feed's exercise button says: the day's session ("squats"), "rest" on a rest day."""
    d = exercise.decide(date)
    return d.get("title") or d["kind"]


def _exercise_logged_today(title: str) -> bool:
    """A stopped exercise timer today for this session (the app's own entries)."""
    start = dt.datetime.combine(dt.date.fromisoformat(db.today()), dt.time()).timestamp()
    return any(toggl.routine_key(e["label"]) == toggl.routine_key(title)
               for e in db.rows("SELECT label FROM time_entries WHERE kind='exercise' AND started_at>=? AND stopped_at IS NOT NULL", (start,)))


def _checklist_state(date: str) -> dict:
    cl = sheets.daily_checklist()
    done_local = db.checklist_done_today(date)
    items = []
    for r in cl["items"]:
        extra = {k: v for k, v in r.items() if k not in ("item", "done_today", "_row", "checkable", "computed") and v not in ("", None)}
        items.append({"item": r["item"], "done": bool(r.get("done_today")) or r["item"] in done_local, "fields": extra,
                      "checkable": r.get("checkable", True), "computed": bool(r.get("computed"))})
    return {"items": items, "source": cl["source"], "writable": sheets.writable()}


# ============================================================ app
show_window_hook = None   # set by the native app: called with a page fragment to bring the window to the front
notify_hook = None        # set by the native app: called with (title, message) for a macOS notification


# ---- the focus-monitor prompt: at a random interval (60-120 min by default) through the work day ---------------
def _in_work_day(now: dt.datetime | None = None) -> bool:
    cfg = config.load()["general"]
    now = now or dt.datetime.now()
    m = now.hour * 60 + now.minute
    return scheduler.parse_hm(cfg["work_start"]) <= m < scheduler.parse_hm(cfg["work_end"])


def _focus_monitor_schedule_next(from_when: float | None = None) -> float:
    fm = config.load()["focus_monitor"]
    lo, hi = max(5, int(fm.get("min_minutes") or 60)), max(5, int(fm.get("max_minutes") or 120))
    nxt = (from_when or time.time()) + random.uniform(min(lo, hi), max(lo, hi)) * 60
    db.set_setting("focus_monitor_next", str(nxt))
    return nxt


def _focus_monitor_state() -> dict:
    """{enabled, due, next_in_minutes}: due once the interval has run out, within the work day."""
    fm = config.load()["focus_monitor"]
    if not fm.get("enable", True):
        return {"enabled": False, "due": False, "next_in_minutes": None}
    raw = db.get_setting("focus_monitor_next")
    try:
        nxt = float(raw) if raw else None
    except ValueError:
        nxt = None
    if nxt is None:
        nxt = _focus_monitor_schedule_next()
    due = time.time() >= nxt and _in_work_day()
    return {"enabled": True, "due": due, "next_in_minutes": max(0, int((nxt - time.time()) / 60))}


def _focus_monitor_done() -> dict:
    """The user has reviewed: the next prompt is a fresh random interval away; the checklist row is ticked once a day."""
    fm = config.load()["focus_monitor"]
    nxt = _focus_monitor_schedule_next()
    date = db.today()
    sheet = None
    item = (fm.get("item") or "").strip()
    if item and item not in db.checklist_done_today(date):
        db.log_checklist(date, item)
        sheet = sheets.mark_done(item) if sheets.writable() else None
    return {"ok": True, "next_in_minutes": int((nxt - time.time()) / 60), "sheet": sheet}


REPLICA = bool(os.environ.get("TASK_TRACKER_REPLICA"))     # a read-only copy on another machine (README: "The copy on the server")


def create_app() -> FastAPI:
    app = FastAPI(title="Project Tracker")
    app.include_router(agent_api.router)

    if REPLICA:
        # The copy is replaced by the next sync from the Mac, so a change made here would silently vanish: refuse
        # every write and say where the real tracker is. Reads behave as on the Mac. No background loop either
        # (_start_background), so the copy never talks to Toggl, Linear or Claude on its own.
        @app.middleware("http")
        async def read_only(request, call_next):
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                return JSONResponse({"detail": "This is the read-only copy of the Project Tracker. Changes are made in the "
                                               "app on Eli's Mac and reach this copy at the next sync."}, status_code=503)
            return await call_next(request)

    @app.get("/api/replica")
    def replica():
        """{replica, synced_at}: whether this is the read-only copy, and when it was last brought up to date (epoch seconds)."""
        try:
            at = float((config.APP_SUPPORT / "replica_synced_at").read_text().strip())
        except (OSError, ValueError):
            at = None
        return {"replica": REPLICA, "synced_at": at}

    @app.post("/api/window")
    def show_window(fragment: str | None = None):
        """Bring the app window to the front (used by the launcher and `python -m tracker open`)."""
        if show_window_hook is None:
            return {"ok": False, "error": "not running as the native app"}
        show_window_hook(fragment)
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def index():
        # never cache the page itself: the app picks up a new dashboard.html on the next open
        return HTMLResponse(HTML_PATH.read_text(), headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    def status():
        cfg = config.load()
        return {
            "toggl": toggl.enabled(), "linear": linear.enabled(), "llm": llm.enabled(),
            "calendar": bool(cfg["calendar"]["ics_urls"]),
            "sheets": sheets.daily_checklist()["source"] if (cfg["sheets"].get("apps_script_url") or cfg["sheets"]["daily_checklist_csv"]) else "none",
            "sheets_writable": sheets.writable(),
            "work_start": cfg["general"]["work_start"], "work_end": cfg["general"]["work_end"],
            "show_subtitles": cfg["general"]["show_subtitles"],
            "linear_last_sync": db.get_setting("linear_last_sync"),
            "timer": _timer_state(),
        }

    # ------------------------------------------------ projects
    @app.get("/api/tree")
    def tree(active: bool = True, completed: bool = False, archived: bool = False, someday: bool = False):
        return db.project_tree(include_archived=archived, active=active, completed=completed, someday=someday)

    @app.post("/api/projects")
    def create_project(p: ProjectIn):
        if not p.name.strip():
            raise HTTPException(400, "name required")
        return {"id": db.create_project(p.name, p.parent_id, p.notes)}

    @app.get("/api/someday_root")
    def someday_root():
        """The project behind the loose Someday / maybe items (a task moved there has no project of its own)."""
        return {"id": db.someday_root()}

    @app.patch("/api/projects/{pid}")
    def patch_project(pid: int, p: ProjectPatch):
        if not db.row("SELECT id FROM projects WHERE id=?", (pid,)):
            raise HTTPException(404)
        fields = {k: v for k, v in p.model_dump().items() if v is not None}
        if "toggl_project" in fields:
            fields["toggl_project"] = fields["toggl_project"].strip() or None
        if "subtitle" in fields:
            fields["subtitle"] = " ".join(fields["subtitle"].split())      # one line
        db.update_project(pid, **fields)
        return {"ok": True}

    @app.delete("/api/projects/{pid}")
    def archive_project(pid: int):
        if not db.row("SELECT id FROM projects WHERE id=?", (pid,)):
            raise HTTPException(404)
        db.archive_project(pid)
        return {"ok": True}

    @app.get("/api/toggl/projects")
    def toggl_projects():
        """The user's Toggl projects, for the project header's picker and the Settings list."""
        return {"projects": toggl.projects() if toggl.enabled() else [], "enabled": toggl.enabled()}

    @app.post("/api/toggl/suggest_routines")
    def toggl_suggest_routines():
        """Claude sorts every routine into a Toggl project once; the lines come back for the Settings list,
        unsaved until the user saves. Projects Claude proposes are created in Toggl right away."""
        if not toggl.enabled():
            raise HTTPException(400, "Toggl is not configured")
        if not llm.enabled():
            raise HTTPException(400, "Claude is not configured")
        labels = _routine_labels()
        if not labels:
            raise HTTPException(400, "no recurring cards: list checklist rows with their own card or exercise rules first")
        try:
            return toggl.suggest_routines(labels)
        except (toggl.TogglError, llm.LLMError) as e:
            raise HTTPException(502, str(e))

    @app.post("/api/projects/{pid}/archive_done")
    def archive_done(pid: int):
        """The little button at the top of a project: its completed tasks go away (undo restores them)."""
        if not db.row("SELECT id FROM projects WHERE id=?", (pid,)):
            raise HTTPException(404)
        return {"ids": db.archive_completed(pid)}

    @app.post("/api/tasks/{tid}/archive")
    def archive_task(tid: int):
        """Archive in the right-click menu: the task and its sub-items are put away, finished or not (undo restores)."""
        if not db.task(tid):
            raise HTTPException(404)
        ids = db.archive_task(tid)
        e = db.running_entry()
        if e and e["task_id"] in ids:
            _stop_timer()
        for i in ids:      # an archived task leaves today's plan too
            for it in db.rows("SELECT id FROM plan_items WHERE task_id=? AND status IN ('pending','active')", (i,)):
                db.set_item_status(it["id"], "skipped")
        return {"ids": ids}

    @app.post("/api/tasks/{tid}/restore")
    def restore_task(tid: int):
        return {"ids": db.restore_task(tid)}

    @app.post("/api/tasks/unarchive")
    def unarchive(body: dict[str, Any]):
        db.unarchive_tasks([int(x) for x in body.get("ids") or []])
        return {"ok": True}

    @app.post("/api/projects/{pid}/delete_empty")
    def delete_empty_project(pid: int):
        if not db.row("SELECT id FROM projects WHERE id=?", (pid,)):
            raise HTTPException(404)
        try:
            db.delete_empty_project(pid)
        except ValueError as e:
            raise HTTPException(400, f"Cannot delete: {e}")
        return {"ok": True}

    @app.post("/api/projects/{pid}/restore")
    def restore_project(pid: int):
        db.restore_project(pid)
        return {"ok": True}

    @app.post("/api/projects/reorder")
    def reorder_projects(r: Reorder):
        db.reorder_projects(r.parent_id, r.ids)
        return {"ok": True}

    @app.get("/api/projects/{pid}/completed")
    def completed(pid: int):
        return [_tick_task_fields(t) for t in db.completed_tasks(pid)]

    @app.get("/api/completed")
    def completed_all():
        out = []
        for t in db.completed_tasks(None, 300):
            t = _tick_task_fields(t)
            t["path"] = " / ".join(db.project_path(t["project_id"]))
            out.append(t)
        return out

    # ------------------------------------------------ tasks
    @app.post("/api/tasks")
    def create_task(t: TaskIn):
        if not t.title.strip():
            raise HTTPException(400, "title required")
        d = t.model_dump()
        d["blocking"] = int(d["blocking"])
        d["chunkable"] = int(d["chunkable"])
        d["work_kind"] = _work_kind(d["work_kind"])
        for flag in ("reading", "writing"):
            if d[flag] is not None:
                d[flag] = int(d[flag])
        if d["importance"] is not None:
            d["importance"] = max(1, min(5, d["importance"]))
        for k in ("deadline", "soft_deadline"):
            if d[k]:
                d[k] = tags.parse_date(d[k])
                if not d[k]:
                    raise HTTPException(400, f"could not read {k.replace('_', ' ')} (use YYYY-MM-DD)")
        if not _someday(d["project_id"]):
            d["title"], tagged = tags.parse_tags(d["title"])   # "#p1 #due:fri #blocking:Sam" typed in the line
            d.update(tagged)
        try:
            tid = db.create_task(d.pop("project_id"), d.pop("title"), **d)
        except ValueError as e:
            raise HTTPException(400, str(e))
        _forecast_estimate_later(tid)
        return db.task(tid)

    @app.patch("/api/tasks/{tid}")
    def patch_task(tid: int, p: TaskPatch):
        if not db.task(tid):
            raise HTTPException(404)
        d = p.model_dump()
        skip = {"clear_deadline", "clear_estimate", "project_id", "parent_id", "after_id"}
        fields: dict[str, Any] = {k: v for k, v in d.items() if k in p.model_fields_set and k not in skip}
        if d["clear_deadline"]:
            fields["deadline"] = None
        if d["clear_estimate"]:
            fields["estimate_minutes"] = None
        if fields.get("importance") is not None:
            fields["importance"] = max(1, min(5, int(fields["importance"])))
        for k in ("deadline", "soft_deadline"):   # "fri", "tomorrow", "nov3" are read the same way as in a title tag or the agent API
            if fields.get(k):
                parsed = tags.parse_date(fields[k])
                if not parsed:
                    raise HTTPException(400, f"could not read {k.replace('_', ' ')} '{fields[k]}' (use YYYY-MM-DD)")
                fields[k] = parsed
        if fields.get("title") is not None and not fields["title"].strip():
            raise HTTPException(400, "title required")
        if fields.get("title") and not _someday(db.task(tid)["project_id"]):
            fields["title"], tagged = tags.parse_tags(fields["title"])
            fields.update(tagged)
        if "work_kind" in fields:
            fields["work_kind"] = _work_kind(fields["work_kind"])
        for flag in ("reading", "writing"):
            if fields.get(flag) is not None:
                fields[flag] = int(bool(fields[flag]))
        for b in ("blocking", "chunkable"):
            if fields.get(b) is not None:
                fields[b] = int(fields[b])
        cur = db.task(tid)
        moving = ("parent_id" in p.model_fields_set or "after_id" in p.model_fields_set
                  or (d["project_id"] is not None and d["project_id"] != cur["project_id"]))
        if moving:
            parent_id = d["parent_id"] if "parent_id" in p.model_fields_set else (
                cur["parent_id"] if d["project_id"] in (None, cur["project_id"]) else None)
            try:
                db.move_task(tid, d["project_id"], parent_id, d["after_id"])
            except ValueError as e:
                raise HTTPException(400, str(e))
        if fields.get("title") and fields["title"].strip() != (cur["title"] or "").strip():
            fields["toggl_title"] = None          # the Toggl phrasing follows the title: re-phrased by the background pass
        db.update_task(tid, **fields)
        err = linear.safe_push(tid) if fields else None
        return {**db.task(tid), "linear_error": err}

    # ------------------------------------------------ the Priorities page: one list, dragging sets the priority
    def _priority_rows() -> list[dict]:
        paths: dict[int, list[str]] = {}
        return [{**t, "path": paths.setdefault(t["project_id"], db.project_path(t["project_id"]))} for t in db.priority_list()]

    @app.get("/api/priority")
    def priority_list():
        return _priority_rows()

    @app.post("/api/priority/move")
    def priority_move(body: dict[str, Any]):
        """{id, before_id (null: the end); when undoing also importance and clear (ids whose priority to remove)}"""
        try:
            kw: dict[str, Any] = {"importance": body["importance"]} if "importance" in body else {}
            clear = tuple(int(i) for i in body.get("clear") or ())
            was = db.priority_move(int(body["id"]), None if body.get("before_id") is None else int(body["before_id"]), clear=clear, **kw)
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(400, str(e))
        tid = int(body["id"])
        push = ([tid] if db.task(tid)["importance"] != was["importance"] else []) + was["raised"] + list(clear)
        errs = [e for e in (linear.safe_push(i) for i in push) if e]
        return {"was": was, "task": db.task(tid), "linear_error": errs[0] if errs else None, "tasks": _priority_rows()}

    @app.post("/api/tasks/{tid}/complete")
    def complete_task(tid: int):
        db.complete_task(tid)
        e = db.running_entry()
        if e and e["task_id"] == tid:
            _stop_timer()
        for it in db.rows("SELECT id FROM plan_items WHERE task_id=? AND status IN ('pending','active')", (tid,)):
            db.set_item_status(it["id"], "done")
        return {"ok": True, "linear_error": linear.safe_push(tid)}

    @app.post("/api/tasks/{tid}/uncomplete")
    def uncomplete_task(tid: int):
        db.uncomplete_task(tid)
        return {"ok": True, "linear_error": linear.safe_push(tid)}

    # ------------------------------------------------ settings (everything the menu bar item and the files offer)
    @app.get("/api/settings")
    def get_settings():
        return {"config": config.load(), "secrets": config.secret_status(),
                "rules": config.rules_text(),
                "exercise_brief": config.exercise_text(),
                "paths": {"config": str(config.CONFIG_PATH), "data": str(config.APP_SUPPORT), "rules": str(config.rules_path()),
                          "snapshots": str(snapshot.snapshot_dir())}}

    @app.put("/api/settings")
    def put_settings(body: dict[str, Any]):
        before = config.load().get("calendar")
        cfg = config.save(body.get("config") or {})
        if "rules" in body and isinstance(body["rules"], str):
            config.rules_path().write_text(body["rules"])
        if "exercise_brief" in body and isinstance(body["exercise_brief"], str):
            config.exercise_path().write_text(body["exercise_brief"])
        if cfg.get("calendar") != before:      # the page saves on every edit: only a calendar change refetches the feeds
            calendar_feed._cache.clear()
        warnings = []
        if toggl.enabled() and toggl.projects():          # a routine listed under a Toggl project that does not exist
            for label, entry in toggl.routine_map().items():
                if not toggl.find_project(entry["project"]):
                    warnings.append(f"no Toggl project called '{entry['project']}' (for {label})")
        return {"ok": True, "config": cfg, "warnings": warnings}

    @app.post("/api/settings/secret")
    def put_secret(body: dict[str, str]):
        name, value = body.get("name", ""), (body.get("value") or "").strip()
        if name not in config.SECRETS:
            raise HTTPException(400, "unknown secret")
        if not value:
            raise HTTPException(400, "empty value")
        config.keychain_set(name, value)
        llm._creds_ok = False
        toggl.me.cache_clear()
        return {"ok": True, "secrets": config.secret_status()}

    @app.post("/api/settings/test")
    def test_integrations():
        """Try each configured integration and say what happened (the `tracker check` command, for the page)."""
        out: dict[str, str] = {}
        was_paused = llm.paused()
        llm.resume()                      # a contact from the model pauses Claude here; testing again is how you resume it
        out["claude"] = ("key found" if llm.enabled() else ("off in settings" if not config.load()["llm"]["enable"] else "no API key")) \
            + (f" (resumed; it had stopped: {was_paused})" if was_paused else "")
        if toggl.enabled():
            try:
                me = toggl.me()
                left = toggl.quota_left("/me")
                out["toggl"] = f"ok: {me.get('fullname')}" + (f"; {left} user-scoped API calls left this hour" if left is not None else "")
            except Exception as e:
                out["toggl"] = f"error: {e}"
        else:
            out["toggl"] = "no API token"
        if linear.enabled():
            try:
                out["linear"] = f"ok: {len(linear.fetch_my_issues())} issues assigned to you"
            except Exception as e:
                out["linear"] = f"error: {e}"
        else:
            out["linear"] = "off" if not config.load()["linear"]["enable"] else "no API key"
        checks = calendar_feed.check_feeds()
        if checks:
            out["calendar"] = "; ".join(f"ok: {c['events_today']} events today" if c["ok"] else f"{c['url']} {c['error']}" for c in checks)
        else:
            out["calendar"] = "no feeds"
        cfg = config.load()["sheets"]
        if cfg.get("exercise_script_url"):
            try:
                sess = sheets.exercise_sessions()
                out["exercise_log"] = f"ok: {len(sess)} sessions" + (f", last {sess[0]['date']} {sess[0]['exercise']} ({sess[0]['minutes']} min)" if sess else "")
            except Exception as e:
                out["exercise_log"] = f"error: {e}"
        if cfg.get("apps_script_url") or cfg["daily_checklist_csv"]:
            try:
                c = sheets.daily_checklist()
                out["sheets"] = f"ok: {len(c.get('items', []))} checklist items ({c.get('source')}), write-back {'on' if sheets.writable() else 'off'}"
            except Exception as e:
                out["sheets"] = f"error: {e}"
        else:
            out["sheets"] = "not configured"
        return out

    @app.get("/api/snapshots")
    def snapshots():
        return {"dir": str(snapshot.snapshot_dir()), "items": snapshot.list_snapshots()}

    @app.get("/api/snapshots/{date}")
    def snapshot_text(date: str):
        text = snapshot.read(date)
        if text is None:
            raise HTTPException(404)
        return {"date": date, "text": text}

    @app.post("/api/snapshot")
    def snapshot_now():
        path = snapshot.write()
        return {"path": str(path), "dir": str(snapshot.snapshot_dir())}

    @app.post("/api/tasks/apply")
    def apply_to_tasks(a: ApplyIn):
        fields: dict[str, Any] = {}
        if a.text and a.text.strip():
            _, tagged = tags.parse_tags("x " + a.text.strip())
            fields.update(tagged)
        allowed = {"deadline", "soft_deadline", "blocking", "blocking_for", "importance", "chunkable", "work_kind", "reading", "writing"}
        for k, v in (a.fields or {}).items():
            if k in allowed:
                fields[k] = v
        if not fields:
            raise HTTPException(400, "No tags recognised" if a.text else "Nothing to change")
        if fields.get("importance") is not None:
            fields["importance"] = max(1, min(5, int(fields["importance"])))
        for b in ("blocking", "chunkable"):
            if fields.get(b) is not None:
                fields[b] = int(fields[b])
        before: dict[int, dict[str, Any]] = {}
        errors = []
        for tid in dict.fromkeys(a.ids):
            cur = db.task(tid)
            if not cur:
                continue
            before[tid] = {k: cur[k] for k in fields}
            db.update_task(tid, **fields)
            if err := linear.safe_push(tid):
                errors.append(err)
        return {"fields": fields, "before": before, "count": len(before), "linear_error": errors[0] if errors else None}

    @app.get("/api/tasks/{tid}/tags")
    def task_tags(tid: int):
        cur = db.task(tid)
        if not cur:
            raise HTTPException(404)
        return {"text": tags.render(cur)}

    @app.put("/api/tasks/{tid}/tags")
    def set_task_tags(tid: int, o: TagsIn):
        """The opened task's tag line, edited by hand: replaces the task's tags with exactly what the line says."""
        cur = db.task(tid)
        if not cur:
            raise HTTPException(404)
        try:
            fields = tags.replace_fields(cur, o.text)
        except ValueError as e:
            raise HTTPException(400, str(e))
        before = {k: cur[k] for k in fields}
        if fields:
            db.update_task(tid, **fields)
        err = linear.safe_push(tid) if fields else None
        return {"fields": fields, "before": before, "text": tags.render(db.task(tid)), "linear_error": err}

    @app.post("/api/tasks/bulk")
    def create_tasks_bulk(o: OutlineIn):
        items = []
        for i in o.items:
            if not i.title.strip():
                continue
            title, fields = (i.title, {}) if _someday(o.project_id) else tags.parse_tags(i.title)
            fields.update({k: v for k, v in (i.fields or {}).items() if k in db.SUBTREE_FIELDS})
            items.append({"depth": i.depth, "title": title, "fields": fields})
        if not items:
            raise HTTPException(400, "nothing to add")
        if o.parent_id and not db.task(o.parent_id):
            raise HTTPException(404, "parent task not found")
        try:
            ids = db.create_tasks_outline(o.project_id, o.parent_id, o.after_id, items)
        except ValueError as e:
            raise HTTPException(400, str(e))
        for tid in ids:
            _forecast_estimate_later(tid)
        return {"ids": ids}

    @app.post("/api/tasks/reorder")
    def reorder_tasks(r: TaskReorder):
        db.reorder_tasks(r.project_id, r.ids, r.parent_id)
        return {"ok": True}

    @app.delete("/api/tasks/{tid}")
    def delete_task(tid: int, subtree: bool = False):
        if not db.task(tid):
            raise HTTPException(404)
        try:
            if subtree:
                return {"ok": True, "outline": db.delete_task_tree(tid)}
            db.delete_task(tid)
        except ValueError as e:
            raise HTTPException(400, f"Cannot delete: {e}")
        return {"ok": True}

    @app.post("/api/tasks/{tid}/to_project")
    def task_to_project(tid: int):
        try:
            return {"project_id": db.task_to_project(tid)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/projects/{pid}/to_task")
    def project_to_task(pid: int):
        try:
            return {"task_id": db.project_to_task(pid)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/api/tasks/{tid}")
    def get_task(tid: int):
        t = db.task(tid)
        if not t:
            raise HTTPException(404)
        return {**_tick_task_fields(t), "path": db.project_path(t["project_id"]), "notes_added": db.notes_for("task", tid)}

    # ------------------------------------------------ quick add
    @app.post("/api/quick_add")
    def quick_add(q: QuickAdd):
        text = q.text.strip()
        if not text:
            raise HTTPException(400, "empty")
        created = []
        if not llm.enabled():
            pid = db.ensure_project_path(["Inbox"])
            first, tagged = tags.parse_tags(text.splitlines()[0][:200])
            tid = db.create_task(pid, first, description="\n".join(text.splitlines()[1:]), source="quick_add", **tagged)
            return {"created": [{**db.task(tid), "path": ["Inbox"]}], "note": "Claude not configured; filed in Inbox."}
        try:
            res = llm.quick_add(text, scheduler.tree_text(), db.today())
        except llm.LLMError as e:
            raise HTTPException(502, str(e))
        for t in res["tasks"]:
            pid, where = db.resolve_project_path(t["project_path"])
            if pid is None:                                   # Claude named a top level that does not exist: never a new tree
                pid, where = db.ensure_project_path(["Inbox"]), f"in Inbox ({where}; file it by hand)"
            title, tagged = tags.parse_tags(t["title"])       # explicit tags in the note win over Claude's reading
            tid = db.create_task(pid, title, **{"description": t["description"], "deadline": t["deadline"], "soft_deadline": t.get("soft_deadline"),
                                 "blocking": int(t["blocking"]), "blocking_for": t["blocking_for"],
                                 "importance": None if t["importance"] is None else max(1, min(5, int(t["importance"]))),
                                 "importance_by": "user" if "importance" in tagged else "claude",     # a typed #p2 is yours; otherwise Claude read it from the note
                                 "estimate_minutes": t["estimate_minutes"], "chunkable": int(t["chunkable"]), "source": "quick_add", **tagged})
            created.append({**db.task(tid), "path": db.project_path(pid), "where": where})
        return {"created": created, "note": res.get("note", "")}

    # ------------------------------------------------ plan & feed
    @app.get("/api/plan")
    def get_plan(date: str | None = None):
        return db.plan(date or db.today()) or {"date": date or db.today(), "items": None}

    @app.post("/api/plan/rebuild")
    def rebuild(r: RebuildIn):
        day = dt.date.fromisoformat(r.date) if r.date else None
        return scheduler.build(day, from_now=r.from_now, until=r.until, actor="user", reason="the user asked for a rebuild")

    @app.get("/api/calendar")
    def calendar(date: str | None = None):
        day = dt.date.fromisoformat(date) if date else dt.date.today()
        return [{**e, "start": e["start"].isoformat() if e["start"] else None, "end": e["end"].isoformat() if e["end"] else None}
                for e in calendar_feed.events_for(day)]

    @app.get("/api/feed")
    def feed():
        """The stack for now: the top card, what follows, what is waiting for a longer gap, and the gap itself."""
        date = db.today()
        now = dt.datetime.now()
        now_min = now.hour * 60 + now.minute
        cfg = config.load()["general"]
        ws, we = scheduler.parse_hm(cfg["work_start"]), scheduler.parse_hm(cfg["work_end"])
        out: dict[str, Any] = {"date": date, "now": now.strftime("%H:%M"), "current": None,
                               "timer": _timer_state(), "exercise": _current_exercise(date), "focus_monitor": _focus_monitor_state(),
                               "status": {"toggl": toggl.enabled(), "sheets_writable": sheets.writable()}}
        out["outside_hours"] = None if ws <= now_min < we else ("before" if now_min < ws else "after")
        out["work_window"] = [scheduler.hm(ws), scheduler.hm(we)]
        st = feed_mod.cards(now)
        g = st["gap"]
        e = db.running_entry()

        def decorate(c: dict, full: bool) -> dict:
            c = dict(c)
            c["status"] = "active" if _timer_is_for(c, e) else "pending"
            t = c.pop("task", None)
            if c["kind"] == "task" and t is not None:
                c["path"] = db.project_path(t["project_id"])
                if full:
                    c["task"] = {**_tick_task_fields(db.task(t["id"]) or t), "notes_added": db.notes_for("task", t["id"])}
            flag = _suggest_flag(c["title"]) if c["kind"] == "routine" else None
            if flag and full:
                c["suggest"] = {**(_suggest_pick(flag) or {"task": None, "path": None}), "flag": flag, "verb": SUGGEST_VERB[flag],
                                "fallback": config.load()[flag].get("fallback") or ("Read a book" if flag == "reading" else "Free writing")}
            return c

        stack = list(st["stack"])
        active = next((c for c in stack + st["waiting"] if _timer_is_for(c, e)), None)   # the card being worked on stays on top
        if active:
            stack = [active] + [c for c in stack if c["key"] != active["key"]]
        current = decorate(stack[0], True) if stack else None
        midnight = dt.datetime.combine(now.date(), dt.time()).timestamp()
        done_tasks = db.row("SELECT count(*) AS n FROM tasks WHERE completed_at >= ?", (midnight,))["n"]
        done = int(done_tasks) + len(db.checklist_done_today(date))
        out.update({
            "current": current,
            "up_next": [decorate(c, False) for c in stack[1:6]],
            "stack_size": len(stack),
            "waiting": [{"key": c["key"], "title": c["title"], "minutes": c["minutes"]} for c in st["waiting"][:6]],
            "waiting_count": len(st["waiting"]),
            "gap": {k: g[k] for k in ("minutes", "until", "at", "kind")},
            "meeting_now": g["meeting_now"],
            "meetings": [{"title": m["title"], "start": m["start"].strftime("%H:%M"), "end": m["end"].strftime("%H:%M")} for m in feed_mod.meetings_today(now.date())],
            "meeting_alert_minutes": cfg["meeting_alert_minutes"], "meeting_alert_all_day": cfg["meeting_alert_all_day"],
            "progress": {"done": done, "total": done + len(stack) + len(st["waiting"])},
        })
        extra = (config.load()["exercise"].get("after_done") or "").strip()
        out["exercise_done"] = _exercise_logged_today(out["exercise"])
        out["exercise_extra"] = extra or None                           # offered once the day's session is done
        out["exercise_extra_done"] = bool(extra) and _exercise_logged_today(extra)
        return out

    def _card(k: str) -> dict:
        try:
            c = feed_mod.find(k)
        except ValueError as ex:
            raise HTTPException(400, str(ex))
        if not c:
            raise HTTPException(404, "no such card")
        return c

    @app.post("/api/feed/start")
    def feed_start(r: CardRef):
        c = _card(r.card)
        flag = _suggest_flag(c["title"]) if c["kind"] == "routine" else None
        pick = _suggest_pick(flag) if flag else None
        if pick:                                      # the reading / writing card: the timer is the task's, so its time is tracked
            res = _start_timer("task", pick["task"]["title"], pick["task"]["id"])
        elif flag:                                    # nothing queued: a book, a paper, a piece, named by the user, filed by its title
            ftag = [t for t in [(config.load()[flag].get("tag") or "").strip()] if t]     # tagged reading / writing whatever its project
            res = _start_timer(flag, (r.label or "").strip() or flag.capitalize(), None, ftag, context=f"{flag} a book, paper or piece of writing")
        elif c["kind"] == "task":
            res = _start_timer("task", c["title"], c["task_id"])
        else:
            res = _start_timer("routine", c["title"], None, context="daily routine from the checklist")
        return {"ok": True, **res}

    @app.post("/api/feed/complete")
    def feed_complete(r: CardRef):
        """Checkbox on the card: a routine is ticked (here and in the sheet); a task is completed."""
        c = _card(r.card)
        date = db.today()
        if _timer_is_for(c, db.running_entry()):
            _stop_timer()
        feed_mod.unskip(c["key"], date)
        if c["kind"] == "routine":            # a checklist row: tick it in the sheet
            if c["title"] not in db.checklist_done_today(date):
                db.log_checklist(date, c["title"])
            db.execute("UPDATE plan_items SET status='done' WHERE date=? AND kind='routine' AND status='pending' AND lower(title)=lower(?)", (date, c["title"]))
            sheet = sheets.mark_done(c["title"]) if sheets.writable() else {"ok": False, "error": "write-back not configured"}
            return {"ok": True, "linear_error": None, "sheet": sheet}
        db.complete_task(c["task_id"])
        err = linear.safe_push(c["task_id"])
        db.execute("UPDATE plan_items SET status='done' WHERE task_id=? AND status='pending'", (c["task_id"],))   # the plan page follows
        return {"ok": True, "linear_error": err}

    @app.post("/api/feed/uncomplete")
    def feed_uncomplete(r: CardRef):
        """Undo of the card's checkbox: the routine unticked, or the task reopened."""
        c = _card(r.card)
        date = db.today()
        if c["kind"] == "routine":
            db.execute("DELETE FROM checklist_log WHERE date=? AND item=?", (date, c["title"]))
            db.execute("UPDATE plan_items SET status='pending' WHERE date=? AND kind='routine' AND status='done' AND lower(title)=lower(?)", (date, c["title"]))
            if sheets.writable():
                sheets.mark_done(c["title"], undo=True)
            return {"ok": True, "linear_error": None}
        db.uncomplete_task(c["task_id"])
        err = linear.safe_push(c["task_id"])
        db.execute("UPDATE plan_items SET status='pending' WHERE task_id=? AND status='done' AND date=?", (c["task_id"], date))
        return {"ok": True, "linear_error": err}

    @app.post("/api/feed/skip")
    def feed_skip(r: CardRef):
        """Next: the card goes to the bottom of today's stack (its timer, if running, stops)."""
        c = _card(r.card)
        if _timer_is_for(c, db.running_entry()):
            _stop_timer()
        return {"ok": True, "skipped": feed_mod.skip(c["key"], db.today())}

    @app.post("/api/feed/unskip")
    def feed_unskip(r: CardRef):
        return {"ok": True, "skipped": feed_mod.unskip(r.card, db.today())}

    @app.post("/api/focus_monitor/done")
    def focus_monitor_done():
        return _focus_monitor_done()

    @app.post("/api/timer/stop")
    def timer_stop():
        return _stop_timer() or {"stopped": None}

    @app.post("/api/exercise/start")
    def exercise_start(body: ExerciseStart | None = None):
        date = db.today()
        if body and (body.title or "").strip():                       # an extra session: not the plan's exercise block
            title = body.title.strip()
            res = _start_timer("exercise", _going_to(title), None, ["exercise"], context="extra exercise session after the day's one, travel included")
            return {"ok": True, "kind": title, "item_id": None, **res}
        kind = _current_exercise(date)
        res = _start_timer("exercise", _exercise_label(kind), None, ["exercise"], context="exercise session, travel included")
        return {"ok": True, "kind": kind, **res}

    @app.post("/api/journal/start")
    def journal_start():
        res = _start_timer("journal", "Journaling", None, ["journal"], context="journaling")
        # Opening the notes app is a thing for the Mac in front of Eli; when this server is elsewhere, the Mac app's
        # relay (tracker/remote.py) does it after this call returns.
        err = journal.open_notes_app() if sys.platform == "darwin" else None
        return {"ok": True, **res, "open_error": err}

    @app.get("/api/checklist")
    def checklist():
        return _checklist_state(db.today())

    @app.post("/api/checklist/done")
    def checklist_done(c: ChecklistDone):
        date = db.today()
        if c.undo:
            db.execute("DELETE FROM checklist_log WHERE date=? AND item=?", (date, c.item))
        elif c.item not in db.checklist_done_today(date):
            db.log_checklist(date, c.item)
        sheet = sheets.mark_done(c.item, undo=c.undo) if sheets.writable() else {"ok": False, "error": "write-back not configured"}
        return {"ok": True, "sheet": sheet}

    # ------------------------------------------------ misc
    @app.get("/api/stats/estimates")
    def stats():
        return estimates.summary()

    @app.post("/api/linear/sync")
    def linear_sync():
        if not linear.enabled():
            raise HTTPException(400, "Linear not configured (LINEAR_API_KEY + [linear] enable = true)")
        try:
            return linear.pull()
        except linear.LinearError as e:
            raise HTTPException(502, str(e))

    @app.post("/api/refresh")
    def refresh():
        sheets.invalidate()
        calendar_feed._cache.clear()
        config.reload()
        return {"ok": True}

    @app.exception_handler(Exception)
    async def _err(request, exc):
        log.exception("unhandled: %s", exc)
        return JSONResponse({"detail": str(exc)}, status_code=500)

    _start_background()
    return app


# ============================================================ background loop
_bg_started = False


def _start_background() -> None:
    global _bg_started
    if _bg_started or REPLICA:
        return
    _bg_started = True
    threading.Thread(target=_loop, daemon=True, name="tracker-bg").start()


def _loop() -> None:
    last_linear = last_check = last_classify = 0.0
    notified_for = None
    while True:
        try:
            cfg = config.load()
            # 1. Toggl reconciliation: if the user stopped our timer in Toggl, stop it here too. Every few minutes,
            #    not every minute: this read counts against Toggl's 30-an-hour user bucket, and never its last calls.
            e = db.running_entry()
            if (e and e["toggl_id"] and toggl.enabled() and time.time() - last_check >= max(1, cfg["toggl"].get("check_minutes", 5)) * 60
                    and (toggl.quota_left("/me") is None or toggl.quota_left("/me") > 3)):
                last_check = time.time()
                with contextlib.suppress(toggl.TogglError):
                    cur = toggl.current()
                    if not cur or cur["id"] != e["toggl_id"]:
                        db.stop_entry(e["id"])
                        for it in db.rows("SELECT id FROM plan_items WHERE status='active'"):
                            db.set_item_status(it["id"], "pending")
            # 2. Linear sync
            if linear.enabled() and time.time() - last_linear > cfg["linear"]["sync_minutes"] * 60:
                last_linear = time.time()
                try:
                    log.info("linear sync: %s", linear.pull())
                except linear.LinearError as ex:
                    log.warning("linear sync failed: %s", ex)
            # 3. The daily Markdown snapshot of the outline (once the configured hour has passed). Nothing plans the
            #    day by itself any more: the feed is a stack, and the plan page is built only on request.
            now = dt.datetime.now()
            snapshot.maybe_snapshot(now)
            # 4. Today's exercise: decided once, in the morning (from 06:00), and kept for the day
            if now.hour >= 6 and not exercise.decided_today():
                exercise.decide(db.today())
            # 5. Fresh copies of the sheets and calendar before the internet goes off for the night
            pf = (cfg["sheets"].get("prefetch_time") or "").strip()
            if pf and now.hour * 60 + now.minute >= scheduler.parse_hm(pf) and db.get_setting("prefetch_date") != db.today():
                db.set_setting("prefetch_date", db.today())
                log.info("nightly prefetch of sheets and calendar")
                sheets.prefetch()
                calendar_feed.prefetch()
                exercise.history()           # refreshes the Toggl part of the exercise history too
            # 6. The focus-monitor prompt: a macOS notification once per interval when it comes due
            fm = _focus_monitor_state()
            if fm["due"] and config.load()["focus_monitor"].get("notify", True) and notify_hook is not None:
                key = db.get_setting("focus_monitor_next")
                if key != notified_for:
                    notified_for = key
                    notify_hook("Review your focus monitor", "Then press Done on the feed; the next prompt is 1-2 hours away.")
            # 7. Tasks Claude has not yet judged quick-task-or-focused-work: a batch every few minutes
            if llm.enabled() and time.time() - last_classify > 300:
                last_classify = time.time()
                try:
                    n = _classify_backlog()
                    if n:
                        log.info("work kind judged for %d tasks", n)
                except Exception as ex:
                    log.warning("work kind classification failed: %s", ex)
        except Exception:
            log.exception("background loop")
        time.sleep(60)
