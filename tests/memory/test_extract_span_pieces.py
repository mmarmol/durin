"""The extract pass reads the whole unread span of a conversation, in pieces.

A span longer than one stage prompt is cut at message boundaries; the stages
run per piece and the session's cursor moves past a piece only once the model
has answered for it. These tests drive ``run_extract_for_session`` with a
model call that records which messages each prompt carried.
"""

from __future__ import annotations

import json
import re

import pytest

import durin.memory.extract_runner as extract_runner
from durin.memory.extract_dream import PIECE_CHARS
from durin.memory.extract_runner import get_extract_cursor, run_extract_for_session
from durin.memory.llm_invoke import LLMResponse

_MARK = re.compile(r"msg-(\d{4})")


def _message(i: int, *, role: str = "user", chars: int = 900) -> dict:
    """Message *i* of a session, recognizable in a prompt by its mark."""
    return {
        "role": role,
        "content": f"msg-{i:04d} " + "x" * chars,
        "timestamp": f"2026-10-01T12:{i % 60:02d}:00",
    }


def _session(ws, messages: list[dict]):
    sdir = ws / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / "chat.jsonl"
    lines = [json.dumps({"_type": "metadata", "key": "chat"})]
    lines += [json.dumps(m) for m in messages]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _stage(prompt: str) -> str:
    if "memory discovery pass" in prompt:
        return "discover"
    if "CONVERSATION SPAN:" in prompt:
        return "learnings"
    if "memory extractor" in prompt:
        return "extract"
    return "other"


class Model:
    """A model call that records, per stage, the messages each prompt
    carried and where the session's cursor stood when it was asked.

    ``fail`` maps ``(stage, nth call of that stage)`` to how that call ends:
    ``"error"`` answers as the provider layer does once its retries are
    spent (the error text, ``finish_reason="error"``), ``"raise"`` raises,
    ``"garbage"`` is a successful reply no stage can parse.
    """

    def __init__(self, path, fail: dict | None = None, after_call=None):
        self.path = path
        self.fail = fail or {}
        self.after_call = after_call
        self.calls: list[tuple[str, list[int], int]] = []
        self.prompts: list[str] = []

    def __call__(self, prompt: str, **kwargs) -> LLMResponse:
        self.prompts.append(prompt)
        stage = _stage(prompt)
        marks = [int(m) for m in _MARK.findall(prompt)]
        self.calls.append((stage, marks, get_extract_cursor(self.path)))
        nth = sum(1 for s, _m, _c in self.calls if s == stage)
        how = self.fail.get((stage, nth))
        if self.after_call is not None:
            self.after_call(self)
        if how == "raise":
            raise ConnectionError("connection reset by peer")
        if how == "error":
            return LLMResponse(
                text="Error: stream stalled for more than 90 seconds", finish_reason="error")
        if how == "garbage":
            return LLMResponse(text="Here is what I found in the conversation")
        return LLMResponse(text="{}" if stage == "extract" else "[]")

    def pieces(self, stage: str) -> list[list[int]]:
        return [marks for s, marks, _c in self.calls if s == stage]


def _long_session(ws):
    """Forty-odd messages of every kind the session file holds, several
    stage prompts long, one of them larger than a prompt on its own."""
    messages = []
    for i in range(40):
        if i == 17:
            messages.append(_message(i, role="tool", chars=PIECE_CHARS + 5000))
        elif i % 5 == 3:
            messages.append(_message(i, role="tool", chars=2500))
        elif i % 2:
            messages.append(_message(i, role="assistant"))
        else:
            messages.append(_message(i))
    return _session(ws, messages), messages


def _run(ws, path, model, **kwargs):
    return run_extract_for_session(
        ws, path, llm_invoke=model, skill_signals=False, **kwargs)


def test_every_message_of_a_long_span_is_sent_in_exactly_one_piece(tmp_path):
    path, messages = _long_session(tmp_path)
    model = Model(path)

    out = _run(tmp_path, path, model)

    for stage in ("discover", "learnings"):
        pieces = model.pieces(stage)
        assert len(pieces) > 3
        sent = [i for piece in pieces for i in piece]
        assert sent == list(range(len(messages))), stage  # no gap, no overlap, in order
    assert [17] in model.pieces("discover")  # the oversized message is a piece of its own
    assert out["cursor"] == len(messages) == get_extract_cursor(path)
    assert out["pieces"] == len(model.pieces("discover"))


def test_the_cursor_never_passes_a_message_that_was_not_sent(tmp_path):
    path, messages = _long_session(tmp_path)
    model = Model(path)

    _run(tmp_path, path, model)

    sent: set[int] = set()
    for stage, marks, cursor in model.calls:
        if stage == "discover":  # the first stage to read a piece
            assert set(range(cursor)) <= sent
            assert marks and marks[0] == cursor
        sent |= set(marks)
    assert sent == set(range(len(messages)))


@pytest.mark.parametrize("stage", ["discover", "learnings"])
@pytest.mark.parametrize("how", ["error", "raise"])
def test_provider_failure_stops_at_the_piece_and_the_next_run_resumes_there(
    tmp_path, stage, how,
):
    path, messages = _long_session(tmp_path)
    failing = Model(path, fail={(stage, 3): how})

    out = _run(tmp_path, path, failing)

    done = failing.pieces("discover")[:2]
    failed_piece = failing.pieces("discover")[2]
    assert get_extract_cursor(path) == done[-1][-1] + 1 == failed_piece[0]
    assert out["cursor"] == failed_piece[0]
    assert out["stopped"] == "provider_error" and out["error"]
    assert out["pieces"] == 2
    assert len(failing.pieces("discover")) == 3  # nothing was asked after the failure

    healthy = Model(path)
    again = _run(tmp_path, path, healthy)

    assert healthy.pieces("discover")[0] == failed_piece
    resumed = [i for piece in healthy.pieces("discover") for i in piece]
    assert resumed == list(range(failed_piece[0], len(messages)))
    assert again["cursor"] == len(messages) == get_extract_cursor(path)
    assert "stopped" not in again


def test_provider_failure_while_extracting_an_upserted_entity_does_not_advance(tmp_path):
    upsert = {
        "role": "assistant", "content": "msg-0001 noted",
        "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "memory_upsert_entity",
            "arguments": json.dumps({"ref": "company:globex", "name": "Globex"})}}],
    }
    path = _session(tmp_path, [_message(0), upsert])
    model = Model(path, fail={("extract", 1): "error"})

    out = _run(tmp_path, path, model)

    assert out["stopped"] == "provider_error"
    assert get_extract_cursor(path) == 0
    assert model.pieces("discover") == []


def test_span_that_fits_one_piece_is_one_call_per_stage(tmp_path):
    upsert = {
        "role": "assistant", "content": "msg-0001 noted",
        "tool_calls": [{"id": "c1", "type": "function", "function": {
            "name": "memory_upsert_entity",
            "arguments": json.dumps({"ref": "company:globex", "name": "Globex"})}}],
    }
    path = _session(tmp_path, [_message(0), upsert, _message(2, role="assistant")])
    model = Model(path)

    out = _run(tmp_path, path, model)

    assert [(stage, marks) for stage, marks, _c in model.calls] == [
        ("extract", [0, 1, 2]), ("discover", [0, 1, 2]), ("learnings", [0, 1, 2])]
    assert out["cursor"] == 3 == get_extract_cursor(path)
    assert out["pieces"] == 1
    assert out["new_turns"] == 3
    assert out["extracted"] == [{"ref": "company:globex", "committed": False}]


def test_time_budget_spent_mid_span_leaves_the_cursor_at_the_last_completed_piece(
    tmp_path, monkeypatch,
):
    path, messages = _long_session(tmp_path)
    clock = {"now": 100.0}
    monkeypatch.setattr(extract_runner, "_clock", lambda: clock["now"])

    def spend(model):  # the budget runs out while the second piece is being read
        if len(model.pieces("discover")) == 2:
            clock["now"] = 500.0

    model = Model(path, after_call=spend)

    out = _run(tmp_path, path, model, deadline=200.0)

    pieces = model.pieces("discover")
    assert len(pieces) == 2
    assert model.pieces("learnings") == pieces  # the piece in flight was finished
    assert out["yielded"] is True
    assert out["cursor"] == pieces[-1][-1] + 1 == get_extract_cursor(path)
    assert out["cursor"] < len(messages)


def test_unparseable_reply_still_advances_the_cursor(tmp_path):
    path, messages = _long_session(tmp_path)
    model = Model(path, fail={("discover", 2): "garbage", ("learnings", 3): "garbage"})

    out = _run(tmp_path, path, model)

    assert "stopped" not in out
    assert out["cursor"] == len(messages) == get_extract_cursor(path)
    sent = [i for piece in model.pieces("discover") for i in piece]
    assert sent == list(range(len(messages)))


def test_coverage_counts_the_span_and_what_was_sent(tmp_path):
    path, messages = _long_session(tmp_path)
    model = Model(path)

    out = _run(tmp_path, path, model)

    # Only the message larger than a stage prompt loses text: its tail.
    assert out["span_chars"] - out["sent_chars"] > 4000
    assert out["span_chars"] - out["sent_chars"] < 6000
    assert out["sent_chars"] > 60_000


def test_failure_placeholders_are_passed_over_without_a_call(tmp_path):
    from durin.utils.runtime import MODEL_ERROR_PLACEHOLDER

    path = _session(tmp_path, [
        _message(0),
        {"role": "assistant", "content": MODEL_ERROR_PLACEHOLDER,
         "timestamp": "2026-10-01T12:01:00"},
    ])
    model = Model(path)

    out = _run(tmp_path, path, model)

    assert model.pieces("discover") == [[0]]
    assert not any(MODEL_ERROR_PLACEHOLDER in prompt for prompt in model.prompts)
    assert out["cursor"] == 2 == get_extract_cursor(path)


# --- the pass: budget, failures and coverage across sessions -----------------


def _events(monkeypatch) -> list[tuple[str, dict]]:
    import durin.agent.tools._telemetry as tel

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(tel, "emit_tool_event", lambda name, data: events.append((name, data)))
    return events


def test_pass_reports_coverage_and_counts_a_session_stopped_by_the_provider(
    tmp_path, monkeypatch,
):
    from durin.memory.dream_passes import run_extract_pass

    events = _events(monkeypatch)
    path, messages = _long_session(tmp_path)
    model = Model(path, fail={("learnings", 2): "error"})

    out = run_extract_pass(tmp_path, llm_invoke=model, skill_signals=False)

    assert [e["session"] for e in out["errors"]] == ["chat"]
    assert "stream stalled" in out["errors"][0]["error"]
    assert 0 < get_extract_cursor(path) < len(messages)
    end = [d for n, d in events if n == "memory.dream.end"][0]
    assert end["pieces"] == out["pieces"] == 1
    assert end["span_chars"] == out["span_chars"] > 0
    assert end["sent_chars"] == out["sent_chars"] == end["span_chars"]
    assert end["entities_failed"] == 1

    # the next pass reads the rest; over both, every character was counted once
    rest = run_extract_pass(tmp_path, llm_invoke=Model(path), skill_signals=False)
    assert rest["errors"] == []
    assert get_extract_cursor(path) == len(messages)
    assert rest["pieces"] > 1


def test_pass_budget_spent_inside_a_session_yields_and_keeps_the_rest(tmp_path, monkeypatch):
    import time

    from durin.memory.dream_passes import run_extract_pass

    events = _events(monkeypatch)
    path, messages = _long_session(tmp_path)
    started = time.perf_counter()

    def spend(model):  # the pass's budget is gone once the first piece is read
        monkeypatch.setattr(extract_runner, "_clock", lambda: started + 10_000)

    out = run_extract_pass(
        tmp_path, llm_invoke=Model(path, after_call=spend), skill_signals=False,
        max_seconds=3600)

    assert out["yielded"] is True and out["pieces"] == 1
    assert 0 < get_extract_cursor(path) < len(messages)
    assert [n for n, _d in events].count("memory.dream.max_seconds_reached") == 1
