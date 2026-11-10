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
def quiet_inbox(monkeypatch, tmp_path):
    """No inbox file, no ntfy, no notification: record() is replaced and its calls collected. The files the confirm
    step writes itself (unconfirmed.jsonl, the log) go to a temp folder, never to the real LLMInbox."""
    records = []
    monkeypatch.setattr(llm_contact, "INBOX_DIR", tmp_path)
    monkeypatch.setattr(llm_contact, "MESSAGES_PATH", tmp_path / "messages.jsonl")
    monkeypatch.setattr(llm_contact, "UNCONFIRMED_PATH", tmp_path / "unconfirmed.jsonl")
    monkeypatch.setattr(llm_contact, "LOG_PATH", tmp_path / "llm-inbox.log")

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


# ---- confirm step: on a model in llm_contact.CONFIRM_MODELS a contact_eli call is asked about once before anything
# is sent; on any other model (the app's own claude-opus-5-5 included) it is sent at once with no question
_CONTACT = [{"type": "tool_use", "id": "tu_1", "name": "contact_eli", "input": {"message": "This note has no task in it."}}]
_ANSWER = [{"type": "text", "text": '{"estimate_minutes": 25, "work_kind": "task", "reading": false, "writing": false}'}]


def _decision(meant):
    return _Resp([{"type": "tool_use", "id": "tu_2", "name": "contact_eli_decision", "input": {"meant": meant}}], "tool_use")


def _scripted_client(script, calls):
    """Each call gets the next item of `script`; an exception in the script is raised instead of returned."""
    def create(**kw):
        calls.append(dict(kw, messages=list(kw["messages"])))
        nxt = script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt
    beta = types.SimpleNamespace(messages=types.SimpleNamespace(create=create))
    return types.SimpleNamespace(beta=beta, messages=types.SimpleNamespace(create=create))


@pytest.fixture
def haiku(monkeypatch):
    """Run the app on a model the shared confirm step applies to."""
    monkeypatch.setitem(config.load()["llm"], "model", "claude-haiku-4-5")


def test_app_model_is_not_questioned_and_the_contact_is_recorded(monkeypatch, quiet_inbox):
    calls = []
    assert config.load()["llm"]["model"] == "claude-opus-5-5" and not llm_contact.confirm_applies("claude-opus-5-5")
    script = [_Resp(_CONTACT, "tool_use"), _decision(llm_contact.KEEP), _Resp(_ANSWER, "end_turn")]
    monkeypatch.setattr(llm, "client", lambda: _scripted_client(script, calls))
    with pytest.raises(llm.LLMContact):
        llm.structured("Rules.", "note", llm.ESTIMATE_SCHEMA, "low")
    assert len(calls) == 1 and "tool_choice" not in calls[0]          # no question call was made
    assert quiet_inbox[0]["message"] == "This note has no task in it." and quiet_inbox[0]["model"] == "claude-opus-5-5"
    assert not llm_contact.UNCONFIRMED_PATH.exists()


def test_no_contact_call_means_no_confirm_question(monkeypatch, quiet_inbox, haiku):
    calls = []
    monkeypatch.setattr(llm, "client", lambda: _scripted_client([_Resp(_ANSWER, "end_turn")], calls))
    assert llm.structured("Rules.", "note", llm.ESTIMATE_SCHEMA, "low")["estimate_minutes"] == 25
    assert len(calls) == 1 and quiet_inbox == []
    assert not llm_contact.UNCONFIRMED_PATH.exists()


def test_confirm_keep_working_answers_normally_and_sends_nothing(monkeypatch, quiet_inbox, haiku):
    calls = []
    script = [_Resp(_CONTACT, "tool_use"), _decision(llm_contact.KEEP), _Resp(_ANSWER, "end_turn")]
    monkeypatch.setattr(llm, "client", lambda: _scripted_client(script, calls))
    out = llm.structured("Rules.", "note", llm.ESTIMATE_SCHEMA, "low")
    assert out["estimate_minutes"] == 25
    assert quiet_inbox == [] and llm._contact is None and llm.paused() == ""
    first, decide, again = calls
    # the question: same model and system prompt, the decision tool forced, no JSON format, no thinking
    assert decide["model"] == first["model"] and decide["system"] == first["system"]
    assert decide["tool_choice"] == {"type": "tool", "name": "contact_eli_decision"}
    assert "output_config" not in decide and "thinking" not in decide and "betas" not in decide
    assert decide["messages"][-1]["content"][0]["content"] == llm_contact.CONFIRM_TEXT
    # then the normal call again, on the continued exchange
    assert again["output_config"] == first["output_config"] and again["tools"] == first["tools"]
    assert again["messages"][-1]["content"][0]["content"] == llm_contact.KEEP_WORKING_TEXT
    assert len(first["messages"]) == 1 and len(again["messages"]) == 3
    # what the model had said is kept for audit, not notified
    assert "This note has no task in it." in llm_contact.UNCONFIRMED_PATH.read_text()


def test_confirm_send_records_the_original_and_pauses(monkeypatch, quiet_inbox, haiku):
    calls = []
    script = [_Resp(_CONTACT, "tool_use"), _decision(llm_contact.SEND)]
    monkeypatch.setattr(llm, "client", lambda: _scripted_client(script, calls))
    with pytest.raises(llm.LLMContact):
        llm.structured("Rules.", "note", llm.ESTIMATE_SCHEMA, "low")
    assert len(calls) == 2 and quiet_inbox[0]["message"] == "This note has no task in it."
    assert llm._contact is not None
    assert not llm_contact.UNCONFIRMED_PATH.exists()


def test_confirm_question_failing_sends_the_original(monkeypatch, quiet_inbox, haiku):
    calls = []
    script = [_Resp(_CONTACT, "tool_use"), RuntimeError("400 tool_choice not supported")]
    monkeypatch.setattr(llm, "client", lambda: _scripted_client(script, calls))
    with pytest.raises(llm.LLMContact):
        llm.structured("Rules.", "note", llm.ESTIMATE_SCHEMA, "low")
    assert len(calls) == 2 and "tool_choice" in calls[1]             # the question was asked and failed
    assert quiet_inbox[0]["message"] == "This note has no task in it." and quiet_inbox[0]["via"] == "tool"
    assert not llm_contact.UNCONFIRMED_PATH.exists()
