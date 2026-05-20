"""Tests for the memory provenance ContextVar."""

from __future__ import annotations

import asyncio

import pytest

from durin.memory.provenance import author_scope, current_author


def test_default_is_user_authored() -> None:
    assert current_author() == "user_authored"


def test_scope_sets_author() -> None:
    with author_scope("agent_created"):
        assert current_author() == "agent_created"


def test_scope_resets_on_exit() -> None:
    with author_scope("agent_created"):
        pass
    assert current_author() == "user_authored"


def test_nested_scopes() -> None:
    with author_scope("agent_created"):
        assert current_author() == "agent_created"
        with author_scope("user_authored"):
            assert current_author() == "user_authored"
        assert current_author() == "agent_created"
    assert current_author() == "user_authored"


def test_scope_resets_on_exception() -> None:
    with pytest.raises(RuntimeError):
        with author_scope("agent_created"):
            raise RuntimeError("boom")
    assert current_author() == "user_authored"


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
