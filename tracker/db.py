"""SQLite storage. One connection per thread; every write commits immediately."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import time
from typing import Any, Iterable

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES projects(id),
    name TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    archived_at REAL,
    linear_key TEXT                                  -- linear project/team id this mirrors, if any
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id),
    parent_id INTEGER REFERENCES tasks(id),          -- sub-task of another task, if any
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    deadline TEXT,                                   -- ISO date (YYYY-MM-DD) or datetime
    blocking INTEGER NOT NULL DEFAULT 0,             -- 1 = someone is waiting on this
    blocking_for TEXT NOT NULL DEFAULT '',
    importance INTEGER,                              -- 1 (low) .. 5 (critical); NULL = no particular priority
    estimate_minutes INTEGER,
    chunkable INTEGER NOT NULL DEFAULT 1,            -- 0 = must be done in one sitting
    position INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    completed_at REAL,
    source TEXT NOT NULL DEFAULT 'local',            -- local | quick_add | linear
    linear_id TEXT UNIQUE,
    linear_identifier TEXT,
    linear_url TEXT,
    linear_state TEXT,
    linear_team_id TEXT,
    linear_updated_at TEXT
);
CREATE INDEX IF NOT EXISTS tasks_project ON tasks(project_id, completed_at);
CREATE TABLE IF NOT EXISTS time_entries (
    id INTEGER PRIMARY KEY,
    task_id INTEGER REFERENCES tasks(id),
    kind TEXT NOT NULL DEFAULT 'task',               -- task | exercise | journal | checklist
    label TEXT NOT NULL DEFAULT '',
    toggl_id INTEGER,
    started_at REAL NOT NULL,
    stopped_at REAL
);
CREATE INDEX IF NOT EXISTS time_entries_task ON time_entries(task_id);
CREATE TABLE IF NOT EXISTS plans (
    date TEXT PRIMARY KEY,
    built_at REAL NOT NULL,
    work_start TEXT NOT NULL,
    work_end TEXT NOT NULL,
    exercise TEXT NOT NULL DEFAULT '',
    rationale TEXT NOT NULL DEFAULT '',
    raw TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS plan_items (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,                              -- task | meeting | checklist | exercise | journal | break
    task_id INTEGER REFERENCES tasks(id),
    title TEXT NOT NULL,
    start TEXT NOT NULL,                             -- HH:MM local
    end TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',          -- pending | active | done | skipped
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS plan_items_date ON plan_items(date, position);
CREATE TABLE IF NOT EXISTS checklist_log (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    item TEXT NOT NULL,
    done_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_local = threading.local()
_write_lock = threading.RLock()


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        _migrate(conn)
        _local.conn = conn
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring older databases up to the current tasks schema (sub-tasks, optional importance)."""
    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(tasks)")}
    if "parent_id" not in cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN parent_id INTEGER REFERENCES tasks(id)")
        conn.commit()
        cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(tasks)")}
    if cols["importance"]["notnull"]:
        # SQLite cannot drop NOT NULL in place: rebuild the table with the current definition.
        body = SCHEMA.split("CREATE TABLE IF NOT EXISTS tasks (")[1].split("\n);")[0]
        names = [c for c in cols]
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute(f"CREATE TABLE tasks_new ({body})")
            conn.execute(f"INSERT INTO tasks_new({','.join(names)}) SELECT {','.join(names)} FROM tasks")
            conn.execute("DROP TABLE tasks")
            conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
            conn.execute("CREATE INDEX IF NOT EXISTS tasks_project ON tasks(project_id, completed_at)")
            conn.commit()
        finally:
            conn.execute("PRAGMA foreign_keys=ON")


def rows(sql: str, args: Iterable = ()) -> list[dict]:
    return [dict(r) for r in connect().execute(sql, tuple(args)).fetchall()]


def row(sql: str, args: Iterable = ()) -> dict | None:
    r = connect().execute(sql, tuple(args)).fetchone()
    return dict(r) if r else None


def execute(sql: str, args: Iterable = ()) -> int:
    with _write_lock:
        cur = connect().execute(sql, tuple(args))
        connect().commit()
        return cur.lastrowid


def today() -> str:
    return dt.date.today().isoformat()


# ---------------------------------------------------------------- settings
def get_setting(key: str, default: Any = None) -> Any:
    r = row("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value: Any) -> None:
    execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)))


# ---------------------------------------------------------------- projects
def _next_position(table: str, where: str, args: tuple) -> int:
    r = row(f"SELECT COALESCE(MAX(position),-1)+1 AS p FROM {table} WHERE {where}", args)
    return int(r["p"]) if r else 0


def create_project(name: str, parent_id: int | None = None, notes: str = "", linear_key: str | None = None) -> int:
    pos = _next_position("projects", "parent_id IS ?" if parent_id is None else "parent_id=?", (parent_id,))
    return execute(
        "INSERT INTO projects(parent_id,name,notes,position,created_at,linear_key) VALUES(?,?,?,?,?,?)",
        (parent_id, name.strip(), notes, pos, time.time(), linear_key),
    )


def update_project(pid: int, **fields) -> None:
    allowed = {"name", "notes", "parent_id", "archived_at", "linear_key"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    execute(f"UPDATE projects SET {', '.join(k+'=?' for k in sets)} WHERE id=?", (*sets.values(), pid))


def reorder_projects(parent_id: int | None, ids: list[int]) -> None:
    with _write_lock:
        for i, pid in enumerate(ids):
            execute("UPDATE projects SET position=?, parent_id=? WHERE id=?", (i, parent_id, pid))


def project_descendants(pid: int) -> list[int]:
    """Ids of every project below `pid` (any depth), archived or not."""
    out, frontier = [], [pid]
    while frontier:
        kids = rows(f"SELECT id FROM projects WHERE parent_id IN ({','.join('?'*len(frontier))})", frontier)
        frontier = [k["id"] for k in kids]
        out += frontier
    return out


def delete_empty_project(pid: int) -> None:
    """Hard-delete a project that has nothing in it (no tasks ever, no sub-projects). Used to undo creating one."""
    if row("SELECT 1 FROM tasks WHERE project_id=? LIMIT 1", (pid,)):
        raise ValueError("it has tasks; archive it instead")
    if row("SELECT 1 FROM projects WHERE parent_id=? LIMIT 1", (pid,)):
        raise ValueError("it has sub-projects; archive it instead")
    execute("DELETE FROM projects WHERE id=?", (pid,))


def archive_project(pid: int) -> None:
    """Archive a project with everything in it: its sub-projects go with it; open tasks stay open but are
    out of the schedule until the project is restored."""
    when = time.time()
    with _write_lock:
        for i in [pid, *project_descendants(pid)]:
            execute("UPDATE projects SET archived_at=? WHERE id=? AND archived_at IS NULL", (when, i))


def restore_project(pid: int) -> None:
    """Restore a project, its ancestors (so it is reachable) and the sub-projects archived together with it."""
    p = row("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        return
    with _write_lock:
        when = p["archived_at"]
        execute("UPDATE projects SET archived_at=NULL WHERE id=?", (pid,))
        if when is not None:
            ids = project_descendants(pid)
            if ids:
                execute(f"UPDATE projects SET archived_at=NULL WHERE archived_at=? AND id IN ({','.join('?'*len(ids))})", (when, *ids))
        parent = p["parent_id"]
        seen = set()
        while parent is not None and parent not in seen:
            seen.add(parent)
            execute("UPDATE projects SET archived_at=NULL WHERE id=?", (parent,))
            parent = (row("SELECT parent_id FROM projects WHERE id=?", (parent,)) or {}).get("parent_id")


def projects(include_archived: bool = False) -> list[dict]:
    q = "SELECT * FROM projects" + ("" if include_archived else " WHERE archived_at IS NULL") + " ORDER BY position, id"
    return rows(q)


def project_tree(include_archived: bool = False, with_tasks: bool = True,
                 active: bool = True, completed: bool = False) -> list[dict]:
    """Projects as a tree. `include_archived` adds archived projects; `active`/`completed` pick which tasks
    are attached (any combination). Completed tasks come newest-first after the active ones."""
    ps = projects(include_archived)
    by_id = {p["id"]: dict(p, children=[], tasks=[]) for p in ps}
    roots: list[dict] = []
    for p in by_id.values():
        parent = by_id.get(p["parent_id"]) if p["parent_id"] is not None else None
        (parent["children"] if parent else roots).append(p)
    if with_tasks:
        tasks: list[dict] = []
        if active:
            tasks += open_tasks()
        if completed:
            tasks += completed_tasks(None, 5000)
        by_tid = {t["id"]: dict(t, children=[]) for t in tasks}
        for t in by_tid.values():
            parent = by_tid.get(t["parent_id"]) if t["parent_id"] else None
            if parent is not None:
                parent["children"].append(t)
            elif t["project_id"] in by_id:
                by_id[t["project_id"]]["tasks"].append(t)   # orphans (hidden parent) show at project level
    return roots


def project_path(pid: int) -> list[str]:
    names: list[str] = []
    seen = set()
    while pid is not None and pid not in seen:
        seen.add(pid)
        p = row("SELECT id,parent_id,name FROM projects WHERE id=?", (pid,))
        if not p:
            break
        names.append(p["name"])
        pid = p["parent_id"]
    return list(reversed(names))


def find_project_by_path(path: list[str]) -> int | None:
    parent = None
    for name in path:
        p = row("SELECT id FROM projects WHERE parent_id IS ? AND lower(name)=lower(?) AND archived_at IS NULL",
                (parent, name))
        if not p:
            return None
        parent = p["id"]
    return parent


def ensure_project_path(path: list[str]) -> int:
    parent = None
    for name in path:
        p = row("SELECT id FROM projects WHERE parent_id IS ? AND lower(name)=lower(?) AND archived_at IS NULL",
                (parent, name))
        parent = p["id"] if p else create_project(name, parent)
    assert parent is not None
    return parent


# ---------------------------------------------------------------- tasks
TASK_FIELDS = {"title", "description", "deadline", "blocking", "blocking_for", "importance",
               "estimate_minutes", "chunkable", "project_id", "parent_id", "position", "completed_at",
               "linear_id", "linear_identifier", "linear_url", "linear_state", "linear_team_id",
               "linear_updated_at", "source"}


def _sibling_where(project_id: int, parent_id: int | None) -> tuple[str, tuple]:
    return "project_id=? AND completed_at IS NULL AND parent_id IS ?", (project_id, parent_id)


def _insert_position(project_id: int, parent_id: int | None, after_id: int | None) -> int:
    """Position for a new/moved sibling: right after `after_id` (shifting the rest down), first when
    `after_id` is 0, or at the end."""
    where, args = _sibling_where(project_id, parent_id)
    if after_id == 0:
        execute(f"UPDATE tasks SET position=position+1 WHERE {where}", args)
        return 0
    after = task(after_id) if after_id else None
    if after and after["project_id"] == project_id and after["parent_id"] == parent_id and after["completed_at"] is None:
        pos = int(after["position"]) + 1
        execute(f"UPDATE tasks SET position=position+1 WHERE {where} AND position>=?", (*args, pos))
        return pos
    return _next_position("tasks", where, args)


def create_tasks_outline(project_id: int, parent_id: int | None, after_id: int | None, items: list[dict]) -> list[int]:
    """Create a pasted outline. `items` are {title, depth} in document order: depth-0 items become siblings
    starting right after `after_id` (0 = first, None = last); deeper items nest under the nearest shallower
    item before them (depth 1 right after `after_id` nests under `after_id` itself)."""
    last: dict[int, int | None] = {0: after_id}        # last task created (or given) at each depth
    ids: list[int] = []
    for it in items:
        d = max(0, int(it.get("depth", 0)))
        while d > 0 and not last.get(d - 1):            # nothing real to hang off: flatten
            d -= 1
        parent = parent_id if d == 0 else last[d - 1]
        after = last.get(d) if d == 0 or d in last else None
        tid = create_task(project_id, str(it["title"]).strip() or "Untitled", after_id=after, parent_id=parent, **(it.get("fields") or {}))
        for k in [k for k in last if k > d]:
            del last[k]
        last[d] = tid
        ids.append(tid)
    return ids


def create_task(project_id: int, title: str, after_id: int | None = None, **fields) -> int:
    f = {k: v for k, v in fields.items() if k in TASK_FIELDS}
    f.setdefault("chunkable", 1)
    f.setdefault("blocking", 0)
    f.setdefault("description", "")
    f.setdefault("source", "local")
    parent_id = f.get("parent_id")
    if parent_id:
        parent = task(parent_id)
        if not parent:
            raise ValueError("parent task not found")
        project_id = parent["project_id"]          # a sub-task always lives in its parent's project
    else:
        f["parent_id"] = None
    now = time.time()
    with _write_lock:
        pos = _insert_position(project_id, f["parent_id"], after_id)
        cols = ["project_id", "title", "position", "created_at", "updated_at", *f.keys()]
        vals = [project_id, title.strip(), pos, now, now, *f.values()]
        return execute(f"INSERT INTO tasks({','.join(cols)}) VALUES({','.join('?'*len(cols))})", vals)


def update_task(tid: int, **fields) -> None:
    sets = {k: v for k, v in fields.items() if k in TASK_FIELDS}
    if not sets:
        return
    sets["updated_at"] = time.time()
    execute(f"UPDATE tasks SET {', '.join(k+'=?' for k in sets)} WHERE id=?", (*sets.values(), tid))


def task(tid: int) -> dict | None:
    return row("SELECT * FROM tasks WHERE id=?", (tid,))


def task_by_linear_id(lid: str) -> dict | None:
    return row("SELECT * FROM tasks WHERE linear_id=?", (lid,))


def open_tasks(project_id: int | None = None, schedulable_only: bool = False) -> list[dict]:
    """Open tasks. With `schedulable_only`, tasks inside archived projects are left out."""
    q = "SELECT * FROM tasks WHERE completed_at IS NULL"
    args: tuple = ()
    if project_id is not None:
        q += " AND project_id=?"
        args = (project_id,)
    out = rows(q + " ORDER BY position, id", args)
    if schedulable_only:
        live = {p["id"] for p in projects()}
        out = [t for t in out if t["project_id"] in live]
    return out


def completed_tasks(project_id: int | None = None, limit: int = 500) -> list[dict]:
    q = "SELECT * FROM tasks WHERE completed_at IS NOT NULL"
    args: list = []
    if project_id is not None:
        q += " AND project_id=?"
        args.append(project_id)
    q += " ORDER BY completed_at DESC LIMIT ?"
    args.append(limit)
    return rows(q, args)


def complete_task(tid: int, when: float | None = None) -> None:
    update_task(tid, completed_at=when or time.time())


def uncomplete_task(tid: int) -> None:
    t = task(tid)
    if not t:
        return
    parent = task(t["parent_id"]) if t["parent_id"] else None
    parent_id = t["parent_id"] if parent and parent["completed_at"] is None else None
    pos = _next_position("tasks", *_sibling_where(t["project_id"], parent_id))
    update_task(tid, completed_at=None, position=pos, parent_id=parent_id)


def subtasks(tid: int, open_only: bool = True) -> list[dict]:
    q = "SELECT * FROM tasks WHERE parent_id=?" + (" AND completed_at IS NULL" if open_only else "")
    return rows(q + " ORDER BY position, id", (tid,))


def is_task_descendant(ancestor_id: int, tid: int) -> bool:
    """True if `tid` is `ancestor_id` itself or sits anywhere below it."""
    seen = set()
    while tid is not None and tid not in seen:
        if tid == ancestor_id:
            return True
        seen.add(tid)
        t = task(tid)
        tid = t["parent_id"] if t else None
    return False


def reorder_tasks(project_id: int, ids: list[int], parent_id: int | None = None) -> None:
    with _write_lock:
        for i, tid in enumerate(ids):
            if parent_id and is_task_descendant(tid, parent_id):
                continue
            execute("UPDATE tasks SET position=?, project_id=?, parent_id=?, updated_at=? WHERE id=?",
                    (i, project_id, parent_id, time.time(), tid))
            _move_subtree_project(tid, project_id)


def move_task(tid: int, project_id: int | None = None, parent_id: int | None = None,
              after_id: int | None = None) -> None:
    """Move a task to another project and/or under another task (sub-tasks follow). `after_id` places it
    right after that sibling; otherwise it goes last."""
    t = task(tid)
    if not t:
        return
    if parent_id:
        if is_task_descendant(tid, parent_id):
            raise ValueError("cannot move a task under itself")
        parent = task(parent_id)
        if not parent:
            raise ValueError("parent task not found")
        project_id = parent["project_id"]
    if project_id is None:
        project_id = t["project_id"]
    with _write_lock:
        pos = _insert_position(project_id, parent_id, after_id)
        update_task(tid, project_id=project_id, parent_id=parent_id, position=pos)
        _move_subtree_project(tid, project_id)


def _move_subtree_project(tid: int, project_id: int) -> None:
    for c in subtasks(tid, open_only=False):
        if c["project_id"] != project_id:
            update_task(c["id"], project_id=project_id)
        _move_subtree_project(c["id"], project_id)


def delete_task(tid: int) -> None:
    """Remove a task outright (Backspace on an emptied title). Only for tasks with nothing attached:
    anything with sub-tasks, tracked time or a Linear link should be completed instead."""
    t = task(tid)
    if not t:
        return
    if subtasks(tid, open_only=False):
        raise ValueError("it has sub-tasks")
    if t["linear_id"]:
        raise ValueError("it is mirrored from Linear; complete it instead")
    if row("SELECT id FROM time_entries WHERE task_id=? LIMIT 1", (tid,)):
        raise ValueError("it has tracked time; complete it instead")
    with _write_lock:
        execute("UPDATE plan_items SET task_id=NULL WHERE task_id=?", (tid,))
        execute("DELETE FROM tasks WHERE id=?", (tid,))


def task_to_project(tid: int) -> int:
    """Turn a task into a sub-project of its project. Direct sub-tasks become the new project's tasks
    (their own sub-tasks follow them). Time entries and plan items keep their label but lose the task link."""
    t = task(tid)
    if not t:
        raise ValueError("task not found")
    if t["linear_id"]:
        raise ValueError("a Linear-mirrored task cannot become a project")
    with _write_lock:
        pid = create_project(t["title"], t["project_id"], notes=t["description"])
        execute("UPDATE projects SET created_at=? WHERE id=?", (t["created_at"], pid))
        for i, c in enumerate(subtasks(tid, open_only=False)):
            execute("UPDATE tasks SET project_id=?, parent_id=NULL, position=?, updated_at=? WHERE id=?",
                    (pid, i, time.time(), c["id"]))
            _move_subtree_project(c["id"], pid)
        execute("UPDATE time_entries SET task_id=NULL WHERE task_id=?", (tid,))
        execute("UPDATE plan_items SET task_id=NULL WHERE task_id=?", (tid,))
        execute("DELETE FROM tasks WHERE id=?", (tid,))
    return pid


def project_to_task(pid: int) -> int:
    """Turn a project into a task of its parent project; its tasks become sub-tasks. Needs a parent project
    and no live sub-projects (archived ones move up to the parent)."""
    p = row("SELECT * FROM projects WHERE id=?", (pid,))
    if not p:
        raise ValueError("project not found")
    if p["parent_id"] is None:
        raise ValueError("a top-level project has no project to become a task in")
    if rows("SELECT id FROM projects WHERE parent_id=? AND archived_at IS NULL", (pid,)):
        raise ValueError("convert or move its sub-projects first")
    with _write_lock:
        tid = create_task(p["parent_id"], p["name"], description=p["notes"] or "")
        execute("UPDATE tasks SET created_at=? WHERE id=?", (p["created_at"], tid))
        for i, t in enumerate(rows("SELECT id FROM tasks WHERE project_id=? ORDER BY completed_at IS NOT NULL, position, id", (pid,))):
            execute("UPDATE tasks SET project_id=?, parent_id=COALESCE(parent_id, ?), position=?, updated_at=? WHERE id=?",
                    (p["parent_id"], tid, i, time.time(), t["id"]))
        # sub-tasks of those tasks kept their parent_id; only their project changes
        execute("UPDATE tasks SET project_id=? WHERE project_id=?", (p["parent_id"], pid))
        execute("UPDATE projects SET parent_id=? WHERE parent_id=?", (p["parent_id"], pid))
        execute("DELETE FROM projects WHERE id=?", (pid,))
    return tid


# ---------------------------------------------------------------- time entries
def start_entry(kind: str, label: str, task_id: int | None = None, toggl_id: int | None = None) -> int:
    return execute("INSERT INTO time_entries(task_id,kind,label,toggl_id,started_at) VALUES(?,?,?,?,?)",
                   (task_id, kind, label, toggl_id, time.time()))


def running_entry() -> dict | None:
    return row("SELECT * FROM time_entries WHERE stopped_at IS NULL ORDER BY started_at DESC LIMIT 1")


def stop_entry(eid: int, when: float | None = None) -> None:
    execute("UPDATE time_entries SET stopped_at=? WHERE id=? AND stopped_at IS NULL", (when or time.time(), eid))


def stop_all_entries() -> None:
    execute("UPDATE time_entries SET stopped_at=? WHERE stopped_at IS NULL", (time.time(),))


def task_seconds(tid: int) -> float:
    r = row("SELECT COALESCE(SUM(COALESCE(stopped_at, strftime('%s','now')) - started_at),0) AS s "
            "FROM time_entries WHERE task_id=?", (tid,))
    return float(r["s"]) if r else 0.0


def last_exercise_entries(n: int = 10) -> list[dict]:
    return rows("SELECT * FROM time_entries WHERE kind='exercise' ORDER BY started_at DESC LIMIT ?", (n,))


# ---------------------------------------------------------------- plans
def save_plan(date: str, work_start: str, work_end: str, exercise: str, rationale: str,
              items: list[dict], raw: dict, keep_statuses: bool = True) -> None:
    with _write_lock:
        old = {}
        if keep_statuses:
            for it in rows("SELECT * FROM plan_items WHERE date=?", (date,)):
                key = (it["kind"], it["task_id"], it["title"])
                if it["status"] in ("done", "skipped", "active"):
                    old[key] = it["status"]
        execute("DELETE FROM plan_items WHERE date=?", (date,))
        execute("INSERT INTO plans(date,built_at,work_start,work_end,exercise,rationale,raw) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(date) DO UPDATE SET built_at=excluded.built_at, work_start=excluded.work_start, "
                "work_end=excluded.work_end, exercise=excluded.exercise, rationale=excluded.rationale, raw=excluded.raw",
                (date, time.time(), work_start, work_end, exercise, rationale, json.dumps(raw)))
        for i, it in enumerate(items):
            status = it.get("status") or old.get((it["kind"], it.get("task_id"), it["title"]), "pending")
            execute("INSERT INTO plan_items(date,position,kind,task_id,title,start,end,minutes,status,note) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (date, i, it["kind"], it.get("task_id"), it["title"], it["start"], it["end"],
                     int(it["minutes"]), status, it.get("note", "")))


def plan(date: str) -> dict | None:
    p = row("SELECT * FROM plans WHERE date=?", (date,))
    if not p:
        return None
    p["raw"] = json.loads(p["raw"] or "{}")
    p["items"] = plan_items(date)
    return p


def plan_items(date: str) -> list[dict]:
    return rows("SELECT pi.*, t.description AS task_description, t.deadline, t.blocking, t.blocking_for, "
                "t.importance, t.estimate_minutes, t.chunkable, t.project_id, t.linear_url, t.linear_identifier, "
                "t.completed_at AS task_completed_at "
                "FROM plan_items pi LEFT JOIN tasks t ON t.id=pi.task_id WHERE pi.date=? ORDER BY pi.position", (date,))


def plan_item(iid: int) -> dict | None:
    return row("SELECT * FROM plan_items WHERE id=?", (iid,))


def set_item_status(iid: int, status: str) -> None:
    execute("UPDATE plan_items SET status=? WHERE id=?", (status, iid))


def checklist_done_today(date: str) -> set[str]:
    return {r["item"] for r in rows("SELECT item FROM checklist_log WHERE date=?", (date,))}


def log_checklist(date: str, item: str) -> None:
    execute("INSERT INTO checklist_log(date,item,done_at) VALUES(?,?,?)", (date, item, time.time()))
