"""Heartbeat isolated sessions (OpenClaw-inspired Tier 1).

By default the heartbeat shares one long-running session ``heartbeat``,
trimmed by ``keep_recent_messages`` between ticks. That preserves
short-term context but lets noise accumulate across runs. When
``heartbeat.isolatedSessions=true``, each tick uses a fresh ephemeral
session key (``heartbeat-<uuid>``) that the executor deletes after the
tick — stateless, no drift.
"""

from __future__ import annotations

from durin.config.schema import HeartbeatConfig
from durin.heartbeat.service import heartbeat_session_key


def test_default_shared_session_key_is_stable():
    """Two consecutive calls without isolation return the SAME key — the
    shared session is reused."""
    key1 = heartbeat_session_key(isolated=False)
    key2 = heartbeat_session_key(isolated=False)
    assert key1 == "heartbeat"
    assert key2 == "heartbeat"


def test_isolated_session_keys_are_unique_per_call():
    """Each isolated tick gets a unique key so the executor can delete it
    after the run without touching unrelated sessions."""
    keys = {heartbeat_session_key(isolated=True) for _ in range(20)}
    assert len(keys) == 20
    assert all(k.startswith("heartbeat-") for k in keys)
    # Suffix is a 12-char hex (uuid4 hex prefix).
    assert all(len(k) == len("heartbeat-") + 12 for k in keys)


def test_isolated_session_key_does_not_collide_with_shared_name():
    """Defensive: the ephemeral key must not exactly equal the shared one
    or a delete would clobber the shared session."""
    for _ in range(10):
        assert heartbeat_session_key(isolated=True) != "heartbeat"


def test_heartbeat_config_default_is_shared_session():
    """Backward compatibility: existing installs see the same behaviour
    until they opt in."""
    cfg = HeartbeatConfig()
    assert cfg.isolated_sessions is False


def test_heartbeat_config_alias_camelcase():
    """The config field accepts the ``isolatedSessions`` camelCase alias
    so users editing ~/.durin/config.json can use either form."""
    cfg = HeartbeatConfig(isolatedSessions=True)
    assert cfg.isolated_sessions is True
    cfg2 = HeartbeatConfig(isolated_sessions=True)
    assert cfg2.isolated_sessions is True
