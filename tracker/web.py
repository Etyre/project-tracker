"""FastAPI app: JSON API + the single-page dashboard, plus the background loop
(Toggl reconciliation, Linear sync, automatic plan build at the start of the work day)."""
from __future__ import annotations

import contextlib
import datetime as dt
import logging
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import tags, calendar_feed, config, db, estimates, journal, linear, llm, scheduler, sheets, toggl, snapshot

log = logging.getLogger("web")
HTML_PATH = config.ROOT / "tracker" / "dashboard.html"


# ============================================================ models
class ProjectIn(BaseModel):
    name: str
    parent_id: int | None = None
    notes: str = ""


class ProjectPatch(BaseModel):
    name: str | None = None
    notes: str | None = None
    parent_id: int | None = None


class Reorder(BaseModel):
    parent_id: int | None = None
    ids: list[int]


class TaskIn(BaseModel):
    project_id: int
    title: str
    parent_id: int | None = None          # make it a sub-task of this task (project follows the parent)
    after_id: int | None = None           # insert right after this sibling instead of at the end
    description: str = ""
    deadline: str | None = None
    blocking: bool = False
    blocking_for: str = ""
    importance: int | None = None         # None = no particular priority
    estimate_minutes: int | None = None
    chunkable: bool = True


class TaskPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    deadline: str | None = None
    clear_deadline: bool = False
    blocking: bool | None = None
    blocking_for: str | None = None
    importance: int | None = None
    estimate_minutes: int | None = None
    clear_estimate: bool = False
    chunkable: bool | None = None
    project_id: int | None = None
    parent_id: int | None = None
    after_id: int | None = None
    # Any field sent explicitly (including null) is applied; the clear_* flags are kept for older callers.


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


class ItemRef(BaseModel):
    item_id: int


class ChecklistDone(BaseModel):
    item: str
    undo: bool = False


class RebuildIn(BaseModel):
    from_now: bool = False
    date: str | None = None
    until: str | None = None      # "HH:MM" - extend the work day for this build


# ============================================================ helpers
def _forecast_estimate_later(tid: int) -> None:
    """Estimates are learned, not typed: forecast one in the background for a task created without one."""
    t = db.task(tid)
    if not t or t["estimate_minutes"] or not llm.enabled():
        return

    def run() -> None:
        try:
            path = " / ".join(db.project_path(t["project_id"]))
            est = llm.forecast_estimate(t["title"], path, t["description"], estimates.actuals())
            cur = db.task(tid)
            if cur and not cur["estimate_minutes"]:
                db.update_task(tid, estimate_minutes=est)
        except Exception as e:  # noqa: BLE001 - a missing forecast must never break task creation
            log.warning("estimate forecast failed for task %s: %s", tid, e)

    threading.Thread(target=run, daemon=True, name=f"estimate-{tid}").start()


def _tick_task_fields(t: dict) -> dict:
    t = dict(t)
    t["tracked_minutes"] = round(db.task_seconds(t["id"]) / 60)
    return t


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


def _start_timer(kind: str, label: str, task_id: int | None = None, extra_tags: list[str] | None = None) -> dict:
    _stop_timer()
    toggl_id, err = None, None
    if toggl.enabled():
        try:
            toggl_id = toggl.start(label, extra_tags)["id"]
        except toggl.TogglError as ex:
            err = str(ex)
    eid = db.start_entry(kind, label, task_id, toggl_id)
    return {"entry_id": eid, "toggl_id": toggl_id, "toggl_error": err}


def _item_label(it: dict) -> str:
    if it["kind"] == "exercise":
        return it["title"] if it["title"].lower().startswith("exercise") else f"Exercise: {it['title']}"
    if it["kind"] == "checklist":
        return "Daily checklist"
    if it["kind"] == "journal":
        return config.load()["toggl"]["journal_description"]
    return it["title"]


def _current_exercise(date: str) -> str:
    p = db.row("SELECT exercise FROM plans WHERE date=?", (date,))
    return (p and p["exercise"]) or scheduler.fallback_exercise()


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
def create_app() -> FastAPI:
    app = FastAPI(title="Task Tracker")

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
            "linear_last_sync": db.get_setting("linear_last_sync"),
            "timer": _timer_state(),
        }

    # ------------------------------------------------ projects
    @app.get("/api/tree")
    def tree(active: bool = True, completed: bool = False, archived: bool = False):
        return db.project_tree(include_archived=archived, active=active, completed=completed)

    @app.post("/api/projects")
    def create_project(p: ProjectIn):
        if not p.name.strip():
            raise HTTPException(400, "name required")
        return {"id": db.create_project(p.name, p.parent_id, p.notes)}

    @app.patch("/api/projects/{pid}")
    def patch_project(pid: int, p: ProjectPatch):
        fields = {k: v for k, v in p.model_dump().items() if v is not None}
        db.update_project(pid, **fields)
        return {"ok": True}

    @app.delete("/api/projects/{pid}")
    def archive_project(pid: int):
        if not db.row("SELECT id FROM projects WHERE id=?", (pid,)):
            raise HTTPException(404)
        db.archive_project(pid)
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
        if d["importance"] is not None:
            d["importance"] = max(1, min(5, d["importance"]))
        d["title"], tagged = tags.parse_tags(d["title"])       # "#p1 #due:fri #blocking:Sam" typed in the line
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
        if fields.get("title") is not None and not fields["title"].strip():
            raise HTTPException(400, "title required")
        if fields.get("title"):
            fields["title"], tagged = tags.parse_tags(fields["title"])
            fields.update(tagged)
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
        db.update_task(tid, **fields)
        err = linear.safe_push(tid) if fields else None
        return {**db.task(tid), "linear_error": err}

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
                "rules": config.RULES_PATH.read_text() if config.RULES_PATH.exists() else "",
                "paths": {"config": str(config.CONFIG_PATH), "data": str(config.APP_SUPPORT), "rules": str(config.RULES_PATH),
                          "snapshots": str(snapshot.snapshot_dir())}}

    @app.put("/api/settings")
    def put_settings(body: dict[str, Any]):
        cfg = config.save(body.get("config") or {})
        if "rules" in body and isinstance(body["rules"], str):
            config.RULES_PATH.write_text(body["rules"])
        calendar_feed._cache.clear()
        return {"ok": True, "config": cfg}

    @app.post("/api/settings/secret")
    def put_secret(body: dict[str, str]):
        name, value = body.get("name", ""), (body.get("value") or "").strip()
        if name not in config.SECRETS:
            raise HTTPException(400, "unknown secret")
        if not value:
            raise HTTPException(400, "empty value")
        config.keychain_set(name, value)
        llm._has_credentials.cache_clear()
        toggl.me.cache_clear()
        return {"ok": True, "secrets": config.secret_status()}

    @app.post("/api/settings/test")
    def test_integrations():
        """Try each configured integration and say what happened (the `tracker check` command, for the page)."""
        out: dict[str, str] = {}
        out["claude"] = "key found" if llm.enabled() else ("off in settings" if not config.load()["llm"]["enable"] else "no API key")
        if toggl.enabled():
            try:
                me = toggl.me()
                out["toggl"] = f"ok: {me.get('fullname')}"
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
        urls = config.load()["calendar"]["ics_urls"]
        if urls:
            try:
                out["calendar"] = f"ok: {len(calendar_feed.events_for(dt.date.today()))} events today from {len(urls)} feed(s)"
            except Exception as e:
                out["calendar"] = f"error: {e}"
        else:
            out["calendar"] = "no feeds"
        cfg = config.load()["sheets"]
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
        allowed = {"deadline", "blocking", "blocking_for", "importance", "chunkable"}
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

    @app.post("/api/tasks/bulk")
    def create_tasks_bulk(o: OutlineIn):
        items = []
        for i in o.items:
            if not i.title.strip():
                continue
            title, fields = tags.parse_tags(i.title)
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
        return {**_tick_task_fields(t), "path": db.project_path(t["project_id"])}

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
            path = [s for s in t["project_path"] if s.strip()] or ["Inbox"]
            pid = db.find_project_by_path(path) or db.ensure_project_path(path)
            title, tagged = tags.parse_tags(t["title"])       # explicit tags in the note win over Claude's reading
            tid = db.create_task(pid, title, **{"description": t["description"], "deadline": t["deadline"],
                                 "blocking": int(t["blocking"]), "blocking_for": t["blocking_for"],
                                 "importance": None if t["importance"] is None else max(1, min(5, int(t["importance"]))),
                                 "estimate_minutes": t["estimate_minutes"], "chunkable": int(t["chunkable"]), "source": "quick_add", **tagged})
            created.append({**db.task(tid), "path": db.project_path(pid)})
        return {"created": created, "note": res.get("note", "")}

    # ------------------------------------------------ plan & feed
    @app.get("/api/plan")
    def get_plan(date: str | None = None):
        return db.plan(date or db.today()) or {"date": date or db.today(), "items": None}

    @app.post("/api/plan/rebuild")
    def rebuild(r: RebuildIn):
        day = dt.date.fromisoformat(r.date) if r.date else None
        return scheduler.build(day, from_now=r.from_now, until=r.until)

    @app.get("/api/calendar")
    def calendar(date: str | None = None):
        day = dt.date.fromisoformat(date) if date else dt.date.today()
        return [{**e, "start": e["start"].isoformat() if e["start"] else None, "end": e["end"].isoformat() if e["end"] else None}
                for e in calendar_feed.events_for(day)]

    @app.get("/api/feed")
    def feed():
        date = db.today()
        p = db.plan(date)
        now = dt.datetime.now()
        now_min = now.hour * 60 + now.minute
        out: dict[str, Any] = {"date": date, "now": now.strftime("%H:%M"), "plan": None, "current": None,
                               "timer": _timer_state(), "exercise": _current_exercise(date),
                               "replanning": dict(scheduler.replanning),
                               "status": {"toggl": toggl.enabled(), "sheets_writable": sheets.writable()}}
        cfg = config.load()["general"]
        ws, we = scheduler.parse_hm(cfg["work_start"]), scheduler.parse_hm(p["work_end"] if p else cfg["work_end"])
        out["outside_hours"] = None if ws <= now_min < we else ("before" if now_min < ws else "after")
        out["work_window"] = [scheduler.hm(ws), scheduler.hm(we)]
        if not p:
            return out
        if scheduler.maybe_replan():          # schedule drifted: rebuild the rest of the day in the background
            out["replanning"] = dict(scheduler.replanning)
        items = p["items"]
        active = next((i for i in items if i["status"] == "active"), None)
        pending = [i for i in items if i["status"] == "pending" and i["kind"] != "meeting"]
        current = active or (pending[0] if pending else None)
        meeting_now = next((i for i in items if i["kind"] == "meeting" and scheduler.parse_hm(i["start"]) <= now_min < scheduler.parse_hm(i["end"])), None)
        if current and current["kind"] == "task" and current["task_id"]:
            current = {**current, "task": _tick_task_fields(db.task(current["task_id"])),
                       "path": db.project_path(current["project_id"])}
        if current and current["kind"] == "checklist":
            current = {**current, "checklist": _checklist_state(date)}
        if current:
            current["behind_minutes"] = max(0, now_min - scheduler.parse_hm(current["start"]))
        done = sum(1 for i in items if i["status"] == "done" and i["kind"] != "meeting")
        total = sum(1 for i in items if i["kind"] != "meeting")
        out.update({
            "plan": {"built_at": p["built_at"], "rationale": p["rationale"], "source": p["raw"].get("source"),
                     "work_start": p["work_start"], "work_end": p["work_end"], "off_track": scheduler.off_track()},
            "current": current, "meeting_now": meeting_now,
            "up_next": [i for i in pending if not current or i["id"] != current["id"]][:5],
            "progress": {"done": done, "total": total},
            "exercise_item": next((i for i in items if i["kind"] == "exercise"), None),
        })
        return out

    @app.post("/api/feed/start")
    def feed_start(r: ItemRef):
        it = db.plan_item(r.item_id)
        if not it:
            raise HTTPException(404)
        res = _start_timer(it["kind"] if it["kind"] in ("task", "exercise", "checklist", "journal") else "task",
                           _item_label(it), it["task_id"], [it["kind"]] if it["kind"] != "task" else None)
        db.set_item_status(it["id"], "active")
        return {"ok": True, **res}

    @app.post("/api/feed/complete")
    def feed_complete(r: ItemRef):
        """Checkbox on the card: the block is done AND (for a task) the task itself is complete."""
        it = db.plan_item(r.item_id)
        if not it:
            raise HTTPException(404)
        e = db.running_entry()
        if e and (it["task_id"] and e["task_id"] == it["task_id"] or it["status"] == "active"):
            _stop_timer()
        db.set_item_status(it["id"], "done")
        err = None
        scheduler.maybe_replan()
        if it["kind"] == "task" and it["task_id"]:
            db.complete_task(it["task_id"])
            err = linear.safe_push(it["task_id"])
            for other in db.rows("SELECT id FROM plan_items WHERE task_id=? AND status='pending'", (it["task_id"],)):
                db.set_item_status(other["id"], "done")
        return {"ok": True, "linear_error": err}

    @app.post("/api/feed/finish_block")
    def feed_finish_block(r: ItemRef):
        """Block done, task stays open (partial progress)."""
        it = db.plan_item(r.item_id)
        if not it:
            raise HTTPException(404)
        if it["status"] == "active":
            _stop_timer()
        db.set_item_status(it["id"], "done")
        return {"ok": True}

    @app.post("/api/feed/skip")
    def feed_skip(r: ItemRef):
        it = db.plan_item(r.item_id)
        if not it:
            raise HTTPException(404)
        if it["status"] == "active":
            _stop_timer()
        db.set_item_status(it["id"], "skipped")
        scheduler.maybe_replan()
        return {"ok": True}

    @app.post("/api/feed/reset_item")
    def feed_reset(r: ItemRef):
        db.set_item_status(r.item_id, "pending")
        return {"ok": True}

    @app.post("/api/timer/stop")
    def timer_stop():
        return _stop_timer() or {"stopped": None}

    @app.post("/api/exercise/start")
    def exercise_start():
        date = db.today()
        kind = _current_exercise(date)
        res = _start_timer("exercise", f"Exercise: {kind}", None, ["exercise"])
        it = db.row("SELECT id FROM plan_items WHERE date=? AND kind='exercise' AND status IN ('pending','active')", (date,))
        if it:
            db.set_item_status(it["id"], "active")
        return {"ok": True, "kind": kind, "item_id": it["id"] if it else None, **res}

    @app.post("/api/journal/start")
    def journal_start():
        res = _start_timer("journal", config.load()["toggl"]["journal_description"], None, ["journal"])
        err = journal.open_notes_app()
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
    if _bg_started:
        return
    _bg_started = True
    threading.Thread(target=_loop, daemon=True, name="tracker-bg").start()


def _loop() -> None:
    last_linear = 0.0
    while True:
        try:
            cfg = config.load()
            # 1. Toggl reconciliation: if the user stopped our timer in Toggl, stop it here too.
            e = db.running_entry()
            if e and e["toggl_id"] and toggl.enabled():
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
            # 3. Re-plan when the day has drifted from the schedule (rate-limited inside)
            scheduler.maybe_replan()
            # 4. Build today's plan automatically once the work day starts
            now = dt.datetime.now()
            ws = scheduler.parse_hm(cfg["general"]["work_start"])
            if now.hour * 60 + now.minute >= ws - 15 and not db.plan(db.today()) and db.get_setting("auto_plan_date") != db.today():
                db.set_setting("auto_plan_date", db.today())
                log.info("auto-building today's plan")
                scheduler.build()
            # 5. The daily Markdown snapshot of the outline (once the configured hour has passed)
            snapshot.maybe_snapshot(now)
        except Exception:
            log.exception("background loop")
        time.sleep(60)
