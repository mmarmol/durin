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
