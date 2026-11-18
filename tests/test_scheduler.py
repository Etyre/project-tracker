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

from tracker import db, scheduler, estimates, llm  # noqa: E402

llm._has_credentials = lambda: False      # tests never call Claude, whatever the Keychain holds
llm.enabled = lambda: False


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
    items = scheduler.greedy_plan(windows, tasks, [{"title": "Anki review", "minutes": 20}], exercise="run", exercise_minutes=45)
    for it in items:
        s, e = scheduler.parse_hm(it["start"]), scheduler.parse_hm(it["end"])
        assert any(ws <= s < e <= we for ws, we in windows), it
    # no overlaps
    spans = sorted((scheduler.parse_hm(i["start"]), scheduler.parse_hm(i["end"])) for i in items)
    assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))
    kinds = [i["kind"] for i in items]
    assert "routine" in kinds and "exercise" in kinds and items[0]["title"] == "Anki review"
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
        if i["status"] == "pending" and i["kind"] != "meeting":      # meetings come from the real calendar
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


def test_migrates_old_schema(tmp_path, monkeypatch, request):
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
    request.addfinalizer(db._local.__dict__.clear)   # reconnect to the shared test DB afterwards
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


def test_someday_items_live_outside_the_schedule():
    top = db.create_project("Active home")
    root = db.someday_root()                                       # the hidden project behind the Someday / maybe list
    assert root == db.someday_root() and root not in {p["id"] for p in db.projects()}
    assert db.find_project_by_path(["Someday / maybe"]) is None    # quick add never files into it
    loose = db.create_task(root, "an idea")
    roots = db.project_tree(someday=True)
    assert [t["title"] for p in roots if p["someday_root"] for t in p["tasks"]] == ["an idea"]
    assert "an idea" not in {c["title"] for c in scheduler.candidates(dt.date.today())}
    db.move_task(loose, top, None, None)                           # back into an active project: scheduled again
    assert "an idea" in {c["title"] for c in scheduler.candidates(dt.date.today())}
    assert "an idea" not in {t["title"] for p in db.project_tree(someday=True) for t in p["tasks"]}


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


def test_clearing_tags_and_apply_endpoint():
    from fastapi.testclient import TestClient
    from tracker import tags, web
    _, f = tags.parse_tags("x #nodue #p0 #notblocking #splittable")
    assert f == {"deadline": None, "importance": None, "blocking": 0, "blocking_for": "", "chunkable": 1}
    assert tags.parse_tags("x #due:none")[1] == {"deadline": None}
    pid = db.create_project("Apply")
    a = db.create_task(pid, "one", importance=2)
    b = db.create_task(pid, "two", deadline="2030-01-01")
    c = TestClient(web.create_app())
    r = c.post("/api/tasks/apply", json={"ids": [a, b, a], "text": "#p1 #due:2031-02-03"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2 and body["fields"] == {"importance": 5, "deadline": "2031-02-03"}
    assert body["before"][str(a)] == {"importance": 2, "deadline": None}
    assert db.task(b)["importance"] == 5 and db.task(a)["deadline"] == "2031-02-03"
    r = c.post("/api/tasks/apply", json={"ids": [a], "fields": {"deadline": None, "blocking": True, "blocking_for": "Sam"}})
    assert r.status_code == 200 and db.task(a)["deadline"] is None and db.task(a)["blocking"] == 1
    assert c.post("/api/tasks/apply", json={"ids": [a], "text": "#random"}).status_code == 400


def test_the_opened_tasks_tag_line_round_trips_and_removing_a_tag_clears_the_field():
    from fastapi.testclient import TestClient
    from tracker import tags, web
    pid = db.create_project("Tag line")
    tid = db.create_task(pid, "Draft", importance=4, deadline="2031-03-04", blocking=1, blocking_for="Sam", chunkable=0, work_kind="focus", reading=1)
    line = tags.render(db.task(tid))
    assert line == "#p2 #due:2031-03-04 #blocking:Sam #onesitting #focus #reading"
    assert tags.parse_tags("x " + line)[1] == {"importance": 4, "deadline": "2031-03-04", "blocking": 1, "blocking_for": "Sam", "chunkable": 0, "work_kind": "focus", "reading": 1}
    # a name with a space cannot be written as a tag: the line says #blocking and keeps the name as it is
    assert tags.render({"blocking": 1, "blocking_for": "Sam Smith"}) == "#blocking"
    assert tags.replace_fields({"blocking": 1, "blocking_for": "Sam Smith"}, "#blocking #p1") == {"importance": 5}
    c = TestClient(web.create_app())
    assert c.get(f"/api/tasks/{tid}/tags").json()["text"] == line
    # the line edited by hand: priority raised, the hard deadline dropped, a soft one added, the name and the reading flag removed
    r = c.put(f"/api/tasks/{tid}/tags", json={"text": "#p1 #soft:2031-03-01 #blocking #onesitting #focus"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fields"] == {"importance": 5, "soft_deadline": "2031-03-01", "deadline": None, "blocking_for": "", "reading": 0}   # only what changed
    assert body["before"] == {"importance": 4, "soft_deadline": None, "deadline": "2031-03-04", "blocking_for": "Sam", "reading": 1}
    assert body["text"] == "#p1 #soft:2031-03-01 #blocking #onesitting #focus"
    t = db.task(tid)
    assert (t["importance"], t["deadline"], t["soft_deadline"], t["blocking_for"], t["reading"], t["chunkable"]) == (5, None, "2031-03-01", "", 0, 0)
    # an empty line clears everything the line controlled; the title and notes are untouched
    assert c.put(f"/api/tasks/{tid}/tags", json={"text": ""}).status_code == 200
    t = db.task(tid)
    assert (t["title"], t["importance"], t["soft_deadline"], t["blocking"], t["chunkable"], t["work_kind"]) == ("Draft", None, None, 0, 1, None)
    # words that are not tags are refused, and nothing changes
    r = c.put(f"/api/tasks/{tid}/tags", json={"text": "#p3 call Sam"})
    assert r.status_code == 400 and "call Sam" in r.json()["detail"] and db.task(tid)["importance"] is None
    assert c.put("/api/tasks/999999/tags", json={"text": "#p1"}).status_code == 404
    db.delete_task(tid)   # the shared test DB: a reading-flagged task left here would reach the reading card test


def test_delete_subtree_and_recreate():
    pid = db.create_project("Subtree")
    a = db.create_task(pid, "Parent", description="notes here", importance=3)
    b = db.create_task(pid, "Child", parent_id=a, deadline="2031-01-01")
    c = db.create_task(pid, "Grandchild", parent_id=b)
    db.complete_task(c)
    d = db.create_task(pid, "After")
    outline = db.delete_task_tree(a)
    assert [(o["title"], o["depth"]) for o in outline] == [("Parent", 0), ("Child", 1), ("Grandchild", 2)]
    assert db.task(a) is None and db.task(c) is None and db.task(d)
    ids = db.create_tasks_outline(pid, None, 0, outline)
    tree = db.project_tree(active=True, completed=True)
    p = next(x for x in tree if x["id"] == pid)
    assert [t["title"] for t in p["tasks"]] == ["Parent", "After"]
    parent = db.task(ids[0])
    assert parent["description"] == "notes here" and parent["importance"] == 3
    assert db.task(ids[1])["parent_id"] == ids[0] and db.task(ids[1])["deadline"] == "2031-01-01"
    assert db.task(ids[2])["completed_at"] is not None


def test_daily_snapshot(tmp_path, monkeypatch):
    from tracker import snapshot
    monkeypatch.setitem(config.load()["snapshots"], "dir", str(tmp_path))
    pid = db.create_project("# Snap root")
    sub = db.create_project("## Snap sub", pid)
    a = db.create_task(sub, "First", importance=4, deadline="2030-05-06", blocking=1, blocking_for="Ann", description="why\nand how")
    db.create_task(sub, "Nested", parent_id=a)
    done = db.create_task(pid, "Finished one")
    db.complete_task(done)
    md = snapshot.render(dt.datetime(2030, 1, 2, 5, 0), since=0)
    lines = md.splitlines()
    assert "- **Snap root**" in lines and "  - **Snap sub**" in lines
    assert "    - [ ] First #p2 #due:2030-05-06 #blocking:Ann" in lines
    i = lines.index("    - [ ] First #p2 #due:2030-05-06 #blocking:Ann")
    assert lines[i + 1:i + 4] == ["      why", "      and how", "      - [ ] Nested"]
    assert any(l.startswith("- [x] Finished one") for l in lines)
    # before the hour nothing is written; after it, once per day; forced writes overwrite
    assert snapshot.maybe_snapshot(dt.datetime(2030, 1, 2, 4, 59)) is None
    path = snapshot.maybe_snapshot(dt.datetime(2030, 1, 2, 5, 0))
    assert path == tmp_path / "2030-01-02.md" and path.read_text().startswith("# Project Tracker snapshot")
    assert snapshot.maybe_snapshot(dt.datetime(2030, 1, 2, 9, 0)) is None
    assert snapshot.write(dt.datetime(2030, 1, 2, 9, 0)) == path
    assert [i["date"] for i in snapshot.list_snapshots()] == ["2030-01-02"]
    assert snapshot.read("2030-01-02").startswith("# Project Tracker snapshot") and snapshot.read("nope") is None


def test_settings_save_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "config.toml")
    config.load.cache_clear()
    cfg = config.save({"general": {"work_start": "08:30", "min_block_minutes": "20"},
                       "calendar": {"ics_urls": ["https://a/x.ics", " https://b/y.ics "], "busy_only": False},
                       "linear": {"enable": "true"}, "exercise": {"rotation": "run, swim"}, "bogus": {"x": 1}})
    assert cfg["general"]["work_start"] == "08:30" and cfg["general"]["min_block_minutes"] == 20
    assert cfg["calendar"]["ics_urls"] == ["https://a/x.ics", "https://b/y.ics"] and cfg["calendar"]["busy_only"] is False
    assert cfg["linear"]["enable"] is True and cfg["exercise"]["rotation"] == ["run", "swim"] and "bogus" not in cfg
    import tomllib
    data = tomllib.loads((tmp_path / "config.toml").read_text())
    assert data["general"]["work_start"] == "08:30" and data["calendar"]["ics_urls"][1] == "https://b/y.ics"
    assert set(config.secret_status()) == {"anthropic", "toggl", "linear", "sheets", "agent"}
    config.load.cache_clear()


def test_schedule_prompt_carries_exercise_notes():
    from tracker import llm
    sys_ = llm.schedule_system("Mornings for focus.", "Timing: early afternoon.")
    assert "Mornings for focus." in sys_ and "EXERCISE NOTES" in sys_ and "early afternoon" in sys_
    assert sys_.index("===== USER'S RULES") < sys_.index("===== USER'S EXERCISE NOTES")


def test_exercise_timer_named_going_to_do():
    from tracker import web
    db.set_setting("exercise_decision", "")
    label = web._exercise_label("strength")          # today's decision (rotation in tests) with the travel-time phrasing
    assert label.startswith("going to do ") and len(label) > len("going to do ")


def test_exercise_decided_once_and_length_from_history(monkeypatch):
    from tracker import exercise, tags, sheets
    monkeypatch.setattr(sheets, "exercise_sessions", lambda days=120: [])   # the app's own timers only, never the real sheet
    now = dt.datetime.now().timestamp()
    for i, (kind, mins) in enumerate([("run", 38), ("strength", 61), ("run", 42), ("strength", 58)]):
        eid = db.start_entry("exercise", kind.capitalize(), None, None)
        db.execute("UPDATE time_entries SET started_at=?, stopped_at=? WHERE id=?", (now - (i + 1) * 86400, now - (i + 1) * 86400 + mins * 60, eid))
    hist = exercise.history()
    assert exercise.typical_minutes("run", hist) == 40 and 58 <= exercise.typical_minutes("strength", hist) <= 61
    assert exercise.typical_minutes("mobility", hist) is not None            # falls back to any exercise
    db.set_setting("exercise_decision", "")
    d1 = exercise.decide("2030-03-03")
    assert d1["kind"] and d1["title"] and d1["minutes"] > 0 and d1["date"] == "2030-03-03"
    monkeypatch.setattr(exercise, "_fallback", lambda hist: (_ for _ in ()).throw(AssertionError("decided twice")))
    assert exercise.decide("2030-03-03") == d1                              # remembered, not recomputed
    assert exercise.decided_today() is False


def test_feed_uncomplete_reopens_task_and_block():
    from fastapi.testclient import TestClient
    from tracker import web
    c = TestClient(web.create_app())
    pid = db.create_project("Feed undo")
    tid = db.create_task(pid, "Undo me", estimate_minutes=30)
    date = db.today()
    db.save_plan(date, "09:00", "17:00", "run", "", [dict(kind="task", task_id=tid, title="Undo me", start="09:00", end="09:30", minutes=30, note="")], {})
    item = db.rows("SELECT id FROM plan_items WHERE task_id=?", (tid,))[0]["id"]
    assert c.post("/api/feed/complete", json={"card": f"task:{tid}"}).status_code == 200
    assert db.task(tid)["completed_at"] and db.plan_item(item)["status"] == "done"          # the plan page follows the feed
    assert c.post("/api/feed/uncomplete", json={"card": f"task:{tid}"}).status_code == 200
    assert db.task(tid)["completed_at"] is None and db.plan_item(item)["status"] == "pending"
    assert c.post("/api/feed/complete", json={"card": "task:999999"}).status_code == 404
    assert c.post("/api/feed/complete", json={"card": "nap:1"}).status_code == 400


def test_the_feed_is_a_stack_filtered_by_the_gap_before_the_next_meeting(monkeypatch):
    """No schedule: the top card is the best open task (or recurring card) that fits the time until the next
    meeting; a longer task fits only when it can be split; Next moves the card to the bottom for the day."""
    from fastapi.testclient import TestClient
    from tracker import feed, web, sheets, estimates, calendar_feed
    monkeypatch.setattr(estimates, "calibration", lambda: {"overall": 1.0, "by_root_project": {}, "samples": 0})
    monkeypatch.setattr(sheets, "routines_for_today", lambda: [{"title": "Anki review", "minutes": 20}])
    monkeypatch.setattr(sheets, "card_items", lambda: [{"title": "Anki review", "minutes": 20}])
    monkeypatch.setattr(sheets, "writable", lambda: False)
    monkeypatch.setattr(web, "_suggest_flag", lambda title: None)
    for t in db.open_tasks():
        db.complete_task(t["id"])                                     # only this test's tasks in the stack
    pid = db.create_project("Stack")
    quick = db.create_task(pid, "Quick reply", estimate_minutes=10, importance=2)
    long_fixed = db.create_task(pid, "Long one-sitting review", estimate_minutes=60, importance=5, chunkable=0)
    long_split = db.create_task(pid, "Long draft, can be split", estimate_minutes=90, importance=4, chunkable=1)
    mid = db.create_task(pid, "Half-hour call prep", estimate_minutes=30, importance=3, chunkable=0)
    deep = db.create_task(pid, "Deep draft", estimate_minutes=120, importance=1, work_kind="focus")      # splittable, but focused work
    now = dt.datetime.now()
    mk = lambda start_min, length, title: {"title": title, "all_day": False, "start": now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(minutes=start_min),
                                           "end": now.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(minutes=start_min + length)}
    now_min = now.hour * 60 + now.minute
    # the module itself, with an explicit clock: a meeting in 15 minutes
    at = dt.datetime.combine(dt.date.today(), dt.time(10, 0))
    events = [mk(10 * 60 + 15, 30, "Standup"), mk(14 * 60, 60, "Review")]
    st = feed.cards(at, events)
    assert st["gap"] == {"minutes": 15, "until": "Standup", "at": "10:15", "kind": "meeting", "meeting_now": None}
    assert [c["key"] for c in st["stack"]] == ["routine:Anki review", f"task:{long_split}", f"task:{quick}"]      # fits: 20 split, 90 split, 10
    assert [c["key"] for c in st["waiting"]] == [f"task:{long_fixed}", f"task:{mid}", f"task:{deep}"]              # 60 in one sitting, 30, focus: no
    assert feed.fits(next(c for c in st["waiting"] if c["key"] == f"task:{deep}"), 45)                             # focused work: a 45-min stretch will do
    # writing is started only with an hour free and reading only with twenty minutes, however short the task
    card = lambda **task: {"kind": "task", "title": "x", "minutes": 10, "chunkable": True, "task": task}
    assert not feed.fits(card(writing=1), 59) and feed.fits(card(writing=1), 60) and feed.fits(card(writing=1), None)
    assert not feed.fits(card(reading=1), 19) and feed.fits(card(reading=1), 20)
    assert not feed.fits(card(reading=1, writing=1), 30) and feed.fits(card(), 10)
    daily = lambda title: {"kind": "routine", "title": title, "minutes": 40, "chunkable": True}
    assert not feed.fits(daily(config.load()["writing"]["routine"]), 45) and feed.fits(daily(config.load()["writing"]["routine"]), 60)
    assert not feed.fits(daily(config.load()["reading"]["routine"]), 15) and feed.fits(daily("Anki review"), 15)
    # in the meeting: the gap runs to the next one; after the last meeting, to the end of the work day
    st = feed.cards(dt.datetime.combine(dt.date.today(), dt.time(10, 20)), events)
    assert st["gap"]["meeting_now"]["title"] == "Standup" and st["gap"]["until"] == "Review" and st["gap"]["minutes"] == 220
    assert [c["key"] for c in st["stack"]][:3] == ["routine:Anki review", f"task:{long_fixed}", f"task:{long_split}"]   # by priority
    we = scheduler.parse_hm(config.load()["general"]["work_end"])
    late = dt.datetime.combine(dt.date.today(), dt.time(0, 0)) + dt.timedelta(minutes=we - 20)
    st = feed.cards(late, events)
    assert st["gap"]["kind"] == "end of day" and st["gap"]["minutes"] == 20
    assert feed.cards(dt.datetime.combine(dt.date.today(), dt.time(23, 30)), events)["gap"]["minutes"] is None   # evening: nothing bounds it
    # the API, with the real clock: a meeting 15 minutes from now
    monkeypatch.setattr(calendar_feed, "events_for", lambda day: [mk(now_min + 15, 30, "Standup")])
    c = TestClient(web.create_app())
    f = c.get("/api/feed").json()
    assert f["gap"]["minutes"] in (14, 15) and f["gap"]["until"] == "Standup" and f["current"]["key"] == "routine:Anki review"
    assert [w["title"] for w in f["waiting"]] == ["Long one-sitting review", "Half-hour call prep", "Deep draft"]
    assert f["meetings"][0]["title"] == "Standup" and f["stack_size"] == 3
    # Next: the card goes to the bottom for the day, and the next best card is on top
    assert c.post("/api/feed/skip", json={"card": "routine:Anki review"}).status_code == 200
    f = c.get("/api/feed").json()
    assert f["current"]["key"] == f"task:{long_split}" and f["current"]["task"]["id"] == long_split and f["current"]["path"] == ["Stack"]
    assert [x["key"] for x in f["up_next"]] == [f"task:{quick}", "routine:Anki review"]
    assert c.post("/api/feed/skip", json={"card": f"task:{long_split}"}).status_code == 200
    assert c.get("/api/feed").json()["current"]["key"] == f"task:{quick}"
    assert c.post("/api/feed/unskip", json={"card": "routine:Anki review"}).status_code == 200
    assert c.get("/api/feed").json()["current"]["key"] == "routine:Anki review"
    # a started card stays on top while its timer runs, whatever the order
    assert c.post("/api/feed/start", json={"card": f"task:{quick}"}).status_code == 200
    f = c.get("/api/feed").json()
    assert f["current"]["key"] == f"task:{quick}" and f["current"]["status"] == "active" and db.running_entry()["task_id"] == quick
    assert c.post("/api/feed/complete", json={"card": f"task:{quick}"}).status_code == 200
    assert db.running_entry() is None and db.task(quick)["completed_at"]
    f = c.get("/api/feed").json()
    assert f["current"]["key"] == "routine:Anki review" and f["progress"]["done"] >= 1
    # the agent's schedule for the day puts its tasks first, in its order
    db.set_setting(f"feed_skipped:{db.today()}", "[]")
    db.save_plan(db.today(), "09:00", "17:00", "run", "", [], {"source": "agent:Ada", "agent_plan": {"source": "agent:Ada", "items": [{"kind": "task", "task_id": mid}, {"kind": "task", "task_id": long_split}]}})
    st = feed.cards(dt.datetime.combine(dt.date.today(), dt.time(16, 0)), events)
    assert [c["key"] for c in st["stack"]][:4] == ["routine:Anki review", f"task:{mid}", f"task:{long_split}", f"task:{long_fixed}"]
    db.execute("DELETE FROM plans WHERE date=?", (db.today(),)); db.execute("DELETE FROM plan_items WHERE date=?", (db.today(),))
    db.set_setting(f"feed_skipped:{db.today()}", "[]")
    for t in (long_fixed, long_split, mid, deep):
        db.delete_task(t)


def test_exercise_sessions_parse_and_merge(monkeypatch):
    from tracker import exercise, sheets
    payload = {"ok": True, "sessions": [
        {"id": "a", "exerciseName": "Chin Ups", "category": "Strength Training", "startedAt": "2026-11-02T11:02:44.677", "finishedAt": "2026-11-02T11:50:00.000", "durationSec": 2835, "sets": [{"reps": 8, "weight": 0, "unit": "lb"}]},
        {"id": "b", "exerciseName": "Easy run", "category": "Running", "startedAt": "2026-11-01T14:00:00Z", "finishedAt": None, "durationSec": None},
        {"id": "c", "exerciseName": "Old", "category": "Running", "startedAt": "2020-01-01T10:00:00"},
        {"id": "d", "exerciseName": "Broken", "startedAt": "nope"}]}
    monkeypatch.setattr(sheets, "_get", lambda url, max_age: __import__("json").dumps(payload) if "action=export" in url else "")
    monkeypatch.setitem(config.load()["sheets"], "exercise_script_url", "https://example.invalid/exec")
    sess = sheets.exercise_sessions()
    assert [(x["kind"], x["date"], x["minutes"]) for x in sess] == [("strength", "2026-11-02", 47), ("run", "2026-11-01", 0)]
    assert sess[0]["sets_summary"] == "8 x 0lb"
    eid = db.start_entry("exercise", "Strength", None, None)          # the app's own timer for the same session
    db.execute("UPDATE time_entries SET started_at=?, stopped_at=? WHERE id=?", (dt.datetime(2026, 11, 2, 11, 0).timestamp(), dt.datetime(2026, 11, 2, 11, 40).timestamp(), eid))
    hist = exercise.history()
    same_day = [h for h in hist if h["date"] == "2026-11-02" and h["kind"] == "strength"]
    assert len(same_day) == 1 and same_day[0]["source"] == "sheet" and same_day[0]["minutes"] == 47


TOGGL_FAKE = {"/me": {"default_workspace_id": 1, "fullname": "Test"},
              "/workspaces/1/projects?per_page=200&page=1": [{"id": 1, "name": "Palisade", "client_id": 7, "active": True}, {"id": 2, "name": "Personal", "client_id": None, "active": True}],
              "/workspaces/1/clients": [{"id": 7, "name": "Palisade Research"}]}


def _fake_toggl(monkeypatch):
    from tracker import toggl
    monkeypatch.setattr(toggl, "_req", lambda m, path, body=None: TOGGL_FAKE.get(path))
    toggl.me.cache_clear()
    monkeypatch.setattr(toggl, "entries", lambda days=90: [
        {"description": "Going to do squats", "project_id": 2, "tags": ["exercise"], "start": "2026-11-01T20:00:00+00:00"},
        {"description": "going to do squats", "project_id": 1, "tags": [], "start": "2026-10-01T20:00:00+00:00"},
        {"description": "Strategy sync", "project_id": 1, "tags": ["meeting"], "start": "2026-11-02T14:00:00+00:00"},
        {"description": "", "project_id": None, "tags": [], "start": "2026-11-02T15:00:00+00:00"}])
    toggl.forget_projects()


def test_toggl_history_index_and_exact_match(monkeypatch):
    from tracker import toggl, web
    _fake_toggl(monkeypatch)
    idx = toggl.history_index()
    assert idx["exact"]["going to do squats"] == {"project_id": 2, "project": "Personal", "tags": ["exercise"]}   # newest wins
    assert [p["name"] for p in idx["projects"]] == ["Palisade", "Personal"] and idx["projects"][0]["client"] == "Palisade Research"
    assert "meeting" in idx["tags"] and toggl.clients() == ["Palisade Research"]
    assert toggl.history_match("GOING TO DO SQUATS")["project"] == "Personal"
    assert toggl.history_match("Something never tracked") is None
    assert toggl.find_project("personal")["id"] == 2 and toggl.find_project("Nowhere") is None and toggl.find_project("") is None
    assert web._going_to("squats") == "going to do squats" and web._going_to("Exercise: cardio") == "going to do cardio"
    assert web._going_to("Going to do a run") == "Going to do a run"
    toggl.forget_projects()


def test_toggl_routine_list_and_linked_projects(monkeypatch):
    """Routines take their Toggl project from the Settings list, tasks from the project chain: no model, and the
    project goes into the start request itself."""
    from tracker import toggl, web
    _fake_toggl(monkeypatch)
    monkeypatch.setitem(config.load()["toggl"], "routine_projects", ["Anki review: Personal | learning, spaced repetition", "cardio interval training: Personal", "Daily regather: Nowhere", "no project here"])
    assert toggl.routine_key("Going to do  Squats") == "squats" and toggl.routine_key("Anki review") == "anki review"
    assert toggl.routine_project("Anki review") == {"project_id": 2, "project": "Personal", "tags": ["learning", "spaced repetition"], "source": "routine list"}
    assert toggl.routine_project("going to do cardio interval training")["project"] == "Personal" and toggl.routine_project("cardio interval training")["tags"] == []
    assert toggl.routine_line("Anki review", "Personal", ["learning", "learning", " review "]) == "Anki review: Personal | learning, review"
    assert toggl.routine_project("Daily regather") is None            # listed under a project Toggl does not have
    assert toggl.routine_project("Journaling") is None                # unlisted: Claude's turn
    top = db.create_project("Toggl top"); mid = db.create_project("Mid", top); leaf = db.create_project("Leaf", mid)
    assert db.toggl_project_for(leaf) is None
    db.update_project(top, toggl_project="Palisade")
    assert db.toggl_project_for(leaf) == {"name": "Palisade", "project_id": top, "inherited": True}
    db.update_project(mid, toggl_project="Personal")                  # the nearest ancestor wins
    assert db.toggl_project_for(leaf)["name"] == "Personal" and db.toggl_project_for(top)["inherited"] is False
    tid = db.create_task(leaf, "Leaf task")
    assert web._toggl_project_for_timer("task", "Leaf task", tid) == {"project_id": 2, "project": "Personal", "source": "linked project"}
    assert web._toggl_project_for_timer("routine", "Anki review", None)["project_id"] == 2
    assert web._toggl_project_for_timer("exercise", "going to do squats", None) is None
    started = []
    monkeypatch.setattr(toggl, "enabled", lambda: True)
    monkeypatch.setattr(toggl, "start", lambda label, extra_tags=None, project_id=None: started.append((label, project_id, extra_tags)) or {"id": 99})
    monkeypatch.setattr(toggl, "current", lambda: None)
    monkeypatch.setattr(toggl, "stop", lambda eid=None: None)
    res = web._start_timer("routine", "Anki review", None, ["routine"])
    assert started[-1] == ("Anki review", 2, ["routine", "learning", "spaced repetition"]) and res["toggl_project"] == "Personal"
    web._stop_timer()
    res = web._start_timer("task", "Leaf task", tid)
    assert started[-1] == ("Leafing task", 2, []) and res["toggl_project"] == "Personal"   # rule-based gerund: Claude has not phrased it
    db.update_task(tid, toggl_title="Working on the leaf task")
    web._stop_timer()
    web._start_timer("task", "Leaf task", tid)
    assert started[-1][0] == "Working on the leaf task"                                    # Claude's phrasing wins
    assert db.running_entry()["label"] == "Leaf task"                                       # the app's own card keeps the title
    web._stop_timer()
    toggl.forget_projects()


def test_toggl_titles_are_gerunds():
    from fastapi.testclient import TestClient
    from tracker import toggl, web
    g = toggl.gerund
    assert g("Respond to JDPressman") == "Responding to JDPressman"
    assert g("Write the memo") == "Writing the memo"
    assert g("Run the numbers") == "Running the numbers"
    assert g("Fix the login bug") == "Fixing the login bug"
    assert g("Set up the printer") == "Setting up the printer"
    assert g("Tie off the thread") == "Tying off the thread"
    assert g("See Sam") == "Seeing Sam"
    assert g("Review PR 12") == "Reviewing PR 12"
    assert g("Writing the memo") == "Writing the memo"          # already a gerund
    assert g("Ping Sam") == "Pinging Sam"                        # short -ing words are verbs, not gerunds
    assert g("2026 taxes") == "2026 taxes"                       # not an imperative: left alone
    assert g("  ") == ""
    pid = db.create_project("Gerunds")
    tid = db.create_task(pid, "Call the bank", toggl_title="Calling the bank")
    assert db.task(tid)["toggl_title"] == "Calling the bank"
    assert web._toggl_description("task", "Call the bank", tid) == "Calling the bank"
    assert web._toggl_description("routine", "Anki review", None) == "Anki review"
    c = TestClient(web.create_app())
    assert c.patch(f"/api/tasks/{tid}", json={"importance": 3}).status_code == 200
    assert db.task(tid)["toggl_title"] == "Calling the bank"      # other edits keep the phrasing
    assert c.patch(f"/api/tasks/{tid}", json={"title": "Call the bank about the card"}).status_code == 200
    assert db.task(tid)["toggl_title"] is None                    # a new title is re-phrased by the background pass
    assert any(t["id"] == tid for t in db.tasks_without_work_kind(1000))
    assert web._toggl_description("task", "Call the bank about the card", tid) == "Calling the bank about the card"
    db.delete_task(tid)


def test_exercise_button_offers_extra_session_once_done(monkeypatch):
    from fastapi.testclient import TestClient
    from tracker import web, exercise
    c = TestClient(web.create_app())
    monkeypatch.setattr(exercise, "decide", lambda date=None, force=False: {"kind": "strength", "title": "squats", "minutes": 40, "reason": "", "date": db.today()})
    f = c.get("/api/feed").json()
    assert f["exercise"] == "squats" and f["exercise_done"] is False and f["exercise_extra"] == "zone 2 cardio" and f["exercise_extra_done"] is False
    r = c.post("/api/exercise/start").json()                                  # the day's session, from the button under the deck
    assert r["kind"] == "squats" and db.running_entry()["label"] == "going to do squats"
    web._stop_timer()
    assert c.get("/api/feed").json()["exercise_done"] is True
    r = c.post("/api/exercise/start", json={"title": "zone 2 cardio"}).json()
    assert r["kind"] == "zone 2 cardio" and db.running_entry()["label"] == "going to do zone 2 cardio"
    assert c.get("/api/feed").json()["exercise_extra_done"] is False          # still running: not done yet
    web._stop_timer()
    assert c.get("/api/feed").json()["exercise_extra_done"] is True
    assert "zone 2 cardio" in web._routine_labels()


def test_exercise_pick_made_offline_is_provisional(monkeypatch):
    """Decided at midnight with the exercise sheet unreachable (the internet is off): the pick is remade as soon
    as the sheet can be read again, and kept while it still cannot."""
    from tracker import exercise, sheets
    monkeypatch.setitem(config.load()["sheets"], "exercise_script_url", "https://example.invalid/exec")
    today = dt.date(2031, 5, 5)
    H = lambda name, days_ago, kind="strength": {"kind": kind, "exercise": name, "date": (today - dt.timedelta(days=days_ago)).isoformat(), "weekday": "", "minutes": 40, "source": "sheet"}
    offline = {"on": True}
    def fake_history(days=90):
        sheets.exercise_log_failed = offline["on"]
        return [] if offline["on"] else [H("Squats", 2), H("Cardio Interval Training", 1, "cardio")]
    monkeypatch.setattr(exercise, "history", fake_history)
    db.set_setting("exercise_decision", "")
    d1 = exercise.decide(today.isoformat())
    assert d1["title"] == "squats" and d1["provisional"] is True            # an empty log makes squats look overdue
    assert exercise.decide(today.isoformat())["decided_at"] == d1["decided_at"]   # still offline: kept, not re-decided
    offline["on"] = False
    d2 = exercise.decide(today.isoformat())
    assert d2["title"] == "benchpress" and not d2.get("provisional") and d2["decided_at"] != d1["decided_at"]
    assert exercise.decide(today.isoformat())["decided_at"] == d2["decided_at"]   # now final
    sheets.exercise_log_failed = False
    db.set_setting("exercise_decision", "")


def test_nightly_prefetch_reads_every_source_afresh(monkeypatch):
    from tracker import sheets, calendar_feed
    got = []
    monkeypatch.setattr(sheets, "_get", lambda url, max_age: got.append(("sheet", max_age)) or "")
    monkeypatch.setattr(calendar_feed, "_fetch", lambda url, max_age=600: got.append(("cal", max_age)) or b"")
    monkeypatch.setitem(config.load()["sheets"], "exercise_script_url", "https://example.invalid/exec")
    monkeypatch.setitem(config.load()["sheets"], "daily_checklist_csv", "https://example.invalid/csv")
    monkeypatch.setitem(config.load()["calendar"], "ics_urls", ["https://example.invalid/a.ics"])
    sheets.prefetch(); calendar_feed.prefetch()
    assert ("sheet", 0) in got and ("cal", 0) in got and all(m == 0 for _, m in got)
    assert scheduler.parse_hm(config.DEFAULTS["sheets"]["prefetch_time"]) == 23 * 60 + 50


def test_feed_start_answers_and_runs_the_timer():
    from fastapi.testclient import TestClient
    from tracker import web
    c = TestClient(web.create_app())
    pid = db.create_project("Feed start")
    tid = db.create_task(pid, "Start me", estimate_minutes=30)
    date = db.today()
    r = c.post("/api/feed/start", json={"card": f"task:{tid}"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["entry_id"]
    assert db.running_entry()["task_id"] == tid
    assert c.get("/api/feed").json()["current"]["key"] == f"task:{tid}"          # the started card is on top
    web._stop_timer()


def test_done_on_a_card_stops_its_timer(monkeypatch):
    """"Done" on a routine card stops the running timer for that routine even when the card was never marked
    active (the timer came from elsewhere); a timer for something else is left running."""
    from fastapi.testclient import TestClient
    from tracker import web, sheets
    monkeypatch.setattr(sheets, "writable", lambda: False)
    monkeypatch.setattr(sheets, "card_items", lambda: [{"title": "Anki review", "minutes": 20}, {"title": "Personal email", "minutes": 20}])
    c = TestClient(web.create_app())
    date = db.today()
    web._start_timer("routine", "Anki review")
    assert c.post("/api/feed/complete", json={"card": "routine:Personal email"}).status_code == 200 and db.running_entry()   # other card: timer stays
    assert c.post("/api/feed/complete", json={"card": "routine:Anki review"}).status_code == 200 and db.running_entry() is None
    assert {"Anki review", "Personal email"} <= db.checklist_done_today(date)
    assert c.post("/api/feed/uncomplete", json={"card": "routine:Anki review"}).status_code == 200
    web._start_timer("routine", "anki review")
    assert c.post("/api/feed/skip", json={"card": "routine:Anki review"}).status_code == 200 and db.running_entry() is None   # Next stops its timer
    db.execute("DELETE FROM checklist_log WHERE date=?", (date,))
    db.set_setting(f"feed_skipped:{date}", "[]")


def test_toggl_quota_backoff(monkeypatch):
    """A 402 from Toggl stops further calls to that bucket (fast, no network) until the reported reset; the other
    bucket is untouched; a success with headers records what is left."""
    import io, urllib.error, urllib.request
    from tracker import toggl
    calls = []
    class Resp(io.BytesIO):
        headers = {"X-Toggl-Quota-Remaining": "12", "X-Toggl-Quota-Resets-In": "100"}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_open(req, timeout=20):
        calls.append(req.full_url)
        if "/me/" in req.full_url:
            raise urllib.error.HTTPError(req.full_url, 402, "Payment Required", {"X-Toggl-Quota-Remaining": "0", "X-Toggl-Quota-Resets-In": "600"}, io.BytesIO(b"quota"))
        return Resp(b'{"ok": true}')
    monkeypatch.setattr(urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(config, "secret", lambda name: "tok" if name == "toggl" else None)
    toggl.quota.clear()
    with pytest.raises(toggl.TogglError, match="402"):
        toggl._req("GET", "/me/time_entries/current")
    with pytest.raises(toggl.TogglError, match="quota used up"):
        toggl._req("GET", "/me/time_entries/current")                # no second request goes out
    assert len(calls) == 1 and toggl.quota_left("/me") == 0
    assert toggl._req("GET", "/workspaces/1/clients") == {"ok": True} and toggl.quota_left("/workspaces/1/x") == 12
    assert len(calls) == 2
    toggl.quota.clear()


def test_toggl_history_trues_up_local_entries():
    """A timer stopped from Toggl's side is stopped here at Toggl's time; drifted stops take Toggl's; a timer still
    running in Toggl, or one that matches, is left alone."""
    import time as _t
    now = _t.time()
    iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    a = db.start_entry("routine", "Reconcile me", None, 501)             # running here, stopped in Toggl an hour ago
    db.execute("UPDATE time_entries SET started_at=? WHERE id=?", (now - 7200, a))
    b = db.start_entry("task", "Drifted", None, 502)                      # stopped here 30 min late
    db.execute("UPDATE time_entries SET started_at=?, stopped_at=? WHERE id=?", (now - 5400, now - 1800, b))
    c = db.start_entry("task", "Still running", None, 503)
    db.execute("UPDATE time_entries SET started_at=? WHERE id=?", (now - 300, c))
    d = db.start_entry("task", "Matches", None, 504)
    db.execute("UPDATE time_entries SET started_at=?, stopped_at=? WHERE id=?", (now - 1000, now - 400, d))
    n = db.reconcile_with_toggl([
        {"id": 501, "start": iso(now - 7200), "stop": iso(now - 3600), "duration": 3600},
        {"id": 502, "start": iso(now - 5400), "stop": iso(now - 3600), "duration": 1800},
        {"id": 503, "start": iso(now - 300), "stop": None, "duration": -1},
        {"id": 504, "start": iso(now - 1000 + 10), "stop": iso(now - 400 - 10), "duration": 580},
        {"id": 999, "start": iso(now), "stop": None, "duration": -1}])
    assert n == 2
    assert abs(db.row("SELECT stopped_at FROM time_entries WHERE id=?", (a,))["stopped_at"] - (now - 3600)) < 2
    assert abs(db.row("SELECT stopped_at FROM time_entries WHERE id=?", (b,))["stopped_at"] - (now - 3600)) < 2
    assert db.row("SELECT stopped_at FROM time_entries WHERE id=?", (c,))["stopped_at"] is None
    assert abs(db.row("SELECT stopped_at FROM time_entries WHERE id=?", (d,))["stopped_at"] - (now - 400)) < 2
    db.execute("UPDATE time_entries SET stopped_at=? WHERE stopped_at IS NULL", (now,))


def test_resolve_project_path_never_duplicates():
    top = db.create_project("# Resolve top")
    pol = db.create_project("## Policy-Maker Engagement", top)
    risch = db.create_project("The Risch Office", pol)
    sib = db.create_project("Sibling", top)
    R = db.resolve_project_path
    assert R(["Resolve top", "Policy-Maker Engagement", "The Risch Office"]) == (risch, "")
    assert R(["resolve top", "policymaker engagement", "the risch office"]) == (risch, "")      # case, punctuation, marks
    assert R(["Resolve top", "The Risch Office"]) == (risch, "")                                # a skipped level
    assert R(["The Risch Office"]) == (risch, "")                                               # dropped top levels
    assert R(["Policy maker engagement", "The Risch Office"]) == (risch, "")
    pid, note = R(["Resolve top", "Sibling", "Brand new"])                                      # a new sub-project is fine
    assert db.project_path(pid) == ["# Resolve top", "Sibling", "Brand new"] and note == "new sub-project Brand new"
    pid2, note2 = R(["Resolve top", "Sibling", "brand-new"])                                    # ...and found again, not duplicated
    assert pid2 == pid and note2 == ""
    assert R(["Nowhere", "Something"]) == (None, "no top-level project called 'Nowhere'")       # never a new top level
    assert db.find_project_by_path(["Nowhere"]) is None
    pid3, note3 = R(["Nowhere"], allow_new_top=True)                                            # unless explicitly asked (the agent)
    assert db.project_path(pid3) == ["Nowhere"] and note3 == "new sub-project Nowhere"
    assert R([]) == (None, "no project named")
    db.delete_empty_project(pid3)


def test_work_kind_tags_api_and_backlog(monkeypatch):
    from fastapi.testclient import TestClient
    from tracker import tags, web, snapshot
    assert tags.parse_tags("Write the memo #focus")[1] == {"work_kind": "focus"}
    assert tags.parse_tags("Buy stamps #task")[1] == {"work_kind": "task"}
    assert tags.parse_tags("Whatever #nokind")[1] == {"work_kind": None}
    c = TestClient(web.create_app())
    pid = db.create_project("Kinds")
    a = c.post("/api/tasks", json={"project_id": pid, "title": "Draft the paper #focus"}).json()
    assert (a["title"], a["work_kind"]) == ("Draft the paper", "focus")
    b = c.post("/api/tasks", json={"project_id": pid, "title": "Call the bank", "work_kind": "focused work"}).json()
    assert b["work_kind"] == "focus"
    assert c.post("/api/tasks", json={"project_id": pid, "title": "x", "work_kind": "nonsense"}).status_code == 400
    assert c.patch(f"/api/tasks/{b['id']}", json={"work_kind": "task"}).status_code == 200 and db.task(b["id"])["work_kind"] == "task"
    assert c.patch(f"/api/tasks/{b['id']}", json={"work_kind": ""}).status_code == 200 and db.task(b["id"])["work_kind"] is None
    assert c.post("/api/tasks/apply", json={"ids": [a["id"], b["id"]], "fields": {"work_kind": "task"}}).status_code == 200
    assert db.task(a["id"])["work_kind"] == "task"
    assert "#task" in snapshot._tags(db.task(a["id"]))
    # the backlog: tasks never judged get a batch call; a user's own setting is never overwritten
    unjudged = db.create_task(pid, "Think through the roadmap")
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "classify_work_kinds", lambda ts: {t["id"]: {"work_kind": "focus", "reading": 0, "writing": int(t["id"] == unjudged)} for t in ts})
    total, rounds = 0, 0
    while db.tasks_without_work_kind() and rounds < 20:                 # a batch of 40 per loop pass
        total += web._classify_backlog(); rounds += 1
    assert total >= 1 and db.task(unjudged)["work_kind"] == "focus" and db.task(a["id"])["work_kind"] == "task"
    assert db.task(unjudged)["writing"] == 1 and db.tasks_without_work_kind() == []
    assert tags.parse_tags("Draft the post #writing")[1] == {"writing": 1}
    w = c.post("/api/tasks", json={"project_id": pid, "title": "Write the memo #writing #reading"}).json()
    assert (w["writing"], w["reading"]) == (1, 1) and "#writing" in snapshot._tags(db.task(w["id"]))
    started = []
    monkeypatch.setattr(web.toggl, "enabled", lambda: True)
    monkeypatch.setattr(web.toggl, "start", lambda label, extra_tags=None, project_id=None: started.append(extra_tags) or {"id": 8})
    monkeypatch.setattr(web.toggl, "current", lambda: None)
    monkeypatch.setattr(web.toggl, "stop", lambda eid=None: None)
    monkeypatch.setattr(web.toggl, "find_project", lambda name: None)
    web._start_timer("task", "Write the memo", w["id"])
    assert started[-1] == ["reading", "writing"]                                              # its timers carry both tags
    web._stop_timer()
    db.complete_task(w["id"]); db.complete_task(unjudged)      # leave no flagged tasks behind for the card tests
    monkeypatch.setattr(llm, "enabled", lambda: False)


def test_reading_card_offers_highest_priority_reading_task(monkeypatch):
    from fastapi.testclient import TestClient
    from tracker import web, tags, sheets
    monkeypatch.setattr(sheets, "writable", lambda: False)
    from tracker import feed
    monkeypatch.setattr(feed, "min_gap", lambda card: 0)   # this runs at the real time of day: near the end of the work day the hour writing needs is not there
    assert tags.parse_tags("Read the RSI bill #reading")[1] == {"reading": 1}
    c = TestClient(web.create_app())
    pid = db.create_project("Reading list")
    low = db.create_task(pid, "Read the newsletter", reading=1, importance=2)
    high = db.create_task(pid, "Read the policy memo", reading=1, importance=5, deadline=db.today())
    db.create_task(pid, "Write the reply", reading=0, importance=5)
    assert [t["id"] for t in db.reading_tasks()] == [low, high]
    assert "Reading books or papers" in [x["title"] for x in sheets.card_items()]          # a card of its own, without listing it
    date = db.today()
    monkeypatch.setattr(sheets, "routines_for_today", lambda: [{"title": "Reading books or papers", "minutes": 30}])
    monkeypatch.setattr(sheets, "card_items", lambda: [{"title": "Reading books or papers", "minutes": 30}])
    item = "routine:Reading books or papers"
    f = c.get("/api/feed").json()
    assert f["current"]["key"] == item and f["current"]["suggest"]["task"]["id"] == high and f["current"]["suggest"]["fallback"] == "Read a book"
    assert f["current"]["suggest"]["verb"] == "Read"
    started = []
    monkeypatch.setattr(web.toggl, "enabled", lambda: True)
    monkeypatch.setattr(web.toggl, "start", lambda label, extra_tags=None, project_id=None: started.append((label, extra_tags)) or {"id": 7})
    monkeypatch.setattr(web.toggl, "current", lambda: None)
    monkeypatch.setattr(web.toggl, "stop", lambda eid=None: None)
    monkeypatch.setattr(web.toggl, "find_project", lambda name: None)
    assert c.post("/api/feed/start", json={"card": item}).status_code == 200
    assert db.running_entry()["task_id"] == high                                             # time goes to the reading task
    assert c.get("/api/feed").json()["current"]["key"] == item and c.get("/api/feed").json()["current"]["status"] == "active"
    assert started[-1] == ("Reading the policy memo", ["reading"])                            # ...tagged reading, named as a gerund
    assert c.post(f"/api/tasks/{high}/complete", json={}).status_code == 200 and db.running_entry() is None
    assert c.get("/api/feed").json()["current"]["suggest"]["task"]["id"] == low                # the next one
    db.complete_task(low)
    assert c.get("/api/feed").json()["current"]["suggest"]["task"] is None                    # nothing queued: read a book
    assert c.post("/api/feed/start", json={"card": item, "label": "The Making of the Atomic Bomb"}).status_code == 200
    e = db.running_entry()
    assert (e["kind"], e["label"], e["task_id"]) == ("reading", "The Making of the Atomic Bomb", None)
    assert started[-1] == ("The Making of the Atomic Bomb", ["reading"])
    assert "Reading books or papers" not in web._routine_labels()                             # never given a fixed Toggl project
    from tracker import toggl
    monkeypatch.setattr(toggl, "history_match", lambda label: {"project_id": 9, "project": "History", "tags": []} if "Atomic" in label else None)
    assert web._toggl_project_for_timer("reading", "The Making of the Atomic Bomb", None)["project"] == "History"
    assert web._toggl_project_for_timer("reading", "Something new", None) is None              # Claude judges it, per title
    assert c.post("/api/feed/complete", json={"card": item}).status_code == 200 and db.running_entry() is None
    db.execute("DELETE FROM checklist_log WHERE date=? AND item=?", (date, "Reading books or papers"))
    # the writing card: the same shape, from the writing tasks
    monkeypatch.setattr(sheets, "card_items", lambda: [{"title": "Writing - 40 minutes", "minutes": 40}])
    assert "Writing - 40 minutes" not in web._routine_labels()                                # filed by what is written, never fixed
    memo = db.create_task(pid, "Write the strategy memo", writing=1, importance=5, deadline=db.today(), blocking=1)   # outranks any other writing task
    monkeypatch.setattr(sheets, "routines_for_today", lambda: [{"title": "Writing - 40 minutes", "minutes": 40}])
    monkeypatch.setattr(sheets, "card_items", lambda: [{"title": "Writing - 40 minutes", "minutes": 40}])
    witem = "routine:Writing - 40 minutes"
    f = c.get("/api/feed").json()
    assert f["current"]["key"] == witem and f["current"]["suggest"]["task"]["id"] == memo and f["current"]["suggest"]["verb"] == "Write"
    assert c.post("/api/feed/start", json={"card": witem}).status_code == 200
    assert db.running_entry()["task_id"] == memo and started[-1] == ("Writing the strategy memo", ["writing"])
    db.complete_task(memo); web._stop_timer()
    f = c.get("/api/feed").json()
    assert f["current"]["suggest"]["task"] is None and f["current"]["suggest"]["fallback"] == "Free writing"
    assert c.post("/api/feed/start", json={"card": witem, "label": "Morning pages"}).status_code == 200
    assert (db.running_entry()["kind"], started[-1]) == ("writing", ("Morning pages", ["writing"]))
    web._stop_timer()


def test_focus_monitor_prompt(monkeypatch):
    from fastapi.testclient import TestClient
    from tracker import web, sheets
    import time as _t
    monkeypatch.setattr(sheets, "writable", lambda: False)
    monkeypatch.setattr(web, "_in_work_day", lambda now=None: True)
    c = TestClient(web.create_app())
    db.set_setting("focus_monitor_next", str(_t.time() - 1))
    assert c.get("/api/feed").json()["focus_monitor"]["due"] is True
    r = c.post("/api/focus_monitor/done").json()
    assert 59 <= r["next_in_minutes"] <= 120 and "Review Focus Monitor" in db.checklist_done_today(db.today())
    assert c.get("/api/feed").json()["focus_monitor"]["due"] is False
    monkeypatch.setattr(web, "_in_work_day", lambda now=None: False)
    db.set_setting("focus_monitor_next", str(_t.time() - 1))
    assert c.get("/api/feed").json()["focus_monitor"]["due"] is False            # never outside the work day
    monkeypatch.setitem(config.load()["focus_monitor"], "enable", False)
    assert c.get("/api/feed").json()["focus_monitor"] == {"enabled": False, "due": False, "next_in_minutes": None}


def test_completed_task_keeps_its_place():
    pid = db.create_project("Keep order")
    a, b, c = (db.create_task(pid, n) for n in ("first", "second", "third"))
    db.complete_task(a)
    proj = next(p for p in db.project_tree() if p["id"] == pid)
    assert [t["title"] for t in proj["tasks"]] == ["first", "second", "third"]


def test_project_paths_ignore_heading_marks():
    """Quick add gets "Palisade Projects" back from Claude for a project stored as "# Palisade Projects"."""
    top = db.create_project("# Heading top")
    sub = db.create_project("## Policy", top)
    assert db.plain_name("##  Policy  ") == "policy" and db.plain_name("Policy-Maker  Engagement") == "policymakerengagement"
    assert db.find_project_by_path(["Heading top", "policy"]) == sub
    assert db.find_project_by_path(["# Heading top", "## Policy"]) == sub
    assert db.ensure_project_path(["heading top", "Policy"]) == sub       # nothing new created
    assert db.find_project_by_path(["Heading top", "Nope"]) is None
    assert "- Heading top" in scheduler.tree_text() and "- # Heading top" not in scheduler.tree_text()   # case kept, marks dropped
    dup = db.create_project("Heading top")                              # a stray empty duplicate beside the real one
    assert db.find_project_by_path(["Heading top", "Policy"]) == sub     # the populated one wins over the exact spelling
    assert db.find_project_by_path(["heading top"]) == top
    db.delete_empty_project(dup)


def test_project_toggl_link_api_and_settings(monkeypatch):
    from fastapi.testclient import TestClient
    from tracker import web, toggl
    c = TestClient(web.create_app())
    pid = db.create_project("Linkable")
    assert c.patch(f"/api/projects/{pid}", json={"toggl_project": "  Palisade "}).status_code == 200
    assert db.row("SELECT toggl_project FROM projects WHERE id=?", (pid,))["toggl_project"] == "Palisade"
    assert any(p["id"] == pid and p["toggl_project"] == "Palisade" for p in c.get("/api/tree").json())
    assert c.patch(f"/api/projects/{pid}", json={"toggl_project": ""}).status_code == 200    # unlinked: Claude decides again
    assert db.row("SELECT toggl_project FROM projects WHERE id=?", (pid,))["toggl_project"] is None
    assert c.patch("/api/projects/999999", json={"name": "x"}).status_code == 404
    labels = web._routine_labels()
    assert "Anki review" in labels and "squats" in labels and "cardio interval training" in labels and "Journaling" in labels
    monkeypatch.setattr(toggl, "enabled", lambda: True)
    assert c.post("/api/toggl/suggest_routines").status_code == 400          # Claude is off in tests
    assert c.get("/api/toggl/projects").json()["enabled"] is True
    monkeypatch.setattr(config, "CONFIG_PATH", pathlib.Path(_tmp) / "config.toml")   # never the user's own config.toml
    try:
        cfg = c.get("/api/settings").json()["config"]
        cfg["toggl"]["routine_projects"] = ["Anki review: Personal", "Journaling: Personal"]
        r = c.put("/api/settings", json={"config": cfg})
        assert r.status_code == 200 and config.load()["toggl"]["routine_projects"] == ["Anki review: Personal", "Journaling: Personal"]
        _fake_toggl(monkeypatch)
        toggl.remember_routine("going to do squats", "Palisade", ["exercise", "strength"])
        assert config.load()["toggl"]["routine_projects"][-1] == "squats: Palisade | exercise, strength"
        toggl.remember_routine("Squats", "Personal")                          # replaces, never duplicates
        assert [l for l in config.load()["toggl"]["routine_projects"] if l.startswith("squats")] == ["squats: Personal"]
        # the page saves on every edit: the calendar is fetched again only when a calendar setting changed
        from tracker import calendar_feed
        calendar_feed._cache["u"] = (0.0, b"")
        c.put("/api/settings", json={"config": cfg})
        assert "u" in calendar_feed._cache
        cfg["calendar"]["ignore_titles"] = ["Lunch"]
        c.put("/api/settings", json={"config": cfg})
        assert "u" not in calendar_feed._cache
        cfg["toggl"]["routine_projects"] = ["Anki review: Nowhere"]
        assert c.put("/api/settings", json={"config": cfg}).json()["warnings"] == ["no Toggl project called 'Nowhere' (for anki review)"]
    finally:
        config.reload()
        toggl.forget_projects()


def test_exercise_rules_engine():
    from tracker import exercise, tags
    rules = exercise.parse_rules(config.exercise_text())   # seeded from Exercise plan.example.md in the temp data dir
    assert [(r["name"], r["period"], r["gap"], r["ideal"]) for r in rules["required"]] == [("squats", 7, 3, (4, 4)), ("cardio interval training", 4, 2, (2, 3))]
    assert rules["never_adjacent"] == [["deadlifts", "squats"]] and rules["fill"] == ["benchpress", "deadlifts", "shoulder press"]
    today = dt.date(2026, 11, 10)
    H = lambda name, days_ago, kind="strength": {"kind": kind, "exercise": name, "date": (today - dt.timedelta(days=days_ago)).isoformat(), "weekday": "", "minutes": 40, "source": "sheet"}
    # both past their deadline: the one done longest ago first
    d = exercise.rules_decide(rules, [H("Cardio Interval Training", 5, "cardio"), H("Squats", 8)], today)
    assert d["title"] == "squats" and "8 days" in d["reason"]
    # squats overdue but deadlifts were yesterday: never back to back, so cardio (also overdue) instead
    d = exercise.rules_decide(rules, [H("Deadlifts", 1), H("Cardio Interval Training", 4, "cardio"), H("Squats", 9)], today)
    assert d["title"] == "cardio interval training"
    # ideal cadence: squats 3 days ago are allowed but not yet wanted (ideal 4); cardio 2 days ago is at the start of its window
    d = exercise.rules_decide(rules, [H("Cardio Interval Training", 2, "cardio"), H("Squats", 3)], today)
    assert d["title"] == "cardio interval training"
    d = exercise.rules_decide(rules, [H("Cardio Interval Training", 2, "cardio"), H("Squats", 4)], today)
    assert d["title"] == "squats"                           # squats at their ideal day beat cardio at the start of its window
    d = exercise.rules_decide(rules, [H("Cardio Interval Training", 3, "cardio"), H("Squats", 4)], today)
    assert d["title"] == "squats"                           # both at the top of their windows: the one done longer ago
    # nothing wanted: the fill-in done longest ago; deadlifts avoided the day before squats' deadline
    hist = [H("Cardio Interval Training", 1, "cardio"), H("Squats", 2), H("Benchpress", 3), H("Shoulder Press", 5), H("Deadlifts", 9)]
    assert exercise.rules_decide(rules, hist, today)["title"] == "deadlifts"
    hist2 = [H("Cardio Interval Training", 1, "cardio"), H("Squats", 6), H("Benchpress", 3), H("Shoulder Press", 5), H("Deadlifts", 9)]
    assert exercise.rules_decide(rules, hist2, today)["title"] == "squats"   # 6 days: past the ideal window, not yet the deadline, but wanted
    hist3 = [H("Cardio Interval Training", 1, "cardio"), H("Squats", 1), H("Benchpress", 3), H("Shoulder Press", 5), H("Deadlifts", 9)]
    assert exercise.rules_decide(rules, hist3, today)["title"] == "shoulder press"   # deadlifts blocked: squats were yesterday
    # never logged: due at once
    assert exercise.rules_decide(rules, [], today)["title"] == "squats"
    assert exercise._same("squats", "Back Squats") and exercise._same("Squat", "squats") and not exercise._same("squats", "deadlifts")
    assert not exercise._same("squats", "cardio interval training")


def test_exercise_decide_uses_rules_and_log_lengths(monkeypatch):
    from tracker import exercise, tags
    today = dt.date.today()
    monkeypatch.setattr(exercise, "history", lambda days=90: [
        {"kind": "cardio", "exercise": "Cardio interval training", "date": (today - dt.timedelta(days=1)).isoformat(), "weekday": "", "minutes": 25, "source": "sheet"},
        {"kind": "strength", "exercise": "Squats", "date": (today - dt.timedelta(days=9)).isoformat(), "weekday": "", "minutes": 52, "source": "sheet"},
        {"kind": "strength", "exercise": "Squats", "date": (today - dt.timedelta(days=16)).isoformat(), "weekday": "", "minutes": 48, "source": "sheet"}])
    db.set_setting("exercise_decision", "")
    d = exercise.decide(force=True)
    assert d["title"] == "squats" and d["kind"] == "strength" and d["minutes"] == 50 and "9 days" in d["reason"]


def test_exercise_notes_exclude_rule_lines():
    from tracker import exercise, tags
    txt = "[help]\nsquats: at least every 7 days\nfill: a, b\nnever back to back: a, b\nMy knee is sore this week.\nTiming: afternoons.\n"
    assert exercise.notes_text(txt) == "My knee is sore this week.\nTiming: afternoons."


def test_routine_cards_from_checklist(monkeypatch):
    from fastapi.testclient import TestClient
    from tracker import sheets, web
    monkeypatch.setitem(config.load()["sheets"], "card_items", ["Anki review: 20", "Writing - 40 minutes", "Nope: x"])
    monkeypatch.setitem(config.load()["reading"], "routine", "")          # the daily reading card is tested on its own
    assert sheets.card_items() == [{"title": "Anki review", "minutes": 20}, {"title": "Writing - 40 minutes", "minutes": 20}, {"title": "Nope: x", "minutes": 20}]
    monkeypatch.setattr(sheets, "daily_checklist", lambda: {"items": [{"item": "Anki Review", "done_today": False}, {"item": "Writing - 40 minutes", "done_today": True}], "source": "test"})
    monkeypatch.setattr(sheets, "writable", lambda: False)
    db.execute("DELETE FROM checklist_log")
    assert [r["title"] for r in sheets.routines_for_today()] == ["Anki Review", "Nope: x"]
    # completing the routine's card logs the tick; undo clears it
    date = db.today()
    c = TestClient(web.create_app())
    assert c.get("/api/feed").json()["current"]["key"] == "routine:Anki Review"            # recurring cards come first
    assert c.post("/api/feed/complete", json={"card": "routine:Anki Review"}).status_code == 200
    assert "Anki Review" in db.checklist_done_today(date) and [r["title"] for r in sheets.routines_for_today()] == ["Nope: x"]
    assert c.post("/api/feed/uncomplete", json={"card": "routine:Anki Review"}).status_code == 200
    assert "Anki Review" not in db.checklist_done_today(date)


def test_new_task_goes_right_after_a_completed_one():
    pid = db.create_project("Enter on a done line")
    a = db.create_task(pid, "First")
    b = db.create_task(pid, "Done in the middle")
    c = db.create_task(pid, "Also done")
    d = db.create_task(pid, "Last")
    db.complete_task(b); db.complete_task(c)
    db.create_task(pid, "New after the done one", after_id=b)
    titles = [t["title"] for t in next(p for p in db.project_tree() if p["id"] == pid)["tasks"]]
    assert titles == ["First", "Done in the middle", "New after the done one", "Also done", "Last"]


def test_project_subtitle_is_stored_and_in_the_tree():
    pid = db.create_project("With a subtitle")
    assert next(p for p in db.project_tree() if p["id"] == pid)["subtitle"] == ""
    db.update_project(pid, subtitle="What this is for")
    assert next(p for p in db.project_tree() if p["id"] == pid)["subtitle"] == "What this is for"


def test_archive_task_unfinished_takes_sub_items_and_restores():
    pid = db.create_project("Archive one")
    a = db.create_task(pid, "Parent to put away")
    b = db.create_task(pid, "Child of it", parent_id=a)
    keep = db.create_task(pid, "Stays")
    titles = lambda **kw: [t["title"] for t in next(p for p in db.project_tree(**kw) if p["id"] == pid)["tasks"]]
    ids = db.archive_task(a)
    assert sorted(ids) == [a, b] and db.task(a)["completed_at"] is None          # put away, not completed
    assert titles() == ["Stays"] and titles(completed=True) == ["Parent to put away", "Stays"]
    assert {a, b}.isdisjoint({t["id"] for t in db.open_tasks()})
    assert "Parent to put away" not in {c["title"] for c in scheduler.candidates(dt.date.today())}
    assert sorted(db.restore_task(a)) == [a, b]
    assert titles() == ["Parent to put away", "Stays"] and keep in {t["id"] for t in db.open_tasks()}


def test_completed_tasks_stay_until_archived():
    pid = db.create_project("Archive done")
    a = db.create_task(pid, "Open one")
    b = db.create_task(pid, "Done one")
    db.complete_task(b)
    tree = next(p for p in db.project_tree() if p["id"] == pid)
    assert [t["title"] for t in tree["tasks"]] == ["Open one", "Done one"]          # still shown, struck through in the UI
    ids = db.archive_completed(pid)
    assert ids == [b]
    assert [t["title"] for t in next(p for p in db.project_tree() if p["id"] == pid)["tasks"]] == ["Open one"]
    assert [t["title"] for t in next(p for p in db.project_tree(completed=True) if p["id"] == pid)["tasks"]] == ["Open one", "Done one"]
    db.unarchive_tasks(ids)
    assert len(next(p for p in db.project_tree() if p["id"] == pid)["tasks"]) == 2
    db.uncomplete_task(b)
    assert db.task(b)["completed_at"] is None and db.task(b)["archived_at"] is None


def test_stale_exercise_decision_is_redecided():
    """A remembered pick from before the rules existed (the old rotation's "run") does not survive the day."""
    from tracker import exercise, tags
    rules = exercise.parse_rules("squats: at least every 7 days, at most every 3 days, ideally every 4 days\n"
                                 "fill: benchpress, deadlifts")
    assert not exercise._from_rules({"title": "run", "reason": "rotation (Claude not configured)"}, rules)
    assert exercise._from_rules({"title": "squats", "reason": "squats overdue"}, rules)
    assert exercise._from_rules({"title": "Deadlifts", "reason": "fill"}, rules)
    assert exercise._from_rules({"title": "yoga", "reason": "Claude overrode the rules", "overrode": "squats"}, rules)
    assert exercise._from_rules({"title": "run"}, exercise.parse_rules(""))   # no rules yet: anything stands
    assert tags.parse_tags("Write up #deadline:tomorrow", today=dt.date(2026, 11, 4)) == ("Write up", {"deadline": "2026-11-05"})


def test_hard_and_soft_deadlines():
    from tracker import tags
    wed = dt.date(2026, 10, 28)
    title, f = tags.parse_tags("Grant report #soft:fri #due:nov3", today=wed)
    assert title == "Grant report" and f == {"soft_deadline": "2026-10-30", "deadline": "2026-11-03"}   # both at once
    assert tags.parse_tags("x #aim:fri", today=wed)[1] == {"soft_deadline": "2026-10-30"}
    assert tags.parse_tags("x #ideally:nov3", today=wed)[1] == {"soft_deadline": "2026-11-03"}
    assert tags.parse_tags("x #hard:fri", today=wed)[1] == {"deadline": "2026-10-30"}
    assert tags.parse_tags("x #nosoft #nodue", today=wed)[1] == {"soft_deadline": None, "deadline": None}
    today = dt.date.today()
    base = {"importance": 3, "blocking": 0, "created_at": dt.datetime.now().timestamp()}
    hard = scheduler.priority_score({**base, "deadline": today.isoformat()}, today)
    soft = scheduler.priority_score({**base, "soft_deadline": today.isoformat()}, today)
    none = scheduler.priority_score(base, today)
    both = scheduler.priority_score({**base, "deadline": (today + dt.timedelta(days=10)).isoformat(), "soft_deadline": today.isoformat()}, today)
    assert hard > soft > none                      # a hard deadline today outranks a soft one today
    assert abs(both - soft) < 0.01                 # with both, the more pressing date counts (age ticks between calls)
    pid = db.create_project("Deadlines")
    tid = db.create_task(pid, "Both", deadline="2026-12-01", soft_deadline="2026-11-20", estimate_minutes=30)
    t = db.task(tid)
    assert t["deadline"] == "2026-12-01" and t["soft_deadline"] == "2026-11-20"
    from tracker import snapshot
    assert "#due:2026-12-01" in snapshot._tags(t) and "#soft:2026-11-20" in snapshot._tags(t)


def test_moving_a_line_in_the_priority_list_sets_its_priority():
    pid = db.create_project("Priority list")
    a = db.create_task(pid, "A", importance=5); b = db.create_task(pid, "B", importance=5); d = db.create_task(pid, "D", importance=3)
    c = db.create_task(pid, "C"); u = db.create_task(pid, "U")
    mine = lambda: [t["id"] for t in db.priority_list() if t["id"] in (a, b, c, d, u)]
    assert mine() == [a, b, d, c, u]                                  # highest first, no priority last
    was = db.priority_move(c, b)                                      # between A and B (both priority 1): priority 1
    assert mine() == [a, c, b, d, u] and db.task(c)["importance"] == 5 and db.task(c)["importance_by"] == "user"
    assert was == {"importance": None, "before_id": u}
    db.priority_move(c, was["before_id"], importance=was["importance"])       # undo puts it back as it was
    assert mine() == [a, b, d, c, u] and db.task(c)["importance"] is None
    db.priority_move(c, d)                                            # between B (1) and D (3): the lowest that fits, 3
    assert mine() == [a, b, c, d, u] and db.task(c)["importance"] == 3
    db.priority_move(a, u)                                            # above a line with no priority: none, but it keeps its place
    assert db.task(a)["importance"] is None and mine() == [b, c, d, a, u]
    db.priority_move(b, None)                                         # the very end
    assert db.task(b)["importance"] is None and mine()[-1] == b
    db.priority_move(u, c)                                            # to the top: the priority of the line below
    assert db.task(u)["importance"] == 3 and mine()[0] == u
    db.priority_move(d, c)                                            # reordering inside a level changes no priority and sticks
    assert mine()[:3] == [u, d, c] and db.task(d)["importance"] == 3
    import pytest
    with pytest.raises(ValueError):
        db.priority_move(a, a)
    from fastapi.testclient import TestClient
    from tracker import web
    cl = TestClient(web.create_app())
    r = cl.post("/api/priority/move", json={"id": a, "before_id": u}).json()
    assert r["task"]["importance"] == 3 and r["was"]["importance"] is None and [t["id"] for t in r["tasks"] if t["id"] in (a, u)] == [a, u]
    assert r["tasks"][0]["path"] and cl.post("/api/priority/move", json={"id": 999999, "before_id": None}).status_code == 400
    done = db.create_task(pid, "Done one", importance=5); db.complete_task(done)
    assert done not in [t["id"] for t in cl.get("/api/priority").json()]
