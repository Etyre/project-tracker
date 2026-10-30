import datetime as dt
import os
import pathlib
import tempfile

import pytest

os.environ["ANTHROPIC_API_KEY"] = ""     # force deterministic path in tests
_tmp = tempfile.mkdtemp()

from tracker import config  # noqa: E402

config.APP_SUPPORT = pathlib.Path(_tmp)
config.DB_PATH = pathlib.Path(_tmp) / "test.sqlite3"
config.load.cache_clear()

from tracker import db, scheduler, estimates  # noqa: E402


def setup_module():
    pid = db.create_project("Work")
    sub = db.create_project("Paper", pid)
    db.create_task(sub, "Write intro", importance=4, estimate_minutes=90, chunkable=1)
    db.create_task(sub, "Send draft to Sam", importance=3, blocking=1, blocking_for="Sam", estimate_minutes=20, chunkable=0)
    db.create_task(pid, "Expense report", importance=2, estimate_minutes=30, deadline=dt.date.today().isoformat())
    db.create_task(pid, "Long non-chunkable", importance=5, estimate_minutes=600, chunkable=0)


def test_free_windows_subtract_meetings():
    tz = dt.datetime.now().astimezone().tzinfo
    day = dt.date.today()
    ev = [{"title": "standup", "all_day": False,
           "start": dt.datetime.combine(day, dt.time(10, 0), tz), "end": dt.datetime.combine(day, dt.time(10, 30), tz)}]
    w = scheduler.free_windows(day, ev, 9 * 60, 12 * 60)
    assert w == [(540, 600), (630, 720)]


def test_priority_order():
    cands = scheduler.candidates(dt.date.today())
    titles = [c["title"] for c in cands]
    # blocking + deadline-today outrank plain importance-4; the importance-5 one has no deadline/blocking
    assert titles.index("Send draft to Sam") < titles.index("Write intro")
    assert titles.index("Expense report") < titles.index("Write intro")


def test_greedy_plan_fits_and_splits():
    windows = [(540, 660), (720, 840)]      # 9-11, 12-14
    tasks = scheduler.candidates(dt.date.today())
    items = scheduler.greedy_plan(windows, tasks, checklist_minutes=20, exercise="run", exercise_minutes=45, checklist_done=False)
    for it in items:
        s, e = scheduler.parse_hm(it["start"]), scheduler.parse_hm(it["end"])
        assert any(ws <= s < e <= we for ws, we in windows), it
    # no overlaps
    spans = sorted((scheduler.parse_hm(i["start"]), scheduler.parse_hm(i["end"])) for i in items)
    assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))
    kinds = [i["kind"] for i in items]
    assert "checklist" in kinds and "exercise" in kinds
    assert not any(i["title"] == "Long non-chunkable" for i in items)   # 600 min never fits


def test_validate_clamps_model_output():
    windows = [(540, 660)]
    items = [{"kind": "task", "task_id": 1, "title": "Write intro", "start": "08:30", "end": "09:45", "note": ""},
             {"kind": "task", "task_id": 999, "title": "ghost", "start": "09:45", "end": "10:00", "note": ""},
             {"kind": "break", "task_id": None, "title": "Break", "start": "09:40", "end": "09:50", "note": ""}]
    out = scheduler.validate(items, windows, {1})
    assert [i["title"] for i in out] == ["Write intro", "Break"]
    assert out[0]["start"] == "09:00" and out[1]["start"] == "09:45"


def test_build_and_save_plan_without_llm():
    plan = scheduler.build(dt.date.today())
    assert plan and plan["items"]
    assert plan["raw"]["source"] == "greedy"
    first = next(i for i in plan["items"] if i["kind"] == "task")
    db.set_item_status(first["id"], "done")
    plan2 = scheduler.build(dt.date.today(), from_now=False)
    assert not any(i["task_id"] == first["task_id"] for i in plan2["items"] if i["status"] == "pending")


def test_estimate_calibration_and_completion_archive():
    pid = db.create_project("Calib")
    import time
    for k in range(6):
        tid = db.create_task(pid, f"t{k}", estimate_minutes=30)
        eid = db.start_entry("task", f"t{k}", tid)
        db.execute("UPDATE time_entries SET started_at=?, stopped_at=? WHERE id=?", (time.time() - 3600, time.time(), eid))
        db.complete_task(tid)
    cal = estimates.calibration()
    assert cal["samples"] >= 6 and cal["overall"] == 2.0
    done = db.completed_tasks(pid)
    assert len(done) == 6 and all(t["completed_at"] for t in done)
    assert db.open_tasks(pid) == []
    db.uncomplete_task(done[0]["id"])
    assert len(db.open_tasks(pid)) == 1


def test_reorder_and_tree():
    root = db.create_project("A")
    b = db.create_project("B")
    db.reorder_projects(None, [b, root])
    tree = db.project_tree()
    names = [p["name"] for p in tree]
    assert names.index("B") < names.index("A")


def test_off_track_and_replan_pins_active_block():
    today = dt.date.today()
    plan = scheduler.build(today)
    now = dt.datetime.combine(today, dt.time(12, 0))
    # pretend the first task block is 45 min late
    first = next(i for i in plan["items"] if i["kind"] == "task")
    db.execute("UPDATE plan_items SET start='11:00', end='11:30' WHERE id=?", (first["id"],))
    db.execute("UPDATE plan_items SET status='done' WHERE date=? AND id<>? AND kind<>'meeting' AND position<?",
               (today.isoformat(), first["id"], first["position"]))
    reason = scheduler.off_track(now)
    assert reason and "should have started" in reason
    # with an active block that has not overrun, we are not off track
    db.set_item_status(first["id"], "active")
    db.execute("UPDATE plan_items SET start='11:45', end='12:30' WHERE id=?", (first["id"],))
    assert scheduler.off_track(now) is None
    # a re-plan from now keeps the active block and does not reschedule its task
    plan2 = scheduler.build(today, from_now=True)
    kept = [i for i in plan2["items"] if i["id"] and i["status"] == "active"]
    assert len(kept) == 1 and kept[0]["task_id"] == first["task_id"]
    assert not any(i["task_id"] == first["task_id"] and i["status"] == "pending" for i in plan2["items"])
    for i in plan2["items"]:
        if i["status"] == "pending":
            assert scheduler.parse_hm(i["start"]) >= scheduler.parse_hm(kept[0]["end"])


def test_subtasks_insert_after_and_conversion():
    pid = db.create_project("Nest")
    a = db.create_task(pid, "A")
    c = db.create_task(pid, "C")
    b = db.create_task(pid, "B", after_id=a)                      # inline Enter on A inserts B between A and C
    assert [t["title"] for t in db.open_tasks(pid)] == ["A", "B", "C"]
    assert db.task(a)["importance"] is None                        # no priority by default
    a1 = db.create_task(pid, "A.1", parent_id=a)
    a2 = db.create_task(pid, "A.2", parent_id=a)
    tree = db.project_tree()
    proj = next(p for p in tree if p["id"] == pid)
    assert [t["title"] for t in proj["tasks"]] == ["A", "B", "C"]
    assert [t["title"] for t in proj["tasks"][0]["children"]] == ["A.1", "A.2"]
    # a container task is not scheduled; its sub-tasks are
    titles = {c["title"] for c in scheduler.candidates(dt.date.today())}
    assert "A" not in titles and {"A.1", "A.2", "B", "C"} <= titles
    # outdent A.2 after A, then nest B under C via reorder
    db.move_task(a2, parent_id=None, after_id=a)
    top = [t["title"] for t in db.open_tasks(pid) if t["parent_id"] is None]
    assert top == ["A", "A.2", "B", "C"]
    db.reorder_tasks(pid, [b], parent_id=c)
    assert db.task(b)["parent_id"] == c
    with pytest.raises(ValueError):
        db.move_task(a, parent_id=a1)                              # no cycles
    # task -> project keeps the sub-tasks; project -> task brings them back
    new_pid = db.task_to_project(a)
    assert db.task(a) is None
    assert [t["title"] for t in db.open_tasks(new_pid)] == ["A.1"]
    assert db.project_path(new_pid) == ["Nest", "A"]
    new_tid = db.project_to_task(new_pid)
    assert db.task(new_tid)["title"] == "A" and db.task(a1)["parent_id"] == new_tid
    with pytest.raises(ValueError):
        db.project_to_task(pid)                                    # top-level projects stay projects


def test_migrates_old_schema(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE projects (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES projects(id), name TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '', position INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, archived_at REAL, linear_key TEXT);
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id), title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', deadline TEXT, blocking INTEGER NOT NULL DEFAULT 0, blocking_for TEXT NOT NULL DEFAULT '',
            importance INTEGER NOT NULL DEFAULT 3, estimate_minutes INTEGER, chunkable INTEGER NOT NULL DEFAULT 1,
            position INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL,
            source TEXT NOT NULL DEFAULT 'local', linear_id TEXT UNIQUE, linear_identifier TEXT, linear_url TEXT, linear_state TEXT,
            linear_team_id TEXT, linear_updated_at TEXT);
        INSERT INTO projects(id, name, created_at) VALUES (1, 'Old', 1);
        INSERT INTO tasks(id, project_id, title, created_at, updated_at) VALUES (7, 1, 'kept', 1, 1);
        CREATE TABLE time_entries (id INTEGER PRIMARY KEY, task_id INTEGER REFERENCES tasks(id), kind TEXT NOT NULL DEFAULT 'task',
            label TEXT NOT NULL DEFAULT '', toggl_id INTEGER, started_at REAL NOT NULL, stopped_at REAL);
        INSERT INTO time_entries(task_id, started_at, stopped_at) VALUES (7, 1, 100);
    """)
    con.commit(); con.close()
    monkeypatch.setattr(config, "DB_PATH", path)
    db._local.__dict__.clear()
    assert db.task(7)["title"] == "kept"
    tid = db.create_task(1, "new one", parent_id=7)                # parent_id column exists, importance may be NULL
    assert db.task(tid)["importance"] is None and db.task(tid)["parent_id"] == 7
    assert db.task_seconds(7) == 99


def test_archive_takes_subtree_and_hides_from_schedule():
    top = db.create_project("Archive me")
    sub = db.create_project("Inner", top)
    t = db.create_task(sub, "hidden while archived")
    db.archive_project(top)
    assert db.task(t)["completed_at"] is None
    assert "hidden while archived" not in {c["title"] for c in scheduler.candidates(dt.date.today())}
    assert {p["id"] for p in db.projects()} & {top, sub} == set()
    db.restore_project(sub)                           # restoring the inner one brings its parent back too
    assert {top, sub} <= {p["id"] for p in db.projects()}
    assert "hidden while archived" in {c["title"] for c in scheduler.candidates(dt.date.today())}


def test_insert_first_and_delete_empty_project():
    pid = db.create_project("P")
    a = db.create_task(pid, "a")
    b = db.create_task(pid, "b")
    c = db.create_task(pid, "c", after_id=0)          # 0 = first
    assert [t["id"] for t in db.open_tasks(pid)] == [c, a, b]
    db.move_task(b, after_id=0)
    assert [t["id"] for t in db.open_tasks(pid)] == [b, c, a]
    empty = db.create_project("Empty")
    db.delete_empty_project(empty)
    assert db.row("SELECT id FROM projects WHERE id=?", (empty,)) is None
    with pytest.raises(ValueError):
        db.delete_empty_project(pid)


def test_outline_paste_nests_by_depth():
    pid = db.create_project("Paste")
    top = db.create_task(pid, "Existing")
    ids = db.create_tasks_outline(pid, None, top, [
        {"title": "A", "depth": 0}, {"title": "A1", "depth": 1}, {"title": "A1a", "depth": 2},
        {"title": "A2", "depth": 1}, {"title": "B", "depth": 0}, {"title": "orphan deep", "depth": 3},
    ])
    a, a1, a1a, a2, b, deep = ids
    roots = [t["id"] for t in db.open_tasks(pid) if t["parent_id"] is None]
    assert roots == [top, a, b]
    assert db.task(a1)["parent_id"] == a and db.task(a2)["parent_id"] == a
    assert db.task(a1a)["parent_id"] == a1
    assert db.task(deep)["parent_id"] == b                       # deeper than anything before it: hangs off B
    assert [t["id"] for t in db.subtasks(a)] == [a1, a2]


def test_inline_tags():
    import datetime as dt
    from tracker import tags
    wed = dt.date(2026, 10, 28)                       # a Wednesday
    title, f = tags.parse_tags("Send draft to Sam #p2 #due:fri #blocking:Sam", today=wed)
    assert title == "Send draft to Sam"
    assert f == {"importance": 4, "deadline": "2026-10-30", "blocking": 1, "blocking_for": "Sam"}
    assert tags.parse_tags("Pay rent #tomorrow", today=wed)[1]["deadline"] == "2026-10-29"
    assert tags.parse_tags("Taxes #due:nov3", today=wed)[1]["deadline"] == "2026-11-03"
    assert tags.parse_tags("Taxes #due:3nov", today=wed)[1]["deadline"] == "2026-11-03"
    assert tags.parse_tags("Renew #due:01-15", today=wed)[1]["deadline"] == "2027-01-15"   # next January
    assert tags.parse_tags("Renew #due:2026-12-01")[1]["deadline"] == "2026-12-01"
    assert tags.parse_tags("Deep work #onesitting")[1] == {"chunkable": 0}
    assert tags.parse_tags("## Weekly #review") == ("## Weekly #review", {})            # heading hashes and unknown tags stay
    assert tags.parse_tags("see issue #42 #p1") == ("see issue #42", {"importance": 5})
    assert tags.parse_tags("#p3") == ("#p3", {"importance": 3})                           # a title cannot become empty
    assert tags.parse_tags("Call #due:someday") == ("Call #due:someday", {})               # unparseable date is left alone


def test_tags_apply_on_create_and_patch():
    pid = db.create_project("Tagged")
    from tracker import tags
    title, f = tags.parse_tags("Write intro #p1 #blocking")
    tid = db.create_task(pid, title, **f)
    t = db.task(tid)
    assert (t["title"], t["importance"], t["blocking"]) == ("Write intro", 5, 1)
