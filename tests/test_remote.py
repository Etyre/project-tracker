"""The Mac app's relay to a tracker server on another machine (tracker/remote.py)."""
import json

import httpx
from fastapi.testclient import TestClient

from tracker import remote


def _relay(handler):
    return TestClient(remote.create_relay("http://server:8791", transport=httpx.MockTransport(handler)))


def test_requests_and_answers_pass_through_unchanged():
    seen = []

    def server(req):
        seen.append((req.method, req.url.path, dict(req.url.params), req.content, req.headers.get("x-agent-name")))
        if req.url.path == "/":
            return httpx.Response(200, html="<h1>dashboard</h1>")
        return httpx.Response(201, json={"id": 7}, headers={"x-extra": "kept"})

    c = _relay(server)
    assert c.get("/").text == "<h1>dashboard</h1>"
    r = c.post("/api/agent/tasks?dry=1", json={"title": "call the carpet guy"}, headers={"X-Agent-Name": "Atlas"})
    assert r.status_code == 201 and r.json() == {"id": 7} and r.headers["x-extra"] == "kept"
    assert seen[1] == ("POST", "/api/agent/tasks", {"dry": "1"}, b'{"title":"call the carpet guy"}', "Atlas")


def test_window_is_local_and_journal_opens_the_notes_app_here(monkeypatch):
    shown, opened = [], []
    monkeypatch.setattr(remote, "show_window_hook", lambda fragment=None: shown.append(fragment))
    monkeypatch.setattr(remote.journal, "open_notes_app", lambda cfg=None: opened.append(cfg) or None)

    def server(req):
        assert req.url.path != "/api/window"                      # never sent to the server
        if req.url.path == "/api/settings":
            return httpx.Response(200, json={"config": {"journal": {"app": "Roam Research", "desktop": 1}}})
        return httpx.Response(200, json={"ok": True, "started": True, "open_error": None})

    c = _relay(server)
    assert c.post("/api/window?fragment=feed").json() == {"ok": True} and shown == ["feed"]
    out = c.post("/api/journal/start").json()
    assert out["started"] and out["open_error"] is None and opened == [{"app": "Roam Research", "desktop": 1}]


def test_an_unreachable_server_is_explained_not_hidden():
    def server(req):
        raise httpx.ConnectError("no route to host")

    c = _relay(server)
    page = c.get("/")
    assert page.status_code == 502 and "not reachable" in page.text and "location.reload" in page.text
    api = c.get("/api/feed")
    assert api.status_code == 502 and "not reachable" in json.loads(api.text)["detail"]


def test_secrets_go_to_the_env_file_where_there_is_no_keychain(tmp_path, monkeypatch):
    from tracker import config
    monkeypatch.setattr(config, "APP_SUPPORT", tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "env")
    monkeypatch.setattr(config.sys, "platform", "linux")
    monkeypatch.delenv("TOGGL_API_TOKEN", raising=False)
    import functools
    monkeypatch.setattr(config, "secret", functools.lru_cache(maxsize=None)(lambda name: None))   # another test file stubs it bare
    (tmp_path / "env").write_text("TOGGL_API_TOKEN=old\nLINEAR_API_KEY=keep\n")
    config.keychain_set("toggl", "new-token")
    assert (tmp_path / "env").read_text() == "LINEAR_API_KEY=keep\nTOGGL_API_TOKEN=new-token\n"
    assert oct((tmp_path / "env").stat().st_mode)[-3:] == "600" and config.os.environ["TOGGL_API_TOKEN"] == "new-token"
    monkeypatch.delenv("TOGGL_API_TOKEN", raising=False)
