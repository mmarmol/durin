"""The transcription api_key must be resolved through the secret store before
it reaches the provider — base.py passes it straight to Whisper, so an
unresolved ``${secret:}`` ref would fail voice auth."""
import types

import pytest

import durin.channels.manager as mgr


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


def _manager(openai_key=None, groq_key=None):
    m = mgr.ChannelManager.__new__(mgr.ChannelManager)
    m.config = types.SimpleNamespace(
        providers=types.SimpleNamespace(
            openai=types.SimpleNamespace(api_key=openai_key),
            groq=types.SimpleNamespace(api_key=groq_key),
        )
    )
    return m


def test_transcription_key_resolves_secret_ref(secret_store_tmp):
    from durin.security.secrets import store_secret

    ref = store_secret("WHISPER_KEY", "sk-real-whisper",
                       service="openai", scope=["transcription"], origin="user")
    assert ref.startswith("${secret:")  # sanity: it's a ref, not plaintext
    m = _manager(openai_key=ref)
    assert m._resolve_transcription_key("openai") == "sk-real-whisper"


def test_transcription_key_passthrough_literal(secret_store_tmp):
    m = _manager(groq_key="gsk-literal")
    assert m._resolve_transcription_key("groq") == "gsk-literal"


def test_transcription_key_missing_is_falsy(secret_store_tmp):
    m = _manager()  # api_key=None on both → must not crash
    assert not m._resolve_transcription_key("openai")
