"""Calendar events from one or more iCal URLs (Google Calendar's secret iCal address works without OAuth)."""
from __future__ import annotations

import datetime as dt
import logging
import time
import urllib.request

import icalendar
import recurring_ical_events

from . import config

log = logging.getLogger("calendar")
_cache: dict[str, tuple[float, bytes]] = {}


def _fetch(url: str, max_age: float = 600) -> bytes | None:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "task-tracker"}), timeout=30) as r:
            data = r.read()
        _cache[url] = (time.time(), data)
        return data
    except Exception as e:
        log.warning("calendar fetch failed (%s): %s", url[:60], e)
        return hit[1] if hit else None


def events_for(day: dt.date) -> list[dict]:
    """Timed events on `day` in local time: [{title, start: datetime, end: datetime, all_day}]."""
    cfg = config.load()["calendar"]
    out: list[dict] = []
    local_tz = dt.datetime.now().astimezone().tzinfo
    ignore = [s.lower() for s in cfg.get("ignore_titles", [])]
    for url in cfg.get("ics_urls", []):
        data = _fetch(url)
        if not data:
            continue
        try:
            cal = icalendar.Calendar.from_ical(data)
            evs = recurring_ical_events.of(cal).at(day)
        except Exception as e:
            log.warning("calendar parse failed: %s", e)
            continue
        for ev in evs:
            title = str(ev.get("SUMMARY", "")).strip() or "(busy)"
            if any(s in title.lower() for s in ignore):
                continue
            if cfg.get("busy_only", True) and str(ev.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT":
                continue
            if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                continue
            start = ev.get("DTSTART").dt
            end = ev.get("DTEND").dt if ev.get("DTEND") else start
            all_day = not isinstance(start, dt.datetime)
            if all_day:
                out.append({"title": title, "start": None, "end": None, "all_day": True})
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=local_tz)
            if end.tzinfo is None:
                end = end.replace(tzinfo=local_tz)
            start, end = start.astimezone(local_tz), end.astimezone(local_tz)
            if end <= start:
                end = start + dt.timedelta(minutes=30)
            out.append({"title": title, "start": start, "end": end, "all_day": False})
    out.sort(key=lambda e: (e["all_day"], e["start"] or dt.datetime.min.replace(tzinfo=local_tz)))
    return out


def check_feeds(day: dt.date | None = None) -> list[dict]:
    """For the Settings page's connection test: what each configured feed actually returns."""
    day = day or dt.date.today()
    out: list[dict] = []
    for url in config.load()["calendar"].get("ics_urls", []):
        label = url.split("//", 1)[-1].split("/", 1)[0] + "/…/" + url.rstrip("/").rsplit("/", 1)[-1][:24]
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "task-tracker"}), timeout=30) as r:
                data = r.read()
        except Exception as e:
            out.append({"url": label, "ok": False, "error": f"unreachable: {e}"})
            continue
        if data.lstrip()[:1] == b"<" or b"BEGIN:VCALENDAR" not in data[:4000]:
            out.append({"url": label, "ok": False, "error": "returns a web page, not calendar data. Use the calendar's "
                        "\"Secret address in iCal format\" from Google Calendar settings (it ends in basic.ics)"})
            continue
        try:
            n = len(recurring_ical_events.of(icalendar.Calendar.from_ical(data)).at(day))
            _cache[url] = (time.time(), data)
            out.append({"url": label, "ok": True, "events_today": n})
        except Exception as e:
            out.append({"url": label, "ok": False, "error": f"could not parse: {e}"})
    return out
