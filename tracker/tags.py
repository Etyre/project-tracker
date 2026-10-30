"""Inline hashtags typed into a task title, Workflowy / Roam style.

    #p1 … #p5             priority, #p1 highest (importance 5 … 1)
    #today  #tomorrow     deadline
    #due:fri  #due:nov3  #due:2026-11-03  #due:3nov  #due:11-03
    #blocking  #blocking:Sam
    #onesitting           cannot be split across sittings

Known tags are removed from the title and become fields; anything else starting with # stays as text.
A line that *starts* with hashes and a space is a Markdown heading, not a tag, and is left alone.
"""
from __future__ import annotations

import datetime as dt
import re

PRIORITY = {"p1": 5, "p2": 4, "p3": 3, "p4": 2, "p5": 1}
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
TAG = re.compile(r"(?<!\S)#([A-Za-z][\w-]*)(?::(\S+))?")


def parse_date(spec: str, today: dt.date | None = None) -> str | None:
    """A short date: today, tomorrow, a weekday (next one, today counts), nov3 / 3nov / nov-3, 11-03, 2026-11-03."""
    today = today or dt.date.today()
    s = spec.strip().lower().replace("_", "-")
    if s in ("today", "tod"):
        return today.isoformat()
    if s in ("tomorrow", "tom", "tmrw"):
        return (today + dt.timedelta(days=1)).isoformat()
    for i, w in enumerate(WEEKDAYS):
        if s.startswith(w):
            return (today + dt.timedelta(days=(i - today.weekday()) % 7)).isoformat()
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s):
        y, mo, d = map(int, m.groups())
    elif m := re.fullmatch(r"(\d{1,2})[-/](\d{1,2})", s):
        mo, d = map(int, m.groups()); y = None
    elif m := re.fullmatch(r"([a-z]{3,})-?(\d{1,2})", s) or re.fullmatch(r"(\d{1,2})-?([a-z]{3,})", s):
        a, b = m.groups()
        name, d = (a, int(b)) if a.isalpha() else (b, int(a))
        if name[:3] not in MONTHS:
            return None
        mo, y = MONTHS.index(name[:3]) + 1, None
    elif m := re.fullmatch(r"\+?(\d+)d", s):
        return (today + dt.timedelta(days=int(m.group(1)))).isoformat()
    else:
        return None
    try:
        if y is None:   # the next such date
            y = today.year
            if dt.date(y, mo, d) < today:
                y += 1
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def parse_tags(title: str, today: dt.date | None = None) -> tuple[str, dict]:
    """Return (clean title, fields). Fields only contain what the tags said."""
    fields: dict = {}
    heading = re.match(r"^#{1,6}\s", title)

    def take(m: re.Match) -> str:
        if heading and m.start() == 0:
            return m.group(0)
        name, arg = m.group(1).lower(), m.group(2)
        if name in PRIORITY and not arg:
            fields["importance"] = PRIORITY[name]
        elif name in ("today", "tomorrow") and not arg:
            fields["deadline"] = parse_date(name, today)
        elif name == "due" and arg:
            d = parse_date(arg, today)
            if d is None:
                return m.group(0)
            fields["deadline"] = d
        elif name in ("blocking", "blocks"):
            fields["blocking"] = 1
            if arg:
                fields["blocking_for"] = arg.strip('"\'')
        elif name == "onesitting" and not arg:
            fields["chunkable"] = 0
        else:
            return m.group(0)
        return ""

    clean = TAG.sub(take, title)
    clean = re.sub(r"[ \t]{2,}", " ", clean).strip()
    return clean or title.strip(), fields
