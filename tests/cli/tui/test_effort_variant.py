"""The TUI's effort picker (Ctrl+Shift+L) builds the same preset variant the
/effort command does: the active preset with another reasoning effort, every
other setting kept."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from durin.bus.queue import MessageBus
from durin.cli.tui.app import DurinApp
from durin.config.schema import ModelPresetConfig


@pytest.mark.asyncio
async def test_the_effort_picker_keeps_every_setting_of_the_preset(tmp_path) -> None:
    async def _idle_run() -> None:
        await asyncio.Event().wait()

    base = ModelPresetConfig(
        model="gpt-4.1",
        provider="openai",
        temperature=0.3,
        request_timeout_s=42.0,
        top_p=0.9,
        top_k=40,
        repeat_penalty=1.1,
        preemptive_compact_ratio=0.6,
        preemptive_compact_max_tokens=300_000,
    )
    fake_loop = SimpleNamespace(
        bus=MessageBus(),
        workspace=str(tmp_path),
        model="gpt-4.1",
        model_preset="fast",
        model_presets={"fast": base},
        context_window_tokens=200_000,
        sessions=SimpleNamespace(get_or_create=lambda key: SimpleNamespace(messages=[], metadata={})),
        run=_idle_run,
    )
    published: list[str] = []

    app = DurinApp(agent_loop=fake_loop)
    async with app.run_test() as pilot:
        async def _pick_high(_screen):
            return "high"

        async def _record(text, _media, **_kwargs):
            published.append(text)

        app.push_screen_wait = _pick_high  # type: ignore[method-assign]
        app._publish_inbound = _record  # type: ignore[method-assign]
        await app._open_variant_picker().wait()
        await pilot.pause()

    variant = fake_loop.model_presets["fast:high"]
    assert variant.model_dump() == {**base.model_dump(), "reasoning_effort": "high"}
    assert published == ["/model fast:high"]
