"""Tests for the per-session meta file (Phase 2 memory foundation).

Verifies the lifecycle: append events, update by id, find by filter,
markdown title extraction, and end-to-end plan lifecycle (pending →
executing → superseded).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from durin.session.session_meta import (
    append_event,
    extract_markdown_title,
    find_event,
    find_executing_plan,
    make_plan_event,
    mark_plan_approved,
    mark_plan_cancelled,
    mark_plan_superseded,
    meta_path_for,
    read_meta,
    update_event,
)


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


class TestMetaPath:

    def test_basic_key(self, tmp_path: Path):
        p = meta_path_for("cli:direct", tmp_path)
        assert p.parent == tmp_path
        assert p.name == "cli_direct.meta.json"

    def test_special_chars_sanitized(self, tmp_path: Path):
        p = meta_path_for("websocket:chat 42/sub", tmp_path)
        assert "chat" in p.name and "42" in p.name
        # No colon, no slash, no space in the filename
        assert ":" not in p.name and "/" not in p.name and " " not in p.name

    def test_empty_key_uses_default(self, tmp_path: Path):
        p = meta_path_for("", tmp_path)
        assert p.name == "default.meta.json"


# ---------------------------------------------------------------------------
# read_meta + atomicity
# ---------------------------------------------------------------------------


class TestReadMeta:

    def test_missing_file_returns_empty_skeleton(self, tmp_path: Path):
        """Skeleton now includes a ``derived`` block (added when
        ``_last_summary`` moved out of session.jsonl into the sidecar)."""
        p = tmp_path / "nope.meta.json"
        data = read_meta(p)
        assert data == {"session_key": None, "events": [], "derived": {}}

    def test_corrupt_file_returns_empty_skeleton(self, tmp_path: Path):
        p = tmp_path / "bad.meta.json"
        p.write_text("not valid json")
        data = read_meta(p)
        assert data == {"session_key": None, "events": [], "derived": {}}

    def test_missing_events_key_normalized(self, tmp_path: Path):
        p = tmp_path / "x.meta.json"
        p.write_text(json.dumps({"session_key": "k"}))
        data = read_meta(p)
        assert data["events"] == []


# ---------------------------------------------------------------------------
# append_event + update_event
# ---------------------------------------------------------------------------


class TestEventLifecycle:

    def test_append_single_event(self, tmp_path: Path):
        p = tmp_path / "s.meta.json"
        append_event(p, "cli:test", {"type": "plan", "id": "plan_1", "title": "Refactor X"})
        data = read_meta(p)
        assert data["session_key"] == "cli:test"
        assert len(data["events"]) == 1
        assert data["events"][0]["id"] == "plan_1"
        # recorded_at auto-added
        assert "recorded_at" in data["events"][0]

    def test_append_multiple_events(self, tmp_path: Path):
        p = tmp_path / "s.meta.json"
        append_event(p, "cli:test", {"type": "plan", "id": "a", "title": "A"})
        append_event(p, "cli:test", {"type": "plan", "id": "b", "title": "B"})
        data = read_meta(p)
        assert [e["id"] for e in data["events"]] == ["a", "b"]

    def test_update_event_by_id(self, tmp_path: Path):
        p = tmp_path / "s.meta.json"
        append_event(p, "cli:test", {"type": "plan", "id": "x", "outcome": "pending"})
        assert update_event(p, "x", {"outcome": "executing"})
        events = read_meta(p)["events"]
        assert events[0]["outcome"] == "executing"

    def test_update_event_unknown_id_returns_false(self, tmp_path: Path):
        p = tmp_path / "s.meta.json"
        append_event(p, "cli:test", {"type": "plan", "id": "x"})
        assert update_event(p, "y", {"outcome": "executing"}) is False

    def test_update_event_merges_nested_dict(self, tmp_path: Path):
        p = tmp_path / "s.meta.json"
        append_event(p, "cli:test", {
            "type": "plan",
            "id": "x",
            "msg_index": {"approved": None, "closed": None},
        })
        update_event(p, "x", {"msg_index": {"approved": 100}})
        events = read_meta(p)["events"]
        # `approved` was set; `closed` remained intact (merged, not overwritten)
        assert events[0]["msg_index"] == {"approved": 100, "closed": None}


# ---------------------------------------------------------------------------
# find_event filters
# ---------------------------------------------------------------------------


class TestFindEvent:

    def test_filter_by_type(self, tmp_path: Path):
        p = tmp_path / "s.meta.json"
        append_event(p, "k", {"type": "plan", "id": "p1"})
        append_event(p, "k", {"type": "review", "id": "r1"})
        assert len(find_event(p, type="plan")) == 1
        assert len(find_event(p, type="review")) == 1

    def test_filter_by_outcome(self, tmp_path: Path):
        p = tmp_path / "s.meta.json"
        append_event(p, "k", {"type": "plan", "id": "p1", "outcome": "executing"})
        append_event(p, "k", {"type": "plan", "id": "p2", "outcome": "superseded"})
        assert find_event(p, outcome="executing")[0]["id"] == "p1"
        assert find_event(p, outcome="superseded")[0]["id"] == "p2"

    def test_find_executing_plan_returns_latest(self, tmp_path: Path):
        p = tmp_path / "s.meta.json"
        # Two executing — find_executing_plan returns latest (a real
        # session should never have two; this is defensive)
        append_event(p, "k", {"type": "plan", "id": "p1", "outcome": "executing"})
        append_event(p, "k", {"type": "plan", "id": "p2", "outcome": "executing"})
        assert find_executing_plan(p)["id"] == "p2"

    def test_find_executing_plan_none_returns_none(self, tmp_path: Path):
        p = tmp_path / "s.meta.json"
        append_event(p, "k", {"type": "plan", "id": "p1", "outcome": "pending"})
        assert find_executing_plan(p) is None


# ---------------------------------------------------------------------------
# Plan-specific helpers
# ---------------------------------------------------------------------------


class TestPlanHelpers:

    def test_make_plan_event_shape(self):
        evt = make_plan_event(
            plan_id="plan_001",
            plan_path=".durin/plans/x/plan_001.md",
            title="Refactor auth",
        )
        assert evt["type"] == "plan"
        assert evt["id"] == "plan_001"
        assert evt["outcome"] == "pending"
        assert evt["msg_index"] == {"approved": None, "closed": None}
        assert evt["approved_at"] is None and evt["closed_at"] is None

    def test_plan_lifecycle_pending_to_executing(self, tmp_path: Path):
        p = tmp_path / "s.meta.json"
        evt = make_plan_event(plan_id="p1", plan_path="x", title="t")
        append_event(p, "k", evt)
        assert mark_plan_approved(p, "p1", msg_index=240)
        result = find_executing_plan(p)
        assert result is not None
        assert result["outcome"] == "executing"
        assert result["msg_index"]["approved"] == 240
        assert result["approved_at"] is not None

    def test_plan_lifecycle_executing_to_superseded(self, tmp_path: Path):
        p = tmp_path / "s.meta.json"
        append_event(p, "k", make_plan_event(plan_id="p1", plan_path="x", title="t"))
        mark_plan_approved(p, "p1", msg_index=240)
        mark_plan_superseded(p, "p1", msg_index=300)
        result = find_event(p, event_id="p1")[0]
        assert result["outcome"] == "superseded"
        assert result["msg_index"]["closed"] == 300
        # approved remains
        assert result["msg_index"]["approved"] == 240
        # find_executing_plan no longer returns it
        assert find_executing_plan(p) is None

    def test_plan_cancelled(self, tmp_path: Path):
        p = tmp_path / "s.meta.json"
        append_event(p, "k", make_plan_event(plan_id="p1", plan_path="x", title="t"))
        mark_plan_cancelled(p, "p1")
        result = find_event(p, event_id="p1")[0]
        assert result["outcome"] == "cancelled"
        # No approved_at was set
        assert result["approved_at"] is None


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------


class TestExtractTitle:

    def test_h1_heading(self):
        text = "# Refactor authentication\n\nDetails here..."
        assert extract_markdown_title(text) == "Refactor authentication"

    def test_h2_heading_also_works(self):
        text = "## Sub-plan\n\nContent..."
        assert extract_markdown_title(text) == "Sub-plan"

    def test_no_heading_uses_first_line(self):
        text = "Just plain text starting here.\n\nMore..."
        assert extract_markdown_title(text) == "Just plain text starting here."

    def test_first_line_truncated_at_fallback_len(self):
        text = "x" * 200
        result = extract_markdown_title(text, fallback_len=80)
        assert len(result) == 80

    def test_empty_text(self):
        assert extract_markdown_title("") == ""
        assert extract_markdown_title("\n\n\n") == ""


# ---------------------------------------------------------------------------
# End-to-end integration with plan tools and slash commands
# ---------------------------------------------------------------------------


class _FakeSessions:
    def __init__(self, session, sessions_dir: Path):
        self._session = session
        self.sessions_dir = sessions_dir

    def get_or_create(self, key: str):
        return self._session


class TestPlanToolIntegration:
    """The exit_plan_mode tool writes both the .md and an event in session meta."""

    def test_exit_plan_mode_writes_meta_event(self, tmp_path: Path):
        from durin.agent.agent_mode import (
            PLAN_MODE,
            SESSION_MODE_KEY,
            SESSION_PRE_PLAN_KEY,
        )
        from durin.agent.tools.context import RequestContext
        from durin.agent.tools.plan_mode import ExitPlanModeTool

        session_key = "cli:test"
        session = SimpleNamespace(
            metadata={SESSION_MODE_KEY: "plan", SESSION_PRE_PLAN_KEY: "build"},
            messages=[],
            key=session_key,
        )
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        tool = ExitPlanModeTool(
            sessions=_FakeSessions(session, sessions_dir),
            workspace=tmp_path,
        )
        tool.set_context(RequestContext(channel="cli", chat_id="c", session_key=session_key))

        plan_text = "# Refactor authentication module\n\n1. Read code\n2. Apply OAuth"
        asyncio.run(tool.execute(plan=plan_text))

        # Meta file exists with a pending plan event
        mp = meta_path_for(session_key, sessions_dir)
        assert mp.exists()
        data = read_meta(mp)
        assert data["session_key"] == session_key
        assert len(data["events"]) == 1
        evt = data["events"][0]
        assert evt["type"] == "plan"
        assert evt["outcome"] == "pending"
        assert evt["title"] == "Refactor authentication module"
        assert evt["plan_path"]


class TestSlashCommandIntegration:
    """/build and /plan update the meta file as plans transition."""

    @pytest.mark.asyncio
    async def test_build_marks_plan_approved(self, tmp_path: Path):
        from durin.agent.agent_mode import (
            SESSION_MODE_KEY,
            SESSION_PRE_PLAN_KEY,
        )
        from durin.agent.tools.plan_mode import _ACTIVE_PLAN_PATH_KEY
        from durin.bus.events import InboundMessage
        from durin.command.builtin import cmd_build
        from durin.command.router import CommandContext

        session_key = "cli:test"
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        # Pre-seed the meta with a pending plan
        mp = meta_path_for(session_key, sessions_dir)
        append_event(mp, session_key,
                     make_plan_event(plan_id="plan_xyz",
                                     plan_path=".durin/plans/.../plan_xyz.md",
                                     title="t"))

        # Session in plan mode with the active_plan_path matching plan_xyz
        session = SimpleNamespace(
            metadata={
                SESSION_MODE_KEY: "plan",
                SESSION_PRE_PLAN_KEY: "build",
                _ACTIVE_PLAN_PATH_KEY: ".durin/plans/.../plan_xyz.md",
            },
            messages=[{"role": "user", "content": "x"}] * 50,
            key=session_key,
        )
        loop = SimpleNamespace(sessions=SimpleNamespace(sessions_dir=sessions_dir))
        msg = InboundMessage(channel="cli", sender_id="u", chat_id="d", content="/build")
        ctx = CommandContext(msg=msg, session=session, key=session_key, raw="/build", loop=loop)

        await cmd_build(ctx)

        # Meta event for plan_xyz transitioned to executing
        result = find_event(mp, event_id="plan_xyz")[0]
        assert result["outcome"] == "executing"
        assert result["msg_index"]["approved"] == 50

    @pytest.mark.asyncio
    async def test_plan_supersedes_executing_plan(self, tmp_path: Path):
        from durin.agent.agent_mode import SESSION_MODE_KEY
        from durin.bus.events import InboundMessage
        from durin.command.builtin import cmd_plan
        from durin.command.router import CommandContext

        session_key = "cli:test"
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        # Pre-seed: an executing plan
        mp = meta_path_for(session_key, sessions_dir)
        append_event(mp, session_key, make_plan_event(
            plan_id="plan_old", plan_path=".durin/plans/.../plan_old.md", title="old",
        ))
        mark_plan_approved(mp, "plan_old", msg_index=100)

        # Session in build mode with executing_plan_path set
        session = SimpleNamespace(
            metadata={
                SESSION_MODE_KEY: "build",
                "executing_plan_path": ".durin/plans/.../plan_old.md",
            },
            messages=[{"role": "user", "content": "x"}] * 150,
            key=session_key,
        )
        loop = SimpleNamespace(sessions=SimpleNamespace(sessions_dir=sessions_dir))
        msg = InboundMessage(channel="cli", sender_id="u", chat_id="d", content="/plan")
        ctx = CommandContext(msg=msg, session=session, key=session_key, raw="/plan", loop=loop)

        await cmd_plan(ctx)

        # plan_old now superseded
        result = find_event(mp, event_id="plan_old")[0]
        assert result["outcome"] == "superseded"
        assert result["msg_index"]["closed"] == 150
        assert result["msg_index"]["approved"] == 100  # preserved
