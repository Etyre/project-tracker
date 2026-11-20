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
# A fetch that failed is not retried for a few minutes. Without this every feed poll retried at once, and Google
# answered the bursts with HTTP 429 for hours (398 of them in the log by 2026-11-20). Same rule as sheets._get.
_failed_at: dict[str, float] = {}
RETRY_AFTER = 300


def _fetch(url: str, max_age: float = 600, force: bool = False) -> bytes | None:
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    if not force and time.time() - _failed_at.get(url, 0) < RETRY_AFTER:
        return hit[1] if hit else None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "task-tracker"}), timeout=30) as r:
            data = r.read()
        _cache[url] = (time.time(), data)
        _failed_at.pop(url, None)
        return data
    except Exception as e:
        log.warning("calendar fetch failed (%s): %s", url[:60], e)
        _failed_at[url] = time.time()
        return hit[1] if hit else None


def prefetch() -> None:
    """Fetch every feed afresh now (see sheets.prefetch)."""
    for url in config.load()["calendar"].get("ics_urls", []):
        _fetch(url, 0, force=True)


def events_for(day: dt.date) -> list[dict]:
    """Timed events on `day` in local time: [{title, start: datetime, end: datetime, all_day}].

    The day is the local day. `.at(day)` alone matches in each event's own time zone, so an evening event that
    Google exports in UTC (20:00 here is 01:00Z tomorrow) was missing today and then blocked all of tomorrow; so
    the local day is also asked for as a span of instants, and timed events are kept only if they overlap it.
    Each time is converted with its own offset, not today's, so dates across a clock change are right."""
    cfg = config.load()["calendar"]
    out: list[dict] = []
    day_start = dt.datetime.combine(day, dt.time()).astimezone()
    day_end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time()).astimezone()
    seen: set = set()
    ignore = [s.lower() for s in cfg.get("ignore_titles", [])]
    for url in cfg.get("ics_urls", []):
        data = _fetch(url)
        if not data:
            continue
        try:
            cal = icalendar.Calendar.from_ical(data)
            q = recurring_ical_events.of(cal)
            evs = list(q.at(day)) + list(q.between(day_start, day_end))
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
                last = end - dt.timedelta(days=1) if end > start else start       # DTEND of an all-day event is the day after
                if not (start <= day <= last) or (str(ev.get("UID", "")), title, start) in seen:
                    continue
                seen.add((str(ev.get("UID", "")), title, start))
                out.append({"title": title, "start": None, "end": None, "all_day": True})
                continue
            start, end = start.astimezone(), end.astimezone()      # a time without a zone is local time
            if end <= start:
                end = start + dt.timedelta(minutes=30)
            if end <= day_start or start >= day_end or (str(ev.get("UID", "")), title, start) in seen:
                continue
            seen.add((str(ev.get("UID", "")), title, start))
            out.append({"title": title, "start": start, "end": end, "all_day": False})
    out.sort(key=lambda e: (e["all_day"], e["start"] or day_start))
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
            _failed_at.pop(url, None)
            out.append({"url": label, "ok": True, "events_today": n})
        except Exception as e:
            out.append({"url": label, "ok": False, "error": f"could not parse: {e}"})
    return out
