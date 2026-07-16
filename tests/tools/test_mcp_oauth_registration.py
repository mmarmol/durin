"""Tests for ensure_registration_covers: DCR re-registration on redirect mismatch."""
from __future__ import annotations

import pytest


async def test_ensure_registration_forgets_stale_dynamic_client(tmp_path, monkeypatch):
    from durin.agent.tools.mcp_oauth import ensure_registration_covers

    class _Storage:
        def __init__(self):
            self.forgotten = False

        async def get_client_info(self):
            class _Info:
                redirect_uris = ["http://127.0.0.1:1456/callback"]
            return _Info()

        def forget(self):
            self.forgotten = True

    st = _Storage()
    await ensure_registration_covers(st, None, "https://durin.ts.net/api/v1/mcp/oauth/callback")
    assert st.forgotten is True


async def test_ensure_registration_static_client_raises_actionable(tmp_path):
    from durin.agent.tools.mcp_oauth import (
        McpOauthRedirectMismatch,
        ensure_registration_covers,
    )

    class _OC:
        client_id = "static-app"

    class _Storage:
        async def get_client_info(self):
            class _Info:
                redirect_uris = ["http://127.0.0.1:1456/callback"]
            return _Info()

        def forget(self):
            raise AssertionError("static clients are never forgotten")

    with pytest.raises(McpOauthRedirectMismatch, match="api/v1/mcp/oauth/callback"):
        await ensure_registration_covers(_Storage(), _OC(), "https://durin.ts.net/api/v1/mcp/oauth/callback")


async def test_ensure_registration_noop_when_covered_or_absent():
    from durin.agent.tools.mcp_oauth import ensure_registration_covers

    class _Covered:
        async def get_client_info(self):
            class _Info:
                redirect_uris = ["https://durin.ts.net/api/v1/mcp/oauth/callback"]
            return _Info()

        def forget(self):
            raise AssertionError("must not forget a covering registration")

    class _Absent:
        async def get_client_info(self):
            return None

        def forget(self):
            raise AssertionError("nothing stored, nothing to forget")

    await ensure_registration_covers(_Covered(), None, "https://durin.ts.net/api/v1/mcp/oauth/callback")
    await ensure_registration_covers(_Absent(), None, "https://durin.ts.net/api/v1/mcp/oauth/callback")
