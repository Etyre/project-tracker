"""The executive-assistant agent's API: adding tasks and projects, signed context on any item, setting the schedule."""
import datetime as dt
import os
import pathlib
import tempfile

os.environ["ANTHROPIC_API_KEY"] = ""     # deterministic paths only
_tmp = tempfile.mkdtemp()

from tracker import config  # noqa: E402

config.APP_SUPPORT = pathlib.Path(_tmp)
config.DB_PATH = pathlib.Path(_tmp) / "test.sqlite3"
config.load.cache_clear()

from tracker import agent_api, calendar_feed, db, llm, scheduler, web  # noqa: E402

llm._has_credentials = lambda: False
llm.enabled = lambda: False
calendar_feed.events_for = lambda day: []          # no real calendar in tests: the whole work window is free
web._bg_started = True                             # no background loop under test
agent_api.config.secret = lambda name: None        # no token configured (whatever the Keychain holds)

from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(web.create_app())
H = {"X-Agent-Name": "Ada"}
TODAY = dt.date.today()


def test_ping_names_the_agent_and_lists_the_calls():
    r = client.get("/api/agent", headers=H)
    assert r.status_code == 200
    j = r.json()
    assert j["agent"] == "Ada" and j["token_required"] is False and "PUT /api/agent/schedule" in j["endpoints"]
    assert client.get("/api/agent/").json()["agent"] == "assistant"


def test_token_is_enforced_when_configured(monkeypatch):
    monkeypatch.setattr(agent_api.config, "secret", lambda name: "s3cret" if name == "agent" else None)
    assert client.get("/api/agent").status_code == 401
    assert client.get("/api/agent", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/agent", headers={"Authorization": "Bearer s3cret"}).json()["token_required"] is True
    assert client.get("/api/status").status_code == 200       # the app's own API is untouched


def test_projects_and_tasks_are_flagged_as_the_agents():
    r = client.post("/api/agent/projects", json={"name": "Agent Work"}, headers=H)
    pid = r.json()["id"]
    assert db.row("SELECT source FROM projects WHERE id=?", (pid,))["source"] == "agent"
    r = client.post("/api/agent/projects", json={"name": "Sub", "parent_path": ["Agent Work"]}, headers=H)
    assert r.json()["path"] == ["Agent Work", "Sub"]
    assert client.post("/api/agent/projects", json={"name": "X", "parent_path": ["Nope"]}, headers=H).status_code == 404
    r = client.post("/api/agent/tasks", json={"title": "Book flights #p2", "project_path": ["Agent Work", "Sub"],
                                               "deadline": "2026-12-01", "note": "Eli mentioned this in the 1:1"}, headers=H)
    t = r.json()
    assert t["source"] == "agent" and t["importance"] == 4 and t["deadline"] == "2026-12-01" and t["title"] == "Book flights"
    assert t["path"] == ["Agent Work", "Sub"] and t["notes_added"][0]["author"] == "Ada"
    r = client.post("/api/agent/tasks", json={"title": "Compare fares", "parent_id": t["id"]}, headers=H)
    assert r.json()["parent_id"] == t["id"] and r.json()["project_id"] == t["project_id"]
    r = client.post("/api/agent/tasks", json={"title": "Loose end"}, headers=H)          # nowhere to put it, no Claude
    assert r.json()["path"] == ["Inbox"] and r.json()["source"] == "agent"
    r = client.post("/api/agent/tasks", json={"title": "Fresh", "project_path": ["Agent Work", "New area"], "create_project": True}, headers=H)
    assert r.json()["path"] == ["Agent Work", "New area"]
    assert client.post("/api/agent/tasks", json={"title": "x", "project_id": pid, "deadline": "someday"}, headers=H).status_code == 400
    assert client.post("/api/agent/tasks", json={"title": "   ", "project_id": pid}, headers=H).status_code == 400
    r = client.post("/api/agent/quick_add", json={"text": "Call the bank #today"}, headers=H)
    c = r.json()["created"][0]
    assert c["source"] == "agent" and c["deadline"] == TODAY.isoformat() and c["path"] == ["Inbox"]
    # the UI's tree shows the chip's reason
    tree = client.get("/api/tree").json()
    p = next(p for p in tree if p["id"] == pid)
    assert p["source"] == "agent"


def test_notes_are_separate_from_the_users_text_and_travel_to_the_ui():
    pid = db.create_project("User Project")
    tid = db.create_task(pid, "User task", description="my own words")
    r = client.post("/api/agent/notes", json={"task_id": tid, "body": "Sam asked for this by Friday"}, headers=H)
    n = r.json()
    assert n["author"] == "Ada" and n["source"] == "agent" and n["target_kind"] == "task"
    assert db.task(tid)["description"] == "my own words"
    client.post("/api/agent/notes", json={"project_id": pid, "body": "Budget approved"}, headers=H)
    assert client.post("/api/agent/notes", json={"body": "x"}, headers=H).status_code == 400
    assert client.post("/api/agent/notes", json={"task_id": tid, "project_id": pid, "body": "x"}, headers=H).status_code == 400
    assert client.post("/api/agent/notes", json={"task_id": 999999, "body": "x"}, headers=H).status_code == 404
    assert client.post("/api/agent/notes", json={"task_id": tid, "body": "   "}, headers=H).status_code == 400
    tree = client.get("/api/tree").json()
    p = next(p for p in tree if p["id"] == pid)
    assert p["notes_added"][0]["body"] == "Budget approved"
    t = next(t for t in p["tasks"] if t["id"] == tid)
    assert t["notes_added"][0]["author"] == "Ada"
    assert client.get(f"/api/tasks/{tid}").json()["notes_added"][0]["id"] == n["id"]
    a = client.get("/api/agent/tree", headers=H).json()
    assert any(x["path"] == ["User Project"] and x["notes_added"] for x in a["projects"])
    assert client.get(f"/api/agent/tasks/{tid}", headers=H).json()["notes_added"][0]["id"] == n["id"]
    assert client.get("/api/agent/tasks", params={"q": "user task"}, headers=H).json()[0]["id"] == tid
    assert client.get("/api/agent/notes", params={"task_id": tid}, headers=H).json() == [n]
    assert client.delete(f"/api/agent/notes/{n['id']}", headers=H).status_code == 200
    assert db.notes_for("task", tid) == []
    assert client.delete(f"/api/agent/notes/{n['id']}", headers=H).status_code == 404
    client.post("/api/agent/notes", json={"task_id": tid, "body": "needs the quiet room"}, headers=H)
    c = next(c for c in scheduler.candidates(TODAY) if c["id"] == tid)
    assert c["agent_notes"] == ["[Ada] needs the quiet room"]        # what the planner's prompt sees


def test_agent_sets_the_schedule_and_it_stays_pinned():
    pid = db.create_project("Sched")
    a = db.create_task(pid, "Alpha", estimate_minutes=30)
    b = db.create_task(pid, "Beta", estimate_minutes=30)
    ctx = client.get("/api/agent/schedule", headers=H).json()
    assert ctx["date"] == TODAY.isoformat() and any(c["id"] == a for c in ctx["candidates"]) and ctx["free_windows"]
    items = [{"kind": "task", "task_id": a, "start": "10:00", "end": "10:45"},
             {"kind": "task", "task_id": b, "start": "10:45", "end": "11:15", "title": "Beta (draft)"},
             {"kind": "break", "title": "Break", "start": "11:15", "end": "11:25"},
             {"kind": "task", "task_id": a, "start": "03:00", "end": "03:30"}]          # outside the day: dropped
    r = client.put("/api/agent/schedule", json={"items": items, "work_start": "09:00", "work_end": "17:00", "rationale": "Ada's plan"}, headers=H)
    assert r.status_code == 200, r.text
    out = r.json()
    plan = out["plan"]
    assert plan["raw"]["source"] == "agent:Ada" and plan["raw"]["pinned"] is True and plan["rationale"] == "Ada's plan"
    got = [(i["kind"], i["title"], i["start"]) for i in plan["items"]]
    assert ("task", "Alpha", "10:00") in got and ("task", "Beta (draft)", "10:45") in got and ("break", "Break", "11:15") in got
    assert not any(i["start"] == "03:00" for i in plan["items"]) and out["dropped"] == ["Alpha at 03:00"]
    assert plan["work_start"] == "09:00" and plan["work_end"] == "17:00"
    noon = dt.datetime.combine(TODAY, dt.time(12, 30))
    assert scheduler.off_track(noon) is None                      # pinned: the drift re-planner leaves it alone
    first = next(i for i in plan["items"] if i["title"] == "Alpha")
    db.set_item_status(first["id"], "done")
    r = client.put("/api/agent/schedule", json={"items": items[:3], "work_start": "09:00", "work_end": "17:00"}, headers=H)
    assert next(i for i in r.json()["plan"]["items"] if i["title"] == "Alpha")["status"] == "done"   # re-sending keeps progress
    f = client.get("/api/feed").json()
    assert f["plan"]["pinned"] is True and f["plan"]["source"] == "agent:Ada"
    assert client.get("/api/agent/schedule", headers=H).json()["plan"]["pinned"] is True
    assert client.put("/api/agent/schedule", json={"items": [{"kind": "task", "task_id": 999999, "start": "10:00", "end": "10:30"}]}, headers=H).status_code == 400
    assert client.put("/api/agent/schedule", json={"items": [{"kind": "task", "task_id": a, "start": "10:00", "end": "09:30"}]}, headers=H).status_code == 400
    assert client.put("/api/agent/schedule", json={"items": [{"kind": "task", "start": "10:00", "end": "10:30"}]}, headers=H).status_code == 400
    assert client.put("/api/agent/schedule", json={"items": [{"kind": "nap", "title": "x", "start": "10:00", "end": "10:30"}]}, headers=H).status_code == 400
    r = client.post("/api/agent/schedule/unpin", headers=H)
    assert r.json()["raw"]["pinned"] is False
    assert scheduler.off_track(noon) and "should have started" in scheduler.off_track(noon)


def test_instructions_steer_the_days_plan(monkeypatch):
    today = TODAY.isoformat()
    r = client.put("/api/agent/schedule/instructions", json={"text": "End the day by 13:00; grant report first."}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["instructions"].startswith("End the day") and r.json()["plan"]["raw"]["instructions"].startswith("End the day")
    assert not r.json()["plan"]["raw"].get("pinned")                  # a rebuild hands the day back to the app
    assert scheduler.day_instructions(today).startswith("End the day")
    assert client.get("/api/agent/schedule", headers=H).json()["instructions"].startswith("End")
    seen = {}

    def fake_schedule(ctx_text, rules, exercise_brief=""):
        seen["rules"], seen["ctx"] = rules, ctx_text
        return {"rationale": "", "items": []}

    monkeypatch.setattr(scheduler.llm, "enabled", lambda: True)
    monkeypatch.setattr(scheduler.llm, "schedule", fake_schedule)
    scheduler.build(TODAY)
    assert "End the day by 13:00" in seen["rules"] and "ASSISTANT AGENT" in seen["rules"]
    assert '"assistant_instructions_today": "End the day by 13:00; grant report first."' in seen["ctx"]
    r = client.delete("/api/agent/schedule/instructions", headers=H)
    assert r.status_code == 200 and scheduler.day_instructions(today) == ""
    r = client.put("/api/agent/schedule/instructions", json={"text": "x", "date": "2026-13-45"}, headers=H)
    assert r.status_code == 400


def test_rebuild_from_the_agent():
    r = client.post("/api/agent/schedule/rebuild", json={"from_now": False}, headers=H)
    assert r.status_code == 200 and r.json()["items"] and r.json()["raw"]["source"] == "greedy"


def test_plain_patch_reads_deadline_words_and_deleting_a_task_drops_its_notes():
    pid = client.post("/api/agent/projects", json={"name": "Patch checks"}, headers=H).json()["id"]
    tid = client.post("/api/agent/tasks", json={"title": "Renew passport", "project_id": pid}, headers=H).json()["id"]
    # The agent's update_task goes through the app's own PATCH route: words must become dates, not be stored raw.
    r = client.patch(f"/api/tasks/{tid}", json={"deadline": "tomorrow"})
    assert r.status_code == 200 and r.json()["deadline"] == (TODAY + dt.timedelta(days=1)).isoformat()
    assert client.patch(f"/api/tasks/{tid}", json={"deadline": "2026-12-01"}).json()["deadline"] == "2026-12-01"
    assert client.patch(f"/api/tasks/{tid}", json={"deadline": "someday"}).status_code == 400
    assert client.patch(f"/api/tasks/{tid}", json={"clear_deadline": True}).json()["deadline"] is None
    # Notes attached to a task go with it when the task is deleted.
    nid = client.post("/api/agent/notes", json={"task_id": tid, "body": "ctx"}, headers=H).json()["id"]
    assert client.delete(f"/api/tasks/{tid}").status_code == 200
    assert all(n["id"] != nid for n in client.get("/api/agent/notes", headers=H).json())
