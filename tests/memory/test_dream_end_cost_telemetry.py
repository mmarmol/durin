"""`memory.dream.end` carries the A5 cost-telemetry fields.

Per doc 07 §6.2 and doc 11 audit A5:

- `entities_consolidated` (existed pre-A5)
- `entities_failed` (existed pre-A5)
- `entities_quarantined` (NEW)
- `llm_call_count` (NEW — sums per-entity LLM calls incl. retries)
- `llm_input_tokens_total` (NEW)
- `llm_output_tokens_total` (NEW)
- `duration_ms` (NEW — pre-A5 used `duration_s`)

Per `feedback_sync_tests_exercise_behavior` in personal memory:
this test exercises the BEHAVIOUR (the event is emitted with the
right values), not just the schema TypedDict shape.
"""

from __future__ import annotations

import datetime
import json as _json
from pathlib import Path

import pytest

from durin.memory.dream import LLMResponse
from durin.memory.dream_runner import DreamRunner
from durin.memory.store import store_memory


def _seed_pending_entry(workspace: Path, slug: str = "marcelo") -> None:
    """Write one episodic entry that tags `person:<slug>` so the
    runner picks it up."""
    store_memory(
        workspace,
        content=f"{slug} observation",
        entities=[f"person:{slug}"],
        valid_from=datetime.date(2026, 5, 23),
    )


def _make_response(slug: str = "marcelo") -> LLMResponse:
    """Return a well-formed LLMResponse that parses successfully."""
    ops = [
        {"op": "add", "path": "/aliases/-", "value": slug,
         "provenance": "episodic/e1.md"},
        {"op": "add", "path": "/attributes/note", "value": "observed",
         "provenance": "episodic/e1.md"},
    ]
    text = (
        "===PATCH===\n"
        + _json.dumps(ops, indent=2) + "\n"
        + "===BODY_DELTA===\n"
        + "Observed.\n"
        + "===COMMIT===\n"
        + f"Consolidate person:{slug} (rev 1)\n"
        + "\nInitial pass.\n"
        + f"\nSources: episodic/e1.md\nEntities-touched: person:{slug}\n"
        + "Cursor-after: 2026-05-23T00:00:00\n"
        + "===END===\n"
    )
    return LLMResponse(text=text, prompt_tokens=180, completion_tokens=42)


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.dream_runner.emit_tool_event",
        lambda t, d: captured.append((t, d)),
    )
    return captured


def test_dream_end_carries_token_totals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One consolidation -> one LLM call -> exactly the tokens from
    the LLMResponse end up in the `memory.dream.end` payload."""
    _seed_pending_entry(tmp_path)
    events = _capture_events(monkeypatch)

    response = _make_response()

    def stub(prompt, *, model):
        return response

    runner = DreamRunner(
        workspace=tmp_path,
        llm_invoke=stub,
        min_seconds_between_runs=0,
    )
    runner.run(trigger="cron_daily")

    end = next(d for t, d in events if t == "memory.dream.end")
    assert end["entities_consolidated"] == 1
    assert end["entities_failed"] == 0
    assert end["entities_quarantined"] == 0
    assert end["llm_call_count"] == 1
    assert end["llm_input_tokens_total"] == 180
    assert end["llm_output_tokens_total"] == 42
    assert end["duration_ms"] > 0  # wall-clock; just non-zero suffices
    assert "duration_s" not in end  # the old field is gone


def test_dream_end_backward_compat_with_str_returning_llm_invoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy `llm_invoke` that returns a bare `str` (pre-A5
    Protocol) must still drive the pipeline. Tokens default to 0 in
    that case -- under-report is safe; crashing would not be."""
    _seed_pending_entry(tmp_path)
    events = _capture_events(monkeypatch)

    response_text = _make_response().text  # extract just the str

    def stub_legacy(prompt, *, model):
        return response_text  # bare str -- pre-A5 shape

    runner = DreamRunner(
        workspace=tmp_path,
        llm_invoke=stub_legacy,
        min_seconds_between_runs=0,
    )
    runner.run(trigger="cron_daily")

    end = next(d for t, d in events if t == "memory.dream.end")
    assert end["entities_consolidated"] == 1
    assert end["llm_call_count"] == 1
    # Legacy shape doesn't provide usage -> totals stay at 0 (under-
    # report, safe-failure direction per doc 07 sec 6.2).
    assert end["llm_input_tokens_total"] == 0
    assert end["llm_output_tokens_total"] == 0


def test_dream_end_aggregates_tokens_across_multiple_entities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token totals SUM across all entities in a single pass -- the
    payload is the per-pass aggregate, not per-entity.

    We can't reliably distinguish per-entity from the prompt text
    (canonical pages reference each other, aliases overlap), so we
    drive the test with a call counter and inspect aggregates only."""
    _seed_pending_entry(tmp_path, slug="marcelo")
    _seed_pending_entry(tmp_path, slug="durin")
    events = _capture_events(monkeypatch)

    # Two distinct responses; the runner iterates entities in dict
    # insertion order, so the order we get is determined by the
    # pending-discovery walk. Either order yields the same totals.
    responses = [
        LLMResponse(text=_make_response("marcelo").text,
                    prompt_tokens=100, completion_tokens=20),
        LLMResponse(text=_make_response("durin").text,
                    prompt_tokens=200, completion_tokens=40),
    ]
    call_idx = {"n": 0}

    def stub(prompt, *, model):
        # Each call gets the next response in sequence. The stub
        # response text is generic enough that both entities can use
        # either (the parse only requires well-formed markers + ops).
        idx = call_idx["n"]
        call_idx["n"] = idx + 1
        return responses[idx % len(responses)]

    runner = DreamRunner(
        workspace=tmp_path,
        llm_invoke=stub,
        min_seconds_between_runs=0,
    )
    runner.run(trigger="cron_daily")

    end = next(d for t, d in events if t == "memory.dream.end")
    assert end["entities_consolidated"] == 2
    assert end["llm_call_count"] == 2          # one call per entity
    # Aggregation invariant: 100 + 200 = 300 (in some order), 20 + 40 = 60.
    assert end["llm_input_tokens_total"] == 300
    assert end["llm_output_tokens_total"] == 60


def test_dream_end_schema_has_required_a5_fields() -> None:
    """First-line check: the TypedDict ships the A5 fields. Catches
    the case where someone reverts the schema but the payload code
    still emits them -- keeping doc 07 sec 6.2 honest."""
    from durin.telemetry.schema import MemoryDreamEndEvent

    required = {
        "trigger", "entity_filter",
        "entities_consolidated", "entities_failed", "entities_quarantined",
        "llm_call_count", "llm_input_tokens_total", "llm_output_tokens_total",
        "duration_ms",
    }
    declared = set(MemoryDreamEndEvent.__annotations__.keys())
    missing = required - declared
    assert not missing, f"MemoryDreamEndEvent missing A5 fields: {missing}"
    # And the deprecated duration_s is gone.
    assert "duration_s" not in declared, (
        "duration_s should be removed (replaced by duration_ms in A5)"
    )
