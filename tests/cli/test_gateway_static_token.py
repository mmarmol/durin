"""The gateway's static admin token may be a ${secret:NAME} reference.

The channel resolves it at startup and `durin status` resolves it for display,
but the gateway read it literally — so configuring it the documented way made
the *reference string* the accepted admin password while the real token was
refused. A reference string is not a credential: it is printed verbatim in
config dumps and status output by design.
"""

from __future__ import annotations

from durin.cli.commands import _resolve_static_token
from durin.security.secrets import SecretNotFoundError


def test_literal_token_passes_through():
    assert _resolve_static_token("plain-token-value") == "plain-token-value"


def test_empty_stays_empty():
    assert _resolve_static_token("") == ""


def test_reference_resolves_to_the_stored_secret(monkeypatch):
    monkeypatch.setattr(
        "durin.security.secrets.resolve_secret",
        lambda _v: "the-real-token",
    )
    assert _resolve_static_token("${secret:WEBSOCKET_TOKEN}") == "the-real-token"


def test_reference_never_authenticates_as_itself(monkeypatch):
    """The regression: the raw reference must never end up as the password."""
    monkeypatch.setattr(
        "durin.security.secrets.resolve_secret",
        lambda _v: "the-real-token",
    )
    ref = "${secret:WEBSOCKET_TOKEN}"
    assert _resolve_static_token(ref) != ref


def test_dangling_reference_disables_the_token_instead_of_falling_back(monkeypatch):
    """Fail closed. A lost bootstrap login is recoverable; handing ADMIN to
    anyone who can read the config file is not."""
    def _boom(_v):
        raise SecretNotFoundError("WEBSOCKET_TOKEN")

    monkeypatch.setattr("durin.security.secrets.resolve_secret", _boom)
    assert _resolve_static_token("${secret:WEBSOCKET_TOKEN}") == ""


def test_reference_resolving_to_empty_does_not_fall_back_to_the_reference(monkeypatch):
    """A stored-but-empty secret must disable the token, not promote the
    reference string into the password slot."""
    monkeypatch.setattr("durin.security.secrets.resolve_secret", lambda _v: "")
    assert _resolve_static_token("${secret:WEBSOCKET_TOKEN}") == ""
