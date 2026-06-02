"""Tests for the Dream class — two-phase memory consolidation via AgentRunner."""

import json

import pytest

from unittest.mock import AsyncMock, MagicMock, patch

from durin.agent.memory import Dream, MemoryStore
from durin.agent.runner import AgentRunResult
from durin.agent.skills import BUILTIN_SKILLS_DIR
from durin.utils.gitstore import LineAge


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path)
    s.write_soul("# Soul\n- Helpful")
    s.write_user("# User\n- Developer")
    s.write_memory("# Memory\n- Project X active")
    return s


@pytest.fixture
def mock_provider():
    p = MagicMock()
    p.chat_with_retry = AsyncMock()
    return p


@pytest.fixture
def mock_runner():
    return MagicMock()


@pytest.fixture
def dream(store, mock_provider, mock_runner):
    # min_tokens_to_run=0 disables the pre-LLM token gate so generic
    # tests can exercise the runtime path with tiny stub entries. Tests
    # that specifically cover the gate construct their own Dream with a
    # non-zero threshold.
    d = Dream(
        store=store, provider=mock_provider, model="test-model",
        max_batch_size=5, min_tokens_to_run=0,
    )
    d._runner = mock_runner
    return d


def _make_run_result(
    stop_reason="completed",
    final_content=None,
    tool_events=None,
    usage=None,
):
    return AgentRunResult(
        final_content=final_content or stop_reason,
        stop_reason=stop_reason,
        messages=[],
        tools_used=[],
        usage={},
        tool_events=tool_events or [],
    )


class TestDreamRun:
    async def test_noop_when_no_unprocessed_history(self, dream, mock_provider, mock_runner, store):
        """Dream should not call LLM when there's nothing to process."""
        result = await dream.run()
        assert result is False
        mock_provider.chat_with_retry.assert_not_called()
        mock_runner.run.assert_not_called()

    async def test_calls_runner_for_unprocessed_entries(self, dream, mock_provider, mock_runner, store):
        """Dream should call AgentRunner when there are unprocessed history entries."""
        store.append_history("User prefers dark mode")
        mock_provider.chat_with_retry.return_value = MagicMock(content="New fact")
        mock_runner.run = AsyncMock(return_value=_make_run_result(
            tool_events=[{"name": "edit_file", "status": "ok", "detail": "memory/MEMORY.md"}],
        ))
        result = await dream.run()
        assert result is True
        mock_runner.run.assert_called_once()
        spec = mock_runner.run.call_args[0][0]
        assert spec.max_iterations == 10
        assert spec.fail_on_tool_error is False

    async def test_advances_dream_cursor(self, dream, mock_provider, mock_runner, store):
        """Dream should advance the cursor after processing."""
        store.append_history("event 1")
        store.append_history("event 2")
        mock_provider.chat_with_retry.return_value = MagicMock(content="Nothing new")
        mock_runner.run = AsyncMock(return_value=_make_run_result())
        await dream.run()
        assert store.get_last_dream_cursor() == 2

    async def test_compacts_processed_history(self, dream, mock_provider, mock_runner, store):
        """Dream should compact history after processing."""
        store.append_history("event 1")
        store.append_history("event 2")
        store.append_history("event 3")
        mock_provider.chat_with_retry.return_value = MagicMock(content="Nothing new")
        mock_runner.run = AsyncMock(return_value=_make_run_result())
        await dream.run()
        # After Dream, cursor is advanced and 3, compact keeps last max_history_entries
        entries = store.read_unprocessed_history(since_cursor=0)
        assert all(e["cursor"] > 0 for e in entries)

    async def test_skill_phase_uses_builtin_skill_creator_path(self, dream, mock_provider, mock_runner, store):
        """Dream should point skill creation guidance at the builtin skill-creator template."""
        store.append_history("Repeated workflow one")
        store.append_history("Repeated workflow two")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKILL] test-skill: test description")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream.run()

        spec = mock_runner.run.call_args[0][0]
        system_prompt = spec.initial_messages[0]["content"]
        expected = str(BUILTIN_SKILLS_DIR / "skill-creator" / "SKILL.md")
        assert expected in system_prompt

    async def test_skill_write_tool_authors_skill_under_workspace(self, dream, store):
        """Dream skill creation routes through skill_write, writing skills/<name>/SKILL.md."""
        skill_tool = dream._tools.get("skill_write")
        assert skill_tool is not None
        assert dream._tools.get("write_file") is None

        await skill_tool.execute(
            name="test-skill",
            content="---\nname: test-skill\ndescription: Test\n---\n",
            rationale="recurring pattern not covered by existing skills",
        )

        assert (store.workspace / "skills" / "test-skill" / "SKILL.md").exists()

    async def test_skips_llm_when_tokens_below_threshold(
        self, store, mock_provider, mock_runner,
    ):
        """Pre-LLM gate: unprocessed history under min_tokens_to_run must
        NOT invoke Phase 1. Cursor stays untouched so the next pass picks
        up the same entries if more arrive."""
        d = Dream(
            store=store, provider=mock_provider, model="test",
            max_batch_size=5, min_tokens_to_run=2000,
        )
        d._runner = mock_runner
        # ~5 tokens — well under threshold
        store.append_history("hola")
        result = await d.run()
        assert result is False
        mock_provider.chat_with_retry.assert_not_called()
        mock_runner.run.assert_not_called()
        # cursor not advanced — entry remains pending
        assert store.get_last_dream_cursor() == 0

    async def test_runs_llm_when_tokens_meet_threshold(
        self, store, mock_provider, mock_runner,
    ):
        """Threshold is a floor: total tokens >= threshold runs Phase 1.
        Use a low threshold + a payload that empirically clears it under
        cl100k_base so the assertion stays stable across tiktoken updates."""
        d = Dream(
            store=store, provider=mock_provider, model="test",
            max_batch_size=5, min_tokens_to_run=5,  # easy to clear
        )
        d._runner = mock_runner
        # ~8 tokens under cl100k_base — clears a 5-token floor
        store.append_history("User prefers dark mode and writes Python primarily")
        mock_provider.chat_with_retry.return_value = MagicMock(content="ok")
        mock_runner.run = AsyncMock(return_value=_make_run_result())
        result = await d.run()
        assert result is True
        mock_runner.run.assert_called_once()

    async def test_min_tokens_zero_disables_gate(
        self, store, mock_provider, mock_runner,
    ):
        """min_tokens_to_run=0 reverts to pre-fix behaviour: any non-empty
        unprocessed history triggers Phase 1, regardless of size."""
        d = Dream(
            store=store, provider=mock_provider, model="test",
            max_batch_size=5, min_tokens_to_run=0,
        )
        d._runner = mock_runner
        store.append_history("ok")  # ~1 token, would be filtered if gate active
        mock_provider.chat_with_retry.return_value = MagicMock(content="ok")
        mock_runner.run = AsyncMock(return_value=_make_run_result())
        result = await d.run()
        assert result is True
        mock_runner.run.assert_called_once()

    async def test_emits_skipped_no_entries(
        self, dream, mock_provider, mock_runner, store, monkeypatch,
    ):
        """No unprocessed history → memory.dream.legacy.skipped(no_entries)."""
        events: list[tuple[str, dict]] = []
        from durin.agent.tools import _telemetry as _tel_mod
        monkeypatch.setattr(
            _tel_mod, "emit_tool_event",
            lambda et, d: events.append((et, dict(d))),
        )
        result = await dream.run()
        assert result is False
        assert ("memory.dream.legacy.skipped", {
            "reason": "no_entries", "model": "test-model",
        }) in events

    async def test_emits_skipped_below_token_threshold(
        self, store, mock_provider, mock_runner, monkeypatch,
    ):
        """Token-floor skip → memory.dream.legacy.skipped(below_token_threshold)
        with the actual token count + threshold + entries_count."""
        events: list[tuple[str, dict]] = []
        from durin.agent.tools import _telemetry as _tel_mod
        monkeypatch.setattr(
            _tel_mod, "emit_tool_event",
            lambda et, d: events.append((et, dict(d))),
        )
        d = Dream(
            store=store, provider=mock_provider, model="test-model",
            max_batch_size=5, min_tokens_to_run=2000,
        )
        d._runner = mock_runner
        store.append_history("hola")
        result = await d.run()
        assert result is False
        skips = [(et, p) for et, p in events if et == "memory.dream.legacy.skipped"]
        assert len(skips) == 1
        assert skips[0][1]["reason"] == "below_token_threshold"
        assert skips[0][1]["threshold"] == 2000
        assert skips[0][1]["entries_count"] == 1
        assert skips[0][1]["tokens"] < 2000
        assert skips[0][1]["model"] == "test-model"

    async def test_emits_start_and_end_on_success(
        self, dream, mock_provider, mock_runner, store, monkeypatch,
    ):
        """Successful pass emits start + end(status=ok, cursor_advanced=True)."""
        events: list[tuple[str, dict]] = []
        from durin.agent.tools import _telemetry as _tel_mod
        monkeypatch.setattr(
            _tel_mod, "emit_tool_event",
            lambda et, d: events.append((et, dict(d))),
        )
        store.append_history("event 1")
        store.append_history("event 2")
        # Phase 1 response with usage stats so we can assert tokens land in end payload.
        phase1 = MagicMock(content="analysis")
        phase1.usage = {"prompt_tokens": 123, "completion_tokens": 45}
        mock_provider.chat_with_retry.return_value = phase1
        mock_runner.run = AsyncMock(return_value=_make_run_result(
            tool_events=[
                {"name": "edit_file", "status": "ok", "detail": "memory/MEMORY.md"},
            ],
        ))
        result = await dream.run()
        assert result is True

        types = [et for et, _ in events]
        assert "memory.dream.legacy.start" in types
        assert "memory.dream.legacy.end" in types

        start_payload = next(p for et, p in events if et == "memory.dream.legacy.start")
        assert start_payload["entries_count"] == 2
        assert start_payload["batch_size"] == 2
        assert start_payload["model"] == "test-model"
        assert start_payload["tokens"] >= 0

        end_payload = next(p for et, p in events if et == "memory.dream.legacy.end")
        assert end_payload["status"] == "ok"
        assert end_payload["cursor_advanced"] is True
        assert end_payload["changelog_count"] == 1
        assert end_payload["phase1_prompt_tokens"] == 123
        assert end_payload["phase1_completion_tokens"] == 45
        assert end_payload["phase2_tool_events"] == 1
        assert end_payload["model"] == "test-model"
        assert end_payload["duration_ms"] >= 0

    async def test_emits_end_phase1_failed_when_provider_raises(
        self, dream, mock_provider, mock_runner, store, monkeypatch,
    ):
        """Phase 1 exception → end(status=phase1_failed, cursor_advanced=False)."""
        events: list[tuple[str, dict]] = []
        from durin.agent.tools import _telemetry as _tel_mod
        monkeypatch.setattr(
            _tel_mod, "emit_tool_event",
            lambda et, d: events.append((et, dict(d))),
        )
        store.append_history("event 1")
        mock_provider.chat_with_retry.side_effect = RuntimeError("upstream wedged")
        result = await dream.run()
        assert result is False
        end_payload = next(p for et, p in events if et == "memory.dream.legacy.end")
        assert end_payload["status"] == "phase1_failed"
        assert end_payload["cursor_advanced"] is False
        # cursor must NOT advance on failure
        assert store.get_last_dream_cursor() == 0

    async def test_phase1_prompt_includes_line_age_annotations(self, dream, mock_provider, mock_runner, store):
        """Phase 1 prompt should have per-line age suffixes in MEMORY.md when git is available."""
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        # Init git so line_ages works
        store.git.init()
        store.git.auto_commit("initial memory state")

        await dream.run()

        # The MEMORY.md section should not crash and should contain the memory content
        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        assert "## Current MEMORY.md" in user_msg

    async def test_phase1_annotates_only_memory_not_soul_or_user(self, dream, mock_provider, mock_runner, store):
        """SOUL.md and USER.md should never have age annotations — they are permanent."""
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        store.git.init()
        store.git.auto_commit("initial state")

        await dream.run()

        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        # The ← suffix should only appear in MEMORY.md section
        memory_section = user_msg.split("## Current MEMORY.md")[1].split("## Current SOUL.md")[0]
        soul_section = user_msg.split("## Current SOUL.md")[1].split("## Current USER.md")[0]
        user_section = user_msg.split("## Current USER.md")[1]
        # SOUL and USER should not contain age arrows
        assert "\u2190" not in soul_section
        assert "\u2190" not in user_section

    async def test_phase1_prompt_works_without_git(self, dream, mock_provider, mock_runner, store):
        """Phase 1 should work fine even if git is not initialized (no age annotations)."""
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream.run()

        # Should still succeed — just without age annotations
        mock_provider.chat_with_retry.assert_called_once()
        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        assert "## Current MEMORY.md" in user_msg

    async def test_phase1_prompt_carries_age_suffix_for_stale_lines(
        self, dream, mock_provider, mock_runner, store,
    ):
        """End-to-end: ages >14d must appear verbatim in the LLM prompt, ages ≤14d must not."""
        # MEMORY.md fixture has 2 non-blank lines ("# Memory" and "- Project X active").
        # Inject four ages to cover threshold boundaries: >14 suffix, ==14 no suffix, <14 no suffix.
        store.write_memory("# Memory\n- Project X active\n- fresh item\n- edge case line")
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        fake_ages = [
            LineAge(age_days=30),   # "# Memory"        → should get ← 30d
            LineAge(age_days=20),   # "- Project X..."  → should get ← 20d
            LineAge(age_days=14),   # "- fresh item"    → ==14, threshold is strictly >14, no suffix
            LineAge(age_days=5),    # "- edge case..."  → no suffix
        ]
        with patch.object(store.git, "line_ages", return_value=fake_ages):
            await dream.run()

        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        memory_section = user_msg.split("## Current MEMORY.md")[1].split("## Current SOUL.md")[0]
        assert "\u2190 30d" in memory_section
        assert "\u2190 20d" in memory_section
        assert "\u2190 14d" not in memory_section
        assert "\u2190 5d" not in memory_section

    async def test_phase1_skips_annotation_when_disabled(
        self, dream, mock_provider, mock_runner, store,
    ):
        """`annotate_line_ages=False` must bypass the git lookup entirely and keep MEMORY.md raw."""
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        dream.annotate_line_ages = False
        # line_ages must be bypassed entirely — verify with a spy rather than a
        # raising side_effect, because _annotate_with_ages catches Exception
        # (which swallows AssertionError) and would hide an accidental call.
        with patch.object(store.git, "line_ages") as mock_line_ages:
            await dream.run()
            mock_line_ages.assert_not_called()

        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        assert "\u2190" not in user_msg

    async def test_phase1_skips_annotation_on_line_ages_length_mismatch(
        self, dream, mock_provider, mock_runner, store,
    ):
        """If ages length != lines length (dirty working tree), skip annotation instead of mis-tagging."""
        # MEMORY.md has 2 non-blank lines but we hand back only 1 age → mismatch.
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        with patch.object(store.git, "line_ages", return_value=[LineAge(age_days=999)]):
            await dream.run()

        call_args = mock_provider.chat_with_retry.call_args
        user_msg = call_args.kwargs.get("messages", call_args[1].get("messages"))[1]["content"]
        memory_section = user_msg.split("## Current MEMORY.md")[1].split("## Current SOUL.md")[0]
        # No age arrow at all — we refused to annotate rather than tag the wrong line.
        assert "\u2190" not in memory_section

    async def test_phase1_prompt_uses_threshold_from_template_var(
        self, dream, mock_provider, mock_runner, store,
    ):
        """System prompt should reference the stale-threshold constant, not a hardcoded 14."""
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream.run()

        system_msg = mock_provider.chat_with_retry.call_args.kwargs["messages"][0]["content"]
        # The template renders with stale_threshold_days=14 → LLM must see "N>14"
        assert "N>14" in system_msg


class TestDreamPromptCaps:
    """Dream's Phase 1/2 prompt must not be poisoned by a legacy oversized
    history entry or a runaway MEMORY.md. Without caps, a single pre-#3412
    raw_archive dump in history.jsonl would make every subsequent Dream run
    exceed the context window and silently advance the cursor past real work.
    """

    async def test_phase1_caps_huge_memory_file(
        self, dream, mock_provider, mock_runner, store,
    ):
        """A MEMORY.md much larger than _MEMORY_FILE_MAX_CHARS must be truncated
        in the prompt preview (full content is still reachable via read_file)."""
        store.write_memory("M" * (dream._MEMORY_FILE_MAX_CHARS * 5))
        store.append_history("some event")
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream.run()

        user_msg = mock_provider.chat_with_retry.call_args.kwargs["messages"][1]["content"]
        memory_section = user_msg.split("## Current MEMORY.md")[1].split("## Current SOUL.md")[0]
        assert len(memory_section) < dream._MEMORY_FILE_MAX_CHARS + 500

    async def test_phase1_caps_huge_history_entry(
        self, dream, mock_provider, mock_runner, store,
    ):
        """A legacy oversized history entry (e.g. pre-#3412 raw_archive dump)
        must not explode the Phase 1 prompt — each entry is capped in the
        preview, even though the JSONL record itself stays full-size."""
        # Bypass the append_history cap by writing directly, simulating a
        # record that was written by an older durin build before any caps.
        store.history_file.write_text(
            json.dumps({
                "cursor": 1,
                "timestamp": "2026-04-01 10:00",
                "content": "H" * (dream._HISTORY_ENTRY_PREVIEW_MAX_CHARS * 8),
            }) + "\n",
            encoding="utf-8",
        )
        mock_provider.chat_with_retry.return_value = MagicMock(content="[SKIP]")
        mock_runner.run = AsyncMock(return_value=_make_run_result())

        await dream.run()

        user_msg = mock_provider.chat_with_retry.call_args.kwargs["messages"][1]["content"]
        history_section = user_msg.split("## Conversation History\n")[1].split("\n\n## Current Date")[0]
        assert len(history_section) < dream._HISTORY_ENTRY_PREVIEW_MAX_CHARS + 500

