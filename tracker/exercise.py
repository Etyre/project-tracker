"""Today's exercise: decided once a day, in the morning, by the user's own rules applied to their exercise log.

The kind (run, strength, …) is chosen at most once per date and remembered, so re-planning the day never
changes it. The length is never configured: it is the typical length of that kind in the user's own history,
which is the app's exercise timers plus any Toggl entries tagged "exercise" (or named like a known kind).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
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
    """Exercise sessions, newest first: {kind, date, weekday, minutes, source}. The exercise logging sheet (parsed
    by sheets.exercise_sessions) is the primary record; the app's own timers and Toggl entries fill in sessions
    the sheet does not have (same date and kind = the same session)."""
    global _toggl_cache
    since = time.time() - days * 86400
    out: list[dict] = []
    seen_toggl: set[int] = set()
    logged: set[tuple[str, str]] = set()
    for s in sheets.exercise_sessions(days):
        out.append({k: s[k] for k in ("kind", "date", "weekday", "minutes", "source")} | {"exercise": s["exercise"], "summary": s["sets_summary"]})
        logged.add((s["date"], s["kind"]))
    for e in db.rows("SELECT * FROM time_entries WHERE kind='exercise' AND started_at>=? ORDER BY started_at DESC", (since,)):
        mins = int(((e["stopped_at"] or time.time()) - e["started_at"]) / 60)
        d = dt.datetime.fromtimestamp(e["started_at"])
        if (d.date().isoformat(), _kind_of(e["label"])) not in logged:
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
                if (d.date().isoformat(), _kind_of(desc)) in logged:
                    continue
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


def known_exercises(hist: list[dict]) -> list[str]:
    """The user's own vocabulary: distinct session names from the exercise log, most recent first."""
    out: list[str] = []
    for h in hist:
        name = (h.get("exercise") or h.get("kind") or "").strip().lower()
        if name and name not in out:
            out.append(name)
    return out


RULE_LINE = re.compile(r"^\s*(?P<name>[^:#]+?)\s*:\s*(?P<body>.+?)\s*$")


def parse_rules(text: str) -> dict:
    """The exercise rules, one per line, in plain phrases (anything else is ignored):
        squats: at least every 7 days, at most every 3 days, ideally every 3-4 days
        cardio interval training: at least every 4 days, at most every 2 days, ideally every 2-3 days
        never back to back: deadlifts, squats
        fill: benchpress, deadlifts, shoulder press
    "at least every N days" is the deadline, "at most every M days" the minimum gap, "ideally every A-B days" the
    cadence to aim for (defaults to the gap..deadline window). "never back to back" names pairs that may not fall
    on successive days. "fill" (or "around") lists what to do on days nothing is wanted, least recently done first."""
    rules = {"required": [], "never_adjacent": [], "fill": []}
    for line in (text or "").splitlines():
        m = RULE_LINE.match(line)
        if not m:
            continue
        name, body = m.group("name").strip().lower(), m.group("body").strip().lower()
        if name in ("never back to back", "never on successive days", "not back to back"):
            rules["never_adjacent"].append([x.strip() for x in re.split(r",|\band\b", body) if x.strip()])
        elif name in ("fill", "around", "otherwise", "fill-in"):
            rules["fill"] = [x.strip() for x in re.split(r",|\band\b", body) if x.strip()]
        else:
            least = re.search(r"at least (?:once )?(?:every (\d+) days?|a week|once a week)", body)
            most = re.search(r"(?:at most|no more than) (?:once )?(?:every (\d+) days?|every other day)", body)
            ideal = re.search(r"ideal(?:ly)?[^0-9]*(\d+)(?:\s*(?:-|–|to)\s*(\d+))? days?", body)
            if not least and not most and not ideal:
                continue
            period = (int(least.group(1)) if least.group(1) else 7) if least else None
            gap = (int(most.group(1)) if most.group(1) else 2) if most else 1
            lo, hi = (int(ideal.group(1)), int(ideal.group(2) or ideal.group(1))) if ideal else (gap, period or gap)
            rules["required"].append({"name": name, "period": period, "gap": gap, "ideal": (max(lo, gap), max(hi, lo))})
    return rules


def _same(a: str, b: str) -> bool:
    """'squats' matches 'Squats', 'squat', 'back squats'; 'cardio interval training' matches 'Cardio Interval Training'."""
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return False
    stem = lambda w: re.sub(r"(es|s)$", "", w)
    return stem(a) == stem(b) or a in b or b in a or (len(a.split()) == 1 and stem(a) in [stem(w) for w in b.split()]) or (len(b.split()) == 1 and stem(b) in [stem(w) for w in a.split()])


def _days_since(name: str, hist: list[dict], today: dt.date) -> int | None:
    for h in hist:                                      # newest first
        if _same(name, h.get("exercise") or h.get("kind") or ""):
            return (today - dt.date.fromisoformat(h["date"])).days
    return None


def rules_decide(rules: dict, hist: list[dict], today: dt.date) -> dict:
    """Today's session from the rules and the log.
    1. Anything past its deadline (or never done) that is allowed today: the one done longest ago.
    2. Else anything inside its ideal window: the one furthest through the window (closest to its upper bound).
    3. Else the fill-in done longest ago.
    Never-back-to-back pairs are kept apart, including not doing one the day before the other reaches its deadline."""
    hist = sorted(hist, key=lambda h: h["date"], reverse=True)
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    done_yesterday = [h.get("exercise") or h.get("kind") or "" for h in hist if h["date"] == yesterday]

    def blocked(name: str) -> str | None:
        for pair in rules["never_adjacent"]:
            if any(_same(name, p) for p in pair):
                for other in pair:
                    if not _same(other, name) and any(_same(other, y) for y in done_yesterday):
                        return other
        return None

    overdue, wanted = [], []
    for r in rules["required"]:
        since = _days_since(r["name"], hist, today)
        if blocked(r["name"]) or (since is not None and since < r["gap"]):
            continue
        if since is None or (r["period"] is not None and since >= r["period"]):
            overdue.append((since if since is not None else 10**6, r))
        elif since >= r["ideal"][0]:
            lo, hi = r["ideal"]
            wanted.append(((since - lo + 1) / (hi - lo + 1), since, r))
    if overdue:
        overdue.sort(key=lambda x: -x[0])
        since, r = overdue[0]
        why = "never logged" if since >= 10**6 else f"{since} days since last, deadline every {r['period']} days"
        reason = f"{r['name']}: {why}"
        if len(overdue) > 1:
            reason += f"; {', '.join(o[1]['name'] for o in overdue[1:])} also overdue, done more recently"
        return {"title": r["name"], "reason": reason}
    if wanted:
        wanted.sort(key=lambda x: (-x[0], -x[1]))
        _, since, r = wanted[0]
        lo, hi = r["ideal"]
        return {"title": r["name"], "reason": f"{r['name']}: {since} days since last, ideal cadence every {lo if lo == hi else f'{lo}-{hi}'} days"}
    deadline_tomorrow = [r["name"] for r in rules["required"] if r["period"] is not None
                         and (lambda s: s is not None and s + 1 >= r["period"])(_days_since(r["name"], hist, today))]

    def clashes_tomorrow(name: str) -> bool:
        for pair in rules["never_adjacent"]:
            if any(_same(name, p) for p in pair):
                for other in pair:
                    if not _same(other, name) and any(_same(other, d) for d in deadline_tomorrow):
                        return True
        return False

    options = [((lambda s: s if s is not None else 10**6)(_days_since(n, hist, today)), n) for n in rules["fill"] if not blocked(n) and not clashes_tomorrow(n)]
    if options:
        options.sort(key=lambda x: -x[0])
        since, name = options[0]
        return {"title": name, "reason": f"nothing due; {name} done longest ago" + (f" ({since} days)" if since < 10**6 else " (never)")}
    for r in rules["required"]:
        s_ = _days_since(r["name"], hist, today)
        if not blocked(r["name"]) and (s_ is None or s_ >= r["gap"]):
            return {"title": r["name"], "reason": f"{r['name']}: nothing else listed"}
    return {"title": "", "reason": "no rules matched"}


def _fallback(hist: list[dict]) -> dict:
    """No rules written yet: the logged session the user has gone longest without, else the config rotation."""
    names = known_exercises(hist)
    if names:
        title = names[-1]
        kind = next((h["kind"] for h in hist if (h.get("exercise") or h.get("kind") or "").strip().lower() == title), title)
        return {"kind": kind, "title": title, "minutes": typical_minutes(kind, hist) or LAST_RESORT_MINUTES, "reason": "least recently done in your log (no rules written yet)"}
    rotation = config.load()["exercise"]["rotation"] or ["exercise"]
    recent = [h["kind"] for h in hist[:len(rotation)]]
    kind = next((k for k in rotation if k.lower() not in recent), rotation[0])
    return {"kind": kind, "title": kind, "minutes": typical_minutes(kind, hist) or LAST_RESORT_MINUTES, "reason": "rotation (no rules written yet)"}


def notes_text(text: str) -> str:
    """The lines of the Exercise box that are not rules (and not the bracketed help): what Claude reads."""
    out = []
    for line in (text or "").splitlines():
        if line.strip().startswith("[") or not line.strip():
            continue
        m = RULE_LINE.match(line)
        if m and (m.group("name").strip().lower() in ("never back to back", "never on successive days", "not back to back", "fill", "around", "otherwise", "fill-in")
                  or re.search(r"at least|at most|no more than|ideal", m.group("body").lower())):
            continue
        out.append(line.strip())
    return "\n".join(out)


def _review(decision: dict, hist: list[dict], date: str) -> dict:
    """Claude may override the rules' pick, rarely: only when the user's notes or the log clearly call for it."""
    if not llm.enabled():
        return decision
    notes = notes_text(config.exercise_text())
    try:
        ctx = {"date": date, "weekday": dt.date.fromisoformat(date).strftime("%A"), "recent_sessions": hist[:20],
               "known_exercises": known_exercises(hist)}
        res = llm.review_exercise(decision, notes, json.dumps(ctx, default=str))
    except Exception as e:
        log.warning("exercise review failed, keeping the rules' pick: %s", e)
        return decision
    title = str(res.get("title") or "").strip().lower()
    if res.get("keep", True) or not title or title == decision["title"]:
        return decision
    kind = _kind_for(title, hist)
    same = [h["minutes"] for h in hist if _same(title, h.get("exercise") or "") and h["minutes"] >= 5]
    mins = int(statistics.median(same[:10])) if same else (typical_minutes(kind, hist) or LAST_RESORT_MINUTES)
    return {"kind": kind, "title": title, "minutes": mins, "reason": f"Claude overrode the rules ({decision['title']}): {res.get('reason') or ''}".strip(), "overrode": decision["title"]}


def _kind_for(title: str, hist: list[dict]) -> str:
    for h in hist:
        if _same(title, h.get("exercise") or ""):
            return h["kind"]
    return title


def decide(date: str | None = None, force: bool = False) -> dict:
    """Today's exercise: {date, kind, title, minutes, reason, decided_at}. Decided once per date from the rules in
    the Exercise box (no model involved) and remembered; the length is the typical length of that session in the
    log (any exercise of the same kind, else the last resort)."""
    date = date or db.today()
    rules = parse_rules(config.exercise_text())
    cur = db.get_setting("exercise_decision")
    stored = None
    if cur and not force:
        try:
            d = json.loads(cur)
            if d.get("date") == date and d.get("title") and _from_rules(d, rules):
                if not d.get("provisional"):
                    return d
                stored = d                   # made while the exercise log was unreachable: try again now
        except ValueError:
            pass
    hist = history()
    if stored and sheets.exercise_log_failed:
        return stored                        # still unreachable (e.g. the internet is off at night): keep it for now
    if rules["required"] or rules["fill"]:
        pick = rules_decide(rules, history(), dt.date.fromisoformat(date))
        title = pick["title"]
        if title:
            kind = _kind_for(title, hist)
            same = [h["minutes"] for h in hist if _same(title, h.get("exercise") or "") and h["minutes"] >= 5]
            mins = int(statistics.median(same[:10])) if same else (typical_minutes(kind, hist) or LAST_RESORT_MINUTES)
            decision = {"kind": kind, "title": title, "minutes": mins, "reason": pick["reason"]}
            decision = _review(decision, hist, date)
        else:
            decision = _fallback(hist)
    else:
        decision = _fallback(hist)
    decision.update({"date": date, "decided_at": time.time()})
    if sheets.exercise_log_failed:           # decided from an incomplete log: decided again once the log can be read
        decision["provisional"] = True
        log.warning("exercise log unreachable: today's pick (%s) is provisional", decision["title"])
    db.set_setting("exercise_decision", json.dumps(decision))
    db.retitle_exercise_blocks(date, decision["title"])      # a plan built on an older decision shows the new one
    log.info("exercise for %s: %s (%s min) - %s", date, decision["title"], decision["minutes"], decision["reason"])
    return decision


def _from_rules(decision: dict, rules: dict) -> bool:
    """A remembered decision counts only if today's rules could have produced it (or Claude overrode them); a pick
    from before the rules were written, e.g. the old rotation's "run", is decided again."""
    names = [r["name"] for r in rules["required"]] + list(rules["fill"])
    if not names:
        return True
    return bool(decision.get("overrode")) or any(_same(decision["title"], n) for n in names)


def decided_today() -> bool:
    cur = db.get_setting("exercise_decision")
    try:
        return bool(cur) and json.loads(cur).get("date") == db.today()
    except ValueError:
        return False
