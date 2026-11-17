"""Running the server away from the Mac: no Keychain, and the read-only copy (TASK_TRACKER_REPLICA)."""


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


def test_the_copy_refuses_writes_and_runs_no_background_loop(monkeypatch):
    from fastapi.testclient import TestClient
    from tracker import web
    monkeypatch.setattr(web, "REPLICA", True)
    monkeypatch.setattr(web, "_bg_started", False)
    c = TestClient(web.create_app())
    assert c.get("/api/replica").json()["replica"] is True
    r = c.post("/api/agent/tasks", json={"title": "x"})
    assert r.status_code == 503 and "read-only copy" in r.json()["detail"]
    assert c.get("/api/agent/tree").status_code == 200           # reads still work
    web._start_background()
    assert web._bg_started is False                              # never talks to Toggl, Linear or Claude on its own
