"""Tests for _is_local_endpoint detection and keepalive configuration."""

from unittest.mock import MagicMock

from durin.providers.openai_compat_provider import (
    OpenAICompatProvider,
    _is_local_endpoint,
)


def _make_spec(is_local: bool = False) -> MagicMock:
    spec = MagicMock()
    spec.is_local = is_local
    return spec


class TestIsLocalEndpoint:
    """Test the _is_local_endpoint helper."""

    def test_spec_is_local_true(self):
        assert _is_local_endpoint(_make_spec(is_local=True), None) is True

    def test_spec_is_local_false_no_base(self):
        assert _is_local_endpoint(_make_spec(is_local=False), None) is False

    def test_no_spec_no_base(self):
        assert _is_local_endpoint(None, None) is False

    def test_localhost(self):
        assert _is_local_endpoint(None, "http://localhost:1234/v1") is True

    def test_localhost_https(self):
        assert _is_local_endpoint(None, "https://localhost:8080/v1") is True

    def test_loopback_127(self):
        assert _is_local_endpoint(None, "http://127.0.0.1:11434/v1") is True

    def test_private_192_168(self):
        assert _is_local_endpoint(None, "http://192.168.8.188:1234/v1") is True

    def test_private_10(self):
        assert _is_local_endpoint(None, "http://10.0.0.5:8000/v1") is True

    def test_private_172_16(self):
        assert _is_local_endpoint(None, "http://172.16.0.1:1234/v1") is True

    def test_private_172_31(self):
        assert _is_local_endpoint(None, "http://172.31.255.255:1234/v1") is True

    def test_not_private_172_32(self):
        assert _is_local_endpoint(None, "http://172.32.0.1:1234/v1") is False

    def test_docker_internal(self):
        assert _is_local_endpoint(None, "http://host.docker.internal:11434/v1") is True

    def test_ipv6_loopback(self):
        assert _is_local_endpoint(None, "http://[::1]:1234/v1") is True

    def test_public_api(self):
        assert _is_local_endpoint(None, "https://api.openai.com/v1") is False

    def test_openrouter(self):
        assert _is_local_endpoint(None, "https://openrouter.ai/api/v1") is False

    def test_spec_overrides_public_url(self):
        """spec.is_local=True takes precedence even with a public-looking URL."""
        assert _is_local_endpoint(_make_spec(is_local=True), "https://api.example.com/v1") is True

    def test_case_insensitive(self):
        assert _is_local_endpoint(None, "http://LOCALHOST:1234/v1") is True

    def test_trailing_slash(self):
        assert _is_local_endpoint(None, "http://192.168.1.1:8080/v1/") is True

    def test_public_hostname_containing_localhost_is_not_local(self):
        assert _is_local_endpoint(None, "https://notlocalhost.example/v1") is False

    def test_public_hostname_containing_private_ip_prefix_is_not_local(self):
        assert _is_local_endpoint(None, "https://api10.example.com/v1") is False

    def test_url_without_scheme(self):
        assert _is_local_endpoint(None, "192.168.1.1:8080/v1") is True


class TestLocalKeepaliveConfig:
    """Verify that local endpoints get keepalive_expiry=0."""

    def test_local_spec_disables_keepalive(self):
        spec = _make_spec(is_local=True)
        spec.env_key = ""
        spec.default_api_base = "http://localhost:11434/v1"
        provider = OpenAICompatProvider(
            api_key="test", api_base="http://localhost:11434/v1", spec=spec,
        )
        pool = provider._client._client._transport._pool
        assert pool._keepalive_expiry == 0

    def test_lan_ip_disables_keepalive(self):
        """A generic 'openai' spec with a LAN IP should still disable keepalive."""
        spec = _make_spec(is_local=False)
        spec.env_key = ""
        spec.default_api_base = None
        provider = OpenAICompatProvider(
            api_key="test", api_base="http://192.168.8.188:1234/v1", spec=spec,
        )
        pool = provider._client._client._transport._pool
        assert pool._keepalive_expiry == 0

    def test_cloud_keeps_default_keepalive(self):
        spec = _make_spec(is_local=False)
        spec.env_key = ""
        spec.default_api_base = "https://api.openai.com/v1"
        provider = OpenAICompatProvider(
            api_key="test", api_base=None, spec=spec,
        )
        pool = provider._client._client._transport._pool
        # Default httpx keepalive is 5.0s
        assert pool._keepalive_expiry == 5.0


class TestStreamIdleTimeoutResolution:
    """Local backends stall legitimately during prompt eval — no idle watchdog
    by default; an explicit DURIN_STREAM_IDLE_TIMEOUT_S always wins."""

    def _provider(self, *, is_local: bool) -> OpenAICompatProvider:
        spec = _make_spec(is_local=is_local)
        spec.env_key = ""
        spec.default_api_base = (
            "http://localhost:11434/v1" if is_local else "https://api.openai.com/v1"
        )
        return OpenAICompatProvider(
            api_key="test",
            api_base="http://localhost:11434/v1" if is_local else None,
            spec=spec,
        )

    def test_cloud_defaults_to_90s(self, monkeypatch):
        monkeypatch.delenv("DURIN_STREAM_IDLE_TIMEOUT_S", raising=False)
        assert self._provider(is_local=False)._resolve_stream_idle_timeout() == 90.0

    def test_local_defaults_to_disabled(self, monkeypatch):
        monkeypatch.delenv("DURIN_STREAM_IDLE_TIMEOUT_S", raising=False)
        assert self._provider(is_local=True)._resolve_stream_idle_timeout() is None

    def test_explicit_env_wins_on_local(self, monkeypatch):
        monkeypatch.setenv("DURIN_STREAM_IDLE_TIMEOUT_S", "45")
        assert self._provider(is_local=True)._resolve_stream_idle_timeout() == 45.0

    def test_env_zero_disables_on_cloud(self, monkeypatch):
        monkeypatch.setenv("DURIN_STREAM_IDLE_TIMEOUT_S", "0")
        assert self._provider(is_local=False)._resolve_stream_idle_timeout() is None

    def test_garbage_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("DURIN_STREAM_IDLE_TIMEOUT_S", "not-a-number")
        assert self._provider(is_local=False)._resolve_stream_idle_timeout() == 90.0
