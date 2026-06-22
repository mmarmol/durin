"""`memory.search.failure` event tests.

- `memory.search.failure` is emitted when at least one safe wrapper caught
  an exception during `run_search_pipeline`. The infrastructure to detect
  this (`SearchPipelineResult.recovered_from` / `.recovery_duration_ms`) was
  already shipped; the emit is wired separately.

- `memory.silent_retrieval_miss` was DISCARDED (not deferred) — heuristics
  for this are inherently language-specific and would not work cross-lingual
  without an LLM-based classifier that breaks the telemetry budget.

Tests exercise the behaviour: emit the event with the right payload shape
under each failure scenario.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.agent.tools._telemetry.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    return events


def test_clean_run_emits_no_failure_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No source failed → no `memory.search.failure` event."""
    from durin.memory.entity_page import EntityPage
    from durin.memory.indexer import rebuild_fts_index
    from durin.memory.search_pipeline import run_search_pipeline

    EntityPage(
        type="person", name="Marcelo", aliases=["m"], body="content",
    ).save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    rebuild_fts_index(tmp_path)

    events = _capture(monkeypatch)
    run_search_pipeline(tmp_path, "Marcelo")
    assert not any(t == "memory.search.failure" for t, _ in events)


def test_vector_failure_emits_event_with_lexical_only_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Vector raises but lexical produces hits → emit with
    `degraded_to="lexical_only"`."""
    from durin.memory.entity_page import EntityPage
    from durin.memory.indexer import rebuild_fts_index
    from durin.memory.search_pipeline import run_search_pipeline

    EntityPage(
        type="person", name="Marcelo", aliases=["m"], body="content",
    ).save(tmp_path / "memory" / "entities" / "person" / "marcelo.md")
    rebuild_fts_index(tmp_path)

    class _Broken:
        def search(self, *_a, **_kw):
            raise RuntimeError("simulated lance crash")

    events = _capture(monkeypatch)
    result = run_search_pipeline(
        tmp_path, "Marcelo", vector_index=_Broken(),
    )

    failures = [d for t, d in events if t == "memory.search.failure"]
    assert len(failures) == 1
    payload = failures[0]
    assert payload["component"] == "vector"
    assert payload["recovery_attempted"] is True
    assert payload["recovery_succeeded"] == (len(result.hits) > 0)
    assert payload["recovery_duration_ms"] > 0.0
    # Lexical produced hits → degraded_to=lexical_only.
    if result.hits:
        assert payload["degraded_to"] == "lexical_only"


def test_all_sources_fail_emits_none_degradation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the pipeline recovered from a failure but the surviving
    sources produced nothing, `recovery_succeeded=False` and
    `degraded_to="none"`."""
    from durin.memory.search_pipeline import run_search_pipeline

    # Empty workspace + broken vector → nothing matches anyway.
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

    class _Broken:
        def search(self, *_a, **_kw):
            raise RuntimeError("simulated lance crash")

    events = _capture(monkeypatch)
    run_search_pipeline(
        tmp_path, "no-such-query", vector_index=_Broken(),
    )

    failures = [d for t, d in events if t == "memory.search.failure"]
    assert len(failures) == 1
    payload = failures[0]
    assert payload["component"] == "vector"
    assert payload["recovery_succeeded"] is False
    assert payload["degraded_to"] == "none"


def test_typed_dict_registered_in_events() -> None:
    """Schema registration — catches a silent revert."""
    from durin.telemetry.schema import EVENTS, MemoryRecallFailureEvent

    assert "memory.search.failure" in EVENTS
    fields = MemoryRecallFailureEvent.__annotations__
    for required in (
        "component", "recovery_attempted", "recovery_succeeded",
        "recovery_duration_ms", "degraded_to",
    ):
        assert required in fields, (
            f"MemoryRecallFailureEvent missing field {required!r}"
        )


def test_payload_does_not_break_telemetry_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If `emit_tool_event` itself raises (e.g. logger detached),
    the search pipeline must still return the result. Privacy and
    correctness > telemetry."""
    from durin.memory.search_pipeline import run_search_pipeline

    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

    class _Broken:
        def search(self, *_a, **_kw):
            raise RuntimeError("simulated lance crash")

    def boom(*_a, **_kw):
        raise RuntimeError("simulated logger detached")

    monkeypatch.setattr(
        "durin.agent.tools._telemetry.emit_tool_event", boom,
    )
    # Must not raise.
    result = run_search_pipeline(
        tmp_path, "anything", vector_index=_Broken(),
    )
    assert result.recovered_from == ("vector",)
