"""Nightly session-summary pass: idle conversations leave a searchable record."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from durin.memory.session_summary_dream import (
    get_summary_cursor,
    run_session_summary_pass,
    summarize_session,
)
from durin.memory.session_summary_store import (
    get_session_summary,
    session_summary_path,
)
from durin.memory.storage import load_entry


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


def _invoke(prompt: str, *, model=None) -> _Resp:
    assert "Extract key facts" in prompt          # the archive template rides in the prompt
    return _Resp("- user asked three questions\n---\nentities: []\ntopics: [testing]")


def _write_session(
    ws: Path, key: str, n_pairs: int = 3, *,
    idle: timedelta = timedelta(days=1), metadata: dict | None = None,
    last_consolidated: int = 0,
) -> Path:
    """Write a session file the way SessionManager lays it out: a metadata
    line 0, then one JSON message per line."""
    sdir = ws / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now() - idle).isoformat()
    rows = [{
        "_type": "metadata", "key": key, "created_at": ts, "updated_at": ts,
        "metadata": metadata or {}, "last_consolidated": last_consolidated, "preview": "",
    }]
    for i in range(n_pairs):
        rows.append({"role": "user", "content": f"question {i}", "timestamp": ts})
        rows.append({"role": "assistant", "content": f"answer {i}", "timestamp": ts})
    path = sdir / f"{key.replace(':', '_')}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def test_idle_session_gets_a_summary_and_a_cursor(tmp_path: Path) -> None:
    path = _write_session(tmp_path, "websocket:abc")

    first = summarize_session(tmp_path, path, llm_invoke=_invoke)
    text, _ = get_session_summary(tmp_path, "websocket:abc")

    assert first["written"] is True
    assert "user asked three questions" in text
    assert get_summary_cursor(path) == 6
    # The prompt's trailing tag block rides the entry, not the floor.
    assert load_entry(session_summary_path(tmp_path, "websocket:abc")).topics == ["testing"]

    second = summarize_session(tmp_path, path, llm_invoke=_invoke)
    assert second["skipped"] == "too_short"     # nothing new since the cursor


def test_summary_keeps_the_entities_the_prompt_extracted(tmp_path: Path) -> None:
    """The archive prompt returns typed entity refs alongside the bullets; the
    pass persists them so the search index and the renderer can use them."""
    path = _write_session(tmp_path, "websocket:tagged")

    def _tagged(prompt: str, *, model=None) -> _Resp:
        return _Resp(
            "- user asked three questions\n---\n"
            "entities: [person:marcelo, project:durin]\ntopics: [testing, memory]"
        )

    assert summarize_session(tmp_path, path, llm_invoke=_tagged)["written"] is True

    entry = load_entry(session_summary_path(tmp_path, "websocket:tagged"))
    assert entry.entities == ["person:marcelo", "project:durin"]
    # Order is recency (first-seen order), not alphabetical — this is the
    # first span, so it's simply the order the prompt returned them in.
    assert entry.topics == ["testing", "memory"]


def test_the_pass_leaves_out_failure_placeholders_and_keeps_the_request(tmp_path: Path) -> None:
    """The pass writes into the same bounded summary compaction does, so it
    leaves out the placeholders of turns that produced no answer as
    compaction does, and keeps what the user asked."""
    from durin.utils.runtime import MODEL_ERROR_PLACEHOLDER

    path = _write_session(tmp_path, "websocket:failed")
    rows = path.read_text(encoding="utf-8").splitlines()
    ts = json.loads(rows[1])["timestamp"]
    rows[3:3] = [
        json.dumps({"role": "user", "content": "deploy on port 8443", "timestamp": ts}),
        json.dumps({"role": "assistant", "content": MODEL_ERROR_PLACEHOLDER, "timestamp": ts}),
    ]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    prompts: list[str] = []

    def _capture(prompt: str, *, model=None) -> _Resp:
        prompts.append(prompt)
        return _invoke(prompt, model=model)

    assert summarize_session(tmp_path, path, llm_invoke=_capture)["written"] is True
    assert "deploy on port 8443" in prompts[0]
    assert "[Assistant reply unavailable" not in prompts[0]


def _long_session(ws: Path, key: str, n_pairs: int, words: int = 1_000) -> Path:
    """A session of *n_pairs* exchanges whose questions are about *words*
    words each, numbered: far longer than one summarizing call takes."""
    path = _write_session(ws, key, n_pairs=0)
    rows = path.read_text(encoding="utf-8").splitlines()
    ts = json.loads(rows[0])["updated_at"]
    for i in range(n_pairs):
        rows.append(json.dumps({"role": "user", "content": _question(i, words), "timestamp": ts}))
        rows.append(json.dumps({"role": "assistant", "content": f"answer {i}", "timestamp": ts}))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


def _question(i: int, words: int = 1_000) -> str:
    return f"question {i}: " + "detail " * words


# A summarizing call's input budget a few of those questions fill.
_PIECE_BUDGET = 4_000


def _numbered(prompts: list[str]):
    """An invoke that records each prompt and answers with a numbered block."""

    def invoke(prompt: str, *, model=None) -> _Resp:
        prompts.append(prompt)
        return _Resp(f"- span {len(prompts)}\n---\nentities: []\ntopics: []")

    return invoke


def test_a_span_longer_than_one_call_is_summarized_whole_and_in_order(tmp_path: Path) -> None:
    """The pass handed the summarizer only the last 48,000 characters of a
    longer span and moved its cursor to the end of it: compaction skips what
    the cursor covers, so the earlier turns were never summarized. Each
    message now reaches a call whole, in order, every call's block stored."""
    path = _long_session(tmp_path, "websocket:long", n_pairs=20)
    prompts: list[str] = []

    result = summarize_session(tmp_path, path, llm_invoke=_numbered(prompts), budget_tokens=_PIECE_BUDGET)

    assert result["written"] is True
    assert len(prompts) > 1
    assert not any("(earlier turns omitted)" in p for p in prompts)
    first_call = [next(n for n, p in enumerate(prompts) if _question(i) in p) for i in range(20)]
    assert first_call == sorted(first_call)
    text, _ = get_session_summary(tmp_path, "websocket:long")
    blocks = [text.index(f"span {n}") for n in range(1, len(prompts) + 1)]
    assert blocks == sorted(blocks)
    assert get_summary_cursor(path) == 40


def test_a_failed_call_leaves_the_rest_for_the_next_pass(tmp_path: Path) -> None:
    """The cursor moves past each piece once its block is stored, so a call
    that fails costs only its own piece, which the next pass starts with."""
    import pytest

    path = _long_session(tmp_path, "websocket:flaky", n_pairs=20)
    prompts: list[str] = []
    numbered = _numbered(prompts)

    def flaky(prompt: str, *, model=None) -> _Resp:
        if len(prompts) == 1:
            raise RuntimeError("the provider is down")
        return numbered(prompt)

    with pytest.raises(RuntimeError):
        summarize_session(tmp_path, path, llm_invoke=flaky, budget_tokens=_PIECE_BUDGET)
    stored = [i for i in range(20) if _question(i) in prompts[0]]
    assert get_summary_cursor(path) == 2 * (stored[-1] + 1)

    rest: list[str] = []
    summarize_session(tmp_path, path, llm_invoke=_numbered(rest), budget_tokens=_PIECE_BUDGET)
    assert _question(stored[-1] + 1) in rest[0]
    assert not any(_question(i) in p for p in rest for i in stored)
    assert get_summary_cursor(path) == 40


def test_the_time_budget_leaves_the_rest_for_the_next_pass(tmp_path: Path) -> None:
    """Out of time between pieces, the pass stops where its last stored
    piece ended and says it yielded; the next one goes on from there."""
    import time

    path = _long_session(tmp_path, "websocket:slow", n_pairs=20)
    prompts: list[str] = []

    first = summarize_session(
        tmp_path, path, llm_invoke=_numbered(prompts), deadline=time.perf_counter(), budget_tokens=_PIECE_BUDGET,
    )

    assert first["yielded"] is True
    assert len(prompts) == 1
    stored = [i for i in range(20) if _question(i) in prompts[0]]
    assert get_summary_cursor(path) == 2 * (stored[-1] + 1)
    rest: list[str] = []
    summarize_session(tmp_path, path, llm_invoke=_numbered(rest), budget_tokens=_PIECE_BUDGET)
    assert _question(stored[-1] + 1) in rest[0]
    assert get_summary_cursor(path) == 40


def test_a_message_larger_than_the_budget_is_cut_alone_and_summarized(tmp_path: Path) -> None:
    """A message larger than the memory model takes was sent whole: on a
    model with a smaller window its call failed every night, the cursor
    stayed before it, and the nightly pass never summarized that session
    again. It is now cut alone, exactly as compaction cuts one, summarized,
    and the cursor moves past it."""
    from durin.memory.session_summary_dream import _format_turns
    from durin.utils.prompt_templates import render_template
    from durin.utils.runtime import truncate_to_tokens

    path = _write_session(tmp_path, "websocket:big", n_pairs=0)
    rows = path.read_text(encoding="utf-8").splitlines()
    ts = json.loads(rows[0])["updated_at"]
    messages = [
        {"role": "user", "content": "question 0: a short one", "timestamp": ts},
        {"role": "assistant", "content": "answer 0", "timestamp": ts},
        {"role": "user", "content": _question(1, words=3_000), "timestamp": ts},
        {"role": "assistant", "content": "answer 1", "timestamp": ts},
        {"role": "user", "content": "question 2: another short one", "timestamp": ts},
        {"role": "assistant", "content": "answer 2", "timestamp": ts},
    ]
    path.write_text("\n".join(rows + [json.dumps(m) for m in messages]) + "\n", encoding="utf-8")
    prompts: list[str] = []

    summarize_session(tmp_path, path, llm_invoke=_numbered(prompts), budget_tokens=1_000)

    head = render_template("agent/consolidator_archive.md", strip=True) + "\n\n"
    sent = [p[len(head):] for p in prompts]
    assert truncate_to_tokens(_format_turns([messages[2]]), 1_000) in sent
    assert any("question 0: a short one" in s for s in sent)
    assert any("question 2: another short one" in s for s in sent)
    assert get_summary_cursor(path) == 6


def test_the_dream_sizes_the_pass_by_the_memory_model(tmp_path: Path, monkeypatch) -> None:
    """The dream gives the pass the input budget of the model it summarizes
    with, sized as compaction sizes its calls: the window less the output
    ceiling and compaction's safety buffer."""
    from durin.agent.memory import Consolidator
    from durin.config.schema import AuxModelConfig, Config, ModelPresetConfig
    from durin.memory import session_summary_dream
    from durin.memory.session_summary_dream import memory_input_budget

    config = Config()
    config.model_presets["dream"] = ModelPresetConfig(
        model="gpt-4.1-mini", provider="openai", context_window_tokens=32_768, max_tokens=4_096,
    )
    config.agents.aux_models.memory = AuxModelConfig(preset="dream")
    assert memory_input_budget(config) == 32_768 - 4_096 - Consolidator._SAFETY_BUFFER

    seen: list[int | None] = []
    real = session_summary_dream.summarize_session

    def spy(*args, **kwargs):
        seen.append(kwargs.get("budget_tokens"))
        return real(*args, **kwargs)

    monkeypatch.setattr(session_summary_dream, "summarize_session", spy)
    _write_session(tmp_path, "websocket:a")
    run_session_summary_pass(tmp_path, llm_invoke=_invoke, budget_tokens=27_648)
    assert seen == [27_648]


def test_active_session_is_left_to_the_compactor(tmp_path: Path) -> None:
    path = _write_session(tmp_path, "websocket:abc", idle=timedelta(minutes=5))
    assert summarize_session(tmp_path, path, llm_invoke=_invoke)["skipped"] == "active"
    assert get_session_summary(tmp_path, "websocket:abc") == (None, None)


def test_span_starts_after_the_compactor_cursor(tmp_path: Path) -> None:
    path = _write_session(tmp_path, "websocket:abc", n_pairs=3, last_consolidated=4)
    assert summarize_session(tmp_path, path, llm_invoke=_invoke)["skipped"] == "too_short"

    path = _write_session(tmp_path, "websocket:def", n_pairs=5, last_consolidated=4)
    assert summarize_session(tmp_path, path, llm_invoke=_invoke)["written"] is True


def test_non_conversations_are_skipped(tmp_path: Path) -> None:
    wf = _write_session(tmp_path, "workflow:run1:node", n_pairs=4)
    sub = _write_session(tmp_path, "subagent:t1", n_pairs=4)
    tagged = _write_session(tmp_path, "websocket:node", n_pairs=4,
                            metadata={"origin_type": "workflow_node"})

    out = run_session_summary_pass(tmp_path, llm_invoke=_invoke)

    assert out["written"] == 0
    for p in (wf, sub, tagged):
        assert get_summary_cursor(p) == 0


def test_pass_counts_and_yields_on_max_seconds(tmp_path: Path) -> None:
    _write_session(tmp_path, "websocket:a")
    _write_session(tmp_path, "websocket:b")

    out = run_session_summary_pass(tmp_path, llm_invoke=_invoke)
    assert out["sessions"] == 2 and out["written"] == 2 and out["yielded"] is False

    _write_session(tmp_path, "websocket:c")
    out = run_session_summary_pass(tmp_path, llm_invoke=_invoke, max_seconds=1e-9)
    assert out["yielded"] is True


def test_cursor_past_the_end_restarts_the_conversation(tmp_path: Path) -> None:
    """A cursor past the end of the file means the file was rewritten.

    `/new` empties the session file and the file cap trims it; neither resets
    the cursor. Whatever is in the file now is a new conversation, so the pass
    must summarize it instead of waiting for it to outgrow the stale index.
    """
    path = _write_session(tmp_path, "websocket:abc", n_pairs=5)
    assert summarize_session(tmp_path, path, llm_invoke=_invoke)["written"] is True
    assert get_summary_cursor(path) == 10

    # Same key, same file: /new emptied it and two fresh turns landed.
    _write_session(tmp_path, "websocket:abc", n_pairs=2)

    def _invoke_fresh(prompt: str, *, model=None) -> _Resp:
        return _Resp("- the fresh conversation\n---\nentities: []\ntopics: []")

    second = summarize_session(tmp_path, path, llm_invoke=_invoke_fresh)

    assert second["written"] is True
    assert get_summary_cursor(path) == 4
    text, _ = get_session_summary(tmp_path, "websocket:abc")
    assert "the fresh conversation" in text
