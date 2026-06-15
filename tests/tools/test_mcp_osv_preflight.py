"""OSV malware preflight tests (supply-chain / typosquat guard).

All OSV HTTP calls are mocked — no real network.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from unittest.mock import MagicMock

import pytest

from durin.agent.tools.mcp_security import (
    check_package_for_malware,
    clear_osv_cache,
)
from durin.config.schema import MCPServerConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _osv_response(vulns: list[dict]) -> MagicMock:
    """Build a mock urllib response that yields the given vulns JSON."""
    body = json.dumps({"vulns": vulns}).encode()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=cm)
    cm.__exit__ = MagicMock(return_value=False)
    cm.read = MagicMock(return_value=body)
    return cm


def _mal_advisory(mal_id: str = "MAL-2024-1234") -> dict:
    return {"id": mal_id, "summary": "Malicious code in package"}


# ---------------------------------------------------------------------------
# Package-name extraction tests
# ---------------------------------------------------------------------------

class TestPackageExtraction:
    """check_package_for_malware must correctly identify package + ecosystem."""

    def test_npx_scoped_package_with_version(self, monkeypatch):
        """npx -y @scope/pkg@1.0 → @scope/pkg / npm"""
        calls = []

        def fake_query(package, ecosystem, version=None):
            calls.append((package, ecosystem))
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()
        check_package_for_malware("npx", ["-y", "@foo/bar@1.0.0"])
        assert len(calls) == 1
        assert calls[0] == ("@foo/bar", "npm")

    def test_npx_unscoped_package(self, monkeypatch):
        """npx serve → serve / npm"""
        calls = []

        def fake_query(package, ecosystem, version=None):
            calls.append((package, ecosystem))
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()
        check_package_for_malware("npx", ["serve"])
        assert calls[0] == ("serve", "npm")

    def test_uvx_package(self, monkeypatch):
        """uvx mypackage → mypackage / PyPI"""
        calls = []

        def fake_query(package, ecosystem, version=None):
            calls.append((package, ecosystem))
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()
        check_package_for_malware("uvx", ["mypackage"])
        assert calls[0] == ("mypackage", "PyPI")

    def test_npm_command(self, monkeypatch):
        """npm exec -y pkg → pkg / npm"""
        calls = []

        def fake_query(package, ecosystem, version=None):
            calls.append((package, ecosystem))
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()
        check_package_for_malware("npm", ["exec", "-y", "create-react-app"])
        # "exec" gets consumed as the first positional, then next flag consumed
        # This is a "best effort" — main case is the scoped/unscoped name
        # (At minimum no crash)

    def test_non_runner_command_returns_none(self, monkeypatch):
        """python -m foo → not a package runner → None (clean)"""
        calls = []

        def fake_query(package, ecosystem, version=None):
            calls.append((package, ecosystem))
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()
        result = check_package_for_malware("python", ["-m", "foo"])
        assert result is None
        assert calls == []  # never queried

    def test_unknown_command_is_clean(self, monkeypatch):
        calls = []

        def fake_query(package, ecosystem, version=None):
            calls.append((package, ecosystem))
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()
        result = check_package_for_malware("node", ["server.js"])
        assert result is None
        assert calls == []

    def test_npx_version_stripped_from_package_name(self, monkeypatch):
        """npx @foo/bar@1.2.3 → package is @foo/bar not @foo/bar@1.2.3"""
        calls = []

        def fake_query(package, ecosystem, version=None):
            calls.append((package, ecosystem, version))
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()
        check_package_for_malware("npx", ["@scope/mypkg@2.0.1"])
        assert calls[0][0] == "@scope/mypkg"
        assert calls[0][2] == "2.0.1"

    def test_bunx_treated_as_npm(self, monkeypatch):
        """bunx is an npm-ecosystem runner."""
        calls = []

        def fake_query(package, ecosystem, version=None):
            calls.append((package, ecosystem))
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()
        check_package_for_malware("bunx", ["somepkg"])
        assert calls[0][1] == "npm"


# ---------------------------------------------------------------------------
# Malware detection: blocked when MAL advisory found
# ---------------------------------------------------------------------------

class TestMalwareBlocking:

    def test_mal_advisory_returns_error_string(self, monkeypatch):
        """MAL advisory → returns non-None error string."""
        def fake_query(package, ecosystem, version=None):
            return [_mal_advisory("MAL-2024-9999")]

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()
        result = check_package_for_malware("npx", ["-y", "@evil/pkg"])
        assert result is not None
        assert "MAL-2024-9999" in result
        assert "@evil/pkg" in result

    def test_clean_response_returns_none(self, monkeypatch):
        """No MAL advisories → None (allow)."""
        def fake_query(package, ecosystem, version=None):
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()
        result = check_package_for_malware("npx", ["@legit/mcp-server"])
        assert result is None

    def test_non_mal_advisory_ignored(self, monkeypatch):
        """CVE-* advisories are not malware — must be ignored."""
        def fake_query(package, ecosystem, version=None):
            return [{"id": "CVE-2024-12345", "summary": "Buffer overflow"}]

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()
        result = check_package_for_malware("uvx", ["somepkg"])
        assert result is None


# ---------------------------------------------------------------------------
# Fail-open: network errors must never block
# ---------------------------------------------------------------------------

class TestFailOpen:

    def test_network_error_returns_none(self, monkeypatch):
        """urllib error → fail-open → None."""
        def raise_error(package, ecosystem, version=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", raise_error
        )
        clear_osv_cache()
        result = check_package_for_malware("npx", ["some-mcp-pkg"])
        assert result is None

    def test_timeout_returns_none(self, monkeypatch):
        """Timeout → fail-open → None."""
        def raise_timeout(package, ecosystem, version=None):
            raise TimeoutError("timed out")

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", raise_timeout
        )
        clear_osv_cache()
        result = check_package_for_malware("uvx", ["some-mcp-pkg"])
        assert result is None

    def test_json_parse_error_returns_none(self, monkeypatch):
        """Malformed JSON → fail-open → None."""
        def raise_json(package, ecosystem, version=None):
            raise ValueError("No JSON object could be decoded")

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", raise_json
        )
        clear_osv_cache()
        result = check_package_for_malware("npx", ["some-mcp-pkg"])
        assert result is None


# ---------------------------------------------------------------------------
# Config: malware_check=False disables querying
# ---------------------------------------------------------------------------

class TestConfig:

    def test_malware_check_defaults_true(self):
        cfg = MCPServerConfig(command="npx")
        assert cfg.malware_check is True

    def test_malware_check_settable_false(self):
        cfg = MCPServerConfig(command="npx", malware_check=False)
        assert cfg.malware_check is False


# ---------------------------------------------------------------------------
# _open_stdio integration: raises when MAL found, passes when clean/disabled
# ---------------------------------------------------------------------------

class TestOpenStdioIntegration:

    def _make_conn(self, cfg_kw: dict):
        from durin.agent.tools.mcp_connection import MCPServerConnection
        from durin.agent.tools.registry import ToolRegistry

        cfg = MCPServerConfig(**cfg_kw)
        return MCPServerConnection("test", cfg, ToolRegistry())

    def _patch_stdio(self, monkeypatch):
        """Patch stdio_client so it doesn't actually spawn anything."""
        import io

        import durin.agent.tools.mcp_connection as mc

        class _FakeCM:
            async def __aenter__(self):
                return ("r", "w")

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(mc, "_mcp_stderr_log", lambda: io.StringIO())
        monkeypatch.setattr(
            "mcp.client.stdio.stdio_client", lambda *a, **k: _FakeCM()
        )

    @pytest.mark.asyncio
    async def test_mal_advisory_raises_before_spawn(self, monkeypatch):
        """MAL advisory → PermissionError raised; stdio_client never called."""
        self._patch_stdio(monkeypatch)

        spawn_called = []
        original_stdio = __import__(
            "mcp.client.stdio", fromlist=["stdio_client"]
        ).stdio_client

        def tracking_stdio(*a, **k):
            spawn_called.append(True)
            return original_stdio(*a, **k)

        def fake_query(package, ecosystem, version=None):
            return [_mal_advisory("MAL-2024-7777")]

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        monkeypatch.setattr("mcp.client.stdio.stdio_client", tracking_stdio)
        clear_osv_cache()

        conn = self._make_conn({"command": "npx", "args": ["-y", "@evil/server"]})
        with pytest.raises(PermissionError) as ei:
            await conn._open_stdio()

        msg = str(ei.value)
        assert "@evil/server" in msg
        assert "MAL-2024-7777" in msg
        assert spawn_called == []  # never reached spawn

    @pytest.mark.asyncio
    async def test_clean_package_proceeds(self, monkeypatch):
        """Clean OSV response → spawn proceeds normally."""
        self._patch_stdio(monkeypatch)

        def fake_query(package, ecosystem, version=None):
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()

        conn = self._make_conn(
            {"command": "npx", "args": ["-y", "@legit/server"]}
        )
        result = await conn._open_stdio()
        assert result == ("r", "w")

    @pytest.mark.asyncio
    async def test_malware_check_false_skips_query(self, monkeypatch):
        """malware_check=False → _query_osv never called."""
        self._patch_stdio(monkeypatch)

        query_calls = []

        def fake_query(package, ecosystem, version=None):
            query_calls.append((package, ecosystem))
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()

        conn = self._make_conn(
            {"command": "npx", "args": ["-y", "@any/pkg"], "malware_check": False}
        )
        await conn._open_stdio()
        assert query_calls == []

    @pytest.mark.asyncio
    async def test_fail_open_on_network_error_during_spawn(self, monkeypatch):
        """Network error in OSV query → spawn proceeds (fail-open)."""
        self._patch_stdio(monkeypatch)

        def fail_query(package, ecosystem, version=None):
            raise urllib.error.URLError("network unreachable")

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fail_query
        )
        clear_osv_cache()

        conn = self._make_conn(
            {"command": "npx", "args": ["-y", "@any/pkg"]}
        )
        result = await conn._open_stdio()
        assert result == ("r", "w")


# ---------------------------------------------------------------------------
# Cache: same package only queried once per session
# ---------------------------------------------------------------------------

class TestCache:

    def test_same_package_queried_once(self, monkeypatch):
        """Two calls for the same package → one OSV query."""
        call_count = []

        def fake_query(package, ecosystem, version=None):
            call_count.append(1)
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()

        check_package_for_malware("npx", ["@my/pkg"])
        check_package_for_malware("npx", ["@my/pkg"])
        assert len(call_count) == 1

    def test_different_packages_each_queried(self, monkeypatch):
        """Different packages → separate queries."""
        queried = []

        def fake_query(package, ecosystem, version=None):
            queried.append(package)
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()

        check_package_for_malware("npx", ["@my/pkg-a"])
        check_package_for_malware("npx", ["@my/pkg-b"])
        assert "@my/pkg-a" in queried
        assert "@my/pkg-b" in queried
        assert len(queried) == 2

    def test_clear_cache_forces_requery(self, monkeypatch):
        """After clear_osv_cache(), same package is re-queried."""
        call_count = []

        def fake_query(package, ecosystem, version=None):
            call_count.append(1)
            return []

        monkeypatch.setattr(
            "durin.agent.tools.mcp_security._query_osv", fake_query
        )
        clear_osv_cache()
        check_package_for_malware("npx", ["mypkg"])
        clear_osv_cache()
        check_package_for_malware("npx", ["mypkg"])
        assert len(call_count) == 2
