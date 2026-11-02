"""Today's exercise: decided once a day, in the morning, by Claude from the user's brief and their history.

The kind (run, strength, rest, …) is chosen at most once per date and remembered, so re-planning the day never
changes it. The length is never configured: it is the typical length of that kind in the user's own history,
which is the app's exercise timers plus any Toggl entries tagged "exercise" (or named like a known kind).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import statistics
import time

from . import config, db, llm, sheets, toggl

log = logging.getLogger(__name__)
LAST_RESORT_MINUTES = 45          # only when there is no history of any kind at all
_toggl_cache: tuple[float, list[dict]] | None = None


def _kind_of(label: str) -> str:
    k = label.strip()
    if k.lower().startswith("exercise:"):
        k = k[9:].strip()
    return k.lower()


def history(days: int = 90) -> list[dict]:
    """Exercise sessions, newest first: {kind, date, weekday, minutes, source}."""
    global _toggl_cache
    since = time.time() - days * 86400
    out: list[dict] = []
    seen_toggl: set[int] = set()
    for e in db.rows("SELECT * FROM time_entries WHERE kind='exercise' AND started_at>=? ORDER BY started_at DESC", (since,)):
        mins = int(((e["stopped_at"] or time.time()) - e["started_at"]) / 60)
        d = dt.datetime.fromtimestamp(e["started_at"])
        out.append({"kind": _kind_of(e["label"]), "date": d.date().isoformat(), "weekday": d.strftime("%a"), "minutes": mins, "source": "app"})
        if e["toggl_id"]:
            seen_toggl.add(int(e["toggl_id"]))
    known = {h["kind"] for h in out} | {k.lower() for k in config.load()["exercise"]["rotation"]}
    if toggl.enabled():
        try:
            if _toggl_cache is None or time.time() - _toggl_cache[0] > 1800:
                _toggl_cache = (time.time(), toggl.entries(days))
            for t in _toggl_cache[1]:
                if int(t.get("id") or 0) in seen_toggl or not t.get("start"):
                    continue
                desc = str(t.get("description") or "")
                tags = [str(x).lower() for x in (t.get("tags") or [])]
                if "exercise" not in tags and _kind_of(desc) not in known:
                    continue
                dur = int(t.get("duration") or 0)
                if dur <= 0:
                    continue
                d = dt.datetime.fromisoformat(str(t["start"]).replace("Z", "+00:00")).astimezone()
                out.append({"kind": _kind_of(desc), "date": d.date().isoformat(), "weekday": d.strftime("%a"), "minutes": dur // 60, "source": "toggl"})
        except toggl.TogglError as e:
            log.warning("toggl history unavailable: %s", e)
    out.sort(key=lambda h: h["date"], reverse=True)
    return out


def typical_minutes(kind: str, hist: list[dict] | None = None) -> int | None:
    """Median length of this kind in the history; failing that of any exercise; None with no history."""
    hist = history() if hist is None else hist
    same = [h["minutes"] for h in hist if h["kind"] == kind.lower() and h["minutes"] >= 5]
    if same:
        return int(statistics.median(same[:10]))
    any_ = [h["minutes"] for h in hist if h["minutes"] >= 5]
    return int(statistics.median(any_[:10])) if any_ else None


def _fallback(hist: list[dict]) -> dict:
    cfg = config.load()["exercise"]
    rotation = cfg["rotation"] or ["exercise"]
    recent = [h["kind"] for h in hist[:len(rotation)]]
    kind = next((k for k in rotation if k.lower() not in recent), None)
    if kind is None:
        last = recent[0] if recent else None
        lower = [r.lower() for r in rotation]
        kind = rotation[(lower.index(last) + 1) % len(rotation)] if last in lower else rotation[0]
    return {"kind": kind, "minutes": typical_minutes(kind, hist) or LAST_RESORT_MINUTES, "reason": "rotation (Claude not configured)"}


def decide(date: str | None = None, force: bool = False) -> dict:
    """Today's exercise: {date, kind, minutes, reason, decided_at}. Decided once per date and remembered."""
    date = date or db.today()
    cur = db.get_setting("exercise_decision")
    if cur and not force:
        try:
            d = json.loads(cur)
            if d.get("date") == date:
                return d
        except ValueError:
            pass
    hist = history()
    decision: dict | None = None
    if llm.enabled():
        day = dt.date.fromisoformat(date)
        ctx = {
            "date": date, "weekday": day.strftime("%A"),
            "recent_sessions": hist[:30],
            "typical_minutes_by_kind": {k: typical_minutes(k, hist) for k in sorted({h["kind"] for h in hist})},
            "sessions_this_week": [h for h in hist if dt.date.fromisoformat(h["date"]) >= day - dt.timedelta(days=day.weekday())],
            "exercise_sheet_csv": sheets.exercise_sheet()[:4000],
        }
        try:
            res = llm.pick_exercise(config.exercise_text(), json.dumps(ctx, indent=1, default=str))
            kind = str(res.get("kind") or "").strip().lower() or "rest"
            mins = typical_minutes(kind, hist) if kind != "rest" else 0
            if mins is None:
                mins = int(res.get("minutes") or 0) or LAST_RESORT_MINUTES
            decision = {"kind": kind, "minutes": mins, "reason": str(res.get("reason") or "")}
        except (llm.LLMError, ValueError) as e:
            log.warning("exercise decision failed, using rotation: %s", e)
    if decision is None:
        decision = _fallback(hist)
    decision.update({"date": date, "decided_at": time.time()})
    db.set_setting("exercise_decision", json.dumps(decision))
    log.info("exercise for %s: %s (%s min) - %s", date, decision["kind"], decision["minutes"], decision["reason"])
    return decision


def decided_today() -> bool:
    cur = db.get_setting("exercise_decision")
    try:
        return bool(cur) and json.loads(cur).get("date") == db.today()
    except ValueError:
        return False
