"""A near-miss ``${secret:NAME}`` reference must fail loudly, not pass through.

`resolve_secret` returns anything that is not a canonical reference untouched.
That is right for real literals, but a mistyped placeholder — `{{secret:X}}`,
`$secret:X`, a lowercase name — used to be handed downstream verbatim as if it
were the credential. The consumer then failed with an opaque auth error far from
the typo, with nothing pointing back at the config.
"""
import pytest

from durin.security.secrets import (
    MalformedSecretRefError,
    resolve_secret,
    store_secret,
)


@pytest.fixture()
def secret_store_tmp(tmp_path, monkeypatch):
    import durin.security.secrets as s
    from durin.config.loader import save_config
    from durin.config.schema import Config

    path = tmp_path / "config.json"
    save_config(Config(), path)
    monkeypatch.setattr("durin.config.loader._current_config_path", path)
    monkeypatch.setattr(s, "_STORE", None)  # rebind the process-wide store to tmp
    return path


@pytest.mark.parametrize(
    "value",
    [
        "{{secret:SENTRY_AUTH_TOKEN}}",  # mustache — the shape that broke a live MCP server
        "{{ secret:SENTRY_AUTH_TOKEN }}",
        "$secret:SENTRY_AUTH_TOKEN",  # no braces
        "{secret:SENTRY_AUTH_TOKEN}",  # no dollar
        "${secrets:SENTRY_AUTH_TOKEN}",  # plural
        "${secret:sentry_auth_token}",  # lowercase name never matches _NAME_RE
        "${SECRET:SENTRY_AUTH_TOKEN}",  # uppercased keyword
        "<secret:SENTRY_AUTH_TOKEN>",
        "%secret:SENTRY_AUTH_TOKEN%",
    ],
)
def test_near_miss_reference_is_rejected(value):
    with pytest.raises(MalformedSecretRefError) as exc:
        resolve_secret(value)
    assert "${secret:" in str(exc.value)  # the message shows the canonical form


@pytest.mark.parametrize(
    "value",
    [
        "Bearer ${secret:SENTRY_AUTH_TOKEN}",
        "token=${secret:SENTRY_AUTH_TOKEN}",
        "${secret:A}${secret:B}",
    ],
)
def test_embedded_reference_is_rejected(value):
    """A reference is the whole field value — durin does not interpolate."""
    with pytest.raises(MalformedSecretRefError) as exc:
        resolve_secret(value)
    assert "whole field value" in str(exc.value)


def test_surrounding_whitespace_still_resolves(secret_store_tmp):
    """`is_secret_ref` strips, so a padded reference is a reference, not an embed."""
    store_secret(
        "SENTRY_AUTH_TOKEN", "s3cr3t-value-123",
        service="mcp:sentry", scope=["mcp:sentry"], origin="user",
    )
    assert resolve_secret("  ${secret:SENTRY_AUTH_TOKEN}  ") == "s3cr3t-value-123"


@pytest.mark.parametrize(
    "value",
    [
        "https://sentry.io/api/0/",
        "hunter2",
        "{{name}}",  # a template placeholder that is not about secrets
        "{{ user.email }}",
        "sk-ant-not-a-reference",
        "the secret: keep it quiet",  # prose, not a whole-value placeholder
        "",
        None,
        42,
        True,
    ],
)
def test_ordinary_values_still_pass_through(value):
    assert resolve_secret(value) == value


def test_valid_reference_still_resolves(secret_store_tmp):
    ref = store_secret(
        "SENTRY_AUTH_TOKEN", "s3cr3t-value-123",
        service="mcp:sentry", scope=["mcp:sentry"], origin="user",
    )
    assert resolve_secret(ref) == "s3cr3t-value-123"


def test_mcp_env_map_rejects_near_miss():
    """The reported path: a mistyped ref in an MCP server's ``env``."""
    from durin.agent.tools.mcp_connection import _resolve_secret_map

    with pytest.raises(MalformedSecretRefError) as exc:
        _resolve_secret_map({"SENTRY_ACCESS_TOKEN": "{{secret:SENTRY_AUTH_TOKEN}}"})
    assert "SENTRY_ACCESS_TOKEN" in str(exc.value)  # names the offending env key
