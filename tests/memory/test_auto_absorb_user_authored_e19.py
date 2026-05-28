"""E19 (audit second pass, 2026-05-28): `_maybe_auto_absorb` must
skip entity-page pairs where EITHER page carries `author:
user_authored`. Pre-E19 this protection only existed for episodic
entries (`cli/memory_cmd.py:150`) — entity pages were unprotected
despite the doc 01 §4.6.1 promise that Dream and the curator never
auto-modify user-authored content.

The risk: a user opens two entity pages by hand whose aliases
happen to overlap. Without this check, auto-absorb would judge
them and may merge the user's deliberate distinction away.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from durin.memory.dream_runner import DreamRunner
from durin.memory.entity_page import EntityPage


def _write_page(
    tmp_path: Path,
    type_: str,
    slug: str,
    *,
    aliases: list[str],
    author: str = "agent_created",
) -> Path:
    page = EntityPage(
        type=type_,
        name=slug.title(),
        aliases=aliases,
        body=f"{slug} body",
        author=author,
    )
    path = tmp_path / "memory" / "entities" / type_ / f"{slug}.md"
    page.save(path)
    return path


def _build_runner(tmp_path: Path) -> DreamRunner:
    return DreamRunner(
        workspace=tmp_path,
        model="glm-5.1",
        auto_absorb_enabled=True,
    )


def _capture_events(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "durin.memory.dream_runner.emit_tool_event",
        lambda t, d: events.append((t, d)),
    )
    return events


def test_auto_absorb_skips_pair_when_page_a_is_user_authored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the canonical (first) page carries `author:
    user_authored`, the pair must be skipped without invoking the
    judge — the user authored the page deliberately."""
    _write_page(
        tmp_path, "person", "marcelo",
        aliases=["m"], author="user_authored",
    )
    _write_page(
        tmp_path, "person", "marcelo-2",
        aliases=["m"], author="agent_created",
    )

    runner = _build_runner(tmp_path)
    events = _capture_events(monkeypatch)

    # Patch the judge so a leaking call would be obvious.
    judge_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "durin.memory.absorb_judge.judge_pair",
        lambda **kw: judge_calls.append(
            (kw["canonical"].name, kw["absorbed"].name),
        ),
    )

    runner._maybe_auto_absorb()

    # No judge call landed on this pair.
    assert judge_calls == []
    # An absorb.skipped event fires with reason=user_authored.
    skipped = [
        p for t, p in events if t == "memory.absorb.skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0].get("reason") == "user_authored"


def test_auto_absorb_skips_pair_when_page_b_is_user_authored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Symmetric: the absorbed (second) page being user_authored
    also blocks the pair. find_candidates may return either
    ordering depending on slug sort."""
    _write_page(
        tmp_path, "person", "marcelo",
        aliases=["m"], author="agent_created",
    )
    _write_page(
        tmp_path, "person", "marcelo-2",
        aliases=["m"], author="user_authored",
    )

    runner = _build_runner(tmp_path)
    events = _capture_events(monkeypatch)

    judge_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "durin.memory.absorb_judge.judge_pair",
        lambda **kw: judge_calls.append(
            (kw["canonical"].name, kw["absorbed"].name),
        ),
    )

    runner._maybe_auto_absorb()

    assert judge_calls == []
    skipped = [
        p for t, p in events if t == "memory.absorb.skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0].get("reason") == "user_authored"


def test_auto_absorb_proceeds_when_both_pages_agent_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity: the user_authored check must NOT block the normal
    path. When both pages are agent-created, the judge gets invoked
    (and may proceed to merge or skip based on its own logic)."""
    _write_page(
        tmp_path, "person", "marcelo",
        aliases=["m"], author="agent_created",
    )
    _write_page(
        tmp_path, "person", "marcelo-2",
        aliases=["m"], author="agent_created",
    )

    runner = _build_runner(tmp_path)
    # The quarantine check applies first — bypass it by overriding
    # the freshly-written mtime check (both files just landed, so
    # they're in the 24h quarantine window).
    monkeypatch.setattr(
        runner, "_is_quarantined", lambda *_a, **_kw: False,
    )

    judge_calls: list[Any] = []

    class _JudgeStub:
        verdict = "different"
        confidence = 50
        reasoning = "stub"

    def fake_judge(**kw):
        judge_calls.append(kw)
        return _JudgeStub()

    monkeypatch.setattr(
        "durin.memory.absorb_judge.judge_pair", fake_judge,
    )

    runner._maybe_auto_absorb()

    # Judge was reached (the user_authored gate didn't fire).
    assert len(judge_calls) == 1
