"""A daily Markdown snapshot of the whole outline, so the history of the to-do list is kept as plain files.

One file per day, `<data folder>/snapshots/YYYY-MM-DD.md` (or `[snapshots] dir`), written at `[snapshots] time`
(05:00 by default) by the background loop. If the app was not running at that hour, the day's file is written as
soon as it is, so a day is only missed when the app never ran that day. `python -m tracker snapshot` or
POST /api/snapshot write one on demand (overwriting today's).
"""
from __future__ import annotations

import datetime as dt
import logging
import pathlib
import re
import time

from . import config, db

log = logging.getLogger(__name__)


def snapshot_dir() -> pathlib.Path:
    d = config.load()["snapshots"]["dir"]
    return pathlib.Path(d).expanduser() if d else config.APP_SUPPORT / "snapshots"


def _tags(t: dict) -> str:
    tags = []
    if t.get("importance"):
        tags.append(f"#p{6 - int(t['importance'])}")
    if t.get("deadline"):
        tags.append(f"#due:{str(t['deadline'])[:10]}")
    if t.get("blocking"):
        tags.append("#blocking" + (f":{t['blocking_for']}" if t.get("blocking_for") else ""))
    if t.get("chunkable") == 0:
        tags.append("#onesitting")
    return (" " + " ".join(tags)) if tags else ""


def _notes(text: str | None, depth: int, out: list[str]) -> None:
    for line in (text or "").strip().splitlines():
        out.append("  " * depth + "  " + line.rstrip())


def render(now: dt.datetime | None = None, since: float | None = None) -> str:
    """The outline as an indented Markdown list: projects bold, tasks as checkboxes with their tags, notes
    indented underneath, then the tasks completed since the previous snapshot."""
    now = now or dt.datetime.now()
    tree = db.project_tree(include_archived=False, active=True, completed=False)
    out = [f"# Task tracker snapshot — {now:%A %Y-%m-%d %H:%M}", ""]

    def task(t: dict, depth: int) -> None:
        out.append("  " * depth + "- [ ] " + re.sub(r"^#{1,6}\s+", "", t["title"].strip()) + _tags(t))
        _notes(t.get("description"), depth, out)
        for c in t["children"]:
            task(c, depth + 1)

    def project(p: dict, depth: int) -> None:
        name = re.sub(r"^#{1,6}\s+", "", p["name"].strip())
        out.append("  " * depth + f"- **{name}**")
        _notes(p.get("notes"), depth, out)
        for t in p["tasks"]:
            task(t, depth + 1)
        for c in p["children"]:
            project(c, depth + 1)

    for p in tree:
        project(p, 0)
    if not tree:
        out.append("_(no projects)_")

    if since is None:
        since = float(db.get_setting("snapshot_last") or 0) or (now.timestamp() - 86400)
    done = [t for t in db.completed_tasks(None, 5000) if (t["completed_at"] or 0) >= since]
    if done:
        out += ["", f"## Completed since {dt.datetime.fromtimestamp(since):%Y-%m-%d %H:%M}", ""]
        for t in done:
            path = " / ".join(db.project_path(t["project_id"]))
            out.append(f"- [x] {t['title'].strip()}  _({path})_")
    return "\n".join(out) + "\n"


def write(now: dt.datetime | None = None, force: bool = True) -> pathlib.Path | None:
    """Write today's snapshot; with `force` false, leave an existing file alone (returns None)."""
    now = now or dt.datetime.now()
    d = snapshot_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{now:%Y-%m-%d}.md"
    if path.exists() and not force:
        return None
    path.write_text(render(now))
    db.set_setting("snapshot_last", str(time.time()))
    log.info("snapshot written: %s", path)
    return path


def maybe_snapshot(now: dt.datetime | None = None) -> pathlib.Path | None:
    """Called every minute by the background loop: write the day's file once the snapshot time has passed."""
    cfg = config.load()["snapshots"]
    if not cfg.get("enable", True):
        return None
    now = now or dt.datetime.now()
    try:
        h, m = (int(x) for x in str(cfg["time"]).split(":"))
    except ValueError:
        h, m = 5, 0
    if (now.hour, now.minute) < (h, m):
        return None
    return write(now, force=False)


def list_snapshots() -> list[dict]:
    """Newest first: {date, size, modified}."""
    d = snapshot_dir()
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("????-??-??.md"), reverse=True):
        st = f.stat()
        out.append({"date": f.stem, "size": st.st_size, "modified": st.st_mtime})
    return out


def read(date: str) -> str | None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        return None
    f = snapshot_dir() / f"{date}.md"
    return f.read_text() if f.exists() else None
