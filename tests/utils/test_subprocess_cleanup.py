"""Tests for the subprocess-transport cleanup helper."""

from __future__ import annotations

import asyncio

import pytest

from durin.utils.subprocess_cleanup import aclose_subprocess


@pytest.mark.asyncio
async def test_none_is_noop():
    await aclose_subprocess(None)  # must not raise


@pytest.mark.asyncio
async def test_object_without_transport_is_noop():
    class _Fake:
        pass

    await aclose_subprocess(_Fake())  # no _transport attr → no-op


@pytest.mark.asyncio
async def test_closes_real_subprocess_transport():
    import sys

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    transport = proc._transport
    await aclose_subprocess(proc)
    assert transport.is_closing() or getattr(transport, "_closed", True)
