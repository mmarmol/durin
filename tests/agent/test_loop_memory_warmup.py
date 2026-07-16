"""Resilience of `AgentLoop._warmup_memory_embedding`.

Vector memory is ON by default. The startup warmup must degrade gracefully
when the optional `[memory]` extra (fastembed + lancedb) is absent — warn,
fall back to grep-level recall, and crucially NOT attempt to load/download a
model (there's no fastembed to do it). It must NOT pip-install at runtime.

Warmup is built via `provider_from_config` (not the `FastembedProvider.warmup`
classmethod) so it picks up the configured isolation mode. Under
isolation="process" the model must load in the disposable worker child, not
the long-lived gateway parent — see
`test_warmup_process_mode_keeps_model_out_of_parent` below.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from durin.agent.loop import AgentLoop


def _fake_self(*, enabled: bool):
    return SimpleNamespace(
        app_config=SimpleNamespace(
            memory=SimpleNamespace(
                enabled=enabled,
                embedding=SimpleNamespace(model="intfloat/multilingual-e5-small"),
            )
        )
    )


@pytest.mark.asyncio
async def test_warmup_skips_when_extra_missing():
    """enabled=True but the [memory] extra is absent → no warmup attempt
    (degrade to grep), no runtime install."""
    with patch(
        "durin.memory.vector_index.vector_index_available", return_value=False
    ), patch(
        "durin.memory.embedding.provider_from_config"
    ) as provider_from_config:
        await AgentLoop._warmup_memory_embedding(_fake_self(enabled=True))
    provider_from_config.assert_not_called()


@pytest.mark.asyncio
async def test_warmup_runs_when_extra_present():
    """enabled=True and the extra is available → build the provider from
    config (picking up the configured isolation) and warm it with a short
    embed call rather than the isolation-blind `warmup` classmethod."""
    fake_provider = Mock()
    with patch(
        "durin.memory.vector_index.vector_index_available", return_value=True
    ), patch(
        "durin.memory.embedding.provider_from_config", return_value=fake_provider
    ) as provider_from_config:
        await AgentLoop._warmup_memory_embedding(_fake_self(enabled=True))
    provider_from_config.assert_called_once()
    fake_provider.embed.assert_called_once_with(["warmup"])


@pytest.mark.asyncio
async def test_warmup_noop_when_memory_disabled():
    with patch(
        "durin.memory.embedding.provider_from_config"
    ) as provider_from_config:
        await AgentLoop._warmup_memory_embedding(_fake_self(enabled=False))
    provider_from_config.assert_not_called()


@pytest.mark.asyncio
async def test_warmup_process_mode_keeps_model_out_of_parent():
    """isolation="process" → the boot warmup embed() call must be served
    by the worker pool, never by loading the ONNX model in the parent.

    Regression pin for the containment bug: `FastembedProvider.warmup`
    always constructed with isolation="inline" and loaded in the caller,
    which silently defeated process isolation on every gateway boot.
    """
    from tests.memory.test_embedding import _inject_fake_fastembed

    from durin.memory.embedding import FastembedProvider

    class _StubPool:
        """Stands in for the ProcessPoolExecutor — submit().result()
        succeeds without spawning a real worker, so this exercises the
        containment contract without needing a real fastembed model."""

        def submit(self, fn, *args):
            return SimpleNamespace(result=lambda: [[0.0, 1.0]])

    with _inject_fake_fastembed():
        provider = FastembedProvider(isolation="process")
    provider._pool = _StubPool()

    with patch(
        "durin.memory.vector_index.vector_index_available", return_value=True
    ), patch(
        "durin.memory.embedding.provider_from_config", return_value=provider
    ):
        await AgentLoop._warmup_memory_embedding(_fake_self(enabled=True))

    assert provider._model is None
    assert provider._isolation == "process"
