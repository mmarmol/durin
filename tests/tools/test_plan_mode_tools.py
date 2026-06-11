"""Tests for the LLM-callable plan mode tools (Sprint B / L3)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from durin.agent.agent_mode import (
    PLAN_MODE,
    SESSION_MODE_KEY,
    SESSION_PRE_PLAN_KEY,
)
from durin.agent.tools.context import RequestContext
from durin.agent.tools.plan_mode import (
    _ACTIVE_PLAN_PATH_KEY,
    _PLAN_DIR,
    EnterPlanModeTool,
    ExitPlanModeTool,
)
from durin.telemetry.logger import (
    TelemetryLogger,
    bind_telemetry,
    reset_telemetry,
)


class _FakeSessions:
    """Minimal SessionManager stand-in: hands out the same session for any key."""

    def __init__(self, session):
        self._session = session

    def get_or_create(self, key: str):
        return self._session


def _session(meta: dict | None = None):
    return SimpleNamespace(metadata=meta if meta is not None else {})


# Minimal tail that satisfies the verification lint — tests that exercise
# storage/metadata mechanics (not lint behavior) append this to their plans.
_VERIF = "\n\n## Verification\n- verify: covered by test assertions"


# ---------------------------------------------------------------------------
# EnterPlanModeTool
# ---------------------------------------------------------------------------


class TestEnterPlanModeTool:

    def test_enters_plan(self):
        session = _session({SESSION_MODE_KEY: "build"})
        tool = EnterPlanModeTool(sessions=_FakeSessions(session))
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))
        out = asyncio.run(tool.execute())
        assert "Entered PLAN MODE" in out
        assert session.metadata[SESSION_MODE_KEY] == "plan"
        assert session.metadata[SESSION_PRE_PLAN_KEY] == "build"

    def test_idempotent_when_already_in_plan(self):
        session = _session({SESSION_MODE_KEY: "plan"})
        tool = EnterPlanModeTool(sessions=_FakeSessions(session))
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))
        out = asyncio.run(tool.execute())
        assert "Already in PLAN" in out

    def test_no_session_returns_error(self):
        tool = EnterPlanModeTool(sessions=_FakeSessions(SimpleNamespace(metadata=None)))
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))
        out = asyncio.run(tool.execute())
        assert "Error" in out

    def test_emits_telemetry_with_trigger_tool(self, tmp_path: Path):
        log_path = tmp_path / "tel.jsonl"
        logger = TelemetryLogger(log_path)
        session = _session({SESSION_MODE_KEY: "build"})
        tool = EnterPlanModeTool(sessions=_FakeSessions(session))
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))

        token = bind_telemetry(logger)
        try:
            asyncio.run(tool.execute(reason="testing"))
        finally:
            reset_telemetry(token)

        events = [json.loads(l) for l in log_path.read_text().splitlines() if l]
        switch = [e for e in events if e["type"] == "agent_mode.switch"]
        assert len(switch) == 1
        assert switch[0]["data"]["trigger"] == "tool"
        assert switch[0]["data"]["reason"] == "testing"


# ---------------------------------------------------------------------------
# ExitPlanModeTool
# ---------------------------------------------------------------------------


class TestExitPlanModeTool:

    def test_writes_plan_to_disk_and_keeps_in_plan_mode(self, tmp_path: Path):
        """exit_plan_mode writes to <workspace>/.durin/plans/<session>/,
        returns path, session stays in plan until /build approves."""
        session = _session({SESSION_MODE_KEY: "plan", SESSION_PRE_PLAN_KEY: "build"})
        tool = ExitPlanModeTool(sessions=_FakeSessions(session), workspace=tmp_path)
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="cli_chat42"))

        plan_text = "# Plan\n\n1. Read files\n2. Apply edits" + _VERIF
        out = asyncio.run(tool.execute(plan=plan_text))

        # Plan path is in the tool result
        assert "plan_" in out and ".md" in out
        assert ".durin/plans" in out
        # The channel presents the plan (payload-canonical contract) — the
        # result must NOT duplicate the plan body nor instruct re-presentation.
        assert "presented to the user" in out
        assert "/build" in out
        assert "1. Read files" not in out
        # The pending payload carries the plan for channel rendering/fallback.
        pending = session.metadata.get("pending_plan_review")
        assert pending is not None
        assert pending["plan"] == plan_text
        assert pending["path"].endswith(".md")
        # Session remains in plan mode
        assert session.metadata[SESSION_MODE_KEY] == "plan"
        # Active plan path tracked in session metadata
        active_path = session.metadata.get(_ACTIVE_PLAN_PATH_KEY)
        assert active_path is not None
        assert Path(active_path).exists()
        assert Path(active_path).read_text() == plan_text
        # File is under <workspace>/.durin/plans/<session-slug>/
        expected_dir = (tmp_path / _PLAN_DIR / "cli_chat42").resolve()
        assert Path(active_path).parent == expected_dir

    def test_consecutive_plans_create_distinct_files(self, tmp_path: Path):
        """A second exit_plan_mode call creates a fresh file and updates
        the active_plan_path — earlier versions remain on disk for history."""
        session = _session({SESSION_MODE_KEY: "plan"})
        tool = ExitPlanModeTool(sessions=_FakeSessions(session), workspace=tmp_path)
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))

        asyncio.run(tool.execute(plan="version 1" + _VERIF))
        first_path = session.metadata[_ACTIVE_PLAN_PATH_KEY]

        # Sleep briefly so timestamp differs; the ms suffix would handle
        # same-second collisions anyway.
        import time as _t
        _t.sleep(0.01)

        asyncio.run(tool.execute(plan="version 2 with refinement" + _VERIF))
        second_path = session.metadata[_ACTIVE_PLAN_PATH_KEY]

        assert first_path != second_path
        assert Path(first_path).read_text() == "version 1" + _VERIF
        assert Path(second_path).read_text() == "version 2 with refinement" + _VERIF

    def test_user_edit_to_plan_file_persists(self, tmp_path: Path):
        """The user can edit the plan file directly before /build — the
        next read returns the edited content."""
        session = _session({SESSION_MODE_KEY: "plan"})
        tool = ExitPlanModeTool(sessions=_FakeSessions(session), workspace=tmp_path)
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))

        asyncio.run(tool.execute(plan="step 1\nstep 2" + _VERIF))
        path = Path(session.metadata[_ACTIVE_PLAN_PATH_KEY])

        # Simulate the user editing the plan file directly
        path.write_text("step 1\nstep 2 (edited)\nstep 3")

        assert path.read_text() == "step 1\nstep 2 (edited)\nstep 3"

    def test_rejects_empty_plan(self, tmp_path: Path):
        session = _session({SESSION_MODE_KEY: "plan"})
        tool = ExitPlanModeTool(sessions=_FakeSessions(session), workspace=tmp_path)
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))
        out = asyncio.run(tool.execute(plan=""))
        assert "Error" in out
        # No file written
        assert not (tmp_path / _PLAN_DIR).exists()

    def test_rejects_when_not_in_plan_mode(self, tmp_path: Path):
        session = _session({SESSION_MODE_KEY: "build"})
        tool = ExitPlanModeTool(sessions=_FakeSessions(session), workspace=tmp_path)
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))
        out = asyncio.run(tool.execute(plan="plan text"))
        assert "only be called while in plan mode" in out

    def test_emits_presented_telemetry_with_path(self, tmp_path: Path):
        log_path = tmp_path / "tel.jsonl"
        logger = TelemetryLogger(log_path)
        session = _session({SESSION_MODE_KEY: "plan"})
        tool = ExitPlanModeTool(sessions=_FakeSessions(session), workspace=tmp_path)
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))

        token = bind_telemetry(logger)
        try:
            asyncio.run(tool.execute(plan="# My plan\n- Step 1" + _VERIF))
        finally:
            reset_telemetry(token)

        events = [json.loads(l) for l in log_path.read_text().splitlines() if l]
        presented = [e for e in events if e["type"] == "plan_mode.presented"]
        assert len(presented) == 1
        assert presented[0]["data"]["plan_chars"] > 0
        assert presented[0]["data"]["from_mode"] == "plan"
        assert presented[0]["data"]["plan_path"]
        assert Path(presented[0]["data"]["plan_path"]).exists()

    def test_rejects_plan_without_verification_criteria(self, tmp_path: Path):
        """Hard reject: a plan with no Verification section / verify: lines
        is never written to disk (approved design 2026-06-11)."""
        session = _session({SESSION_MODE_KEY: "plan", SESSION_PRE_PLAN_KEY: "build"})
        tool = ExitPlanModeTool(sessions=_FakeSessions(session), workspace=tmp_path)
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))

        out = asyncio.run(tool.execute(plan="# Plan\n\n1. Edit file\n2. Run app"))

        assert "Error" in out
        assert "verification" in out.lower()
        assert "## Verification" in out  # error must teach the fix
        # Nothing persisted: no file, no metadata side effects
        assert session.metadata.get(_ACTIVE_PLAN_PATH_KEY) is None
        assert session.metadata.get("pending_plan_review") is None
        assert not list(tmp_path.rglob("plan_*.md"))
        # Session stays in plan mode for the retry
        assert session.metadata[SESSION_MODE_KEY] == "plan"

    def test_accepts_plan_with_verification_heading(self, tmp_path: Path):
        session = _session({SESSION_MODE_KEY: "plan", SESSION_PRE_PLAN_KEY: "build"})
        tool = ExitPlanModeTool(sessions=_FakeSessions(session), workspace=tmp_path)
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))

        plan = "# Plan\n\n1. Edit file\n\n## Verification\n- pytest tests/ passes"
        out = asyncio.run(tool.execute(plan=plan))

        assert "Error" not in out
        assert session.metadata.get(_ACTIVE_PLAN_PATH_KEY) is not None

    def test_accepts_plan_with_per_step_verify_markers(self, tmp_path: Path):
        session = _session({SESSION_MODE_KEY: "plan", SESSION_PRE_PLAN_KEY: "build"})
        tool = ExitPlanModeTool(sessions=_FakeSessions(session), workspace=tmp_path)
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))

        plan = "# Plan\n\n1. Edit file\n   verify: app boots\n2. Ship"
        out = asyncio.run(tool.execute(plan=plan))

        assert "Error" not in out

    def test_verification_lint_is_case_insensitive(self, tmp_path: Path):
        session = _session({SESSION_MODE_KEY: "plan", SESSION_PRE_PLAN_KEY: "build"})
        tool = ExitPlanModeTool(sessions=_FakeSessions(session), workspace=tmp_path)
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key="s"))

        plan = "# Plan\n\n1. Step\n\n### VERIFICATION\n- check output"
        out = asyncio.run(tool.execute(plan=plan))

        assert "Error" not in out


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestPlanFileNaming:
    """Plan files are organized per session in their own subdirectory."""

    def test_session_slug_sanitizes_special_chars(self, tmp_path: Path):
        """Session keys with colons / slashes / spaces become filesystem-safe."""
        session = _session({SESSION_MODE_KEY: "plan"})
        tool = ExitPlanModeTool(sessions=_FakeSessions(session), workspace=tmp_path)
        tool.set_context(RequestContext(channel="ws", chat_id="x", session_key="websocket:chat-42 alpha"))

        asyncio.run(tool.execute(plan="content" + _VERIF))
        path = Path(session.metadata[_ACTIVE_PLAN_PATH_KEY])
        # Colons and spaces become underscores; dashes survive.
        assert path.parent.name == "websocket_chat-42_alpha"

    def test_concurrent_sessions_get_separate_dirs(self, tmp_path: Path):
        """Two sessions writing plans land in distinct subdirectories."""
        sess_a = _session({SESSION_MODE_KEY: "plan"})
        tool_a = ExitPlanModeTool(sessions=_FakeSessions(sess_a), workspace=tmp_path)
        tool_a.set_context(RequestContext(channel="cli", chat_id="a", session_key="cli_direct"))

        sess_b = _session({SESSION_MODE_KEY: "plan"})
        tool_b = ExitPlanModeTool(sessions=_FakeSessions(sess_b), workspace=tmp_path)
        tool_b.set_context(RequestContext(channel="tg", chat_id="b", session_key="telegram_999"))

        asyncio.run(tool_a.execute(plan="A plan" + _VERIF))
        asyncio.run(tool_b.execute(plan="B plan" + _VERIF))

        path_a = Path(sess_a.metadata[_ACTIVE_PLAN_PATH_KEY])
        path_b = Path(sess_b.metadata[_ACTIVE_PLAN_PATH_KEY])
        assert path_a.parent != path_b.parent
        assert path_a.parent.name == "cli_direct"
        assert path_b.parent.name == "telegram_999"

    def test_session_slug_helper_handles_edge_cases(self):
        """The slug helper has direct unit coverage for tricky inputs."""
        from durin.agent.tools.plan_mode import _session_slug

        assert _session_slug(None) == "default"
        assert _session_slug("") == "default"
        assert _session_slug("clean") == "clean"
        assert _session_slug("ws:chat42") == "ws_chat42"
        assert _session_slug("a/b/c") == "a_b_c"
        # Length cap (>80 chars truncated)
        long = "x" * 200
        assert len(_session_slug(long)) == 80


class TestPlanModeToolsRegistered:

    def test_both_tools_in_loader(self):
        from durin.agent.tools.loader import ToolLoader

        names = {t.__name__ for t in ToolLoader().discover()}
        assert "EnterPlanModeTool" in names
        assert "ExitPlanModeTool" in names
