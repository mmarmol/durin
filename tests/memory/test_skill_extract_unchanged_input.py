"""The skill-extract pass does not run its sub-agent when everything it would
send the model is identical to what its last completed run sent.

The pass reads the open skill gaps that were seen often enough, with what
their sessions ended with; on a night where none of that moved, a model run
can only repeat the previous night's answer. Each test drives the real pass
function with a fake provider and counts the model calls.
"""
import json
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from durin.agent.skill_observations import (
    add_principle,
    log_observation,
    open_observations,
    resolve_observation,
)
from durin.memory import dream_passes
from durin.memory.dream_passes import run_skill_extract_pass
from durin.providers.base import LLMResponse, ToolCallRequest

STATE_FILE = ".skill_extract_input.json"


def _session(ws, key, *turns, mtime=None):
    sdir = ws / "sessions"
    sdir.mkdir(parents=True, exist_ok=True)
    path = sdir / f"{key}.jsonl"
    path.write_text("".join(
        json.dumps({"role": role, "content": text}) + "\n" for role, text in turns),
        encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _gap(ws, name="release-runbook", issue="no skill covers releases", session=None):
    return log_observation(ws, skill=f"new:{name}", kind="gap", issue=issue,
                           improvement="steps: build wheel, reinstall, verify",
                           session=session)


def _answering_provider():
    """A provider whose every call ends the sub-agent with a plain answer."""
    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="Nothing reusable here.", tool_calls=[], finish_reason="stop", usage={}))
    return provider


def _calls(provider) -> int:
    return provider.chat_with_retry.await_count


@pytest.fixture
def events(monkeypatch):
    out: list[tuple[str, dict]] = []
    monkeypatch.setattr(dream_passes, "_emit", lambda event, **data: out.append((event, data)))
    return out


def _extract_events(events):
    return [data for event, data in events if event == "memory.dream.skill_extract"]


@pytest.fixture
def ws(tmp_path):
    _session(tmp_path, "alpha", ("user", "how do I cut a release?"),
             ("assistant", "build the wheel, reinstall it, run the doctor"), mtime=1_000)
    _session(tmp_path, "beta", ("user", "rotate the logs"),
             ("assistant", "done with logrotate"), mtime=2_000)
    # Two gaps the pass is handed: each was seen in both sessions.
    for session in ("alpha", "beta"):
        _gap(tmp_path, session=session)
        _gap(tmp_path, name="db-backup", issue="the database was backed up by hand",
             session=session)
    return tmp_path


# --- the gate ---------------------------------------------------------------


def test_no_stored_state_runs(ws, events):
    provider = _answering_provider()
    out = run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) >= 1
    assert "skipped" not in out
    (ev,) = _extract_events(events)
    assert ev["stop_reason"] == "completed"
    assert "skipped" not in ev


def test_unchanged_inputs_second_run_makes_no_model_call(ws, events):
    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    first = _calls(provider)
    assert first >= 1

    out = run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) == first
    assert out["skipped"] == "unchanged"
    assert out["skills_touched"] == 0 and out["gaps_closed"] == 0

    run_ev, skip_ev = _extract_events(events)
    assert run_ev["stop_reason"] == "completed"
    assert skip_ev["skipped"] == "unchanged"
    # A skip still carries the event's regular fields, well-formed.
    assert skip_ev["skills_touched"] == 0
    assert skip_ev["gaps_closed"] == 0
    assert isinstance(skip_ev["duration_ms"], int)
    assert "stop_reason" not in skip_ev


def test_a_corrupt_state_file_runs(ws):
    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    first = _calls(provider)
    (ws / "memory" / STATE_FILE).write_text('{"fingerprint": "abc', encoding="utf-8")
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) > first


# --- every input the model receives re-runs the pass when it changes ---------


def _gap_opened(ws):
    # Seen in two sessions: one the extractor is handed.
    _gap(ws, name="weekly-report", issue="no skill drafts the weekly report", session="mon")
    _gap(ws, name="weekly-report", issue="no skill drafts the weekly report", session="tue")


def _gap_closed(ws):
    rec = next(r for r in open_observations(ws) if r["skill"] == "new:release-runbook")
    resolve_observation(ws, rec["id"], "declined")


def _gap_seen_again(ws):
    _gap(ws, session="later")  # same gap, one more occasion


def _gap_evidence_changed(ws):
    # The gap's own session moves on.
    with (ws / "sessions" / "origin.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"role": "assistant", "content": "the doctor step was wrong"}) + "\n")


def _correction_recorded_from_gap_session(ws):
    log_observation(ws, skill="release-notes", kind="correction",
                    issue="release notes skip the changelog",
                    improvement="read the changelog first", session="origin")


def _principle_added(ws):
    add_principle(ws, "skills must name their verification command")


def _skill_authored(ws):
    d = ws / "skills" / "log-rotation"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: log-rotation\ndescription: Rotate logs.\n---\n# Rotate\n",
        encoding="utf-8")


def _skill_retired(ws):
    from durin.agent.skill_retirements import record_retirement
    record_retirement(ws, "old-release-steps", by="user", reason="superseded")


def _workflow_installed(ws):
    d = ws / "workflows"
    d.mkdir(parents=True, exist_ok=True)
    (d / "fan-out.json").write_text(json.dumps({
        "name": "fan-out", "description": "fan out searches, synthesize",
        "start": "only",
        "nodes": [{"id": "only", "title": "t", "kind": "work",
                   "mode": "read", "tools": "none", "prompt": "p"}],
    }), encoding="utf-8")


CHANGES = {
    "gap_opened": _gap_opened,
    "gap_closed": _gap_closed,
    "gap_seen_again": _gap_seen_again,
    "principle_added": _principle_added,
    "skill_authored": _skill_authored,
    "skill_retired": _skill_retired,
    "workflow_installed": _workflow_installed,
}


@pytest.mark.parametrize("change", sorted(CHANGES))
def test_an_input_change_runs_the_pass(ws, change):
    _session(ws, "origin", ("user", "release it"),
             ("assistant", "built, reinstalled, doctor green"), mtime=500)
    _gap(ws, session="earlier")
    _gap(ws, session="origin")
    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    settled = _calls(provider)
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) == settled  # the setup itself is settled

    CHANGES[change](ws)
    out = run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) > settled, change
    assert "skipped" not in out


EVIDENCE_CHANGES = {
    "gap_evidence_changed": _gap_evidence_changed,
    "correction_from_gap_session": _correction_recorded_from_gap_session,
}


@pytest.mark.parametrize("change", sorted(EVIDENCE_CHANGES))
def test_a_gap_evidence_change_runs_the_pass(ws, change):
    """A gap's own session reaches the model as the gap's evidence: a change
    there re-runs the pass."""
    _session(ws, "origin", ("user", "release it"),
             ("assistant", "built, reinstalled, doctor green"), mtime=500)
    _gap(ws, session="earlier")
    _gap(ws, session="origin")
    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    settled = _calls(provider)
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) == settled

    EVIDENCE_CHANGES[change](ws)
    out = run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) > settled, change
    assert "skipped" not in out


def test_a_session_no_gap_was_seen_in_does_not_rerun_the_pass(ws):
    """The pass reads sessions only as a gap's evidence, so a conversation
    with no gap behind it is not its input."""
    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    settled = _calls(provider)

    _session(ws, "gamma", ("user", "draft the weekly report"), mtime=3_000)
    out = run_skill_extract_pass(ws, provider=provider, model="test-model")

    assert _calls(provider) == settled
    assert out["skipped"] == "unchanged"


def test_no_gap_seen_often_enough_makes_no_model_call(tmp_path, events):
    _session(tmp_path, "alpha", ("user", "how do I cut a release?"),
             ("assistant", "build the wheel, reinstall it, run the doctor"))
    _gap(tmp_path, session="alpha")
    provider = _answering_provider()

    out = run_skill_extract_pass(tmp_path, provider=provider, model="test-model")

    assert _calls(provider) == 0
    assert out["skipped"] == "no_gaps" and out["skills_touched"] == 0
    (ev,) = _extract_events(events)
    assert ev["skipped"] == "no_gaps"
    assert ev["gaps_waiting"] == 1


def test_a_different_model_runs_the_pass(ws):
    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    settled = _calls(provider)
    run_skill_extract_pass(ws, provider=provider, model="other-model")
    assert _calls(provider) > settled


def test_a_changed_doctrine_runs_the_pass(ws, monkeypatch):
    from durin.agent import skills_doctrine
    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    settled = _calls(provider)
    monkeypatch.setattr(skills_doctrine, "composition_doctrine",
                        lambda: "## Composition\nA revised doctrine.")
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) > settled


def test_a_changed_tool_set_runs_the_pass(ws, monkeypatch):
    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    settled = _calls(provider)

    build = dream_passes._build_skill_extract_tools

    def fewer_tools(workspace, fs, **kwargs):
        tools = build(workspace, fs, **kwargs)
        tools.unregister("list_workflows")
        return tools

    monkeypatch.setattr(dream_passes, "_build_skill_extract_tools", fewer_tools)
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) > settled


# --- only a completed run is remembered --------------------------------------


def test_a_provider_failure_leaves_no_fingerprint(ws, events):
    failing = MagicMock()
    failing.chat_with_retry = AsyncMock(return_value=LLMResponse(
        content="Error calling LLM: upstream unavailable", finish_reason="error"))
    out = run_skill_extract_pass(ws, provider=failing, model="test-model")
    assert out["stop_reason"] == "error"
    (ev,) = _extract_events(events)
    assert ev["stop_reason"] == "error"
    assert not (ws / "memory" / STATE_FILE).exists()

    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) >= 1


def test_a_raising_provider_leaves_no_fingerprint(ws, events):
    failing = MagicMock()
    failing.chat_with_retry = AsyncMock(side_effect=RuntimeError("socket closed"))
    run_skill_extract_pass(ws, provider=failing, model="test-model")
    (ev,) = _extract_events(events)
    assert ev["stop_reason"] != "completed"
    assert not (ws / "memory" / STATE_FILE).exists()

    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) >= 1


def test_a_budget_stop_leaves_no_fingerprint(ws, events):
    looping = MagicMock()
    looping.chat_with_retry = AsyncMock(side_effect=lambda *a, **k: LLMResponse(
        content="", finish_reason="tool_calls", usage={},
        tool_calls=[ToolCallRequest(id="c", name="list_workflows", arguments={})]))
    out = run_skill_extract_pass(ws, provider=looping, model="test-model")
    assert out["stop_reason"] == "max_iterations"
    (ev,) = _extract_events(events)
    assert ev["stop_reason"] == "max_iterations"
    assert not (ws / "memory" / STATE_FILE).exists()

    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) >= 1


# --- the stored state --------------------------------------------------------


def test_an_aborted_state_write_keeps_the_previous_state(ws, monkeypatch):
    """The state is replaced through the atomic-write helper: a write that dies
    leaves the previous file whole, the pass still returns, and because the
    stored fingerprint is the old one the next run tries again."""
    import durin.utils.atomic_write as aw

    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    state = ws / "memory" / STATE_FILE
    before = state.read_bytes()

    _gap_seen_again(ws)
    real = aw.atomic_write_text
    writes: list[str] = []

    def dying_write(path, *args, **kwargs):
        writes.append(str(path))
        raise OSError("disk full")

    monkeypatch.setattr(aw, "atomic_write_text", dying_write)
    out = run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert out["stop_reason"] == "completed"
    assert writes == [str(state)]
    assert state.read_bytes() == before

    monkeypatch.setattr(aw, "atomic_write_text", real)
    settled = _calls(provider)
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) > settled


def test_a_gap_seen_once_is_counted_as_waiting(ws, events):
    """The pass reports how many open gaps it was not handed, so a queue
    that only grows is visible from the event."""
    _gap(ws, name="weekly-report", issue="no skill drafts the weekly report", session="mon")

    run_skill_extract_pass(ws, provider=_answering_provider(), model="test-model")

    (ev,) = _extract_events(events)
    assert ev["gaps_waiting"] == 1


def test_a_gap_seen_for_the_first_time_does_not_rerun_the_pass(ws):
    """A new gap waits to be seen again, so it changes nothing the pass reads."""
    provider = _answering_provider()
    run_skill_extract_pass(ws, provider=provider, model="test-model")
    settled = _calls(provider)

    _gap(ws, name="weekly-report", issue="no skill drafts the weekly report", session="mon")
    out = run_skill_extract_pass(ws, provider=provider, model="test-model")

    assert _calls(provider) == settled
    assert out["skipped"] == "unchanged"


# --- a gap the pass keeps turning down is closed ------------------------------


def _sighting(ws, n):
    _gap(ws, session=f"later-{n}")
    _gap(ws, name="db-backup", issue="the database was backed up by hand",
         session=f"later-{n}")


def test_a_gap_left_unauthored_three_times_is_closed(ws, events):
    """The pass turns a gap down when something already covers it, and was
    handed the same gap again on every new sighting, for good."""
    from durin.agent.skill_observations import GAP_HANDOVERS, closed_gaps, open_gaps

    provider = _answering_provider()
    for n in range(GAP_HANDOVERS):
        assert len(open_gaps(ws)) == 2
        _sighting(ws, n)
        out = run_skill_extract_pass(ws, provider=provider, model="test-model")

    assert out["gaps_given_up"] == 2
    assert _extract_events(events)[-1]["gaps_given_up"] == 2
    assert open_gaps(ws) == []
    assert sorted(g["skill"] for g in closed_gaps(ws)) == ["new:db-backup", "new:release-runbook"]

    # Seen again: nothing reopens, and the pass has nothing to read.
    calls = _calls(provider)
    _sighting(ws, 99)
    out = run_skill_extract_pass(ws, provider=provider, model="test-model")
    assert _calls(provider) == calls
    assert out["skipped"] == "no_gaps"
    assert open_gaps(ws) == []


def test_a_failed_run_does_not_count_against_a_gap(ws):
    from durin.agent.skill_observations import GAP_HANDOVERS, open_gaps

    provider = MagicMock()
    provider.chat_with_retry = AsyncMock(side_effect=RuntimeError("provider down"))
    for n in range(GAP_HANDOVERS + 1):
        _sighting(ws, n)
        run_skill_extract_pass(ws, provider=provider, model="test-model")

    assert len(open_gaps(ws)) == 2
