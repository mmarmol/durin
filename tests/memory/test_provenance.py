"""Tests for the memory provenance ContextVar.

Contract per `durin/memory/provenance.py`: NO implicit default.
Every memory write must wrap in :func:`author_scope`. The
"raises outside scope" assertions live in
``test_provenance_no_default.py`` (which opts out of the conftest's
test-default scope). The tests here cover the scope-management
semantics (set, nest, propagate across await) using *explicit*
scopes throughout.
"""

from __future__ import annotations

import asyncio

import pytest

from durin.memory.provenance import author_scope, current_author


def test_scope_sets_author() -> None:
    with author_scope("agent_created"):
        assert current_author() == "agent_created"


def test_nested_scopes() -> None:
    """Inner scope overrides outer; outer is restored on exit."""
    with author_scope("agent_created"):
        assert current_author() == "agent_created"
        with author_scope("user_authored"):
            assert current_author() == "user_authored"
        assert current_author() == "agent_created"


@pytest.mark.asyncio
async def test_propagates_across_await() -> None:
    with author_scope("agent_created"):
        assert current_author() == "agent_created"
        await asyncio.sleep(0)
        assert current_author() == "agent_created"


@pytest.mark.asyncio
async def test_propagates_to_create_task() -> None:
    captured: list[str] = []

    async def child() -> None:
        captured.append(current_author())

    with author_scope("agent_created"):
        task = asyncio.create_task(child())
        await task

    assert captured == ["agent_created"]


@pytest.mark.asyncio
async def test_isolated_between_concurrent_tasks() -> None:
    """Verify ContextVar provides per-task isolation under interleaved execution."""
    results_a: list[str] = []
    results_b: list[str] = []
    entered_a = asyncio.Event()
    release_a = asyncio.Event()

    async def writer_a() -> None:
        with author_scope("agent_created"):
            entered_a.set()
            await release_a.wait()
            results_a.append(current_author())

    async def writer_b() -> None:
        await entered_a.wait()
        with author_scope("user_authored"):
            results_b.append(current_author())
        release_a.set()

    await asyncio.gather(writer_a(), writer_b())

    assert results_a == ["agent_created"]
    assert results_b == ["user_authored"]
