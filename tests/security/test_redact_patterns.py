"""A5 — pattern-based redaction layer (union with value-based).

The value-based redactor only knows secrets in the store; credentials
surfaced via ``exec.allowed_env_keys`` (ambient ``os.environ``) — or pasted
into output — are invisible to it. These tests pin a pattern layer that
catches credential-*shaped* strings regardless of whether the store knows
them, modelled on hermes/openclaw (vendor prefixes + KV heuristics).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from durin.security.secrets import SecretRedactor, SecretStore


def _r() -> SecretRedactor:
    """A pattern-only redactor (no stored values)."""
    return SecretRedactor({}, patterns=True)


# -- vendor-prefix patterns --------------------------------------------------


def test_pattern_redacts_openai_key() -> None:
    out = _r().redact_text("key: sk-proj-abcdef1234567890ABCDEFGHIJklmno done")
    assert "sk-proj-abcdef1234567890ABCDEFGHIJklmno" not in out
    assert "«redacted»" in out


def test_pattern_redacts_github_pat() -> None:
    tok = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    out = _r().redact_text(f"clone with {tok} now")
    assert tok not in out
    assert "«redacted»" in out


def test_pattern_redacts_aws_access_key_id() -> None:
    out = _r().redact_text("AWS key AKIAIOSFODNN7EXAMPLE here")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "«redacted»" in out


def test_pattern_redacts_slack_token() -> None:
    tok = "xoxb-123456789012-abcdefghijklmnop"
    out = _r().redact_text(f"slack {tok}")
    assert tok not in out


def test_pattern_redacts_google_api_key() -> None:
    tok = "AIza" + "Sy" + "A" * 33  # AIza + 35 chars
    out = _r().redact_text(f"maps {tok}")
    assert tok not in out


def test_pattern_redacts_jwt() -> None:
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".ZHVtbXlTaWduYXR1cmVfMTIzNDU2"
    )
    out = _r().redact_text(f"Authorization cookie {jwt}")
    assert jwt not in out


def test_pattern_redacts_pem_private_key_block() -> None:
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAabc123\nmorebase64==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = _r().redact_text(f"here is the key:\n{pem}\nend")
    assert "MIIEowIBAAKCAQEAabc123" not in out
    assert "«redacted»" in out


def test_pattern_redacts_bearer_token() -> None:
    out = _r().redact_text("Authorization: Bearer abc123DEF456ghi789JKL")
    assert "abc123DEF456ghi789JKL" not in out
    assert "Bearer" in out  # the scheme word is kept, only the credential goes


# -- KV heuristics (this is what closes the allowed_env_keys gap) ------------


def test_pattern_redacts_env_style_secret_assignment() -> None:
    """An `env` dump leaking an ambient credential value."""
    out = _r().redact_text("MY_DB_PASSWORD=hunter2supersecretvalue")
    assert "hunter2supersecretvalue" not in out
    assert "MY_DB_PASSWORD=" in out  # key kept, value masked
    assert "«redacted»" in out


def test_pattern_redacts_quoted_json_secret_field() -> None:
    out = _r().redact_text('{"apiKey": "plainsecretvalue12345"}')
    assert "plainsecretvalue12345" not in out
    assert "apiKey" in out


# -- negatives: must NOT over-redact -----------------------------------------


def test_pattern_keeps_token_count_output() -> None:
    """`tokens` (lowercase, common in agent output) is not a secret key."""
    text = "Total tokens: 123456789 used this turn"
    assert _r().redact_text(text) == text


def test_pattern_keeps_innocuous_env_var() -> None:
    text = "PATH=/usr/local/bin:/usr/bin:/bin"
    assert _r().redact_text(text) == text


def test_pattern_keeps_plain_prose() -> None:
    text = "The quick brown fox jumps over the lazy dog repeatedly."
    assert _r().redact_text(text) == text


# -- layering: default off, union with value-based ---------------------------


def test_patterns_disabled_by_default() -> None:
    """A bare SecretRedactor (value-based) must not pattern-redact."""
    r = SecretRedactor({})
    text = "key sk-proj-abcdef1234567890ABCDEFGHIJklmno"
    assert r.redact_text(text) == text


@pytest.fixture
def store_at(tmp_path):
    config_path = tmp_path / "config.json"
    from durin.security import secrets as _secrets

    _secrets._STORE = None
    with patch("durin.config.loader.get_config_path", return_value=config_path):
        yield tmp_path / "secrets.json"
    _secrets._STORE = None


def test_redact_secrets_applies_both_layers(store_at) -> None:
    """redact_secrets (via build_redactor) redacts a stored value AND a
    credential-shaped value not in the store, in one pass."""
    from durin.security.secrets import redact_secrets

    store = SecretStore(path=store_at)
    store.put("STORED", value="stored-value-abcdef", service="x")
    store.save()

    text = "stored=stored-value-abcdef ambient=ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    out = redact_secrets(text)
    assert "stored-value-abcdef" not in out
    assert "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8" not in out
    assert "«redacted:STORED»" in out  # value-based marker
    assert "«redacted»" in out  # pattern marker
