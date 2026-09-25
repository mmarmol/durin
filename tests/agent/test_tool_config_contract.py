"""The tool-context config contract — guards the class of bug that shipped as the
quarantine `approve → HTTP 500`.

ROOT CAUSE of that 500: code OUTSIDE the agent loop (``_get_exec_run``) built a
tool context whose ``.config`` was the top-level ``Config`` instead of the
``ToolsConfig`` (``config.tools``). ``ExecTool.create(ctx)`` reads
``ctx.config.exec`` (a ``ToolsConfig`` field) → ``AttributeError`` → uncaught →
500. Nothing exercised the contract, so it shipped broken.

These tests pin the contract: a tool context's ``.config`` is a ``ToolsConfig``,
and the non-loop helper that fakes one (``_get_exec_run``) honours it.
"""
from __future__ import annotations

import pytest

from durin.agent.tools.context import ToolContext
from durin.agent.tools.shell import ExecTool
from durin.config.schema import Config


def test_exec_tool_create_with_tools_config(tmp_path) -> None:
    """The contract: ExecTool reads ctx.config.exec / .restrict_to_workspace /
    .process — all ToolsConfig fields. A ToolsConfig-shaped ctx must work."""
    ctx = ToolContext(config=Config().tools, workspace=str(tmp_path))
    assert ExecTool.enabled(ctx) in (True, False)  # reads ctx.config.exec.enable
    assert ExecTool.create(ctx) is not None  # reads .exec / .restrict_to_workspace / .process


def test_exec_tool_rejects_top_level_config(tmp_path) -> None:
    """The violated contract: handing ExecTool the top-level Config (no ``.exec``)
    is exactly the bug that 500'd. Lock it in so a regression fails loudly here
    instead of as a runtime 500."""
    ctx = ToolContext(config=Config(), workspace=str(tmp_path))
    with pytest.raises(AttributeError):
        ExecTool.create(ctx)


def test_get_exec_run_returns_callable(tmp_path) -> None:
    """The non-loop helper that builds a fake tool ctx must feed ExecTool the
    tools sub-config and return a working exec callable (the exact bug site)."""
    from durin.agent.skills_store import _get_exec_run

    run = _get_exec_run(tmp_path)
    assert callable(run)


@pytest.mark.asyncio
async def test_get_exec_run_is_the_non_asking_runner(tmp_path) -> None:
    """The web skill's approve path runs a dep-install step here with no chat
    turn behind it. A step whose command hits the deny list must fail with the
    refusal text, never ask — asking would open a second, nested approval in
    the middle of the one already being carried out.

    Asserts the wiring directly (``__func__ is ExecTool._run``): a
    context-less ``ExecTool`` never asks either way, so a behavioral-only
    check here would pass just as well with ``.execute`` wired in — it would
    not have caught a regression back to the asking entry point."""
    from durin.agent import approval_store
    from durin.agent.skills_store import _get_exec_run

    run = _get_exec_run(tmp_path)

    assert run.__func__ is ExecTool._run

    out = await run(command=f"rm -rf {tmp_path}/whatever")

    assert "blocked by deny pattern" in out
    assert approval_store.list_records(tmp_path, include_legacy=False) == []
