"""Without a setup secret, the dashboard bootstrap answers only a loopback name.

With no setup secret, ``/webui/bootstrap`` mints an admin token for any
loopback peer. A page in the local browser could reach it through DNS
rebinding: a name it controls resolves to 127.0.0.1, so the peer is loopback
while the ``Host`` header still carries the page's own name. The no-secret
branch therefore also requires the ``Host`` to be a loopback name
(``localhost``, ``127.0.0.1`` or ``[::1]``, any port), and says to configure
the setup secret otherwise. The secret path, which reverse-proxy deployments
use, is left as it was.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from durin.channels.websocket import WebSocketChannel
from durin.service.types import ForbiddenError

_LOCAL = ("127.0.0.1", 50000)
_REMOTE = ("203.0.113.7", 50000)


def _channel(**cfg: Any) -> WebSocketChannel:
    return WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"], "path": "/", "websocketRequiresToken": False,
         **cfg},
        MagicMock(),
    )


@pytest.mark.parametrize("host", ["localhost:8765", "127.0.0.1:18996", "[::1]:8765",
                                  "localhost", "LOCALHOST:8765"])
def test_a_loopback_name_mints_without_a_secret(host):
    payload = _channel(host="127.0.0.1").bootstrap(peer=_LOCAL, headers={"host": host})

    assert payload["token"].startswith("nbwt_")


@pytest.mark.parametrize("host", ["evil.example:8765", "localhost.evil.example:8765",
                                  "127.0.0.1.nip.io:8765", ""])
def test_any_other_name_is_refused_without_a_secret(host):
    with pytest.raises(ForbiddenError) as refused:
        _channel(host="127.0.0.1").bootstrap(peer=_LOCAL, headers={"host": host})

    assert "token_issue_secret" in refused.value.message


def test_a_missing_host_header_is_refused_without_a_secret():
    with pytest.raises(ForbiddenError):
        _channel(host="127.0.0.1").bootstrap(peer=_LOCAL, headers={})


def test_with_a_secret_any_name_mints_when_the_secret_is_presented():
    payload = _channel(host="0.0.0.0", tokenIssueSecret="s3cret").bootstrap(
        peer=_REMOTE, headers={"host": "durin.example.org", "X-Durin-Auth": "s3cret"})

    assert payload["token"].startswith("nbwt_")


def test_a_non_loopback_peer_is_still_refused_first():
    with pytest.raises(ForbiddenError) as refused:
        _channel(host="127.0.0.1").bootstrap(peer=_REMOTE, headers={"host": "localhost:8765"})

    assert "localhost-only" in refused.value.message
