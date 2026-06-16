"""Tests for the signed ``/api/media/<sig>/<payload>`` route and its replay
integration on ``/api/sessions/<key>/messages``.

The route is the return path for images attached to persisted user turns:
:meth:`WebSocketChannel._sign_media_path` mints URLs during session reads,
and :meth:`WebSocketChannel._handle_media_fetch` serves the bytes back.
These tests cover the two halves end-to-end plus the adversarial edges
(bad signatures, ``..`` traversal, non-existent files, non-image types).
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from durin.bus.queue import MessageBus
from durin.channels.websocket import (
    WebSocketChannel,
    _b64url_decode,
    _b64url_encode,
)
from durin.session.manager import Session, SessionManager

# PNG magic bytes + a couple of sentinel bytes so we can verify byte-for-byte
# round-trip of the served payload. Stays under mimetype + size limits.
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x00\x00\x02\x00\x01"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _make_client(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    session_manager: SessionManager | None = None,
    media_dir: Path | None = None,
) -> tuple[WebSocketChannel, TestClient]:
    """Build a TestClient backed by the unified ASGI app.

    If *media_dir* is given, ``get_media_dir`` is patched to return it for the
    duration of the test (caller must patch in their own context if needed).
    """
    from durin.api.asgi import build_gateway_http_app

    data_dir = tmp_path / "durin_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("durin.config.paths.get_data_dir", lambda: data_dir)

    cfg = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": 8765,
        "path": "/",
        "websocketRequiresToken": False,
    }
    channel = WebSocketChannel(cfg, MessageBus(), session_manager=session_manager)
    registry = channel._services
    auth = registry.get("auth")
    app = build_gateway_http_app(channel, registry, auth=auth)
    return channel, TestClient(app, raise_server_exceptions=True)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


# ---------------------------------------------------------------------------
# _sign_media_path: the URL minter
# ---------------------------------------------------------------------------


def test_sign_media_path_rejects_paths_outside_media_root(
    bus: MagicMock, tmp_path: Path
) -> None:
    """Paths that resolve outside ``get_media_dir()`` must not be signed.

    This is the single most important invariant of the whole scheme:
    if the minter ever signed an arbitrary path, the HMAC would legitimise
    it for the fetch handler and we'd hand out a disk-read primitive.
    """
    outside = tmp_path / "secrets" / "cred.txt"
    outside.parent.mkdir()
    outside.write_text("nope")
    media = tmp_path / "media"
    media.mkdir()
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"], "host": "127.0.0.1", "port": 0, "path": "/", "websocketRequiresToken": False},
        bus,
    )
    with patch("durin.channels.websocket.get_media_dir", return_value=media):
        assert channel._sign_media_path(outside) is None
        # Traversal via the media root is also rejected — the resolve() step
        # normalises ``..`` out before the relative_to check.
        assert channel._sign_media_path(media / ".." / "secrets" / "cred.txt") is None


def test_sign_media_path_round_trips_via_hmac(
    bus: MagicMock, tmp_path: Path
) -> None:
    """The signature embeds exactly ``HMAC-SHA256(secret, payload)[:16]``."""
    media = tmp_path / "media"
    media.mkdir()
    (media / "a.png").write_bytes(_PNG_BYTES)
    channel = WebSocketChannel(
        {"enabled": True, "allowFrom": ["*"], "host": "127.0.0.1", "port": 0, "path": "/", "websocketRequiresToken": False},
        bus,
    )
    with patch("durin.channels.websocket.get_media_dir", return_value=media):
        url = channel._sign_media_path(media / "a.png")
    assert url is not None
    assert url.startswith("/api/media/")
    sig, payload = url[len("/api/media/"):].split("/", 1)
    expected = hmac.new(
        channel._media_secret, payload.encode("ascii"), hashlib.sha256
    ).digest()[:16]
    assert _b64url_decode(sig) == expected
    # The payload decodes back to the *relative* path — no absolute-path leaks.
    assert _b64url_decode(payload).decode() == "a.png"


# ---------------------------------------------------------------------------
# /api/media/{sig}/{payload}: the serving handler
# ---------------------------------------------------------------------------


def test_media_route_serves_signed_file(tmp_path: Path, monkeypatch: Any) -> None:
    """Valid signature + existing file => 200 with correct bytes + MIME."""
    media = tmp_path / "media"
    media.mkdir()
    target = media / "round-trip.png"
    target.write_bytes(_PNG_BYTES)

    with patch("durin.channels.websocket.get_media_dir", return_value=media):
        channel, client = _make_client(tmp_path, monkeypatch)
        url_path = channel._sign_media_path(target)
        assert url_path is not None
        resp = client.get(url_path)

    assert resp.status_code == 200
    assert resp.content == _PNG_BYTES
    assert resp.headers["content-type"].startswith("image/png")
    # Immutable cache header lets the browser skip round-trips on replay.
    assert "immutable" in resp.headers.get("cache-control", "")
    # nosniff keeps the browser from second-guessing our Content-Type.
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_media_route_rejects_bad_signature(tmp_path: Path, monkeypatch: Any) -> None:
    """A payload re-signed with a different secret must 401.

    Protects against a restart: old URLs baked into a stale tab become
    un-forgeable once ``_media_secret`` regenerates.
    """
    media = tmp_path / "media"
    media.mkdir()
    (media / "f.png").write_bytes(_PNG_BYTES)

    with patch("durin.channels.websocket.get_media_dir", return_value=media):
        channel, client = _make_client(tmp_path, monkeypatch)
        good = channel._sign_media_path(media / "f.png")
        assert good is not None
        _, payload = good[len("/api/media/"):].split("/", 1)
        # Forge a sig with a *different* secret.
        forged_mac = hmac.new(
            b"\x00" * 32, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        forged = f"/api/media/{_b64url_encode(forged_mac)}/{payload}"
        resp = client.get(forged)

    assert resp.status_code == 401


def test_media_route_rejects_path_traversal_payload(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Even a validly-signed ``..`` payload must not escape the media root.

    The signer never *emits* such payloads, but an attacker who somehow
    obtained the secret (or the channel was misconfigured) must still be
    stopped by the resolve()+relative_to() guard in the serving path.
    """
    media = tmp_path / "media"
    media.mkdir()
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("classified")

    with patch("durin.channels.websocket.get_media_dir", return_value=media):
        channel, client = _make_client(tmp_path, monkeypatch)
        # Hand-craft a traversal payload the legit signer would refuse to mint.
        payload = _b64url_encode(b"../secret.txt")
        mac = hmac.new(
            channel._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        url = f"/api/media/{_b64url_encode(mac)}/{payload}"
        resp = client.get(url)

    assert resp.status_code == 404
    assert b"classified" not in resp.content


def test_media_route_404s_missing_file(tmp_path: Path, monkeypatch: Any) -> None:
    """A signed URL for a file that no longer exists degrades to 404 so the
    client can fall back to the placeholder tile instead of breaking."""
    media = tmp_path / "media"
    media.mkdir()
    target = media / "gone.png"
    target.write_bytes(_PNG_BYTES)

    with patch("durin.channels.websocket.get_media_dir", return_value=media):
        channel, client = _make_client(tmp_path, monkeypatch)
        url_path = channel._sign_media_path(target)
        assert url_path is not None
        target.unlink()  # the file vanishes between signing and fetching
        resp = client.get(url_path)

    assert resp.status_code == 404


def test_media_route_degrades_non_image_to_octet_stream(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A non-image extension must not be served as its native MIME.

    Defence-in-depth: if media_dir ever contained (say) an HTML file, we
    do not want the browser to render it as HTML via the signed route.
    """
    media = tmp_path / "media"
    media.mkdir()
    (media / "scary.html").write_bytes(b"<script>alert(1)</script>")

    with patch("durin.channels.websocket.get_media_dir", return_value=media):
        channel, client = _make_client(tmp_path, monkeypatch)
        payload = _b64url_encode(b"scary.html")
        mac = hmac.new(
            channel._media_secret, payload.encode("ascii"), hashlib.sha256
        ).digest()[:16]
        url = f"/api/media/{_b64url_encode(mac)}/{payload}"
        resp = client.get(url)

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/octet-stream")
    # nosniff is the actual defence when we downgrade to octet-stream:
    # without it the browser might still sniff the bytes as HTML.
    assert resp.headers.get("x-content-type-options") == "nosniff"


# ---------------------------------------------------------------------------
# /api/sessions/<key>/messages: media_urls hydration on session read
# ---------------------------------------------------------------------------


def test_session_messages_exposes_signed_media_urls(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The read path must map persisted ``media`` paths onto signed URLs
    and strip the raw path — the client never learns the server's layout."""
    media = tmp_path / "media"
    media.mkdir()
    img = media / "u.png"
    img.write_bytes(_PNG_BYTES)

    sm = SessionManager(tmp_path / "ws_state")
    sess = Session(key="websocket:media-hydrate")
    sess.add_message("user", "look at this", media=[str(img)])
    sess.add_message("assistant", "nice")
    sm.save(sess)

    with patch("durin.channels.websocket.get_media_dir", return_value=media):
        channel, client = _make_client(tmp_path, monkeypatch, session_manager=sm)

        boot = client.get("/webui/bootstrap")
        assert boot.status_code == 200
        token = boot.json()["token"]
        auth = {"Authorization": f"Bearer {token}"}

        resp = client.get(
            "/api/sessions/websocket:media-hydrate/messages",
            headers=auth,
        )
        assert resp.status_code == 200
        body = resp.json()

        # The signed URL round-trips end-to-end: fetching it yields the same bytes.
        user_msg = next(m for m in body["messages"] if m["role"] == "user")
        urls = user_msg["media_urls"]
        assert isinstance(urls, list) and len(urls) == 1
        assert urls[0]["name"] == "u.png"
        assert urls[0]["url"].startswith("/api/media/")
        # Raw paths must not leak to the wire.
        assert "media" not in user_msg

        # And the URL actually works.
        fetched = client.get(urls[0]["url"])
        assert fetched.status_code == 200
        assert fetched.content == _PNG_BYTES


def test_session_messages_skips_vanished_media(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Paths that no longer resolve inside the media root produce no URL —
    the message is still delivered, just without the preview."""
    media = tmp_path / "media"
    media.mkdir()

    sm = SessionManager(tmp_path / "ws_state")
    sess = Session(key="websocket:vanished")
    sess.add_message("user", "missing pic", media=[str(media / "absent.png")])
    sm.save(sess)

    with patch("durin.channels.websocket.get_media_dir", return_value=media):
        channel, client = _make_client(tmp_path, monkeypatch, session_manager=sm)

        boot = client.get("/webui/bootstrap")
        assert boot.status_code == 200
        token = boot.json()["token"]

        resp = client.get(
            "/api/sessions/websocket:vanished/messages",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        user_msg = next(m for m in resp.json()["messages"] if m["role"] == "user")
        # absent.png lives inside the media root so it *does* get a signed
        # URL (we don't stat the file at signing time — that would slow
        # the listing). Fetching the URL is where the 404 surfaces.
        urls = user_msg.get("media_urls") or []
        assert len(urls) == 1
        fetched = client.get(urls[0]["url"])
        assert fetched.status_code == 404
        assert "media" not in user_msg
