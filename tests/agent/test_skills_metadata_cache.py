"""A skill's frontmatter is parsed once and then served from a cache until the
file changes.

Every system-prompt build reads each skill's frontmatter several times (the
platform filter, the catalog line, the always-on scan), and parsing the YAML
each time dominated the build. The cache holds each parse per resolved file,
valid while the file's (st_mtime_ns, st_size) match the stat taken before the
read. These tests pin what that must never change: after an edit that moves
the file's stat, a deletion, a new skill, or any write through the store, the
next build is byte for byte what an uncached parse would give.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
from collections import OrderedDict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

import durin.agent.skills as skills_mod
from durin.agent import skills_store as ss
from durin.agent.skills import SkillsLoader
from durin.agent.skills_frontmatter import join_frontmatter


def _write_skill(ws: Path, name: str, description: str) -> Path:
    """A SKILL.md in the form the store itself writes (provenance included, so
    the unverified-origin sweep keeps it live), so swapping a value for one of
    the same length through a store door keeps the file's size."""
    md = ws / "skills" / name / "SKILL.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    durin = {"mode": "auto", "provenance": {"source": "dream", "created_at": "2026-10-01"}}
    md.write_text(
        join_frontmatter(
            {"name": name, "description": description, "metadata": {"durin": durin}},
            f"# {name}\n\nBody.\n",
        ),
        encoding="utf-8",
    )
    return md


def _loader(tmp_path: Path) -> SkillsLoader:
    return SkillsLoader(tmp_path / "ws", builtin_skills_dir=tmp_path / "builtin")


def _uncached(self: SkillsLoader, name: str) -> dict | None:
    """The reference: parse whatever load_skill reads right now."""
    return skills_mod._parse_skill_frontmatter(self.load_skill(name))


@pytest.fixture
def parses(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Count YAML parses (only the loader parses in the tests that use it)."""
    count = {"n": 0}
    real = yaml.safe_load

    def counting(stream):
        count["n"] += 1
        return real(stream)

    monkeypatch.setattr(skills_mod.yaml, "safe_load", counting)
    return count


@pytest.fixture
def no_index_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """The store's memory-index fan-out is not under test; keep it off."""
    monkeypatch.setattr(ss, "_sync_index", lambda ws, name: None)
    monkeypatch.setattr(ss, "_unsync_index", lambda ws, name: None)


def test_a_second_build_parses_no_frontmatter(tmp_path: Path, parses: dict) -> None:
    for i in range(3):
        _write_skill(tmp_path / "ws", f"skill-{i}", f"does thing {i}")
    first = _loader(tmp_path).build_skills_summary()
    parsed = parses["n"]
    assert parsed > 0
    # A second loader shares the cache, as the gateway's subagents and tools do.
    assert _loader(tmp_path).build_skills_summary() == first
    assert parses["n"] == parsed


def test_an_edit_is_reflected_on_the_next_build(tmp_path: Path) -> None:
    md = _write_skill(tmp_path / "ws", "notes", "takes notes")
    loader = _loader(tmp_path)
    assert "takes notes" in loader.build_skills_summary()
    md.write_text(md.read_text().replace("takes notes", "takes careful notes"), encoding="utf-8")
    assert "takes careful notes" in loader.build_skills_summary()


def _save_content(ws: Path, md: Path) -> None:
    res = ss.save_skill_content(ws, "notes", md.read_text().replace("alpha", "gamma"))
    assert res.get("ok"), res


def _apply_edit(ws: Path, md: Path) -> None:
    res = ss.write_skill_edit(ws, "notes", old="alpha", new="gamma", rationale="sharpen")
    assert res.get("ok"), res


def _update_frontmatter(ws: Path, md: Path) -> None:
    ss._update_md(md, lambda data: data.update(description="takes notes gamma"))


@pytest.mark.parametrize(
    "door",
    [
        pytest.param(_save_content, id="save_skill_content"),
        pytest.param(_apply_edit, id="write_skill_edit"),
        pytest.param(_update_frontmatter, id="update_md"),
    ],
)
def test_a_same_size_rewrite_through_a_store_door_is_reflected(
    tmp_path: Path, no_index_sync, door
) -> None:
    """On a filesystem whose mtime granularity is coarser than the gap between
    two writes (a jiffy on ext4, a second or more on HFS+ or FAT), a rewrite
    that keeps the size keeps (mtime, size) too. The store's own writes drop
    the cached parse, so the next build still reads the new text."""
    ws = tmp_path / "ws"
    md = _write_skill(ws, "notes", "takes notes alpha")
    loader = _loader(tmp_path)
    assert loader.get_skill_metadata("notes")["description"] == "takes notes alpha"
    before = md.stat()
    door(ws, md)
    assert md.stat().st_size == before.st_size
    # What the coarse filesystem leaves behind: the old mtime on the new text.
    os.utime(md, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert loader.get_skill_metadata("notes")["description"] == "takes notes gamma"


def test_a_deleted_skill_is_reflected_on_the_next_build(tmp_path: Path) -> None:
    """Deleting the workspace copy of a skill brings back the builtin it
    shadowed; deleting that one too drops the skill."""
    builtin = _write_skill(tmp_path / "builtin-root", "notes", "builtin notes")
    shadow = _write_skill(tmp_path / "ws", "notes", "workspace notes")
    loader = SkillsLoader(tmp_path / "ws", builtin_skills_dir=builtin.parent.parent)
    assert "workspace notes" in loader.build_skills_summary()
    shutil.rmtree(shadow.parent)
    assert "builtin notes" in loader.build_skills_summary()
    shutil.rmtree(builtin.parent)
    assert loader.build_skills_summary() == ""
    assert loader.get_skill_metadata("notes") is None


def test_a_new_skill_is_reflected_on_the_next_build(tmp_path: Path) -> None:
    _write_skill(tmp_path / "ws", "notes", "takes notes")
    loader = _loader(tmp_path)
    assert "writes drafts" not in loader.build_skills_summary()
    _write_skill(tmp_path / "ws", "drafts", "writes drafts")
    assert "writes drafts" in loader.build_skills_summary()


def test_a_skill_whose_stat_fails_is_parsed_uncached(
    tmp_path: Path, parses: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    md = _write_skill(tmp_path / "ws", "notes", "takes notes")
    loader = _loader(tmp_path)
    expected = _uncached(loader, "notes")
    monkeypatch.setattr(skills_mod, "_file_stamp", lambda path: None)
    start = parses["n"]
    assert loader.get_skill_metadata("notes") == expected
    assert loader.get_skill_metadata("notes") == expected
    assert parses["n"] - start == 2  # parsed each time, never served from the cache
    assert str(md.resolve()) not in skills_mod._META_CACHE


def test_a_caller_cannot_change_what_the_next_caller_reads(tmp_path: Path) -> None:
    _write_skill(tmp_path / "ws", "notes", "takes notes")
    loader = _loader(tmp_path)
    loader.get_skill_metadata("notes")["metadata"]["durin"]["mode"] = "manual"  # a parse
    loader.get_skill_metadata("notes")["metadata"]["durin"]["mode"] = "manual"  # a cache hit
    assert loader.get_skill_metadata("notes") == _uncached(loader, "notes")


def test_concurrent_builds_are_safe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway builds prompts for concurrent turns on worker threads.
    Several threads build at once while a skill is rewritten, over a cache too
    small to hold every skill: no build fails, the cache stays within its
    bound, and the result is what an uncached parse gives."""
    monkeypatch.setattr(skills_mod, "_META_CACHE_MAX", 4)
    ws = tmp_path / "ws"
    for i in range(12):
        _write_skill(ws, f"skill-{i}", f"does thing {i}")
    loader = _loader(tmp_path)
    errors: list[BaseException] = []
    start = threading.Barrier(7)

    def build() -> None:
        start.wait()
        try:
            for _ in range(5):
                loader.build_skills_summary()
        except BaseException as exc:  # noqa: BLE001 — the test reports any failure
            errors.append(exc)

    def rewrite() -> None:
        start.wait()
        try:
            # Each take grows the file, so even a coarse mtime sees every one.
            for i in range(5):
                _write_skill(ws, "skill-0", "does thing 0" + "!" * i)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)  # switch threads often, to interleave the builds
    try:
        threads = [threading.Thread(target=build) for _ in range(6)]
        threads.append(threading.Thread(target=rewrite))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(interval)

    assert errors == []
    assert len(skills_mod._META_CACHE) <= 4
    cached = loader.build_skills_summary()
    monkeypatch.setattr(SkillsLoader, "get_skill_metadata", _uncached)
    assert cached == loader.build_skills_summary()


def test_another_lookup_cannot_evict_inside_an_insert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With room for one entry, a second lookup evicts the first one's key. Were
    it to run between the first lookup storing its parse and moving it to the
    newest end, the first lookup would fail on a key that is gone. The insert
    is held open here to give the second lookup that chance: it has to wait."""
    ws = tmp_path / "ws"
    first_md = _write_skill(ws, "first", "first skill")
    _write_skill(ws, "second", "second skill")
    loader = _loader(tmp_path)
    first_key = str(first_md.resolve())
    inserting, evicted = threading.Event(), threading.Event()

    class HeldOpen(OrderedDict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if key == first_key:
                inserting.set()
                evicted.wait(timeout=0.3)

        def popitem(self, last=True):
            item = super().popitem(last=last)
            if item[0] == first_key:
                evicted.set()
            return item

    monkeypatch.setattr(skills_mod, "_META_CACHE_MAX", 1)
    monkeypatch.setattr(skills_mod, "_META_CACHE", HeldOpen())
    errors: list[BaseException] = []

    def first_lookup() -> None:
        try:
            loader.get_skill_metadata("first")
        except BaseException as exc:  # noqa: BLE001 — the test reports any failure
            errors.append(exc)

    t = threading.Thread(target=first_lookup)
    t.start()
    assert inserting.wait(timeout=5)
    assert loader.get_skill_metadata("second")["description"] == "second skill"
    t.join(timeout=5)
    assert errors == []
    assert list(skills_mod._META_CACHE) == [str((ws / "skills" / "second" / "SKILL.md").resolve())]


def test_a_parse_in_flight_across_a_store_write_does_not_cache_the_old_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_index_sync
) -> None:
    """A build reads a skill just before the store rewrites it (same size, and
    on a coarse filesystem the same mtime). The build returns what it read;
    since the store dropped the entry after that read, the old text is not
    stored, and the next build parses the new one."""
    ws = tmp_path / "ws"
    md = _write_skill(ws, "notes", "takes notes alpha")
    loader = _loader(tmp_path)
    before = md.stat()
    read, written = threading.Event(), threading.Event()
    real_parse = skills_mod._parse_skill_frontmatter
    seen: dict = {}

    def parse_then_wait(content):
        meta = real_parse(content)
        if threading.current_thread() is reader:
            read.set()
            written.wait(timeout=5)
        return meta

    monkeypatch.setattr(skills_mod, "_parse_skill_frontmatter", parse_then_wait)
    reader = threading.Thread(target=lambda: seen.update(meta=loader.get_skill_metadata("notes")))
    reader.start()
    assert read.wait(timeout=5)
    _update_frontmatter(ws, md)
    os.utime(md, ns=(before.st_atime_ns, before.st_mtime_ns))
    written.set()
    reader.join(timeout=5)
    assert seen["meta"]["description"] == "takes notes alpha"
    assert loader.get_skill_metadata("notes")["description"] == "takes notes gamma"


async def test_the_system_prompt_is_byte_identical_with_and_without_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_index_sync
) -> None:
    """Over a multi-turn session whose skills change between turns (a same-size
    rewrite through the store, a new skill, a deletion, a direct edit), every
    system prompt the agent sends is the one an uncached parse would build."""
    from durin.agent.loop import AgentLoop
    from durin.bus.events import InboundMessage
    from durin.bus.queue import MessageBus
    from durin.providers.base import LLMResponse

    ws = tmp_path / "ws"

    def seed() -> None:
        if ws.exists():
            shutil.rmtree(ws)
        ws.mkdir()
        _write_skill(ws, "notes", "takes notes alpha")

    def coarse_same_size_rewrite() -> None:
        md = ws / "skills" / "notes" / "SKILL.md"
        before = md.stat()
        _save_content(ws, md)
        os.utime(md, ns=(before.st_atime_ns, before.st_mtime_ns))

    def add_skill() -> None:
        _write_skill(ws, "drafts", "writes drafts")

    def delete_skill() -> None:
        shutil.rmtree(ws / "skills" / "notes")

    def edit_skill() -> None:
        md = ws / "skills" / "drafts" / "SKILL.md"
        md.write_text(md.read_text().replace("writes drafts", "writes short drafts"),
                      encoding="utf-8")

    between_turns = [coarse_same_size_rewrite, add_skill, delete_skill, edit_skill]

    async def session() -> list[str]:
        seed()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        loop = AgentLoop(bus=MessageBus(), provider=provider, workspace=ws, model="test-model")
        loop.tools.get_definitions = MagicMock(return_value=[])
        prompts: list[str] = []

        async def chat(*args, **kwargs):
            system = kwargs["messages"][0]
            assert system["role"] == "system"
            prompts.append(system["content"])
            return LLMResponse(content="ok", tool_calls=[])

        loop.provider.chat_with_retry = AsyncMock(side_effect=chat)
        for change in [None, *between_turns]:
            if change is not None:
                change()
            await loop._process_message(
                InboundMessage(channel="websocket", sender_id="u", chat_id="c", content="hello")
            )
        return prompts

    cached = await session()
    with monkeypatch.context() as m:
        m.setattr(SkillsLoader, "get_skill_metadata", _uncached)
        uncached = await session()

    assert len(cached) == len(between_turns) + 1
    assert cached == uncached
    assert "takes notes gamma" in cached[1] and "takes notes alpha" not in cached[1]
    assert "writes short drafts" in cached[-1]
