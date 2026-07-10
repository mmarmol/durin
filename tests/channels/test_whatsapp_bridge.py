"""Tests for WhatsApp bridge binary resolution and process supervision."""

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from durin.channels.whatsapp_bridge import (
    BridgeSetupError,
    BridgeSupervisor,
    cached_binary_path,
    ensure_bridge_binary,
    platform_asset,
)


class TestPlatformAsset:
    def test_known_platforms(self):
        with patch("durin.channels.whatsapp_bridge.sys") as m_sys, \
             patch("durin.channels.whatsapp_bridge.platform") as m_plat:
            m_sys.platform = "darwin"
            m_plat.machine.return_value = "arm64"
            assert platform_asset() == "durin-whatsapp-bridge-darwin-arm64"
            m_sys.platform = "linux"
            m_plat.machine.return_value = "x86_64"
            assert platform_asset() == "durin-whatsapp-bridge-linux-amd64"

    def test_unsupported_platform_raises(self):
        with patch("durin.channels.whatsapp_bridge.sys") as m_sys:
            m_sys.platform = "win32"
            with pytest.raises(BridgeSetupError):
                platform_asset()


class TestEnsureBridgeBinary:
    @pytest.mark.asyncio
    async def test_cached_binary_returned(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DURIN_HOME", str(tmp_path))
        target = cached_binary_path()
        target.parent.mkdir(parents=True)
        target.write_bytes(b"#!/bin/true\n")
        assert await ensure_bridge_binary() == target

    @pytest.mark.asyncio
    async def test_download_verifies_checksum(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DURIN_HOME", str(tmp_path))
        payload = b"fake-binary-bytes"
        good = hashlib.sha256(payload).hexdigest()
        with patch("durin.channels.whatsapp_bridge._pinned_checksum", return_value=good), \
             patch("durin.channels.whatsapp_bridge._download", return_value=payload):
            path = await ensure_bridge_binary()
        assert path.read_bytes() == payload
        assert path.stat().st_mode & 0o111  # executable

    @pytest.mark.asyncio
    async def test_checksum_mismatch_hard_fails(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DURIN_HOME", str(tmp_path))
        with patch("durin.channels.whatsapp_bridge._pinned_checksum", return_value="0" * 64), \
             patch("durin.channels.whatsapp_bridge._download", return_value=b"tampered"):
            with pytest.raises(BridgeSetupError, match="checksum"):
                await ensure_bridge_binary()
        assert not cached_binary_path().exists()

    @pytest.mark.asyncio
    async def test_no_checksum_no_go_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DURIN_HOME", str(tmp_path))
        with patch("durin.channels.whatsapp_bridge._pinned_checksum", return_value=None), \
             patch("durin.channels.whatsapp_bridge.shutil.which", return_value=None):
            with pytest.raises(BridgeSetupError):
                await ensure_bridge_binary()


class TestBridgeSupervisor:
    def _fake_bridge(self, tmp_path: Path, script: str) -> Path:
        p = tmp_path / "fake-bridge"
        p.write_text(f"#!/bin/sh\n{script}\n")
        p.chmod(0o755)
        return p

    @pytest.mark.asyncio
    async def test_restarts_on_crash(self, tmp_path):
        marker = tmp_path / "runs"
        binary = self._fake_bridge(tmp_path, f'echo run >> "{marker}"; exit 1')
        sup = BridgeSupervisor(binary, port=39999, token="t",
                               auth_dir=tmp_path, media_dir=tmp_path, logger=None)
        sup._initial_delay = 0.05  # keep the test fast
        await sup.start()
        await asyncio.sleep(0.5)
        await sup.stop()
        assert marker.read_text().count("run") >= 2

    @pytest.mark.asyncio
    async def test_no_restart_on_needs_login_exit(self, tmp_path):
        marker = tmp_path / "runs"
        binary = self._fake_bridge(tmp_path, f'echo run >> "{marker}"; exit 3')
        sup = BridgeSupervisor(binary, port=39999, token="t",
                               auth_dir=tmp_path, media_dir=tmp_path, logger=None)
        sup._initial_delay = 0.05
        await sup.start()
        await asyncio.sleep(0.5)
        assert sup.needs_login is True
        assert marker.read_text().count("run") == 1
        await sup.stop()

    @pytest.mark.asyncio
    async def test_stop_terminates_running_process(self, tmp_path):
        binary = self._fake_bridge(tmp_path, "sleep 60")
        sup = BridgeSupervisor(binary, port=39999, token="t",
                               auth_dir=tmp_path, media_dir=tmp_path, logger=None)
        await sup.start()
        await asyncio.sleep(0.2)
        await sup.stop()  # must return promptly, not after 60s
