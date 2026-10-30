"""Google Sheets: daily checklist (read + write back) and exercise log (read).

Two transports:
  * Apps Script web app (apps_script/Code.gs) - reads both sheets as JSON and checks items off. Preferred.
  * Plain CSV export URLs - read-only fallback (publish-to-web or export?format=csv on a link-shared sheet).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import time
import urllib.parse
import urllib.request

from . import config

log = logging.getLogger("sheets")
_cache: dict[str, tuple[float, object]] = {}


def _get(url: str, max_age: float) -> str | None:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]  # type: ignore[return-value]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "task-tracker"}), timeout=30) as r:
            text = r.read().decode("utf-8", errors="replace")
        _cache[url] = (time.time(), text)
        return text
    except Exception as e:
        log.warning("sheet fetch failed: %s", e)
        return hit[1] if hit else None  # type: ignore[return-value]


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
