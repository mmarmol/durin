"""Provider extra_headers / extra_body ${secret:} resolution (factory).

`api_key` was already dereferenced in `_make_provider_core`; these dicts were
passed to the provider verbatim, so a credential placed in a custom header or
body field reached the wire as the literal `${secret:NAME}` string. The factory
must resolve those refs the same way it resolves `api_key`.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def secret_env(tmp_path, monkeypatch):
    """Wire a tmp secret store and config path."""
    from durin.config.loader import save_config
    from durin.config.schema import Config

    config_path = tmp_path / "config.json"
    save_config(Config(), config_path)
    monkeypatch.setattr("durin.config.loader._current_config_path", config_path)
    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path",
        lambda: tmp_path / "secrets.json",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# _resolve_secret_refs unit
# ---------------------------------------------------------------------------


def test_resolve_secret_refs_passthrough_when_no_store(secret_env):
    from durin.providers.factory import _resolve_secret_refs

    # Literals, None and non-string scalars are returned untouched.
    assert _resolve_secret_refs(None) is None
    assert _resolve_secret_refs({"X": "plain"}) == {"X": "plain"}
    assert _resolve_secret_refs({"n": 1, "b": True}) == {"n": 1, "b": True}


def test_resolve_secret_refs_resolves_nested(secret_env):
    from durin.providers.factory import _resolve_secret_refs
    from durin.security.secrets import store_secret

    ref = store_secret(
        "HDR_TOKEN", "shh-token", service="test", scope=["test"]
    )
    nested = {"auth": {"token": ref}, "list": [ref, "plain"], "n": 3}
    assert _resolve_secret_refs(nested) == {
        "auth": {"token": "shh-token"},
        "list": ["shh-token", "plain"],
        "n": 3,
    }


# ---------------------------------------------------------------------------
# _make_provider_core integration
# ---------------------------------------------------------------------------


def test_make_provider_resolves_extra_headers(secret_env):
    from durin.config.loader import load_config
    from durin.providers.factory import _make_provider_core
    from durin.security.secrets import store_secret

    ref = store_secret(
        "OPENAI_EXTRA_HDR", "header-secret", service="test", scope=["test"]
    )
    config = load_config()
    config.agents.defaults.model = "openai/gpt-4o"
    config.providers.openai.api_key = "plain-key"
    config.providers.openai.extra_headers = {"X-Auth": ref}

    provider = _make_provider_core(config, model="openai/gpt-4o")
    assert provider.extra_headers["X-Auth"] == "header-secret"
