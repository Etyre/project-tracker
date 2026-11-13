"""The feed's stack: what to do now, without a schedule.

The feed is a stack of cards ordered roughly by priority, computed afresh on every request from the open tasks and
today's recurring cards, and filtered by how much time there is before the next meeting (or the end of the work
day): a card is offered only if its expected length fits that gap, or the work can be split so a piece of it does.
Nothing is planned ahead; "next" simply moves the top card to the bottom for the rest of the day.

Order: today's recurring cards first (the checklist rows with a card, the daily reading and writing), then tasks by
priority score (blocking, hard deadline, soft deadline, importance, age). If the assistant agent set a schedule for
today, the tasks it listed come first, in its order. Cards skipped today go to the end, in the order they were
skipped. Exercise and journaling are not cards: they are the buttons under the deck.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from . import calendar_feed, config, db, scheduler, sheets

log = logging.getLogger("feed")


def key(card: dict) -> str:
    return f"task:{card['task_id']}" if card["kind"] == "task" else f"routine:{card['title']}"


def parse_key(k: str) -> tuple[str, str]:
    """("task", "<id>") or ("routine", "<title>"); raises ValueError for anything else."""
    kind, _, rest = (k or "").partition(":")
    if kind not in ("task", "routine") or not rest:
        raise ValueError(f"not a card: {k!r}")
    return kind, rest


# ---------------------------------------------------------------- the gap before the next meeting
def meetings_today(day: dt.date) -> list[dict]:
    try:
        return [e for e in calendar_feed.events_for(day) if not e["all_day"]]
    except Exception:          # the calendar never breaks the feed
        log.exception("calendar")
        return []


def gap(now: dt.datetime | None = None, events: list[dict] | None = None) -> dict:
    """How long the user has before the next thing on the calendar: {minutes: int | None, until: str | None,
    kind: "meeting" | "end of day" | None, meeting_now: {title, start, end} | None}. `minutes` is None when
    nothing bounds the gap (outside the work day with no meeting still to come)."""
    now = now or dt.datetime.now()
    events = meetings_today(now.date()) if events is None else events
    now_min = now.hour * 60 + now.minute
    meeting_now = None
    nxt = None
    for e in sorted(events, key=lambda e: e["start"]):
        s = e["start"].hour * 60 + e["start"].minute if e["start"].date() == now.date() else 0
        en = e["end"].hour * 60 + e["end"].minute if e["end"].date() == now.date() else 24 * 60
        if s <= now_min < en:
            meeting_now = {"title": e["title"], "start": scheduler.hm(s), "end": scheduler.hm(en)}
        elif s > now_min and nxt is None:
            nxt = {"title": e["title"], "start": scheduler.hm(s), "end": scheduler.hm(en), "start_min": s}
    cfg = config.load()["general"]
    we = scheduler.parse_hm(cfg["work_end"])
    if nxt and (now_min < we and nxt["start_min"] < we or now_min >= we):
        return {"minutes": nxt["start_min"] - now_min, "until": nxt["title"], "at": nxt["start"], "kind": "meeting", "meeting_now": meeting_now}
    if now_min < we and scheduler.parse_hm(cfg["work_start"]) <= now_min:
        return {"minutes": we - now_min, "until": "the end of the work day", "at": scheduler.hm(we), "kind": "end of day", "meeting_now": meeting_now}
    if nxt:
        return {"minutes": nxt["start_min"] - now_min, "until": nxt["title"], "at": nxt["start"], "kind": "meeting", "meeting_now": meeting_now}
    return {"minutes": None, "until": None, "at": None, "kind": None, "meeting_now": meeting_now}


FOCUS_BLOCK_MINUTES = 45   # focused work is offered in pieces only when at least this long a stretch is free


def fits(card: dict, minutes: int | None) -> bool:
    """A card fits the gap when its expected length does, or when the work can be split and at least a short block
    fits. Focused work (work_kind "focus": needs a stretch of concentration) is split only into a stretch of at
    least FOCUS_BLOCK_MINUTES; a quick task into any block of min_block_minutes."""
    if minutes is None:
        return True
    if card["minutes"] <= minutes:
        return True
    if not card.get("chunkable"):
        return False
    need = config.load()["general"]["min_block_minutes"]
    if (card.get("task") or {}).get("work_kind") == "focus":
        need = max(need, FOCUS_BLOCK_MINUTES)
    return minutes >= need


# ---------------------------------------------------------------- skipped today
def _skip_setting(date: str) -> str:
    return f"feed_skipped:{date}"


def skipped(date: str) -> list[str]:
    try:
        v = json.loads(db.get_setting(_skip_setting(date)) or "[]")
        return [k for k in v if isinstance(k, str)]
    except ValueError:
        return []


def skip(k: str, date: str) -> list[str]:
    keys = [x for x in skipped(date) if x != k] + [k]
    db.set_setting(_skip_setting(date), json.dumps(keys))
    return keys


def unskip(k: str, date: str) -> list[str]:
    keys = [x for x in skipped(date) if x != k]
    db.set_setting(_skip_setting(date), json.dumps(keys))
    return keys


# ---------------------------------------------------------------- the stack
def _agent_order(date: str) -> dict[int, int]:
    """Task id -> rank, for the tasks in the schedule the assistant agent set for the day (empty otherwise)."""
    p = db.plan(date)
    ap = (p or {}).get("raw", {}).get("agent_plan") if p else None
    if not ap:
        return {}
    return {b["task_id"]: n for n, b in enumerate(ap.get("items", [])) if b.get("task_id")}


def cards(now: dt.datetime | None = None, events: list[dict] | None = None) -> dict:
    """The stack for now: {"stack": [cards that fit the gap, best first], "waiting": [cards that need a longer
    gap, best first], "gap": gap(), "skipped": [keys]}. Each card: {kind, key, title, task_id, minutes, chunkable,
    score, path, task (tasks only), skipped}."""
    now = now or dt.datetime.now()
    date = now.date().isoformat()
    today = now.date()
    out: list[dict] = []
    for r in sheets.routines_for_today():
        out.append({"kind": "routine", "task_id": None, "title": r["title"], "minutes": int(r["minutes"]), "chunkable": True,
                    "score": None, "path": None})
    order = _agent_order(date)
    tasks = scheduler.candidates(today, limit=400)
    tasks.sort(key=lambda t: (order.get(t["id"], len(order)), -t["score"]))
    for t in tasks:
        out.append({"kind": "task", "task_id": t["id"], "title": t["title"], "minutes": int(t["expected_minutes"]),
                    "chunkable": bool(t["chunkable"]), "score": t["score"], "path": t["path"], "task": t})
    for c in out:
        c["key"] = key(c)
    skipped_keys = skipped(date)
    rank = {k: n for n, k in enumerate(skipped_keys)}
    for c in out:
        c["skipped"] = c["key"] in rank
    out.sort(key=lambda c: rank.get(c["key"], -1))      # stable: skipped cards go last, in the order they were skipped
    g = gap(now, events)
    stack = [c for c in out if fits(c, g["minutes"])]
    waiting = [c for c in out if not fits(c, g["minutes"])]
    return {"stack": stack, "waiting": waiting, "gap": g, "skipped": skipped_keys}


def find(k: str, now: dt.datetime | None = None) -> dict | None:
    """The card for a key, whether or not it fits the gap right now; None when there is no such open task or routine."""
    kind, rest = parse_key(k)
    all_cards = cards(now)
    for c in all_cards["stack"] + all_cards["waiting"]:
        if c["key"] == k:
            return c
    if kind == "task":            # a task not in the stack (a container, inside Someday, or completed): still addressable by id
        t = db.task(int(rest)) if rest.isdigit() else None
        if t:
            return {"kind": "task", "task_id": t["id"], "title": t["title"], "minutes": int(t["estimate_minutes"] or 0),
                    "chunkable": bool(t["chunkable"]), "score": None, "path": " / ".join(db.project_path(t["project_id"])), "task": t, "key": k, "skipped": False}
        return None
    # a routine already ticked today (for undo): the key's own spelling is the one in the log
    minutes = next((int(r["minutes"]) for r in sheets.card_items() if r["title"].strip().lower() == rest.strip().lower()), 20)
    return {"kind": "routine", "task_id": None, "title": rest, "minutes": minutes, "chunkable": True, "score": None, "path": None, "key": k, "skipped": False}
