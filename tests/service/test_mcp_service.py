"""Tests for McpService: status derivation, reads, writes, oauth."""
from __future__ import annotations

from durin.agent.mcp_runtime import RawConnState
from durin.service.mcp import derive_status


def _raw(breaker_state: str, error: str | None = None) -> RawConnState:
    return RawConnState(breaker_state=breaker_state, error=error, tools=[])


def test_derive_status_disabled_wins() -> None:
    assert derive_status(
        enabled=False, oauth_required=False, oauth_authenticated=False, raw=None
    ) == ("disabled", None)
    # disabled even if a stale live connection is still around
    assert derive_status(
        enabled=False, oauth_required=False, oauth_authenticated=False, raw=_raw("closed")
    ) == ("disabled", None)


def test_derive_status_needs_auth() -> None:
    assert derive_status(
        enabled=True, oauth_required=True, oauth_authenticated=False, raw=None
    ) == ("needs_auth", None)


def test_derive_status_connected() -> None:
    assert derive_status(
        enabled=True, oauth_required=False, oauth_authenticated=False, raw=_raw("closed")
    ) == ("connected", None)
    # oauth satisfied + live
    assert derive_status(
        enabled=True, oauth_required=True, oauth_authenticated=True, raw=_raw("closed")
    ) == ("connected", None)


def test_derive_status_failed_carries_error() -> None:
    assert derive_status(
        enabled=True, oauth_required=False, oauth_authenticated=False, raw=_raw("open", "boom")
    ) == ("failed", "boom")


def test_derive_status_connecting() -> None:
    # half-open breaker is a probe-in-progress
    assert derive_status(
        enabled=True, oauth_required=False, oauth_authenticated=False, raw=_raw("half-open")
    ) == ("connecting", None)
    # enabled + authed but no live connection yet (coming up / runtime absent)
    assert derive_status(
        enabled=True, oauth_required=False, oauth_authenticated=False, raw=None
    ) == ("connecting", None)
