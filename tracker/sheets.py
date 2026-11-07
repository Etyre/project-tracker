"""Google Sheets: daily checklist (read + write back) and exercise log (read).

Two transports:
  * Apps Script web app (apps_script/Code.gs) - reads both sheets as JSON and checks items off. Preferred.
  * Plain CSV export URLs - read-only fallback (publish-to-web or export?format=csv on a link-shared sheet).
"""
from __future__ import annotations

import csv
import io
import datetime as dt
import json
import logging
import time
import urllib.parse
import urllib.request

from . import config, db

log = logging.getLogger("sheets")
_cache: dict[str, tuple[float, object]] = {}
_failed_at: dict[str, float] = {}       # a fetch that failed is not retried for a few minutes (the feed polls every minute)
RETRY_AFTER = 300
exercise_log_failed = False             # the exercise log is configured but the last read of it did not succeed


def _get(url: str, max_age: float) -> str | None:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]  # type: ignore[return-value]
    if time.time() - _failed_at.get(url, 0) < RETRY_AFTER:
        return hit[1] if hit else None  # type: ignore[return-value]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "task-tracker"}), timeout=30) as r:
            text = r.read().decode("utf-8", errors="replace")
        _cache[url] = (time.time(), text)
        _failed_at.pop(url, None)
        return text
    except Exception as e:
        log.warning("sheet fetch failed: %s", e)
        _failed_at[url] = time.time()
        return hit[1] if hit else None  # type: ignore[return-value]


def prefetch() -> None:
    """Read every configured sheet afresh now, so the cached copies carry the app through a stretch without
    internet (the user's is blocked at night; the midnight decisions then use what was read at 23:50)."""
    cfg = config.load()["sheets"]
    urls = []
    if _script_url() and _token():
        urls.append(_script_url() + ("&" if "?" in _script_url() else "?") + urllib.parse.urlencode({"token": _token()}))
    if cfg.get("exercise_script_url"):
        urls.append(cfg["exercise_script_url"] + ("&" if "?" in cfg["exercise_script_url"] else "?") + "action=export")
    urls += [u for u in (cfg.get("daily_checklist_csv"), cfg.get("exercise_csv")) if u]
    for u in urls:
        _failed_at.pop(u, None)
        _get(u, 0)


def _script_url() -> str:
    return config.load()["sheets"].get("apps_script_url", "")


def _token() -> str | None:
    return config.secret("sheets") or None


def writable() -> bool:
    return bool(_script_url() and _token())


def _script_get() -> dict | None:
    url = _script_url()
    if not url or not _token():
        return None
    full = url + ("&" if "?" in url else "?") + urllib.parse.urlencode({"token": _token()})
    text = _get(full, config.load()["sheets"]["cache_minutes"] * 60)
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        log.warning("apps script returned non-JSON (is the deployment 'Anyone' + /exec URL?)")
        return None
    if not data.get("ok"):
        log.warning("apps script error: %s", data.get("error"))
        return None
    return data


def _csv_rows(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return [{(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in reader]


def _to_csv(headers: list[str], rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow([r.get(h, "") for h in headers])
    return buf.getvalue()


def daily_checklist() -> dict:
    """{text: csv-ish text for the LLM, items: [{item, done_today, minutes?, ...}], source}"""
    data = _script_get()
    if data:
        cl = data["checklist"]
        headers = [h for h in cl["headers"] if h]
        return {"text": _to_csv(headers, cl["rows"]), "items": cl["rows"], "source": "apps_script",
                "item_column": cl.get("item_column")}
    url = config.load()["sheets"]["daily_checklist_csv"]
    text = _get(url, config.load()["sheets"]["cache_minutes"] * 60) if url else None
    if not text or text.lstrip().startswith("<"):
        return {"text": "", "items": [], "source": "none"}
    rows = _csv_rows(text)
    first = next(iter(rows[0].keys()), None) if rows else None
    for r in rows:
        r["item"] = r.get(first or "", "")
        r.setdefault("done_today", False)
    return {"text": text, "items": [r for r in rows if r["item"]], "source": "csv", "item_column": first}


def exercise_sheet() -> str:
    data = _script_get()
    if data:
        ex = data["exercise"]
        return _to_csv([h for h in ex["headers"] if h], ex["rows"])
    url = config.load()["sheets"]["exercise_csv"]
    text = _get(url, config.load()["sheets"]["cache_minutes"] * 60) if url else None
    return "" if not text or text.lstrip().startswith("<") else text


def mark_done(item: str, undo: bool = False) -> dict:
    """Check an item off in the Google Sheet. Returns {ok, error?}."""
    if not writable():
        return {"ok": False, "error": "sheet write-back not configured (apps_script_url + SHEETS_TOKEN)"}
    body = json.dumps({"token": _token(), "action": "undo" if undo else "done", "item": item}).encode()
    req = urllib.request.Request(_script_url(), data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "task-tracker"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": f"sheet write failed: {e}"}
    invalidate()
    return out


def invalidate() -> None:
    _cache.clear()


# ---- the exercise logging sheet (a Sessions tab written by the user's exercise app; read-only) -------------
CATEGORY_KIND = {"strength training": "strength", "strength": "strength", "weights": "strength", "lifting": "strength",
                 "running": "run", "run": "run", "cardio": "cardio", "mobility": "mobility", "stretching": "mobility",
                 "yoga": "mobility", "cycling": "cycling", "swimming": "swim", "walking": "walk"}


def _kind_from_session(row: dict) -> str:
    cat = str(row.get("category") or "").strip().lower()
    if cat in CATEGORY_KIND:
        return CATEGORY_KIND[cat]
    if cat:
        return cat.replace(" training", "")
    return str(row.get("exercise") or "exercise").strip().lower()


def _local_dt(s: str) -> dt.datetime | None:
    """An ISO timestamp from the exercise app, with or without a zone, as local naive time."""
    s = str(s or "").strip()
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


def exercise_sessions(days: int = 120) -> list[dict]:
    """Sessions from the exercise logging sheet, newest first, as plain records:
    {kind, exercise, category, date, weekday, minutes, started_at, finished_at, sets_summary, notes, source}.
    Read through the exercise app's own sync script (`?action=export` returns the logged session objects), with no
    write-back. [] when the URL is not configured or unreachable; the app then uses its own timers and Toggl."""
    global exercise_log_failed
    url = config.load()["sheets"].get("exercise_script_url") or ""
    if not url:
        exercise_log_failed = False
        return []
    full = url + ("&" if "?" in url else "?") + "action=export"
    text = _get(full, config.load()["sheets"]["cache_minutes"] * 60)
    exercise_log_failed = True
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        log.warning("exercise log script returned non-JSON (is it the /exec URL?)")
        return []
    if not data.get("ok"):
        log.warning("exercise log script error: %s", data.get("error"))
        return []
    exercise_log_failed = False
    since = dt.datetime.now() - dt.timedelta(days=days)
    out = []
    for s in data.get("sessions") or []:
        st = _local_dt(s.get("startedAt") or s.get("started_at"))
        if st is None or st < since:
            continue
        fin = _local_dt(s.get("finishedAt") or s.get("finished_at"))
        secs = s.get("durationSec")
        mins = float(secs) / 60 if isinstance(secs, (int, float)) else ((fin - st).total_seconds() / 60 if fin else 0)
        name = str(s.get("exerciseName") or s.get("exercise") or "")
        sets = s.get("sets") or []
        sets_summary = ", ".join(f"{x.get('reps', '?')} x {x.get('weight', '?')}{x.get('unit', '')}" for x in sets if isinstance(x, dict))
        out.append({"kind": _kind_from_session({"category": s.get("category"), "exercise": name}), "exercise": name,
                    "category": str(s.get("category") or ""), "date": st.date().isoformat(), "weekday": st.strftime("%a"),
                    "minutes": int(round(mins)), "started_at": st.isoformat(timespec="seconds"),
                    "finished_at": fin.isoformat(timespec="seconds") if fin else "", "sets_summary": sets_summary,
                    "notes": str(s.get("notes") or ""), "source": "sheet"})
    out.sort(key=lambda h: h["started_at"], reverse=True)
    return out


# ---- checklist rows that are daily routines with their own feed card ------------------------------------
def card_items() -> list[dict]:
    """[sheets] card_items lines ("Anki review: 20") as [{title, minutes}]; minutes default to 20."""
    out = []
    for line in config.load()["sheets"].get("card_items") or []:
        line = str(line).strip()
        if not line:
            continue
        title, _, mins = line.rpartition(":")
        if not title or not mins.strip().isdigit():
            title, mins = line, "20"
        out.append({"title": title.strip(), "minutes": max(5, int(mins.strip()))})
    for section in ("reading", "writing"):       # the daily reading and writing: cards of their own, suggesting what to read or write
        r = config.load().get(section) or {}
        title = str(r.get("routine") or "").strip()
        if title and not any(c["title"].lower().startswith(title.lower()) or title.lower().startswith(c["title"].lower()) for c in out):
            out.append({"title": title, "minutes": max(5, int(r.get("minutes") or 30))})
    return out


def routines_for_today() -> list[dict]:
    """Today's routine cards: the configured items, minus any already ticked in the sheet or logged here today.
    Items are matched to sheet rows loosely (case, spaces); an item the sheet does not list is still scheduled."""
    rows = {r["item"].strip().lower(): r for r in daily_checklist()["items"]}
    done = {x.strip().lower() for x in db.checklist_done_today(db.today())}
    out = []
    for c in card_items():
        key = c["title"].strip().lower()
        row = rows.get(key) or next((r for k, r in rows.items() if k.startswith(key) or key.startswith(k)), None)
        if row:
            c = {**c, "title": row["item"].strip(), "done": bool(row.get("done_today")) or row["item"].strip().lower() in done}
        else:
            c = {**c, "done": key in done}
        if not c["done"]:
            out.append(c)
    return out
