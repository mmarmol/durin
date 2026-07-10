"""WhatsApp Go bridge: binary resolution, download verification, supervision."""

import asyncio
import hashlib
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from importlib import metadata, resources
from pathlib import Path
from typing import Any

from loguru import logger as _default_logger


class BridgeSetupError(RuntimeError):
    """The bridge binary could not be resolved."""


_ASSET_TEMPLATE = "durin-whatsapp-bridge-{goos}-{goarch}"
_RELEASE_URL = "https://github.com/mmarmol/durin/releases/download/v{version}/{asset}"
# Exit codes the bridge uses for "re-pair needed" — supervisor must not restart.
_NEEDS_LOGIN_EXIT_CODES = (3, 4)
# Usage/config error — deterministic, restarting can't fix it either.
_FATAL_CONFIG_EXIT_CODE = 2


def platform_asset() -> str:
    goos = {"darwin": "darwin", "linux": "linux"}.get(sys.platform)
    machine = platform.machine().lower()
    goarch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    if not goos or not goarch:
        raise BridgeSetupError(f"WhatsApp bridge has no build for {sys.platform}/{machine}")
    return _ASSET_TEMPLATE.format(goos=goos, goarch=goarch)


def package_version() -> str:
    return metadata.version("durin-agent")


def cached_binary_path() -> Path:
    from durin.config.paths import get_bridge_install_dir

    return get_bridge_install_dir() / package_version() / platform_asset()


def _pinned_checksum(asset: str) -> str | None:
    """sha256 for ``asset`` from the release-generated pin file, if bundled."""
    ref = resources.files("durin.channels").joinpath("bridge_checksums.json")
    if not ref.is_file():
        return None
    return json.loads(ref.read_text(encoding="utf-8")).get("sha256", {}).get(asset)


async def _download(url: str) -> bytes:
    import httpx

    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


async def ensure_bridge_binary() -> Path:
    """Resolve the bridge binary: cache → verified download → dev build."""
    target = cached_binary_path()
    if target.exists():
        return target

    asset = platform_asset()
    expected = _pinned_checksum(asset)
    if expected:
        url = _RELEASE_URL.format(version=package_version(), asset=asset)
        try:
            data = await _download(url)
        except Exception as exc:
            raise BridgeSetupError(
                f"Could not download the WhatsApp bridge from {url}: {exc}"
            ) from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != expected:
            raise BridgeSetupError(
                f"WhatsApp bridge checksum mismatch for {asset}: "
                f"expected {expected}, got {digest}. Refusing to run it."
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        try:
            tmp.write_bytes(data)
            tmp.chmod(0o755)
            tmp.replace(target)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise BridgeSetupError(
                f"Could not install the WhatsApp bridge at {target}: {exc}"
            ) from exc
        return target

    return _dev_build(target)


def _dev_build(target: Path) -> Path:
    """Source install without a checksum pin: build from bridge/ with Go."""
    src = Path(__file__).resolve().parent.parent.parent / "bridge"
    go = shutil.which("go")
    if not (src / "go.mod").exists() or go is None:
        raise BridgeSetupError(
            "No bundled bridge checksum (source install) and no local Go toolchain "
            "or bridge/ source to build from. Install a released durin, or install "
            "Go >= 1.24 and re-run."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [go, "build", "-o", str(target), "."],
            cwd=src, check=True, capture_output=True,
            env={**os.environ, "CGO_ENABLED": "0"},
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr or b""
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise BridgeSetupError(
            f"WhatsApp bridge `go build` failed (exit {exc.returncode}): "
            f"{stderr.strip()[-2000:]}"
        ) from exc
    return target


class BridgeSupervisor:
    """Spawn the bridge in serve mode and keep it alive.

    Restarts with exponential backoff on crashes. Exit codes 3/4 mean the
    WhatsApp session needs (re-)pairing — surfacing that and stopping is
    correct; restarting would loop forever against a dead session.
    """

    def __init__(self, binary: Path, *, port: int, token: str,
                 auth_dir: Path, media_dir: Path, logger=None):
        self.binary = binary
        self.port = port
        self.token = token
        self.auth_dir = auth_dir
        self.media_dir = media_dir
        self.logger = logger or _default_logger
        self.needs_login = False
        self._initial_delay = 2.0
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self.needs_login = False
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        delay = self._initial_delay
        while not self._stopping.is_set():
            started = time.monotonic()
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    str(self.binary), "serve",
                    "--port", str(self.port),
                    "--auth-dir", str(self.auth_dir),
                    "--media-dir", str(self.media_dir),
                    env={**os.environ, "BRIDGE_TOKEN": self.token},
                )
                # stop() may have run while we were awaiting the spawn: it saw
                # no process to terminate and is blocked on this task. Kill the
                # fresh child here so the task (and stop()) return promptly.
                if self._stopping.is_set():
                    self._proc.terminate()
                rc = await self._proc.wait()
            except Exception:
                self.logger.exception("WhatsApp bridge failed to spawn")
                rc = -1
            finally:
                self._proc = None
            if self._stopping.is_set():
                return
            if rc in _NEEDS_LOGIN_EXIT_CODES:
                self.needs_login = True
                self.logger.error(
                    "WhatsApp session not paired or logged out (bridge exit {}). "
                    "Run `durin channels login whatsapp` to (re-)pair.", rc)
                return
            if rc == _FATAL_CONFIG_EXIT_CODE:
                self.logger.error(
                    "bridge usage/config error (exit 2); check bridge_url/port and BRIDGE_TOKEN")
                return
            if time.monotonic() - started > 60:
                delay = self._initial_delay  # it ran fine for a while: reset
            self.logger.warning("WhatsApp bridge exited ({}); restarting in {:.1f}s", rc, delay)
            try:
                await asyncio.wait_for(self._stopping.wait(), delay + random.uniform(0, delay / 4))
                return
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 1.6, 30.0)

    async def stop(self) -> None:
        self._stopping.set()
        proc = self._proc
        if proc is not None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()
        if self._task is not None:
            await self._task
            self._task = None


class PairingSession:
    """Drive a browser-based QR pairing: run the bridge in `qr --emit-frames`
    mode and capture the QR code and pairing status from its stdout so a
    gateway service can relay them to the webui.

    Statuses (also the values the webui branches on):
      ``starting``       — subprocess launching, no QR yet
      ``waiting_scan``   — a QR code is available; ``qr`` holds it
      ``connected``      — the device paired successfully
      ``timeout``        — the QR expired without a scan
      ``already_paired`` — a session already exists (no QR shown)
      ``error``          — the bridge could not start or crashed
    """

    def __init__(self, *, auth_dir: Path, token: str, logger=None):
        self._auth_dir = auth_dir
        self._token = token
        self.logger = logger or _default_logger
        self._status = "idle"
        self._qr: str | None = None
        self._error: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        # Serialize start()/cancel(): two settings tabs (both CONFIG_WRITE) can
        # POST /login/start concurrently; without the lock each spawns a bridge
        # and the loser's subprocess leaks (holding whatsmeow.db open).
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        """Current pairing state for the poll endpoint."""
        return {"status": self._status, "qr": self._qr, "error": self._error}

    async def start(self, *, force: bool = False) -> dict[str, Any]:
        """(Re)start a pairing attempt. Cancels any in-flight one first."""
        async with self._lock:
            await self._cancel_locked()
            try:
                binary = await ensure_bridge_binary()
            except BridgeSetupError as exc:
                self._status, self._error = "error", str(exc)
                return self.snapshot()

            if force:
                db = self._auth_dir / "whatsmeow.db"
                if db.exists():
                    db.unlink()

            self._status, self._qr, self._error = "starting", None, None
            self._auth_dir.mkdir(parents=True, exist_ok=True)
            self._proc = await asyncio.create_subprocess_exec(
                str(binary), "qr", "--emit-frames", "--auth-dir", str(self._auth_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "BRIDGE_TOKEN": self._token},
            )
            self._task = asyncio.create_task(self._read_frames(self._proc))
            return self.snapshot()

    async def _read_frames(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            async for raw in proc.stdout:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue  # ignore any non-frame line
                kind = frame.get("type")
                if kind == "qr":
                    self._qr = frame.get("code")
                    self._status = "waiting_scan"
                elif kind == "status":
                    st = frame.get("status")
                    if st in ("connected", "timeout", "already_paired"):
                        self._status, self._qr = st, None
                elif kind == "error":
                    self._status, self._error = "error", frame.get("error")
        except Exception:  # noqa: BLE001
            self.logger.exception("WhatsApp pairing reader failed")
        finally:
            rc = await proc.wait()
            # If the process exited before reaching a terminal state, treat a
            # nonzero exit as failure so the UI never waits forever, and surface
            # the bridge's stderr tail so a crash isn't a blank error.
            if self._status in ("starting", "waiting_scan"):
                self._status = "error" if rc else "timeout"
                if rc and proc.stderr is not None:
                    tail = (await proc.stderr.read())[-500:].decode(
                        "utf-8", errors="replace").strip()
                    if tail:
                        self._error = tail

    async def cancel(self) -> None:
        """Stop any running pairing subprocess and reader."""
        async with self._lock:
            await self._cancel_locked()

    async def _cancel_locked(self) -> None:
        """Terminate the current subprocess/reader. Caller holds ``self._lock``."""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
        if self._task is not None:
            with suppress(Exception):
                await self._task
            self._task = None
        self._proc = None
