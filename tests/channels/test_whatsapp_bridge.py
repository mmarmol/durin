"""Tests for WhatsApp bridge binary resolution and process supervision."""

import asyncio
import hashlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from durin.channels.whatsapp_bridge import (
    BRIDGE_VERSION,
    BridgeSetupError,
    BridgeSupervisor,
    cached_binary_path,
    ensure_bridge_binary,
    platform_asset,
)


class TestBridgeVersioning:
    """The bridge is versioned and released independently of durin, so its
    cache path and download URL must key on BRIDGE_VERSION — never the durin
    package version — or a durin upgrade would needlessly re-download it."""

    def test_cache_path_keyed_to_bridge_version(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DURIN_HOME", str(tmp_path))
        assert cached_binary_path().parent.name == BRIDGE_VERSION

    def test_download_url_points_at_bridge_release_tag(self):
        from durin.channels.whatsapp_bridge import _RELEASE_URL

        url = _RELEASE_URL.format(version=BRIDGE_VERSION, asset="x")
        assert f"whatsapp-bridge-v{BRIDGE_VERSION}" in url

    def test_committed_pin_covers_every_target(self):
        """The committed checksum pin must list all four release assets, so a
        wheel install can verify the download on any supported platform."""
        import json
        from importlib import resources

        pin = json.loads(
            resources.files("durin.channels")
            .joinpath("bridge_checksums.json")
            .read_text(encoding="utf-8")
        )["sha256"]
        expected = {
            f"durin-whatsapp-bridge-{os}-{arch}"
            for os in ("linux", "darwin")
            for arch in ("amd64", "arm64")
        }
        assert set(pin) == expected
        assert all(len(v) == 64 for v in pin.values())


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

    @pytest.mark.asyncio
    async def test_dev_build_failure_raises_setup_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DURIN_HOME", str(tmp_path))
        boom = subprocess.CalledProcessError(1, ["go", "build"], stderr=b"compile error")
        with patch("durin.channels.whatsapp_bridge._pinned_checksum", return_value=None), \
             patch("durin.channels.whatsapp_bridge.shutil.which", return_value="/usr/bin/go"), \
             patch("durin.channels.whatsapp_bridge.subprocess.run", side_effect=boom):
            with pytest.raises(BridgeSetupError, match="go build"):
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
    async def test_no_restart_on_fatal_config_exit(self, tmp_path):
        """Exit code 2 (usage/config error) is deterministic — restarting
        can't fix a bad bridge_url/port or BRIDGE_TOKEN — so the supervisor
        must stop like it does for 3/4, but without claiming needs_login."""
        marker = tmp_path / "runs"
        binary = self._fake_bridge(tmp_path, f'echo run >> "{marker}"; exit 2')
        sup = BridgeSupervisor(binary, port=39999, token="t",
                               auth_dir=tmp_path, media_dir=tmp_path, logger=None)
        sup._initial_delay = 0.05
        await sup.start()
        await asyncio.sleep(0.5)
        assert sup.needs_login is False
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

    @pytest.mark.asyncio
    async def test_stop_during_spawn_terminates_child(self, tmp_path):
        # stop() issued while _run() is still awaiting the subprocess spawn
        # must not hang and must not orphan the freshly spawned child.
        binary = self._fake_bridge(tmp_path, "sleep 60")
        real_spawn = asyncio.create_subprocess_exec
        spawned = []

        async def slow_spawn(*args, **kwargs):
            await asyncio.sleep(0.3)
            proc = await real_spawn(*args, **kwargs)
            spawned.append(proc)
            return proc

        sup = BridgeSupervisor(binary, port=39999, token="t",
                               auth_dir=tmp_path, media_dir=tmp_path, logger=None)
        with patch("durin.channels.whatsapp_bridge.asyncio.create_subprocess_exec",
                   slow_spawn):
            await sup.start()
            await asyncio.sleep(0.05)  # let _run() enter the spawn await
            await asyncio.wait_for(sup.stop(), timeout=5)  # must not hang
        assert len(spawned) == 1
        assert spawned[0].returncode is not None  # child terminated, not orphaned


async def _wait_status(sess, want, timeout=3.0):
    """Poll the pairing snapshot until it reaches `want` — robust to scheduler
    load, unlike a fixed sleep."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if sess.snapshot()["status"] == want:
            return sess.snapshot()
        await asyncio.sleep(0.02)
    raise AssertionError(f"status never became {want}: {sess.snapshot()}")


class TestPairingSession:
    def _fake_qr_bridge(self, tmp_path: Path, body: str) -> Path:
        """A fake bridge whose `qr --emit-frames` invocation runs `body`."""
        p = tmp_path / "fake-qr-bridge"
        p.write_text(f"#!/bin/sh\n{body}\n")
        p.chmod(0o755)
        return p

    @pytest.mark.asyncio
    async def test_captures_qr_then_connected(self, tmp_path):
        from durin.channels.whatsapp_bridge import PairingSession

        # Emit a qr frame, then (after a beat) a connected status, then exit 0.
        binary = self._fake_qr_bridge(
            tmp_path,
            'echo \'{"type":"qr","code":"WA123"}\'; sleep 0.4; '
            'echo \'{"type":"status","status":"connected"}\'',
        )
        with patch("durin.channels.whatsapp_bridge.ensure_bridge_binary",
                   return_value=binary):
            sess = PairingSession(auth_dir=tmp_path, token="t")
            await sess.start()
            snap = await _wait_status(sess, "waiting_scan")
            assert snap["qr"] == "WA123"
            final = await _wait_status(sess, "connected")
            assert final["qr"] is None

    @pytest.mark.asyncio
    async def test_nonzero_exit_before_terminal_becomes_error(self, tmp_path):
        from durin.channels.whatsapp_bridge import PairingSession

        binary = self._fake_qr_bridge(
            tmp_path, 'echo \'{"type":"qr","code":"X"}\'; exit 1')
        with patch("durin.channels.whatsapp_bridge.ensure_bridge_binary",
                   return_value=binary):
            sess = PairingSession(auth_dir=tmp_path, token="t")
            await sess.start()
            await _wait_status(sess, "error")

    @pytest.mark.asyncio
    async def test_crash_stderr_tail_surfaces_in_error(self, tmp_path):
        from durin.channels.whatsapp_bridge import PairingSession

        # A crash with no error frame must still give the UI something to show.
        binary = self._fake_qr_bridge(
            tmp_path, 'echo "boom: bad auth dir" 1>&2; exit 1')
        with patch("durin.channels.whatsapp_bridge.ensure_bridge_binary",
                   return_value=binary):
            sess = PairingSession(auth_dir=tmp_path, token="t")
            await sess.start()
            await _wait_status(sess, "error")
            assert "boom" in (sess.snapshot()["error"] or "")

    @pytest.mark.asyncio
    async def test_concurrent_start_leaves_one_live_process(self, tmp_path):
        from durin.channels.whatsapp_bridge import PairingSession

        binary = self._fake_qr_bridge(
            tmp_path, 'echo \'{"type":"qr","code":"X"}\'; sleep 5')
        with patch("durin.channels.whatsapp_bridge.ensure_bridge_binary",
                   return_value=binary):
            sess = PairingSession(auth_dir=tmp_path, token="t")
            # Two racing starts (two settings tabs) must serialize under the
            # lock — no leaked second subprocess.
            await asyncio.gather(sess.start(), sess.start())
            await _wait_status(sess, "waiting_scan")
            await sess.cancel()
            assert sess._proc is None

    @pytest.mark.asyncio
    async def test_setup_error_surfaces_as_error_status(self, tmp_path):
        from durin.channels.whatsapp_bridge import BridgeSetupError, PairingSession

        with patch("durin.channels.whatsapp_bridge.ensure_bridge_binary",
                   side_effect=BridgeSetupError("no binary")):
            sess = PairingSession(auth_dir=tmp_path, token="t")
            snap = await sess.start()
            assert snap["status"] == "error"
            assert "no binary" in snap["error"]

    @pytest.mark.asyncio
    async def test_force_deletes_existing_session_db(self, tmp_path):
        from durin.channels.whatsapp_bridge import PairingSession

        db = tmp_path / "whatsmeow.db"
        db.write_text("stale")
        binary = self._fake_qr_bridge(
            tmp_path, 'echo \'{"type":"status","status":"connected"}\'')
        with patch("durin.channels.whatsapp_bridge.ensure_bridge_binary",
                   return_value=binary):
            sess = PairingSession(auth_dir=tmp_path, token="t")
            await sess.start(force=True)
            await asyncio.sleep(0.2)
        assert not db.exists()
