"""GitHub Copilot OAuth token persisted in durin's secret store.

The provider used oauth-cli-kit's ``FileTokenStorage`` (its own app-data dir),
the lone provider credential still outside ``secrets.json``. These tests pin the
secret-store-backed storage (round-trip + one-time migration from the kit file)
and the disconnect path, mirroring ``codex_device_auth``.
"""

from __future__ import annotations

import types

import durin.providers.github_copilot_provider as gcp


def test_copilot_secrets_storage_roundtrip(monkeypatch):
    from oauth_cli_kit.models import OAuthToken

    import durin.security.secrets as secmod

    store: dict[str, str] = {}

    def fake_store(name, value, **kw):
        ref = f"${{secret:{name}}}"
        store[ref] = value
        return ref

    def fake_resolve(ref):
        if isinstance(ref, str) and ref in store:
            return store[ref]
        raise secmod.SecretNotFoundError(str(ref))

    monkeypatch.setattr(secmod, "store_secret", fake_store)
    monkeypatch.setattr(secmod, "resolve_secret", fake_resolve)
    monkeypatch.setattr(gcp, "_kit_file_storage", lambda: types.SimpleNamespace(load=lambda: None))

    s = gcp.get_storage()
    assert s.load() is None
    s.save(OAuthToken(access="A", refresh="R", expires=123, account_id="octocat"))
    loaded = s.load()
    assert loaded.access == "A"
    assert loaded.refresh == "R"
    assert loaded.expires == 123
    assert loaded.account_id == "octocat"


def test_copilot_secrets_storage_migrates_from_kit_file(monkeypatch):
    from oauth_cli_kit.models import OAuthToken

    import durin.security.secrets as secmod

    store: dict[str, str] = {}
    saved: list[str] = []

    def fake_store(name, value, **kw):
        ref = f"${{secret:{name}}}"
        store[ref] = value
        saved.append(value)
        return ref

    def fake_resolve(ref):
        if isinstance(ref, str) and ref in store:
            return store[ref]
        raise secmod.SecretNotFoundError(str(ref))

    monkeypatch.setattr(secmod, "store_secret", fake_store)
    monkeypatch.setattr(secmod, "resolve_secret", fake_resolve)
    legacy = OAuthToken(access="LEG", refresh="LR", expires=99, account_id="la")
    monkeypatch.setattr(gcp, "_kit_file_storage", lambda: types.SimpleNamespace(load=lambda: legacy))

    s = gcp.get_storage()
    loaded = s.load()  # secret absent -> migrate from kit file
    assert loaded.access == "LEG"
    assert saved  # migration persisted into the secret store
    # legacy source no longer consulted now that the secret exists
    monkeypatch.setattr(gcp, "_kit_file_storage", lambda: types.SimpleNamespace(load=lambda: None))
    assert s.load().access == "LEG"


def test_copilot_disconnect_removes_secret(monkeypatch, tmp_path):
    """End-to-end against a real tmp secret store: a saved token is gone
    after disconnect()."""
    from oauth_cli_kit.models import OAuthToken

    monkeypatch.setattr(
        "durin.security.secrets._default_secrets_path",
        lambda: tmp_path / "secrets.json",
    )
    # No legacy file (its absence must not raise).
    monkeypatch.setattr(
        gcp,
        "_kit_file_storage",
        lambda: types.SimpleNamespace(get_token_path=lambda: tmp_path / "github-copilot.json"),
    )

    gcp.get_storage().save(
        OAuthToken(access="A", refresh="R", expires=1, account_id="octocat")
    )
    assert gcp.disconnect() is True
    # A fresh storage (no legacy to migrate) sees nothing.
    monkeypatch.setattr(
        gcp, "_kit_file_storage", lambda: types.SimpleNamespace(load=lambda: None)
    )
    assert gcp.get_storage().load() is None
