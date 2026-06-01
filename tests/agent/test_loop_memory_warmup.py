"""Resilience of `AgentLoop._warmup_memory_embedding`.

Vector memory is ON by default. The startup warmup must degrade gracefully
when the optional `[memory]` extra (fastembed + lancedb) is absent — warn,
fall back to grep-level recall, and crucially NOT attempt to load/download a
model (there's no fastembed to do it). It must NOT pip-install at runtime.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

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
        "durin.memory.embedding.FastembedProvider.warmup"
    ) as warmup:
        await AgentLoop._warmup_memory_embedding(_fake_self(enabled=True))
    warmup.assert_not_called()


@pytest.mark.asyncio
async def test_warmup_runs_when_extra_present():
    """enabled=True and the extra is available → warm (download-if-missing)
    the model so first use doesn't pay the cost."""
    with patch(
        "durin.memory.vector_index.vector_index_available", return_value=True
    ), patch(
        "durin.memory.embedding.FastembedProvider.warmup", return_value=1.0
    ) as warmup:
        await AgentLoop._warmup_memory_embedding(_fake_self(enabled=True))
    warmup.assert_called_once()


@pytest.mark.asyncio
async def test_warmup_noop_when_memory_disabled():
    with patch(
        "durin.memory.embedding.FastembedProvider.warmup"
    ) as warmup:
        await AgentLoop._warmup_memory_embedding(_fake_self(enabled=False))
    warmup.assert_not_called()
