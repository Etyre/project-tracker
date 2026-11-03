"""Build the day's plan: free windows from the calendar, prioritised tasks, checklist, exercise.
Claude does the intelligent packing when configured; a deterministic greedy packer is the fallback and the
validator (anything the model emits outside the free windows is clamped or dropped)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time

from . import calendar_feed, config, db, estimates, exercise as exercise_mod, llm, sheets

log = logging.getLogger("scheduler")


# ---------------------------------------------------------------- time helpers
def hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def round5(m: int, up: bool = True) -> int:
    return ((m + 4) // 5) * 5 if up else (m // 5) * 5


# ---------------------------------------------------------------- inputs
def priority_score(t: dict, today: dt.date) -> float:
    s = float(t["importance"] if t.get("importance") is not None else 3) * 10   # unset = neutral
    if t["blocking"]:
        s += 25
    if t.get("deadline"):
        try:
            d = dt.date.fromisoformat(t["deadline"][:10])
            days = (d - today).days
            s += 40 if days < 0 else 30 if days == 0 else 20 if days == 1 else 12 if days <= 3 else 6 if days <= 7 else 2
        except ValueError:
            pass
    age_days = (dt.datetime.now().timestamp() - t["created_at"]) / 86400
    s += min(10.0, age_days / 7)
    return s


def free_windows(day: dt.date, events: list[dict], start_min: int, end_min: int) -> list[tuple[int, int]]:
    busy = []
    for e in events:
        if e["all_day"]:
            continue
        s = e["start"].hour * 60 + e["start"].minute if e["start"].date() == day else 0
        en = e["end"].hour * 60 + e["end"].minute if e["end"].date() == day else 24 * 60
        busy.append((max(s, start_min), min(en, end_min)))
    busy.sort()
    windows: list[tuple[int, int]] = []
    cur = start_min
    for s, en in busy:
        if en <= cur:
            continue
        if s > cur:
            windows.append((cur, s))
        cur = max(cur, en)
    if cur < end_min:
        windows.append((cur, end_min))
    minb = config.load()["general"]["min_block_minutes"]
    return [(round5(a), round5(b, up=False)) for a, b in windows if round5(b, up=False) - round5(a) >= minb]


def candidates(today: dt.date, limit: int = 60) -> list[dict]:
    cal = estimates.calibration()
    out = []
    for t in db.open_tasks(schedulable_only=True):
        if db.subtasks(t["id"]):
            continue                     # a task with open sub-tasks is a container; schedule the sub-tasks
        c = dict(t)
        c["score"] = round(priority_score(t, today), 1)
        c["expected_minutes"] = estimates.expected_minutes(t, cal)
        c["path"] = " / ".join(db.project_path(t["project_id"]))
        out.append(c)
    out.sort(key=lambda c: -c["score"])
    return out[:limit]


def fallback_exercise() -> str:
    """The rotation's next kind (no Claude). Kept for the feed before any decision exists."""
    return exercise_mod._fallback(exercise_mod.history())["kind"]


# ---------------------------------------------------------------- deterministic packer
def greedy_plan(windows: list[tuple[int, int]], tasks: list[dict], checklist_minutes: int, exercise: str,
                exercise_minutes: int, checklist_done: bool, exercise_done: bool = False) -> list[dict]:
    minb = config.load()["general"]["min_block_minutes"]
    items: list[dict] = []
    remaining = {t["id"]: t["expected_minutes"] for t in tasks}
    placed_exercise = exercise_done
    placed_checklist = checklist_done or checklist_minutes <= 0
    for wi, (ws, we) in enumerate(windows):
        cur = ws
        tail: list[dict] = []
        if not placed_checklist and we - cur >= min(checklist_minutes, minb):
            m = min(checklist_minutes, we - cur)
            items.append(dict(kind="checklist", task_id=None, title="Daily checklist", start=hm(cur), end=hm(cur + m),
                              minutes=m, note="Items from the daily checklist sheet"))
            cur += m
            placed_checklist = True
        # exercise: at the start of the first window after midday that fits; failing that, at the END of the
        # last window (so a single long window still gets its focus work first)
        if not placed_exercise and we - cur >= exercise_minutes:
            if cur >= 12 * 60:
                items.append(dict(kind="exercise", task_id=None, title=f"Exercise: {exercise}", start=hm(cur),
                                  end=hm(cur + exercise_minutes), minutes=exercise_minutes, note=""))
                cur += exercise_minutes
                placed_exercise = True
            elif wi == len(windows) - 1:
                we -= exercise_minutes
                tail.append(dict(kind="exercise", task_id=None, title=f"Exercise: {exercise}", start=hm(we),
                                 end=hm(we + exercise_minutes), minutes=exercise_minutes, note=""))
                placed_exercise = True
        for t in tasks:
            if we - cur < minb:
                break
            need = remaining.get(t["id"], 0)
            if need <= 0:
                continue
            room = we - cur
            if need <= room:
                m = need
            elif t["chunkable"] and room >= minb:
                m = room
            else:
                continue
            m = round5(m)
            m = min(m, room)
            note = "" if m >= need else f"partial: {need - m} min left for another block"
            items.append(dict(kind="task", task_id=t["id"], title=t["title"], start=hm(cur), end=hm(cur + m),
                              minutes=m, note=note))
            remaining[t["id"]] = need - m
            cur += m
            if remaining[t["id"]] <= 0 and we - cur >= 10 and m >= 60:
                items.append(dict(kind="break", task_id=None, title="Break", start=hm(cur), end=hm(cur + 10),
                                  minutes=10, note=""))
                cur += 10
        items.extend(tail)
    return items


# ---------------------------------------------------------------- validation of model output
def validate(items: list[dict], windows: list[tuple[int, int]], task_ids: set[int]) -> list[dict]:
    out: list[dict] = []
    minb = config.load()["general"]["min_block_minutes"]
    for it in items:
        try:
            s, e = parse_hm(it["start"]), parse_hm(it["end"])
        except Exception:
            continue
        if it["kind"] == "task" and it.get("task_id") not in task_ids:
            continue
        for ws, we in windows:
            if s < we and e > ws:
                s2, e2 = max(s, ws), min(e, we)
                if e2 - s2 >= (5 if it["kind"] == "break" else minb):
                    out.append(dict(kind=it["kind"], task_id=it.get("task_id") if it["kind"] == "task" else None,
                                    title=it["title"], start=hm(s2), end=hm(e2), minutes=e2 - s2, note=it.get("note", "")))
                break
    out.sort(key=lambda i: parse_hm(i["start"]))
    # remove overlaps (keep earlier)
    cleaned: list[dict] = []
    for it in out:
        if cleaned and parse_hm(it["start"]) < parse_hm(cleaned[-1]["end"]):
            s = parse_hm(cleaned[-1]["end"])
            if parse_hm(it["end"]) - s < 5:
                continue
            it = dict(it, start=hm(s), minutes=parse_hm(it["end"]) - s)
        cleaned.append(it)
    return cleaned


# ---------------------------------------------------------------- main entry
def build(day: dt.date | None = None, from_now: bool = False, until: str | None = None) -> dict:
    cfg = config.load()
    day = day or dt.date.today()
    date = day.isoformat()
    ws, we = parse_hm(cfg["general"]["work_start"]), parse_hm(cfg["general"]["work_end"])
    now = dt.datetime.now()
    prev = db.plan(date)
    if until:
        we = max(we, parse_hm(until))
    elif prev:                                   # a previous "work late" override sticks for the day
        we = max(we, parse_hm(prev["work_end"]))
    if from_now and day == now.date() and now.hour * 60 + now.minute >= we:
        we = min(24 * 60 - 1, round5(now.hour * 60 + now.minute) + 120)   # working past the end: plan 2 more hours
    start_min = ws
    prev_items = prev["items"] if prev else []
    active = next((i for i in prev_items if i["status"] == "active"), None) if from_now else None
    if from_now and day == now.date():
        start_min = max(ws, round5(now.hour * 60 + now.minute))
        if active:   # keep the block being worked on; plan the rest after it (or after now if it overran)
            start_min = max(start_min, parse_hm(active["end"]))
    events = calendar_feed.events_for(day)
    windows = free_windows(day, events, start_min, we)
    tasks = candidates(day)
    cl = sheets.daily_checklist()
    cl_text, cl_rows = cl["text"], cl["items"]
    done_items = db.checklist_done_today(date) | {r["item"] for r in cl_rows if r.get("done_today")}
    checklist_minutes = 0
    for r in cl_rows:
        for k, v in r.items():
            if k.lower() in ("minutes", "min", "mins", "duration") and str(v).replace(".", "").isdigit():
                checklist_minutes += int(float(v))
    if cl_rows and checklist_minutes == 0:
        checklist_minutes = 30
    recent_ex = [{"kind": e["label"].replace("Exercise: ", ""),
                  "date": dt.datetime.fromtimestamp(e["started_at"]).date().isoformat(),
                  "weekday": dt.datetime.fromtimestamp(e["started_at"]).strftime("%a"),
                  "minutes": int(((e["stopped_at"] or time.time()) - e["started_at"]) / 60)} for e in db.last_exercise_entries(14)]
    decision = exercise_mod.decide(date)          # once a day, in the morning; re-plans keep it
    exercise, ex_minutes = decision["kind"], int(decision["minutes"] or 0)
    # keep done/skipped items of an existing plan so a re-plan does not re-schedule finished work
    finished_task_ids = {i["task_id"] for i in prev_items if i["status"] in ("done", "skipped") and i["task_id"]}
    if active and active["task_id"]:
        finished_task_ids.add(active["task_id"])
    checklist_done = any(i["kind"] == "checklist" and i["status"] in ("done", "active") for i in prev_items) \
        or (bool(cl_rows) and all(r["item"] in done_items for r in cl_rows))
    exercise_done = any(i["kind"] == "exercise" and i["status"] in ("done", "active") for i in prev_items)
    tasks = [t for t in tasks if t["id"] not in finished_task_ids]

    source, rationale, items = "greedy", "", []
    if llm.enabled() and windows:
        ctx = {
            "date": date, "weekday": day.strftime("%A"), "now": now.strftime("%H:%M"),
            "work_window": [hm(ws), hm(we)], "replanning_from": hm(start_min),
            "free_windows": [[hm(a), hm(b)] for a, b in windows],
            "total_free_minutes": sum(b - a for a, b in windows),
            "meetings": [{"title": e["title"], "start": e["start"].strftime("%H:%M"), "end": e["end"].strftime("%H:%M")}
                         for e in events if not e["all_day"]],
            "all_day_events": [e["title"] for e in events if e["all_day"]],
            "tasks": [{k: t[k] for k in ("id", "title", "path", "deadline", "blocking", "blocking_for", "importance",
                                         "chunkable", "expected_minutes", "score")}
                      | {"description": (t["description"] or "")[:300], "age_days": int((now.timestamp() - t["created_at"]) / 86400)}
                      for t in tasks],
            "daily_checklist_csv": cl_text[:6000],
            "checklist_items": [r["item"] for r in cl_rows],
            "checklist_items_done_today": sorted(done_items),
            "checklist_block_done_today": checklist_done,
            "recent_exercise": recent_ex,
            "exercise_done_today": exercise_done,
            "exercise_today": {k: decision.get(k) for k in ("kind", "minutes", "reason")},
            "estimate_calibration": estimates.calibration(),
        }
        try:
            res = llm.schedule(json.dumps(ctx, indent=1, default=str), config.rules_text(), config.exercise_text())
            items = validate(res.get("items", []), windows, {t["id"] for t in tasks})
            if items:
                source, rationale = "claude", res.get("rationale", "")
        except (llm.LLMError, ValueError) as e:
            log.warning("LLM scheduling failed, using greedy packer: %s", e)
            rationale = f"(Claude scheduling failed: {e}; deterministic plan shown.)"
    if not items:
        items = greedy_plan(windows, tasks, checklist_minutes, exercise, ex_minutes, checklist_done, exercise_done or ex_minutes <= 0)
        if not rationale:
            rationale = ("Deterministic plan: blocking work and deadlines first, then by importance. "
                         "Configure ANTHROPIC_API_KEY for Claude-built plans.")
    for e in events:
        if not e["all_day"]:
            s = e["start"].hour * 60 + e["start"].minute
            en = e["end"].hour * 60 + e["end"].minute
            if en > start_min and s < we:
                items.append(dict(kind="meeting", task_id=None, title=e["title"], start=hm(s), end=hm(en),
                                  minutes=en - s, note="calendar"))
    if from_now:   # history: finished/skipped blocks and the active one stay in the plan as they were
        keep = [i for i in prev_items if i["status"] in ("done", "skipped", "active") and i["kind"] != "meeting"]
        items = [dict(kind=i["kind"], task_id=i["task_id"], title=i["title"], start=i["start"], end=i["end"],
                      minutes=i["minutes"], note=i["note"], status=i["status"]) for i in keep] + \
                [i for i in items if not (i["kind"] == "meeting" and parse_hm(i["end"]) <= start_min)]
    items.sort(key=lambda i: parse_hm(i["start"]))
    db.save_plan(date, hm(ws), hm(we), exercise, rationale, items,
                 {"source": source, "windows": windows, "from_now": from_now, "meetings": _meeting_sig(events)},
                 keep_statuses=True)
    db.set_setting("last_plan_build", time.time())
    return db.plan(date)


def _meeting_sig(events: list[dict]) -> list[str]:
    return sorted(f"{e['start'].strftime('%H:%M')}-{e['end'].strftime('%H:%M')} {e['title']}" for e in events if not e["all_day"])


# ---------------------------------------------------------------- drift detection / automatic re-planning
_replan_lock = threading.Lock()
replanning: dict = {"active": False, "reason": "", "started": 0.0}


def off_track(now: dt.datetime | None = None) -> str | None:
    """Why the schedule no longer matches reality, or None. The schedule is internal: when it drifts, we
    rebuild the rest of the day so the next card is the right one."""
    cfg = config.load()["general"]
    now = now or dt.datetime.now()
    date = now.date().isoformat()
    p = db.plan(date)
    if not p:
        return None
    now_min = now.hour * 60 + now.minute
    ws, we = parse_hm(p["work_start"]), parse_hm(p["work_end"])
    if not (ws <= now_min < we):
        return None
    items = [i for i in p["items"] if i["kind"] != "meeting"]
    active = next((i for i in items if i["status"] == "active"), None)
    drift = cfg.get("replan_drift_minutes", 20)
    if active:
        if now_min - parse_hm(active["end"]) > cfg.get("overrun_minutes", 30):
            return f"'{active['title']}' has overrun its block by {now_min - parse_hm(active['end'])} min"
        return None
    pending = [i for i in items if i["status"] == "pending"]
    if not pending:
        if any(parse_hm(i["end"]) < we - drift for i in items if i["status"] in ("done", "skipped")) and we - now_min > drift:
            return "plan finished early with time left in the day"
        return None
    nxt = pending[0]
    behind = now_min - parse_hm(nxt["start"])
    if behind > drift:
        return f"next block '{nxt['title']}' should have started {behind} min ago"
    if -behind > drift:
        return f"ahead of schedule by {-behind} min"
    try:
        if _meeting_sig(calendar_feed.events_for(now.date())) != p["raw"].get("meetings", []):
            return "calendar changed"
    except Exception:
        pass
    return None


def maybe_replan(reason: str | None = None, force: bool = False) -> bool:
    """Rebuild from now in a background thread if off track (rate-limited). Returns True if a rebuild started."""
    cfg = config.load()["general"]
    reason = reason or off_track()
    if not reason:
        return False
    last = db.get_setting("last_plan_build") or 0
    if not force and time.time() - last < cfg.get("replan_min_interval_minutes", 10) * 60:
        return False
    if not _replan_lock.acquire(blocking=False):
        return False

    def run():
        replanning.update(active=True, reason=reason, started=time.time())
        try:
            log.info("re-planning: %s", reason)
            build(from_now=True)
        except Exception:
            log.exception("re-plan failed")
        finally:
            replanning.update(active=False)
            _replan_lock.release()

    threading.Thread(target=run, daemon=True, name="replan").start()
    return True


def tree_text() -> str:
    """Compact project tree for the quick-add prompt."""
    lines: list[str] = []

    def walk(nodes: list[dict], depth: int) -> None:
        for p in nodes:
            lines.append("  " * depth + f"- {p['name']}")
            for t in p["tasks"][:5]:
                lines.append("  " * (depth + 1) + f"· {t['title']}" + (f" ({len(t['children'])} sub-tasks)" if t.get("children") else ""))
            if len(p["tasks"]) > 5:
                lines.append("  " * (depth + 1) + f"· … {len(p['tasks']) - 5} more")
            walk(p["children"], depth + 1)

    walk(db.project_tree(), 0)
    return "\n".join(lines) or "(empty - no projects yet)"
