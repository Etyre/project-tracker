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
    assert path == tmp_path / "2030-01-02.md" and path.read_text().startswith("# Task tracker snapshot")
    assert snapshot.maybe_snapshot(dt.datetime(2030, 1, 2, 9, 0)) is None
    assert snapshot.write(dt.datetime(2030, 1, 2, 9, 0)) == path
    assert [i["date"] for i in snapshot.list_snapshots()] == ["2030-01-02"]
    assert snapshot.read("2030-01-02").startswith("# Task tracker snapshot") and snapshot.read("nope") is None


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
    assert set(config.secret_status()) == {"anthropic", "toggl", "linear", "sheets"}
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
    from tracker import exercise
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
    assert c.post("/api/feed/complete", json={"item_id": item}).status_code == 200
    assert db.task(tid)["completed_at"] and db.plan_item(item)["status"] == "done"
    assert c.post("/api/feed/uncomplete", json={"item_id": item}).status_code == 200
    assert db.task(tid)["completed_at"] is None and db.plan_item(item)["status"] == "pending"


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


def test_toggl_history_index_and_exact_match(monkeypatch):
    from tracker import toggl, web
    monkeypatch.setattr(toggl, "_req", lambda m, path, body=None: [{"id": 1, "name": "Palisade"}, {"id": 2, "name": "Personal"}] if path == "/me/projects" else None)
    monkeypatch.setattr(toggl, "entries", lambda days=90: [
        {"description": "Going to do squats", "project_id": 2, "tags": ["exercise"], "start": "2026-11-01T20:00:00+00:00"},
        {"description": "going to do squats", "project_id": 1, "tags": [], "start": "2026-10-01T20:00:00+00:00"},
        {"description": "Strategy sync", "project_id": 1, "tags": ["meeting"], "start": "2026-11-02T14:00:00+00:00"},
        {"description": "", "project_id": None, "tags": [], "start": "2026-11-02T15:00:00+00:00"}])
    toggl._index_cache = None
    idx = toggl.history_index()
    assert idx["exact"]["going to do squats"] == {"project_id": 2, "project": "Personal", "tags": ["exercise"]}   # newest wins
    assert [p["name"] for p in idx["projects"]][:2] == ["Palisade", "Personal"] and "meeting" in idx["tags"]
    c = toggl.classify("GOING TO DO SQUATS")
    assert (c["project"], c["tags"], c["source"]) == ("Personal", ["exercise"], "history")
    assert toggl.classify("Something never tracked")["source"] == "none"       # Claude is off in tests: no project
    assert web._going_to("squats") == "going to do squats" and web._going_to("Exercise: cardio") == "going to do cardio"
    assert web._going_to("Going to do a run") == "Going to do a run"
    toggl._index_cache = None




def test_exercise_rules_engine():
    from tracker import exercise
    rules = exercise.parse_rules(pathlib.Path(config.EXERCISE_PATH).read_text())
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
    from tracker import exercise
    today = dt.date.today()
    monkeypatch.setattr(exercise, "history", lambda days=90: [
        {"kind": "cardio", "exercise": "Cardio interval training", "date": (today - dt.timedelta(days=1)).isoformat(), "weekday": "", "minutes": 25, "source": "sheet"},
        {"kind": "strength", "exercise": "Squats", "date": (today - dt.timedelta(days=9)).isoformat(), "weekday": "", "minutes": 52, "source": "sheet"},
        {"kind": "strength", "exercise": "Squats", "date": (today - dt.timedelta(days=16)).isoformat(), "weekday": "", "minutes": 48, "source": "sheet"}])
    db.set_setting("exercise_decision", "")
    d = exercise.decide(force=True)
    assert d["title"] == "squats" and d["kind"] == "strength" and d["minutes"] == 50 and "9 days" in d["reason"]


def test_exercise_notes_exclude_rule_lines():
    from tracker import exercise
    txt = "[help]\nsquats: at least every 7 days\nfill: a, b\nnever back to back: a, b\nMy knee is sore this week.\nTiming: afternoons.\n"
    assert exercise.notes_text(txt) == "My knee is sore this week.\nTiming: afternoons."


def test_routine_cards_from_checklist(monkeypatch):
    from fastapi.testclient import TestClient
    from tracker import sheets, web
    monkeypatch.setitem(config.load()["sheets"], "card_items", ["Anki review: 20", "Writing - 40 minutes", "Nope: x"])
    assert sheets.card_items() == [{"title": "Anki review", "minutes": 20}, {"title": "Writing - 40 minutes", "minutes": 20}, {"title": "Nope: x", "minutes": 20}]
    monkeypatch.setattr(sheets, "daily_checklist", lambda: {"items": [{"item": "Anki Review", "done_today": False}, {"item": "Writing - 40 minutes", "done_today": True}], "source": "test"})
    monkeypatch.setattr(sheets, "writable", lambda: False)
    db.execute("DELETE FROM checklist_log")
    assert [r["title"] for r in sheets.routines_for_today()] == ["Anki Review", "Nope: x"]
    # completing the routine's card logs the tick; undo clears it
    date = db.today()
    db.save_plan(date, "09:00", "17:00", "run", "", [dict(kind="routine", task_id=None, title="Anki Review", start="09:00", end="09:20", minutes=20, note="")], {})
    item = db.rows("SELECT id FROM plan_items WHERE kind='routine' AND date=?", (date,))[0]["id"]
    c = TestClient(web.create_app())
    assert c.post("/api/feed/complete", json={"item_id": item}).status_code == 200
    assert "Anki Review" in db.checklist_done_today(date) and [r["title"] for r in sheets.routines_for_today()] == ["Nope: x"]
    assert c.post("/api/feed/uncomplete", json={"item_id": item}).status_code == 200
    assert "Anki Review" not in db.checklist_done_today(date)
