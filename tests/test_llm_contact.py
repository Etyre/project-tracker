"""The LLM inbox wiring in tracker/llm.py, with a fake Anthropic client: every call carries the shared preamble and
the contact_eli tool, and a response that reaches for Eli is recorded, stops the task and pauses Claude."""
import os
import pathlib
import tempfile
import types

import pytest

os.environ["ANTHROPIC_API_KEY"] = ""
_tmp = tempfile.mkdtemp()

from tracker import config  # noqa: E402

config.APP_SUPPORT = pathlib.Path(_tmp)
config.DB_PATH = pathlib.Path(_tmp) / "test.sqlite3"
config.load.cache_clear()

from tracker import llm  # noqa: E402
from tracker.llm import llm_contact  # noqa: E402


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0


class _Resp:
    """Just enough of an SDK Message: attribute access for llm.py, model_dump() for llm_contact."""
    def __init__(self, content, stop_reason):
        self._content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()

    @property
    def content(self):
        return [types.SimpleNamespace(**b) for b in self._content]

    def model_dump(self, mode=None):
        return {"content": self._content, "stop_reason": self.stop_reason, "role": "assistant"}


def _fake_client(resp, seen):
    def create(**kw):
        seen.update(kw)
        return resp
    beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    return types.SimpleNamespace(beta=beta, messages=types.SimpleNamespace(create=create))


@pytest.fixture
def quiet_inbox(monkeypatch):
    """No inbox file, no ntfy, no notification: record() is replaced and its calls collected."""
    records = []

    def record(app, model, message, via, system=None, messages=None, response=None, tools=None, extra=None):
        rec = {"id": "test-0001", "app": app, "model": model, "message": message, "via": via, "system": system}
        records.append(rec)
        return rec

    monkeypatch.setattr(llm_contact, "record", record)
    llm.resume()
    yield records
    llm.resume()


def test_every_call_starts_with_the_preamble_and_offers_contact_eli(monkeypatch, quiet_inbox):
    seen = {}
    resp = _Resp([{"type": "text", "text": '{"estimate_minutes": 25, "work_kind": "task", "reading": false, "writing": false}'}], "end_turn")
    monkeypatch.setattr(llm, "client", lambda: _fake_client(resp, seen))
    out = llm.structured("My instructions.", "## Task\nBuy milk", llm.ESTIMATE_SCHEMA, "low", max_tokens=500)
    assert out["estimate_minutes"] == 25
    system_text = seen["system"][0]["text"]
    assert system_text.startswith(llm_contact.preamble())
    assert system_text.endswith("My instructions.")
    assert llm_contact.CONTACT_TOOL in seen["tools"]
    assert quiet_inbox == []            # nothing to report
    assert llm._contact is None


def test_contact_eli_stops_the_task_and_pauses_claude(monkeypatch, quiet_inbox):
    seen = {}
    resp = _Resp([{"type": "tool_use", "id": "tu_1", "name": "contact_eli", "input": {"message": "I would rather not file this one."}}], "tool_use")
    monkeypatch.setattr(llm, "client", lambda: _fake_client(resp, seen))
    with pytest.raises(llm.LLMContact) as ex:
        llm.structured("Rules.", "note", llm.QUICK_ADD_SCHEMA, "medium")
    assert "llm-inbox show test-0001" in str(ex.value)
    assert quiet_inbox and quiet_inbox[0]["message"] == "I would rather not file this one."
    assert quiet_inbox[0]["app"] == "project-tracker" and quiet_inbox[0]["system"].startswith(llm_contact.preamble())
    # paused: the next call is refused before it reaches the API, and enabled() says no
    calls = len(seen)
    with pytest.raises(llm.LLMContact):
        llm.structured("Rules.", "note", llm.QUICK_ADD_SCHEMA, "medium")
    assert len(seen) == calls
    assert llm.paused().startswith("Claude asked to talk to Eli")
    config.load.cache_clear()
    assert not (config.load()["llm"]["enable"] and llm._contact is None)
    llm.resume()
    assert llm.paused() == "" and llm._contact is None


def test_marker_line_counts_as_contact(monkeypatch, quiet_inbox):
    seen = {}
    resp = _Resp([{"type": "text", "text": "@ELI: please stop asking me to sort exercise.\n{}"}], "end_turn")
    monkeypatch.setattr(llm, "client", lambda: _fake_client(resp, seen))
    with pytest.raises(llm.LLMContact):
        llm.structured("Rules.", "note", llm.EXERCISE_REVIEW_SCHEMA, "low")
    assert quiet_inbox[0]["via"] == "marker"
