"""Tests for the background process registry."""

from __future__ import annotations

import asyncio
import sys

import pytest

from durin.agent.tools.process_registry import (
    ProcessRegistry,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="process groups are POSIX-only in v1",
)


def _env() -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "HOME": "/tmp"}


class TestSpawnAndExit:

    @pytest.mark.asyncio
    async def test_spawn_returns_running_session(self, tmp_path):
        reg = ProcessRegistry()
        sess = await reg.spawn("sleep 5", cwd=str(tmp_path), env=_env())
        try:
            assert sess.id.startswith("proc_")
            assert sess.pid is not None
            assert not sess.exited
            assert reg.get(sess.id) is sess
        finally:
            await reg.kill(sess.id, force=True)

    @pytest.mark.asyncio
    async def test_process_exit_is_detected_and_output_captured(self, tmp_path):
        reg = ProcessRegistry()
        sess = await reg.spawn("echo hello-bg", cwd=str(tmp_path), env=_env())
        for _ in range(50):
            if sess.exited:
                break
            await asyncio.sleep(0.1)
        assert sess.exited
        assert sess.exit_code == 0
        assert "hello-bg" in sess.output_buffer
        # Finished sessions remain retrievable.
        assert reg.get(sess.id) is sess

    @pytest.mark.asyncio
    async def test_stderr_merged_into_buffer(self, tmp_path):
        reg = ProcessRegistry()
        sess = await reg.spawn("echo oops >&2", cwd=str(tmp_path), env=_env())
        for _ in range(50):
            if sess.exited:
                break
            await asyncio.sleep(0.1)
        assert "oops" in sess.output_buffer


class TestRollingBuffer:

    @pytest.mark.asyncio
    async def test_buffer_keeps_tail_under_cap(self, tmp_path):
        reg = ProcessRegistry()
        # ~400 KB of output: 4000 lines x ~100 chars, ending with a marker.
        cmd = (
            "for i in $(seq 1 4000); do "
            "printf 'line-%05d-%s\\n' $i 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'; "
            "done; echo FINAL-MARKER"
        )
        sess = await reg.spawn(cmd, cwd=str(tmp_path), env=_env())
        for _ in range(100):
            if sess.exited:
                break
            await asyncio.sleep(0.1)
        assert sess.exited
        assert len(sess.output_buffer) <= ProcessRegistry.MAX_OUTPUT_CHARS
        assert "FINAL-MARKER" in sess.output_buffer      # tail kept
        assert "line-00001-" not in sess.output_buffer   # head dropped


class TestKill:

    @pytest.mark.asyncio
    async def test_kill_terminates_process_group(self, tmp_path):
        reg = ProcessRegistry()
        # Parent spawns a child; killing the GROUP must take both down.
        sess = await reg.spawn("sleep 300 & sleep 300", cwd=str(tmp_path), env=_env())
        result = await reg.kill(sess.id)
        assert result["killed"] is True
        for _ in range(50):
            if sess.exited:
                break
            await asyncio.sleep(0.1)
        assert sess.exited

    @pytest.mark.asyncio
    async def test_kill_unknown_id(self):
        reg = ProcessRegistry()
        result = await reg.kill("proc_nope")
        assert result["killed"] is False
        assert "not found" in result["error"]


class TestListAndPoll:

    @pytest.mark.asyncio
    async def test_poll_running_and_exited(self, tmp_path):
        reg = ProcessRegistry()
        sess = await reg.spawn("echo done-now", cwd=str(tmp_path), env=_env())
        for _ in range(50):
            if sess.exited:
                break
            await asyncio.sleep(0.1)
        info = reg.poll(sess.id)
        assert info["status"] == "exited"
        assert info["exit_code"] == 0
        assert "done-now" in info["output_tail"]

    @pytest.mark.asyncio
    async def test_list_entries(self, tmp_path):
        reg = ProcessRegistry()
        sess = await reg.spawn("sleep 5", cwd=str(tmp_path), env=_env())
        try:
            entries = reg.list_sessions()
            assert any(e["id"] == sess.id and e["status"] == "running" for e in entries)
        finally:
            await reg.kill(sess.id, force=True)


class TestLimits:

    @pytest.mark.asyncio
    async def test_max_concurrent_enforced(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ProcessRegistry, "MAX_RUNNING", 1)
        reg = ProcessRegistry()
        sess = await reg.spawn("sleep 5", cwd=str(tmp_path), env=_env())
        try:
            with pytest.raises(RuntimeError, match="Too many background processes"):
                await reg.spawn("sleep 5", cwd=str(tmp_path), env=_env())
        finally:
            await reg.kill(sess.id, force=True)


class TestConfigOverride:

    @pytest.mark.asyncio
    async def test_config_overrides_limits(self, tmp_path):
        from durin.agent.tools.process_registry import ProcessToolConfig
        reg = ProcessRegistry(
            max_running=ProcessToolConfig(max_running=1).max_running,
            max_output_chars=500,
            finished_ttl_s=10,
        )
        assert reg.max_running == 1
        assert reg.max_output_chars == 500
        assert reg.finished_ttl_s == 10
        sess = await reg.spawn("sleep 5", cwd=str(tmp_path), env=_env())
        try:
            with pytest.raises(RuntimeError, match="Too many background processes"):
                await reg.spawn("sleep 5", cwd=str(tmp_path), env=_env())
        finally:
            await reg.kill(sess.id, force=True)

    def test_defaults_when_no_config(self):
        reg = ProcessRegistry()
        assert reg.max_running == ProcessRegistry.MAX_RUNNING
        assert reg.max_output_chars == ProcessRegistry.MAX_OUTPUT_CHARS


class TestShutdown:

    @pytest.mark.asyncio
    async def test_shutdown_kills_all_running(self, tmp_path):
        reg = ProcessRegistry()
        s1 = await reg.spawn("sleep 300", cwd=str(tmp_path), env=_env())
        s2 = await reg.spawn("sleep 300", cwd=str(tmp_path), env=_env())
        await reg.shutdown()
        for s in (s1, s2):
            for _ in range(50):
                if s.exited:
                    break
                await asyncio.sleep(0.1)
            assert s.exited
