"""Tests for /restart slash command."""

from __future__ import annotations

import asyncio
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from durin.bus.events import InboundMessage
from durin.config.home import durin_home
from durin.providers.base import LLMResponse
from durin.utils.helpers import safe_filename


def _make_loop():
    """Create a minimal AgentLoop with mocked dependencies."""
    from durin.agent.loop import AgentLoop
    from durin.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    with patch("durin.agent.loop.ContextBuilder"), \
         patch("durin.agent.loop.SessionManager") as MockSessionMgr, \
         patch("durin.agent.loop.SubagentManager"):
        # One method on this double reaches the real filesystem: a turn feeds
        # `sessions._get_session_path(key)` to the turn lease, which formats it
        # into `Path(f"{target}.lock")`. A bare mock formats to its own repr —
        # a *relative* path — so the lock file gets created in the CWD (the
        # repo root) under a `<MagicMock ...>.lock` name. Mirror the real path
        # shape inside this test's throwaway DURIN_HOME instead.
        MockSessionMgr.return_value._get_session_path.side_effect = (
            lambda key: durin_home() / "sessions" / f"{safe_filename(key.replace(':', '_'))}.jsonl"
        )
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop, bus


@pytest.fixture(autouse=True)
def _stub_session_summary_store():
    """Audit (third pass, 2026-05-28): `_format_pending_summary` is
    invoked by `_process_message` on every turn and calls
    `get_session_summary(self.workspace, session.key)`. With the
    fully-mocked workspace + MagicMock session.key from `_make_loop`,
    the lazy file-load path hangs (MagicMock-path `is_file()` returns
    truthy, then `load_entry` chokes). Pre-A10 (2026-05-28) this path
    didn't exist, so the tests passed even with the mocks. Auto-stub
    `get_session_summary` to short-circuit it — these tests exercise
    CLI command handling, not session-summary persistence.
    """
    with patch(
        "durin.memory.session_summary_store.get_session_summary",
        return_value=(None, None),
    ):
        yield


async def _wait_until(predicate, *, timeout: float = 0.2, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    assert predicate()


class TestRestartCommand:

    @pytest.mark.asyncio
    async def test_restart_sends_message_and_calls_execv(self):
        from durin.command.builtin import cmd_restart
        from durin.command.router import CommandContext
        from durin.utils.restart import (
            RESTART_NOTIFY_CHANNEL_ENV,
            RESTART_NOTIFY_CHAT_ID_ENV,
            RESTART_STARTED_AT_ENV,
        )

        loop, bus = _make_loop()
        msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/restart")
        ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw="/restart", loop=loop)

        async def _fast_sleep(_delay: float) -> None:
            return None

        scheduled: list[asyncio.Task] = []

        def _capture_task(coro):
            task = asyncio.create_task(coro)
            scheduled.append(task)
            return task

        fake_asyncio = SimpleNamespace(
            sleep=_fast_sleep,
            create_task=_capture_task,
        )

        with patch.dict(os.environ, {}, clear=False), \
             patch("durin.command.builtin.asyncio", new=fake_asyncio), \
             patch("durin.utils.restart.os.execv") as mock_execv:
            out = await cmd_restart(ctx)
            assert "Restarting" in out.content
            assert os.environ.get(RESTART_NOTIFY_CHANNEL_ENV) == "cli"
            assert os.environ.get(RESTART_NOTIFY_CHAT_ID_ENV) == "direct"
            assert os.environ.get(RESTART_STARTED_AT_ENV)

            assert scheduled
            await scheduled[0]
            mock_execv.assert_called_once()

    @pytest.mark.asyncio
    async def test_restart_journals_the_turns_in_flight_before_exec(self, tmp_path):
        """os.execv discards everything in memory. /restart must journal the
        turns in flight first, like a graceful shutdown, or a turn waiting on
        the user's answer is lost instead of replayed by the new process."""
        from durin.agent import pending_answers
        from durin.agent.loop import AgentLoop
        from durin.agent.tools.ask_user import AskUserQuestionTool
        from durin.bus.queue import MessageBus
        from durin.command.builtin import cmd_restart
        from durin.command.router import CommandContext

        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        with patch("durin.agent.loop.ContextBuilder"), \
             patch("durin.agent.loop.SessionManager"), \
             patch("durin.agent.loop.SubagentManager"):
            loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path)
        pending_answers.reset()
        pending_answers.set_consumer_active(True)
        ask = AskUserQuestionTool(sessions=MagicMock(), blocking=True, answer_timeout_s=60)

        async def fake_dispatch(msg, pending=None):
            # Park on the real blocking-answer wait, as ask_user_question does.
            await ask._await_answer(msg.session_key, "q")

        loop._dispatch = fake_dispatch  # type: ignore[method-assign]
        loop._start_turn_task(
            InboundMessage(channel="websocket", sender_id="u", chat_id="c1", content="deploy it?"),
            "websocket:c1",
        )
        for _ in range(100):
            if pending_answers.is_waiting("websocket:c1"):
                break
            await asyncio.sleep(0)
        assert pending_answers.is_waiting("websocket:c1")

        restart = InboundMessage(channel="websocket", sender_id="u", chat_id="c2", content="/restart")
        ctx = CommandContext(msg=restart, session=None, key=restart.session_key,
                             raw="/restart", loop=loop)
        scheduled: list[asyncio.Task] = []

        async def _fast_sleep(_delay: float) -> None:
            return None

        def _capture_task(coro):
            task = asyncio.create_task(coro)
            scheduled.append(task)
            return task

        fake_asyncio = SimpleNamespace(sleep=_fast_sleep, create_task=_capture_task)
        try:
            with patch.dict(os.environ, {}, clear=False), \
                 patch("durin.command.builtin.asyncio", new=fake_asyncio), \
                 patch("durin.utils.restart.os.execv") as mock_execv:
                await cmd_restart(ctx)
                await scheduled[0]
        finally:
            pending_answers.reset()

        mock_execv.assert_called_once()
        assert [m.content for m in loop._inbound_journal.drain()] == ["deploy it?"]

    @pytest.mark.asyncio
    async def test_a_stuck_fallback_restart_still_reexecs_after_the_deadline(self, tmp_path):
        """The fallback path (no gateway — TUI, legacy REPL) has nothing at
        the asyncio level bounding ``close_mcp``. A real, synchronous
        thread-level block (a ``threading.Event``, not an ``asyncio`` one) is
        immune to any timeout or cancellation the event loop could apply —
        only a genuinely separate OS thread can still make progress while
        it's stuck. The watchdog's daemon thread is exactly that: it must
        still call reexec once its deadline passes, the same way SIGKILL
        rescues a stuck SIGTERM. The mocked ``reexec`` releases the block
        itself once it fires, the way the real one would end everything by
        replacing the process.

        This patches ``reexec`` itself rather than ``os.execv``: the real
        ``reexec`` makes the loser of a race block forever instead of
        returning (see ``tests/utils/test_restart.py``), which is safe in
        production — the winner's real ``execv`` ends that thread along with
        everything else moments later — but here, with nothing actually
        replacing this test process, the fallback's own call once released
        would be a second, legitimate "loser" call that then hangs this test
        forever. A plain mock has no such contract to honor.
        """
        from durin.command.builtin import cmd_restart
        from durin.command.router import CommandContext

        stuck = threading.Event()  # only the mocked reexec below ever sets this

        class _StuckLoop:
            def __init__(self) -> None:
                self.sessions = MagicMock()

            async def close_mcp(self) -> None:
                # A real OS-thread block via to_thread, not an asyncio wait:
                # nothing at the asyncio level could rescue this, only a
                # separate real thread (the watchdog) can. The 10s cap is a
                # last-resort safety net for this test process, well past
                # the patched 0.1s deadline below — it must never be what
                # actually makes the assertion true.
                await asyncio.to_thread(stuck.wait, 10)

            def stop(self) -> None:
                pass

            async def drain_inbound_for_shutdown(self) -> int:
                return 0

        loop = _StuckLoop()
        msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/restart")
        ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw="/restart", loop=loop)

        async def _fast_sleep(_delay: float) -> None:
            return None

        fake_asyncio = SimpleNamespace(sleep=_fast_sleep, create_task=asyncio.create_task)
        mock_reexec = MagicMock(side_effect=lambda: stuck.set())

        with patch.dict(os.environ, {}, clear=False), \
             patch("durin.command.builtin.asyncio", new=fake_asyncio), \
             patch("durin.utils.restart.RESTART_SHUTDOWN_DEADLINE_S", 0.1), \
             patch("durin.utils.restart.reexec", mock_reexec), \
             patch("durin.command.builtin.reexec", mock_reexec):
            await cmd_restart(ctx)
            fired_within_deadline_window = False
            for _ in range(40):  # 2s, well under close_mcp's 10s safety cap
                if mock_reexec.called:
                    fired_within_deadline_window = True
                    break
                await asyncio.sleep(0.05)

            # The watchdog's own call — proof it fired well within its
            # patched deadline, long before close_mcp's 10s safety cap. Once
            # released, the fallback's own path also reaches its end-of-
            # sequence reexec() call in this same window (a second, harmless
            # call to this plain mock, unlike the real idempotent reexec —
            # see the class docstring above), so this doesn't assert an
            # exact count, only that the watchdog's own call happened.
            assert fired_within_deadline_window, (
                "reexec did not run within the patched deadline window"
            )
            # Let the fallback's own path finish naturally so nothing leaks
            # past this patched context.
            await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_a_cancelled_restart_cancels_its_own_watchdog(self, tmp_path):
        """The TUI quitting mid-restart cancels ``_do_restart``'s own task
        (``asyncio.run`` tears it down). Nobody is restarting anymore at
        that point, so the watchdog armed for it must not survive to
        re-launch durin later, well after the user already quit."""
        import durin.utils.restart as restart_mod
        from durin.command.builtin import cmd_restart
        from durin.command.router import CommandContext

        started = asyncio.Event()

        class _SlowLoop:
            def __init__(self) -> None:
                self.sessions = MagicMock()

            async def close_mcp(self) -> None:
                started.set()
                await asyncio.Event().wait()  # the test cancels before this resolves

        loop = _SlowLoop()
        msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/restart")
        ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw="/restart", loop=loop)

        scheduled: list[asyncio.Task] = []

        async def _fast_sleep(_delay: float) -> None:
            return None

        def _capture_task(coro):
            task = asyncio.create_task(coro)
            scheduled.append(task)
            return task

        # cmd_restart's own `except asyncio.CancelledError` needs the real
        # exception type too — this test is the first to actually reach
        # that branch through a fully replaced `asyncio` reference.
        fake_asyncio = SimpleNamespace(
            sleep=_fast_sleep, create_task=_capture_task, CancelledError=asyncio.CancelledError,
        )
        created_timers: list[threading.Timer] = []
        real_arm_restart_deadline = restart_mod.arm_restart_deadline

        def _tracking_arm(*args, **kwargs):
            timer = real_arm_restart_deadline(*args, **kwargs)
            created_timers.append(timer)
            return timer

        with patch.dict(os.environ, {}, clear=False), \
             patch("durin.command.builtin.asyncio", new=fake_asyncio), \
             patch("durin.command.builtin.arm_restart_deadline", _tracking_arm), \
             patch("durin.utils.restart.os.execv") as mock_execv:
            await cmd_restart(ctx)
            await started.wait()
            task = scheduled[0]
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # Captured before this test's own cleanup (none needed here,
            # since the code under test must already have cancelled it) —
            # `finished` is set by a real fire OR a cancel, but the timer's
            # real deadline (30s, unpatched) cannot have elapsed in this
            # test's runtime, so True here can only mean the code cancelled
            # it.
            assert created_timers, "no watchdog timer was armed"
            assert [t.finished.is_set() for t in created_timers] == [True]
        mock_execv.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_intercepted_in_run_loop(self):
        """Verify /restart is handled at the run-loop level, not inside _dispatch."""
        loop, bus = _make_loop()
        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/restart")

        with patch.object(loop, "_dispatch", new_callable=AsyncMock) as mock_dispatch, \
             patch("durin.utils.restart.os.execv"):
            await bus.publish_inbound(msg)

            loop._running = True
            run_task = asyncio.create_task(loop.run())
            await asyncio.sleep(0.1)
            loop._running = False
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass

            mock_dispatch.assert_not_called()
            out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
            assert "Restarting" in out.content

    @pytest.mark.asyncio
    async def test_status_intercepted_in_run_loop(self):
        """Verify /status is handled at the run-loop level for immediate replies."""
        loop, bus = _make_loop()
        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/status")

        with patch.object(loop, "_dispatch", new_callable=AsyncMock) as mock_dispatch:
            await bus.publish_inbound(msg)

            loop._running = True
            run_task = asyncio.create_task(loop.run())
            await asyncio.sleep(0.1)
            loop._running = False
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass

            mock_dispatch.assert_not_called()
            out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
            assert "durin" in out.content.lower() or "Model" in out.content

    @pytest.mark.asyncio
    async def test_run_propagates_external_cancellation(self):
        """External task cancellation should not be swallowed by the inbound wait loop."""
        loop, _bus = _make_loop()

        run_task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.1)
        run_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_restart_hidden_from_help_but_dispatchable(self):
        from durin.command.builtin import register_builtin_commands
        from durin.command.router import CommandRouter

        loop, bus = _make_loop()
        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/help")

        response = await loop._process_message(msg)

        assert response is not None
        # Admin commands hidden from help output
        assert "/restart" not in response.content
        assert "/status" in response.content
        assert response.metadata == {"render_as": "text"}

        # Verify /restart is still registered and dispatchable
        router = CommandRouter()
        register_builtin_commands(router)
        assert "/restart" in router._priority

    @pytest.mark.asyncio
    async def test_status_reports_runtime_info(self):
        loop, _bus = _make_loop()
        session = MagicMock()
        session.get_history.return_value = [{"role": "user"}] * 3
        loop.sessions.get_or_create.return_value = session
        loop._start_time = time.time() - 125
        loop._last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        loop.consolidator.estimate_session_prompt_tokens = MagicMock(
            return_value=(20500, "tiktoken")
        )
        loop.subagents.get_running_count_by_session.return_value = 0

        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/status")

        response = await loop._process_message(msg)

        assert response is not None
        assert "Model: test-model" in response.content
        assert "Tokens: 0 in / 0 out" in response.content
        # Denominated by the compaction trigger (49,152 = 0.75 x 65,536), not the
        # raw window and not the consolidator's own input budget.
        assert "Context: 20k/65k (41% to compaction)" in response.content
        assert "Session: 3 messages" in response.content
        assert "Uptime: 2m 5s" in response.content
        assert "Tasks: 0 active" in response.content
        assert response.metadata == {"render_as": "text"}

    @pytest.mark.asyncio
    async def test_status_counts_running_dispatch_and_subagent_tasks(self):
        loop, _bus = _make_loop()
        session = MagicMock()
        session.get_history.return_value = [{"role": "user"}]
        loop.sessions.get_or_create.return_value = session
        loop.consolidator.estimate_session_prompt_tokens = MagicMock(
            return_value=(1000, "tiktoken")
        )

        running_task = MagicMock()
        running_task.done.return_value = False
        finished_task = MagicMock()
        finished_task.done.return_value = True

        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/status")
        loop._active_tasks[msg.session_key] = [running_task, finished_task]
        loop.subagents.get_running_count_by_session.return_value = 2

        response = await loop._process_message(msg)

        assert response is not None
        assert "Tasks: 3 active" in response.content

    @pytest.mark.asyncio
    async def test_run_agent_loop_resets_usage_when_provider_omits_it(self):
        loop, _bus = _make_loop()
        loop.provider.chat_with_retry = AsyncMock(side_effect=[
            LLMResponse(content="first", usage={"prompt_tokens": 9, "completion_tokens": 4}),
            LLMResponse(content="second", usage={}),
        ])

        await loop._run_agent_loop([])
        assert loop._last_usage["prompt_tokens"] == 9
        assert loop._last_usage["completion_tokens"] == 4

        await loop._run_agent_loop([])
        assert loop._last_usage["prompt_tokens"] == 0
        assert loop._last_usage["completion_tokens"] == 0

    @pytest.mark.asyncio
    async def test_status_falls_back_to_last_usage_when_context_estimate_missing(self):
        loop, _bus = _make_loop()
        session = MagicMock()
        session.get_history.return_value = [{"role": "user"}]
        loop.sessions.get_or_create.return_value = session
        loop._last_usage = {"prompt_tokens": 1200, "completion_tokens": 34}
        loop.consolidator.estimate_session_prompt_tokens = MagicMock(
            return_value=(0, "none")
        )
        loop.subagents.get_running_count_by_session.return_value = 0

        response = await loop._process_message(
            InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/status")
        )

        assert response is not None
        assert "Tokens: 1200 in / 34 out" in response.content
        assert "Context: 1k/65k (2% to compaction)" in response.content
        assert "Tasks: 0 active" in response.content

    @pytest.mark.asyncio
    async def test_history_shows_recent_messages(self):
        loop, _bus = _make_loop()
        session = MagicMock()
        session.get_history.return_value = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "tool", "content": "tool result"},  # should be filtered out
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": "I am doing well."},
        ]
        loop.sessions.get_or_create.return_value = session

        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/history")
        response = await loop._process_message(msg)

        assert response is not None
        assert "👤 You: Hello" in response.content
        assert "🤖 Bot: Hi there!" in response.content
        assert "tool result" not in response.content  # tool messages filtered
        assert response.metadata == {"render_as": "text"}

    @pytest.mark.asyncio
    async def test_history_respects_count_argument(self):
        loop, _bus = _make_loop()
        session = MagicMock()
        session.get_history.return_value = [
            {"role": "user", "content": f"message {i}"} for i in range(20)
        ]
        loop.sessions.get_or_create.return_value = session

        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/history 3")
        response = await loop._process_message(msg)

        assert response is not None
        assert "Last 3 message(s)" in response.content
        assert "message 19" in response.content  # most recent
        assert "message 0" not in response.content  # too old

    @pytest.mark.asyncio
    async def test_history_clamps_count_and_extracts_text_blocks(self):
        loop, _bus = _make_loop()
        session = MagicMock()
        session.get_history.return_value = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "visible text"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
                ],
            },
            *({"role": "assistant", "content": f"reply {i}"} for i in range(60)),
        ]
        loop.sessions.get_or_create.return_value = session

        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/history 999")
        response = await loop._process_message(msg)

        assert response is not None
        assert "Last 50 message(s)" in response.content
        assert "visible text" not in response.content
        assert "reply 59" in response.content
        assert "reply 9" not in response.content

    @pytest.mark.asyncio
    async def test_history_invalid_count_returns_usage(self):
        loop, _bus = _make_loop()

        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/history nope")
        response = await loop._process_message(msg)

        assert response is not None
        assert response.content.startswith("Usage: /history [count]")

    @pytest.mark.asyncio
    async def test_history_empty_session(self):
        loop, _bus = _make_loop()
        session = MagicMock()
        session.get_history.return_value = []
        loop.sessions.get_or_create.return_value = session

        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/history")
        response = await loop._process_message(msg)

        assert response is not None
        assert "No conversation history yet." in response.content

    @pytest.mark.asyncio
    async def test_process_direct_preserves_render_metadata(self):
        loop, _bus = _make_loop()
        session = MagicMock()
        session.get_history.return_value = []
        loop.sessions.get_or_create.return_value = session
        loop.subagents.get_running_count.return_value = 0
        loop.subagents.get_running_count_by_session.return_value = 0

        response = await loop.process_direct("/status", session_key="cli:test")

        assert response is not None
        assert response.metadata == {"render_as": "text"}
