"""SP0: Principal identity + scope authorization."""

import dataclasses

import pytest

from durin.service.principal import Principal, Scope
from durin.service.types import ForbiddenError


def test_local_principal_has_admin():
    p = Principal.local()
    assert p.subject == "local"
    assert p.kind == "local"
    # ADMIN implies every scope, including ones not in the catalog.
    assert p.has_scope(Scope.SETTINGS_WRITE)
    assert p.has_scope("anything:at:all")


def test_remote_principal_scopes():
    p = Principal.remote("tok1", {Scope.SETTINGS_READ.value})
    assert p.kind == "remote"
    assert p.subject == "tok1"
    assert p.has_scope(Scope.SETTINGS_READ)
    assert p.has_scope("settings:read")
    assert not p.has_scope(Scope.SETTINGS_WRITE)


def test_has_scope_accepts_enum_and_str():
    p = Principal.remote("t", {Scope.MEMORY_READ.value})
    assert p.has_scope(Scope.MEMORY_READ)
    assert p.has_scope("memory:read")
    assert not p.has_scope("memory:write")


def test_require_raises_forbidden_when_missing():
    p = Principal.remote("t", {Scope.SETTINGS_READ.value})
    with pytest.raises(ForbiddenError) as excinfo:
        p.require(Scope.SETTINGS_WRITE)
    assert excinfo.value.details == {"scope": "settings:write"}
    assert "settings:write" in excinfo.value.message


def test_require_passes_when_present():
    p = Principal.remote("t", {Scope.SETTINGS_WRITE.value})
    p.require(Scope.SETTINGS_WRITE)  # must not raise


def test_admin_require_always_passes():
    Principal.local().require(Scope.MEMORY_WRITE)  # must not raise


def test_principal_is_frozen():
    p = Principal.local()
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.subject = "tampered"  # type: ignore[misc]


def test_principal_is_hashable():
    p = Principal.local()
    assert p in {p}


def test_scope_values_are_domain_colon_action():
    for scope in Scope:
        if scope is Scope.ADMIN:
            continue
        assert ":" in scope.value
        domain, action = scope.value.split(":", 1)
        assert domain and action in {"read", "write"}


def test_mcp_scopes_defined():
    assert Scope.MCP_READ.value == "mcp:read"
    assert Scope.MCP_WRITE.value == "mcp:write"
