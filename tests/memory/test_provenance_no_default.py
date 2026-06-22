"""`current_author` raises when no scope is active (explicit-only contract).

Every memory write declares its author via :func:`author_scope`. Calling
:func:`current_author` outside an active scope must raise loud, not silently
produce a default that masks "I forgot to wrap" bugs.
"""

from __future__ import annotations

import pytest

from durin.memory.provenance import (
    MissingAuthorScopeError,
    author_scope,
    current_author,
)


@pytest.fixture(autouse=True)
def _test_default_author_scope():
    """Override the suite-wide autouse fixture (``tests/conftest.py``).

    The conftest opens ``author_scope('agent_created')`` around every
    test so memory writes work by default. Tests *here* assert the
    opposite — that calling :func:`current_author` outside a scope
    raises — so we override with a no-op fixture.
    """
    yield


def test_outside_scope_raises() -> None:
    with pytest.raises(MissingAuthorScopeError):
        current_author()


def test_inside_scope_returns_value() -> None:
    with author_scope("agent_created"):
        assert current_author() == "agent_created"
    with author_scope("user_authored"):
        assert current_author() == "user_authored"


def test_nested_scope_overrides_then_restores() -> None:
    with author_scope("user_authored"):
        assert current_author() == "user_authored"
        with author_scope("agent_created"):
            assert current_author() == "agent_created"
        # Outer scope restored after the inner exits.
        assert current_author() == "user_authored"


def test_after_scope_exits_raises_again() -> None:
    """No leakage: once the scope exits, current_author() must raise
    again. Otherwise scope sets become sticky and the contract is
    silently violated for subsequent writes."""
    with author_scope("agent_created"):
        assert current_author() == "agent_created"
    with pytest.raises(MissingAuthorScopeError):
        current_author()


def test_error_message_names_the_fix() -> None:
    """The exception message must hint at the fix so a dev hitting it
    in a test/CI run immediately knows to wrap their call site."""
    try:
        current_author()
    except MissingAuthorScopeError as exc:
        msg = str(exc).lower()
        assert "author_scope" in msg
        assert "agent_created" in msg or "user_authored" in msg
    else:
        pytest.fail("expected MissingAuthorScopeError")
